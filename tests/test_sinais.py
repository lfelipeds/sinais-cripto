"""Testes offline: python -m pytest -q"""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sinais import carteira, dados, estrategia, run as runmod  # noqa: E402

DAY, HOUR = 86_400_000, 3_600_000


def series(n, tf_ms, end_ms, seed=1, drift=0.002, vol=0.01, last=None):
    rng = np.random.default_rng(seed)
    logp = np.cumsum(drift + rng.normal(0, vol, n))
    close = 100 * np.exp(logp)
    if last is not None:
        close = close / close[-1] * last
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * 1.003
    low = np.minimum(open_, close) * 0.997
    start = (end_ms // tf_ms) * tf_ms - n * tf_ms           # último candle já fechado
    return [[start + i * tf_ms, open_[i], high[i], low[i], close[i], 100.0] for i in range(n)]


class Fake:
    """Fonte falsa configurável por par."""

    def __init__(self, now_ms, drifts=None, fail=()):
        self.now, self.drifts, self.fail, self.calls = now_ms, drifts or {}, set(fail), []
        self.hour_override = {}

    def __call__(self, pair, tf, limit):
        self.calls.append((pair, tf))
        if pair in self.fail:
            raise RuntimeError("bloqueado")
        seed = abs(hash(pair)) % 999
        d = self.drifts.get(pair, 0.002)
        if tf == "1d":
            return series(min(limit, 900), DAY, self.now, seed, d)
        if pair in self.hour_override:
            return self.hour_override[pair]
        last_close = series(900, DAY, self.now, seed, d)[-1][4]
        return series(min(limit, 1000), HOUR, self.now, seed + 1, 0.0, 0.001, last=last_close)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    monkeypatch.setattr(runmod, "STATE", tmp_path / "estado.json")
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    return cfg


def market(fake, order=("bybit", "binance")):
    return dados.Market(order, {s: fake for s in order})


def test_fallback_source():
    now = int(time.time() * 1000)
    ok = Fake(now)

    def blocked(*a):
        raise RuntimeError("HTTP 403")
    m = dados.Market(["bybit", "binance"], {"bybit": blocked, "binance": ok})
    df = m.candles("BTC/USDT", "1d", 300)
    assert m.source == "binance" and len(df) == 300 and df.ts.is_monotonic_increasing


def test_only_closed_candles():
    now = int(time.time() * 1000)
    rows = series(10, DAY, now) + [[(now // DAY) * DAY, 1, 1, 1, 1, 1]]     # candle de hoje (aberto)
    df = dados.to_frame(rows, "1d", now)
    assert len(df) == 10


def test_first_run_buys_core_and_is_idempotent(env):
    now = int(time.time() * 1000)
    fake = Fake(now, {"BTC/USDT": 0.004})
    msgs = []
    st = runmod.run(env, market(fake), msgs.append, now)
    assert st["core"]["state"] == "comprado"
    assert any("PONTO DE COMPRA — NÚCLEO BTC" in m for m in msgs)
    assert any("Resumo do fechamento" in m for m in msgs)
    assert len(st["btc_history"]) == 120 and set(st["btc_history"][-1]) == {"d", "c", "e"}
    n_pos = len(st["positions"])
    # rodar de novo no mesmo dia não repete compras nem resumo
    msgs2 = []
    st2 = runmod.run(env, market(fake), msgs2.append, now + 60_000)
    assert len(st2["positions"]) == n_pos and not st2["run"]["new_day"] and msgs2 == []
    saved = json.loads(runmod.STATE.read_text(encoding="utf-8"))
    assert saved["last_candle_day"] == st["last_candle_day"]
    # dinheiro fecha: caixa + posições = patrimônio registrado
    tot = saved["budgets"]["core"] + saved["budgets"]["trade"] + sum(
        p["qty"] * saved["prices"][p["pair"]] for p in saved["positions"])
    assert tot == pytest.approx(saved["equity"][-1]["total"], rel=1e-6)


def test_core_sells_when_btc_below_ema(env):
    now = int(time.time() * 1000)
    msgs = []
    runmod.run(env, market(Fake(now, {"BTC/USDT": 0.004})), msgs.append, now)
    later = now + DAY
    st = runmod.run(env, market(Fake(later, {"BTC/USDT": -0.004})), msgs.append, later)
    assert st["core"]["state"] == "fora" and not [p for p in st["positions"] if p["sleeve"] == "core"]
    assert any("PONTO DE VENDA — NÚCLEO BTC" in m for m in msgs)


def test_breakout_opens_and_stop_closes(env, monkeypatch):
    now = int(time.time() * 1000)
    monkeypatch.setattr(estrategia, "breakout", lambda lv, b, s: {"price": lv["close"], "stop": lv["close"] * 0.9,
                                                                  "reason": "teste"})
    monkeypatch.setattr(estrategia, "exit_rule", lambda lv, s: None)
    msgs = []
    st = runmod.run(env, market(Fake(now, {"BTC/USDT": 0.004})), msgs.append, now)
    tr = [p for p in st["positions"] if p["sleeve"] == "trade"]
    assert len(tr) == 3                                             # máx. 3 posições
    assert sum("Não entrou" in m for m in msgs) == len(env["pairs"]) - 3
    eq_trade0 = 500
    for p in tr:
        assert p["qty"] * (p["entry"] - p["stop"]) <= eq_trade0 * 0.005 * 1.01
    # 2 horas depois o preço de um par despenca abaixo do stop
    later = now + 2 * HOUR
    fake = Fake(later, {"BTC/USDT": 0.004})
    target = tr[0]
    rows = fake(target["pair"], "1h", 50)
    rows[-1][3] = target["stop"] * 0.95                              # mínima da última hora
    fake.hour_override[target["pair"]] = rows
    st = runmod.run(env, market(fake), msgs.append, later)
    assert all(p["id"] != target["id"] for p in st["positions"])
    closed = [c for c in st["closed"] if c["id"] == target["id"]][0]
    assert closed["exit_reason"] == "stop inicial" and closed["pnl"] < 0
    assert any("VENDA —" in m for m in msgs)


def test_drawdown_lock(env):
    cfg = env
    st = carteira.new_state(cfg)
    msgs = []
    pf = carteira.Portfolio(st, cfg, msgs.append)
    st["risk"]["peak_trade"] = 1000
    btc = {"close": 100, "ema200": 90, "above_ema": True, "dist_ema_pct": 11, "date": "2026-01-01"}
    pf.daily({}, btc, estrategia, cfg["strategy"])
    assert st["risk"]["locked"] and any("TRAVA" in m for m in msgs)


def test_stop_checks_hour_open_during_previous_run(env, monkeypatch):
    """A hora que estava em curso na execução anterior também precisa ser checada na seguinte."""
    now = int(time.time() * 1000)
    monkeypatch.setattr(estrategia, "breakout", lambda lv, b, s: {"price": lv["close"], "stop": lv["close"] * 0.9,
                                                                  "reason": "teste"})
    monkeypatch.setattr(estrategia, "exit_rule", lambda lv, s: None)
    st = runmod.run(env, market(Fake(now, {"BTC/USDT": 0.004})), [].append, now)
    target = [p for p in st["positions"] if p["sleeve"] == "trade"][0]
    later = now + 4 * HOUR
    fake = Fake(later, {"BTC/USDT": 0.004})
    rows = fake(target["pair"], "1h", 50)
    gap = next(r for r in rows if r[0] == (now // HOUR) * HOUR)        # hora em curso na 1ª execução
    gap[3] = target["stop"] * 0.95
    fake.hour_override[target["pair"]] = rows
    st = runmod.run(env, market(fake), [].append, later)
    closed = [c for c in st["closed"] if c["id"] == target["id"]]
    assert closed and closed[0]["exit_reason"] == "stop inicial"


def test_daily_loss_blocks_new_entries(env):
    st = carteira.new_state(env)
    msgs = []
    pf = carteira.Portfolio(st, env, msgs.append)
    st["risk"]["day"] = {"date": "2026-01-01", "start": 520.0}       # no ciclo anterior a fatia valia 520
    st["risk"]["peak_trade"] = 520.0                                  # hoje 500: −3,8% no dia, longe da trava de −15%
    fake = SimpleNamespace(breakout=lambda lv, b, s: {"price": lv["close"], "stop": lv["close"] * 0.9, "reason": "teste"},
                           exit_rule=lambda lv, s: None, core_state=lambda b: "fora")
    btc = {"close": 100, "ema200": 90, "above_ema": True, "dist_ema_pct": 11, "date": "2026-01-02"}
    pf.daily({"ETH/USDT": {"close": 100.0}}, btc, fake, env["strategy"])
    assert not pf.positions("trade") and any("perda de 3.8% hoje" in m for m in msgs)
    assert not st["risk"]["locked"] and st["risk"]["day"]["start"] == pytest.approx(500)


def test_rules_match_backtest_definition():
    import pandas as pd
    s = {"breakout_days": 20, "exit_days": 10, "ema_trend": 200, "atr_period": 14, "stop_atr_mult": 2.0}
    now = int(time.time() * 1000)
    df = dados.to_frame(series(400, DAY, now, 3, 0.004), "1d", now)
    d = estrategia.indicators(df, s)
    d.loc[d.index[-1], ["close", "high"]] = d.high.iloc[-21:-1].max() * 1.05
    d = estrategia.indicators(d[["ts", "open", "high", "low", "close", "volume", "time"]], s)
    lv = estrategia.levels(d, s)
    sig = estrategia.breakout(lv, lv, s)
    assert sig and sig["stop"] == pytest.approx(lv["close"] - 2 * lv["atr"])
    lv2 = dict(lv, close=lv["min_10d"] * 0.99)
    assert estrategia.exit_rule(lv2, s)


def test_stale_source_is_skipped():
    now = int(time.time() * 1000)

    def stale(pair, tf, limit):
        return series(100, DAY, now - 30 * DAY)
    m = dados.Market(["okx", "binance"], {"okx": stale, "binance": Fake(now)})
    m.candles("BTC/USDT", "1d", 300)
    assert m.source == "binance" and "desatualizados" in m.errors["okx:BTC/USDT"]


# ---------------- aviso de aporte (longo prazo) ----------------
from sinais import aporte  # noqa: E402


def _daily(closes):
    import pandas as pd
    df = pd.DataFrame({"close": closes})
    df["high"] = df.close * 1.01
    df["time"] = pd.date_range(end="2026-10-01", periods=len(df), freq="D", tz="UTC")
    return df


def test_aporte_zones():
    assert aporte.levels(_daily([100.0] * 150)) is None                     # histórico curto
    lv = aporte.levels(_daily([100.0] * 300))
    assert lv["zone"] == "abaixo_media" and lv["mult"] == 1.5 and lv["mayer"] == pytest.approx(1.0)
    assert len(lv["history"]) == 101 and set(lv["history"][-1]) == {"d", "c", "s"}      # 300 dias − 199 de aquecimento
    lv = aporte.levels(_daily([100.0] * 299 + [75.0]))
    assert lv["zone"] == "barato" and lv["mult"] == 2.0
    lv = aporte.levels(_daily([100.0] * 299 + [130.0]))
    assert lv["zone"] == "normal" and lv["mult"] == 1.0 and lv["price_cheap"] == pytest.approx(lv["sma200"] * 0.8)
    lv = aporte.levels(_daily([100.0] * 299 + [300.0]))
    assert lv["zone"] == "caro" and lv["mult"] == 0.5


def test_aporte_message_every_day_change_or_stay():
    cfg = {"aporte": {"enabled": True, "pairs": ["BTC/USDT"]}}
    st, msgs = {}, []
    day = 86_400_000
    t0 = 1_790_000_000_000

    def step(n, last):
        df = _daily([100.0] * 299 + [last])
        df["time"] = df.time + __import__("pandas").Timedelta(days=n)
        aporte.update(st, {"BTC/USDT": aporte.levels(df)}, cfg, msgs.append, t0 + n * day)

    step(0, 130.0)
    assert len(msgs) == 1 and "faixa atual: NORMAL" in msgs[0] and "Não está vantajoso" in msgs[0]
    step(0, 130.0)
    assert len(msgs) == 1                               # mesmo diário → não repete
    step(1, 131.0)
    assert len(msgs) == 2 and "se mantém em NORMAL (desde ontem)" in msgs[1]
    step(2, 95.0)
    assert len(msgs) == 3 and "MUDOU DE FAIXA: NORMAL → ABAIXO DA MÉDIA" in msgs[2] and "1,5×" in msgs[2] \
        and "Vantajoso" in msgs[2]
    step(3, 96.0)
    step(4, 96.0)
    assert len(msgs) == 5 and "se mantém em ABAIXO DA MÉDIA (há 2 dias)" in msgs[4]
    step(5, 70.0)
    assert "ABAIXO DA MÉDIA → BARATO" in msgs[5] and "2×" in msgs[5]
    step(6, 130.0)
    assert "BARATO → NORMAL" in msgs[6]                 # saída da faixa vantajosa também avisa
    step(7, 300.0)
    assert "NORMAL → CARO" in msgs[7] and "0,5×" in msgs[7]
    assert st["aporte"]["pairs"]["BTC/USDT"]["zone"] == "caro"
    assert sum(e["type"] == "aporte" for e in st["events"]) == 5      # só as trocas entram no histórico
    cfg["aporte"]["notify_when_unchanged"] = False
    step(8, 301.0)
    assert len(msgs) == 8                               # opção de silenciar o "se mantém"


def test_aporte_monthly_reminder_once(env):
    env["aporte"]["reminder_day"] = 1
    now = int(time.time() * 1000)
    msgs = []
    st = runmod.run(env, market(Fake(now, {"BTC/USDT": 0.004})), msgs.append, now)
    assert sum("Dia do aporte mensal" in m for m in msgs) == 1
    assert "BTC/USDT" in st["aporte"]["pairs"]
    assert any("APORTE BTC — faixa atual" in m for m in msgs)
    msgs2 = []
    runmod.run(env, market(Fake(now + 86_400_000, {"BTC/USDT": 0.004})), msgs2.append, now + 86_400_000)
    assert not any("Dia do aporte mensal" in m for m in msgs2) or \
        time.gmtime((now + 86_400_000) / 1000 - 10800).tm_mon != time.gmtime(now / 1000 - 10800).tm_mon

"""Testes offline: python -m pytest -q"""
import json
import sys
import time
from pathlib import Path

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

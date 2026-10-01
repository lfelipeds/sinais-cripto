"""Execução única (chamada pelo GitHub Actions a cada poucas horas, ou pelo botão "Rodar agora").

Idempotente — pode rodar quantas vezes quiser:
  1. checa os stops das posições com a MÍNIMA de cada hora desde a última execução;
  2. se fechou um novo candle diário, roda o ciclo diário (vendas, núcleo BTC, compras) e manda o resumo;
  3. atualiza preços, radar de rompimentos e patrimônio → docs/data/estado.json (lido pelo painel).
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from . import aporte
from . import estrategia as strat
from .carteira import Portfolio, new_state, now_iso
from .dados import Market

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "docs" / "data" / "estado.json"
HISTORY_DAYS = 120
log = logging.getLogger("run")


class Telegram:
    def __init__(self):
        # strip(): segredos colados com espaço ou quebra de linha no fim são o erro mais comum
        self.token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
        self.chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        self.sent = []
        if not self.token or not self.chat:
            faltando = [n for n, v in (("TELEGRAM_TOKEN", self.token), ("TELEGRAM_CHAT_ID", self.chat)) if not v]
            self.status = "segredo ausente: " + ", ".join(faltando)
        else:
            self.status = "sem mensagens nesta execução"

    def __call__(self, text):
        self.sent.append(text)
        log.info("telegram: %s", text.replace("\n", " | ")[:200])
        if not (self.token and self.chat):
            log.warning("telegram não enviado — %s", self.status)
            return
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True},
                              timeout=15)
            if r.status_code == 200:
                self.status = "ok"
            else:
                desc = r.json().get("description", r.text[:150]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:150]
                self.status = f"erro {r.status_code}: {desc}"
                log.warning("telegram %s", self.status)
        except requests.RequestException as e:
            self.status = f"sem conexão: {str(e)[:120]}"
            log.warning("telegram %s", self.status)


def pages_url():
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def load_state(cfg):
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return new_state(cfg)


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)

    def clean(o):
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [clean(v) for v in o]
        return o
    STATE.write_text(json.dumps(clean(st), ensure_ascii=False, indent=1), encoding="utf-8")


def summary_text(st, cfg, day):
    t = sum(v for v in [st["budgets"]["trade"], st["budgets"]["core"]])
    eq = st["equity"][-1] if st["equity"] else None
    start = cfg["paper"]["starting_equity"]
    lines = [f"📋 Resumo do fechamento de {day}"]
    if eq:
        lines.append(f"Carteira simulada: {eq['total']:.2f} USDT ({(eq['total'] / start - 1) * 100:+.2f}% desde o início)")
    c = st.get("core")
    if c:
        estado = "COMPRADO" if c["state"] == "comprado" else "FORA"
        lines.append(f"Núcleo BTC: {estado} — BTC {c['btc']:,.0f}, EMA200 {c['ema200']:,.0f} ({c['dist_pct']:+.1f}%)")
    tr = [p for p in st["positions"] if p["sleeve"] == "trade"]
    if tr:
        lines.append("Posições de rompimento:")
        for p in tr:
            px = st["prices"].get(p["pair"], p["entry"])
            lines.append(f"  {p['pair']}: {(px / p['entry'] - 1) * 100:+.1f}% (stop {p['stop']:.6g})")
    else:
        lines.append("Posições de rompimento: nenhuma")
    near = sorted([r for r in st["radar"] if r["status"] == "aguardando" and r["dist_breakout_pct"] is not None],
                  key=lambda r: r["dist_breakout_pct"])[:3]
    if near:
        lines.append("Mais perto de romper: " + ", ".join(f"{r['pair'].split('/')[0]} (falta {r['dist_breakout_pct']:.1f}%)"
                                                          for r in near))
    if st["risk"].get("locked"):
        lines.append(f"⛔ Trava ativa: {st['risk']['locked']}")
    ap = aporte.summary_line(st)
    if ap:
        lines.append(ap)
    url = pages_url()
    if url:
        lines.append(f"Painel: {url}")
    return "\n".join(lines)


def run(cfg, market=None, notify=None, now_ms=None):
    s = cfg["strategy"]
    st = load_state(cfg)
    notify = notify or Telegram()
    market = market or Market(cfg["data_sources"])
    pf = Portfolio(st, cfg, notify)
    if now_ms:
        market.now_ms = now_ms
    now_ms = now_ms or int(time.time() * 1000)
    btc_pair = cfg["core_btc"]["pair"]
    pairs = list(dict.fromkeys([btc_pair] + cfg["pairs"]))

    # ---- diário ----
    lv = {}
    btc_daily = None
    ap_cfg = cfg.get("aporte") or {}
    ap_pairs = ap_cfg.get("pairs", []) if ap_cfg.get("enabled", True) else []
    ap_lv = {}
    for pair in list(dict.fromkeys(pairs + ap_pairs)):
        try:
            d = strat.indicators(market.candles(pair, "1d", cfg["daily_candles"]), s)
            if pair in ap_pairs:
                x = aporte.levels(d)
                if x:
                    ap_lv[pair] = x
            if pair not in pairs:
                continue
            if len(d) < s["ema_trend"] + 5:
                raise RuntimeError(f"histórico curto ({len(d)} dias)")
            lv[pair] = strat.levels(d, s)
            if pair == btc_pair:
                btc_daily = d
        except Exception as e:  # noqa: BLE001
            log.error("diário %s: %s", pair, e)
    if btc_pair not in lv:
        raise SystemExit("Sem dados do BTC em nenhuma fonte — nada foi alterado.")

    # ---- horário: preço atual + stops ----
    last = st.get("last_stop_check_ms") or now_ms - 48 * 3_600_000
    hours = int((now_ms - last) / 3_600_000) + 3
    hourly = {}
    for pair in pairs:
        try:
            h = market.candles(pair, "1h", max(48, min(hours, 1000)))
            hourly[pair] = h
            if len(h):
                st["prices"][pair] = float(h.close.iloc[-1])
        except Exception as e:  # noqa: BLE001
            log.warning("horário %s: %s", pair, e)
    for pair, v in lv.items():                          # sem preço horário → usa o fechamento diário
        st["prices"].setdefault(pair, v["close"])
    pf.check_stops({p: h[h.ts >= last] for p, h in hourly.items()})
    st["last_stop_check_ms"] = now_ms

    # ---- ciclo diário (só quando fecha um diário novo) ----
    btc_day = lv[btc_pair]["date"]
    new_day = st.get("last_candle_day") is None or btc_day > st["last_candle_day"]
    signals = []
    if new_day:
        signals = pf.daily({p: v for p, v in lv.items() if p in cfg["pairs"]}, lv[btc_pair], strat, s)
        st["last_candle_day"] = btc_day
        aporte.update(st, ap_lv, cfg, notify, now_ms)       # aviso de aporte de longo prazo (só avisa)
        for p, h in hourly.items():                     # valoriza com o preço mais recente
            if len(h):
                st["prices"][p] = float(h.close.iloc[-1])

    # ---- radar ----
    held = {p["pair"]: p for p in st["positions"] if p["sleeve"] == "trade"}
    radar = []
    for pair in cfg["pairs"]:
        if pair not in lv:
            continue
        v = lv[pair]
        now_px = st["prices"].get(pair, v["close"])
        if pair in held:
            status = "em posição"
        elif not lv[btc_pair]["above_ema"]:
            status = "BTC abaixo da EMA200"
        elif not v["above_ema"]:
            status = "abaixo da EMA200"
        else:
            status = "aguardando"
        radar.append({"pair": pair, "close": v["close"], "price": now_px, "max_20d": v["max_20d"],
                      "min_10d": v["min_10d"], "ema200": v["ema200"], "above_ema": v["above_ema"],
                      "dist_breakout_pct": (v["max_20d"] / now_px - 1) * 100,
                      "dist_exit_pct": (v["min_10d"] / now_px - 1) * 100, "status": status,
                      "ret_7d_pct": v["ret_7d_pct"], "signal_today": pair in signals})
    st["radar"] = radar
    if st.get("core"):
        b = st["prices"].get(btc_pair, lv[btc_pair]["close"])
        st["core"]["price_now"] = b
        st["core"]["dist_now_pct"] = (b / lv[btc_pair]["ema200"] - 1) * 100

    # série diária do BTC (fechamento + EMA200) para o gráfico do painel
    if btc_daily is not None:
        st["btc_history"] = [{"d": r.time.strftime("%Y-%m-%d"), "c": round(float(r.close), 2), "e": round(float(r.ema), 2)}
                             for r in btc_daily.tail(HISTORY_DAYS).itertuples()]

    pf.snapshot_equity()
    st["updated_at"] = now_iso()
    st["source"] = market.source
    st["run"] = {"trigger": os.getenv("GITHUB_EVENT_NAME", "local"), "new_day": new_day, "candle_day": btc_day,
                 "errors": market.errors}
    if new_day and cfg["telegram"].get("daily_summary", True):
        notify(summary_text(st, cfg, btc_day))
    elif os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" and not getattr(notify, "sent", None):
        # rodou pelo botão e não havia nada a avisar → confirma que rodou (serve de teste do Telegram)
        c = st.get("core") or {}
        notify(f"✅ Análise manual concluída\nNenhum sinal novo. Núcleo BTC: {c.get('state', '—')}, "
               f"BTC {st['prices'].get(btc_pair, 0):,.0f} ({c.get('dist_now_pct', 0):+.1f}% da EMA200).")
    st["run"]["telegram"] = getattr(notify, "status", None)
    save_state(st)
    return st


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    st = run(cfg)
    c = st.get("core") or {}
    print(f"OK — fonte {st['source']}, diário {st['run']['candle_day']}, novo dia: {st['run']['new_day']}, "
          f"núcleo {c.get('state')}, posições {len(st['positions'])}")


if __name__ == "__main__":
    sys.exit(main())

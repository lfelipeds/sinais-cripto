"""Regras da v2 (idênticas às do backtest de carteira de 29/09/2026). Só código, sem IA.

ROMPIMENTO  compra quando o diário fecha acima da máxima dos 20 dias anteriores, com o par e o BTC acima
            da EMA200 diária; stop inicial = entrada − 2×ATR diário; vende quando o diário fecha abaixo da
            mínima dos 10 dias anteriores.
NÚCLEO BTC  comprado enquanto o diário do BTC fecha acima da EMA200; vende quando fecha abaixo.
"""
from __future__ import annotations

import pandas as pd


def indicators(df: pd.DataFrame, s: dict) -> pd.DataFrame:
    df = df.copy()
    df["ema"] = df.close.ewm(span=s["ema_trend"], adjust=False).mean()
    prev = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / s["atr_period"], adjust=False, min_periods=s["atr_period"]).mean()
    return df


def levels(df: pd.DataFrame, s: dict) -> dict:
    """Números do último diário fechado (df já com indicadores)."""
    n, m = s["breakout_days"], s["exit_days"]
    c = df.iloc[-1]
    hi = float(df.high.iloc[-n - 1:-1].max())
    lo = float(df.low.iloc[-m - 1:-1].min())
    return {
        "date": c.time.strftime("%Y-%m-%d"),
        "close": float(c.close), "ema200": float(c.ema), "atr": float(c.atr),
        "max_20d": hi, "min_10d": lo,
        "above_ema": bool(c.close > c.ema),
        "dist_ema_pct": float((c.close / c.ema - 1) * 100),
        "dist_breakout_pct": float((hi / c.close - 1) * 100),      # quanto falta subir para romper
        "ret_7d_pct": float((c.close / df.close.iloc[-8] - 1) * 100) if len(df) > 8 else None,
    }


def breakout(lv: dict, btc_lv: dict, s: dict) -> dict | None:
    """Sinal de compra (dict com stop) ou None."""
    if not (lv["above_ema"] and btc_lv["above_ema"] and lv["close"] > lv["max_20d"]):
        return None
    return {"price": lv["close"], "stop": lv["close"] - s["stop_atr_mult"] * lv["atr"],
            "reason": f"fechou acima da máxima de {s['breakout_days']} dias ({lv['max_20d']:.6g})"}


def exit_rule(lv: dict, s: dict) -> str | None:
    if lv["close"] < lv["min_10d"]:
        return f"fechou abaixo da mínima de {s['exit_days']} dias ({lv['min_10d']:.6g})"
    return None


def core_state(btc_lv: dict) -> str:
    return "comprado" if btc_lv["above_ema"] else "fora"

"""Candles públicos (sem chave) com fontes de reserva: Bybit → Binance → OKX.

Todas retornam DataFrame com colunas ts (ms, abertura do candle, UTC), open, high, low, close, volume,
em ordem crescente e SÓ com candles já fechados.
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import requests

log = logging.getLogger("dados")
COLS = ["ts", "open", "high", "low", "close", "volume"]
TF_MS = {"1d": 86_400_000, "1h": 3_600_000}
UA = {"User-Agent": "crypto-sinais/1.0"}


def _get(url, params, tries=3):
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:120]}"
        except requests.RequestException as e:
            last = str(e)[:160]
        time.sleep(1.5 * (k + 1))
    raise RuntimeError(last)


def _bybit(pair, tf, limit):
    interval = {"1d": "D", "1h": "60"}[tf]
    j = _get("https://api.bybit.com/v5/market/kline",
             {"category": "spot", "symbol": pair.replace("/", ""), "interval": interval, "limit": min(limit, 1000)})
    if j.get("retCode") != 0:
        raise RuntimeError(f"bybit retCode {j.get('retCode')}: {j.get('retMsg')}")
    rows = [[int(x[0]), *map(float, x[1:6])] for x in j["result"]["list"]]
    return rows


def _binance(pair, tf, limit):
    j = _get("https://data-api.binance.vision/api/v3/klines",
             {"symbol": pair.replace("/", ""), "interval": tf, "limit": min(limit, 1000)})
    return [[int(x[0]), *map(float, x[1:6])] for x in j]


def _okx(pair, tf, limit):
    bar = {"1d": "1Dutc", "1h": "1H"}[tf]
    inst = pair.replace("/", "-")
    rows, after = [], None
    while len(rows) < limit:
        params = {"instId": inst, "bar": bar, "limit": 300 if not after else 100}
        if after:
            params["after"] = after
        # 1ª página: candles recentes; seguintes: histórico mais antigo
        url = "https://www.okx.com/api/v5/market/" + ("history-candles" if after else "candles")
        j = _get(url, params)
        if j.get("code") != "0":
            raise RuntimeError(f"okx {j.get('code')}: {j.get('msg')}")
        data = j["data"]
        if not data:
            break
        rows += [[int(x[0]), *map(float, x[1:6])] for x in data]
        after = data[-1][0]
    return rows


SOURCES = {"bybit": _bybit, "binance": _binance, "okx": _okx}


def to_frame(rows, tf, now_ms=None):
    df = pd.DataFrame(rows, columns=COLS).drop_duplicates("ts").sort_values("ts")
    now_ms = now_ms or int(time.time() * 1000)
    df = df[df.ts + TF_MS[tf] <= now_ms]                       # só candles fechados
    df["time"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    return df.reset_index(drop=True)


class Market:
    """Escolhe a primeira fonte que funciona para o BTC e usa a mesma para todos (consistência)."""

    def __init__(self, order, fetchers=None):
        self.order = list(order)
        self.fetchers = fetchers or SOURCES
        self.source = None
        self.errors = {}
        self.now_ms = None          # só para testes (simular "agora")

    def candles(self, pair, tf="1d", limit=900):
        order = ([self.source] if self.source else []) + [s for s in self.order if s != self.source]
        for src in order:
            try:
                df = to_frame(self.fetchers[src](pair, tf, limit), tf, self.now_ms)
                if len(df) == 0:
                    raise RuntimeError("sem dados")
                now = self.now_ms or int(time.time() * 1000)
                age = now - (int(df.ts.iloc[-1]) + TF_MS[tf])
                if age > 2 * TF_MS[tf]:                    # fonte devolvendo dados velhos → não confiar
                    raise RuntimeError(f"dados desatualizados (último candle há {age / 3_600_000:.0f} h)")
                if self.source is None:
                    self.source = src
                    log.info("fonte de dados: %s", src)
                elif src != self.source:
                    log.warning("%s veio da fonte reserva %s", pair, src)
                return df
            except Exception as e:  # noqa: BLE001
                self.errors[f"{src}:{pair}"] = str(e)[:160]
                log.warning("fonte %s falhou para %s: %s", src, pair, str(e)[:120])
        mine = {k: v for k, v in self.errors.items() if k.endswith(":" + pair)}
        raise RuntimeError(f"nenhuma fonte respondeu para {pair}: {mine}")

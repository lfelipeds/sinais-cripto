"""Carteira simulada (paper) guardada em JSON — mede se os sinais funcionam, sem dinheiro real.

Duas fatias independentes:
  core   núcleo BTC (EMA200)
  trade  rompimentos de 20 dias (0,5% de risco, máx. 3 posições, travas −3%/dia e −15% do pico)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_state(cfg) -> dict:
    total = float(cfg["paper"]["starting_equity"])
    alloc = cfg["core_btc"]["allocation_pct"] / 100 if cfg["core_btc"]["enabled"] else 0.0
    return {
        "version": 1, "created_at": now_iso(), "updated_at": None, "source": None,
        "last_candle_day": None, "last_stop_check_ms": None, "next_id": 1,
        "budgets": {"core": total * alloc, "trade": total * (1 - alloc)},
        "positions": [], "closed": [], "events": [], "equity": [],
        "risk": {"peak_trade": total * (1 - alloc), "day": None, "locked": None},
        "core": None, "radar": [], "prices": {},
    }


class Portfolio:
    def __init__(self, state: dict, cfg: dict, notify):
        self.st, self.cfg, self.p = state, cfg, cfg["paper"]
        self.notify = notify
        self.fee, self.slip = self.p["fee_pct"] / 100, self.p["slippage_pct"] / 100

    # ------------------------------ básico ------------------------------
    def event(self, kind, text, when=None):
        self.st["events"].append({"ts": when or now_iso(), "type": kind, "text": text})
        self.st["events"] = self.st["events"][-300:]

    def positions(self, sleeve=None):
        return [p for p in self.st["positions"] if sleeve is None or p["sleeve"] == sleeve]

    def sleeve_equity(self, sleeve, prices=None):
        prices = prices or self.st["prices"]
        return self.st["budgets"][sleeve] + sum(p["qty"] * prices.get(p["pair"], p["entry"])
                                                for p in self.positions(sleeve))

    def buy(self, sleeve, pair, qty, price, stop, reason, when=None):
        fill = price * (1 + self.slip)
        cost = qty * fill
        fee = cost * self.fee
        self.st["budgets"][sleeve] -= cost + fee
        pos = {"id": self.st["next_id"], "sleeve": sleeve, "pair": pair, "qty": qty, "entry": fill,
               "stop": stop, "initial_stop": stop, "fee_in": fee, "opened_at": when or now_iso(), "reason": reason}
        self.st["next_id"] += 1
        self.st["positions"].append(pos)
        return pos

    def sell(self, pos, price, reason, when=None):
        fill = price * (1 - self.slip)
        proceeds = pos["qty"] * fill
        fee = proceeds * self.fee
        self.st["budgets"][pos["sleeve"]] += proceeds - fee
        pnl = proceeds - fee - pos["qty"] * pos["entry"] - pos["fee_in"]
        r = pos["entry"] - pos["initial_stop"] if pos["initial_stop"] else 0
        closed = pos | {"exit": fill, "closed_at": when or now_iso(), "exit_reason": reason, "pnl": pnl,
                        "pnl_pct": (fill / pos["entry"] - 1) * 100,
                        "r": (fill - pos["entry"]) / r if r > 0 else None}
        self.st["positions"] = [p for p in self.st["positions"] if p["id"] != pos["id"]]
        self.st["closed"].append(closed)
        return closed

    # ------------------------------ stops (a cada execução) ------------------------------
    def check_stops(self, hourly: dict):
        """hourly: par → DataFrame de candles de 1h desde a última checagem. Usa a MÍNIMA de cada hora."""
        for pos in list(self.positions("trade")):
            df = hourly.get(pos["pair"])
            if df is None or not len(df):
                continue
            opened_ms = int(datetime.fromisoformat(pos["opened_at"]).timestamp() * 1000)
            df = df[df.ts + 3_600_000 > opened_ms]          # inclui a hora em que a posição abriu
            hit = df[df.low <= pos["stop"]]
            if len(hit):
                row = hit.iloc[0]
                px = min(pos["stop"], float(row.open))
                when = row.time.isoformat(timespec="seconds")
                c = self.sell(pos, px, "stop inicial", when)
                self._notify_close(c)

    # ------------------------------ ciclo diário ------------------------------
    def daily(self, lv: dict, btc_lv: dict, strat, s: dict):
        """lv: par → níveis do último diário fechado; strat: módulo estrategia."""
        prices = {pair: v["close"] for pair, v in lv.items()}
        self.st["prices"].update(prices)
        when = now_iso()

        # 1) saídas de trading (mínima de 10 dias)
        for pos in list(self.positions("trade")):
            if pos["pair"] in lv:
                why = strat.exit_rule(lv[pos["pair"]], s)
                if why:
                    self._notify_close(self.sell(pos, lv[pos["pair"]]["close"], why, when))

        # 2) núcleo BTC
        if self.cfg["core_btc"]["enabled"]:
            self._core(btc_lv, strat, when)

        # 3) travas da fatia de trading
        eq_t = self.sleeve_equity("trade", prices)
        rk = self.st["risk"]
        today = when[:10]
        # perda do dia = comparação com o patrimônio do ciclo diário anterior (inclui stops do meio do dia)
        start = rk["day"]["start"] if rk.get("day") and rk["day"].get("start") else eq_t
        daily_loss = (eq_t - start) / start * 100
        rk["day"] = {"date": today, "start": eq_t}
        rk["peak_trade"] = max(rk["peak_trade"], eq_t)
        dd = (rk["peak_trade"] - eq_t) / rk["peak_trade"] * 100
        if dd >= self.p["max_drawdown_pct"] and not rk["locked"]:
            rk["locked"] = f"queda de {dd:.1f}% do pico na fatia de trading em {today}"
            self.event("trava", rk["locked"])
            self.notify(f"⛔ TRAVA ACIONADA\n{rk['locked']}\nNovas compras de rompimento suspensas até você liberar.")
        blocked = rk["locked"] or (f"perda de {-daily_loss:.1f}% hoje" if daily_loss <= -self.p["max_daily_loss_pct"] else None)

        # 4) entradas de rompimento (ordem da lista do config)
        signals = []
        for pair in self.cfg["pairs"]:
            if pair not in lv:
                continue
            sig = strat.breakout(lv[pair], btc_lv, s)
            if not sig:
                continue
            signals.append(pair)
            why = self._can_open(pair, blocked)
            if why:
                self.event("sinal", f"Rompimento em {pair} não executado: {why}")
                self.notify(f"🟡 ROMPIMENTO {pair} @ {sig['price']:.6g}\n{sig['reason']}\n"
                            f"Não entrou na carteira simulada: {why}")
                continue
            eq_t = self.sleeve_equity("trade", self.st["prices"])
            R = sig["price"] - sig["stop"]
            qty = min(eq_t * self.p["risk_per_trade_pct"] / 100 / R,
                      eq_t * self.p["max_position_pct"] / 100 / sig["price"],
                      max(0.0, self.st["budgets"]["trade"]) * 0.98 / sig["price"])
            if qty * sig["price"] < 5:
                self.event("sinal", f"Rompimento em {pair}: tamanho abaixo de 5 USDT")
                continue
            pos = self.buy("trade", pair, qty, sig["price"], sig["stop"], sig["reason"], when)
            self.event("compra", f"Compra {pair} @ {pos['entry']:.6g}, stop {pos['stop']:.6g}")
            self.notify(
                f"🟢 COMPRA — rompimento {pair}\nPreço: {pos['entry']:.6g}\n{sig['reason']}\n"
                f"Stop inicial: {pos['stop']:.6g} ({(pos['stop'] / pos['entry'] - 1) * 100:.1f}%)\n"
                f"Venda: quando o diário fechar abaixo da mínima de {s['exit_days']} dias\n"
                f"Simulado: {qty:.6g} (~{qty * pos['entry']:.2f} USDT, risco ~{qty * (pos['entry'] - pos['stop']):.2f})")
        return signals

    def _can_open(self, pair, blocked):
        if blocked:
            return blocked
        tr = self.positions("trade")
        if any(p["pair"] == pair for p in tr):
            return "já existe posição neste par"
        if len(tr) >= self.p["max_open_positions"]:
            return f"já há {len(tr)} posições abertas (máx. {self.p['max_open_positions']})"
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.p["cooldown_hours_after_loss"])
        for c in self.st["closed"]:
            if (c["sleeve"] == "trade" and c["pair"] == pair and c["pnl"] < 0
                    and datetime.fromisoformat(c["closed_at"]) >= cutoff):
                return "par em pausa após stop recente"
        return None

    def _core(self, btc_lv, strat, when):
        pair = self.cfg["core_btc"]["pair"]
        state = strat.core_state(btc_lv)
        held = self.positions("core")
        prev = (self.st.get("core") or {}).get("state")
        action = "manter" if held else "fora"
        if state == "fora" and held:
            for p in held:
                c = self.sell(p, btc_lv["close"], f"BTC fechou abaixo da EMA200 ({btc_lv['ema200']:,.0f})", when)
                self._notify_close(c)
            action = "vendeu"
        elif state == "comprado" and not held:
            cash = max(0.0, self.st["budgets"]["core"])
            qty = cash * 0.995 / (btc_lv["close"] * (1 + self.slip) * (1 + self.fee))
            if qty * btc_lv["close"] >= 5:
                pos = self.buy("core", pair, qty, btc_lv["close"], 0.0, "BTC acima da EMA200", when)
                self.event("compra", f"Núcleo BTC: compra @ {pos['entry']:,.2f}")
                self.notify(f"🟢 PONTO DE COMPRA — NÚCLEO BTC\nBTC fechou em {btc_lv['close']:,.2f}, acima da "
                            f"EMA200 ({btc_lv['ema200']:,.0f}), {btc_lv['dist_ema_pct']:+.1f}%\n"
                            f"Segurar até o diário fechar abaixo da EMA200.")
                action = "comprou"
        since = (self.st.get("core") or {}).get("since")
        if prev != state or not since:
            since = btc_lv["date"]
        self.st["core"] = {"state": state, "action": action, "btc": btc_lv["close"], "ema200": btc_lv["ema200"],
                           "dist_pct": btc_lv["dist_ema_pct"], "since": since, "candle_day": btc_lv["date"]}

    def _notify_close(self, c):
        if c["sleeve"] == "core":
            self.event("venda", f"Núcleo BTC: venda @ {c['exit']:,.2f} ({c['pnl']:+.2f} USDT)", c["closed_at"])
            self.notify(f"🔴 PONTO DE VENDA — NÚCLEO BTC\nVenda @ {c['exit']:,.2f}\nMotivo: {c['exit_reason']}\n"
                        f"Resultado simulado: {c['pnl']:+.2f} USDT ({c['pnl_pct']:+.2f}%)")
        else:
            emoji = "✅" if c["pnl"] > 0 else "🛑"
            self.event("venda", f"Venda {c['pair']} @ {c['exit']:.6g} — {c['exit_reason']} ({c['pnl']:+.2f} USDT)",
                       c["closed_at"])
            rtxt = f", {c['r']:+.2f}R" if c["r"] is not None else ""
            self.notify(f"{emoji} VENDA — {c['pair']} @ {c['exit']:.6g}\nMotivo: {c['exit_reason']}\n"
                        f"Resultado simulado: {c['pnl']:+.2f} USDT ({c['pnl_pct']:+.2f}%{rtxt})")

    # ------------------------------ patrimônio ------------------------------
    def snapshot_equity(self):
        t, c = self.sleeve_equity("trade"), self.sleeve_equity("core")
        self.st["equity"].append({"ts": now_iso(), "total": round(t + c, 4), "trade": round(t, 4), "core": round(c, 4)})
        self.st["equity"] = self.st["equity"][-3000:]
        return t, c

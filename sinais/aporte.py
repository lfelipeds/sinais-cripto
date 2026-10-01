"""AVISO DE APORTE (longo prazo) — só avisa; quem compra é você, na sua corretora. Não mexe na carteira simulada.

Mede se o preço está "vantajoso" pelo MÚLTIPLO DE MAYER = preço ÷ média simples de 200 dias.
Faixas (testadas no BTC de 2015 a 2026, relatório de 01/10/2026):
  ≤ 0,6  muito barato   2% dos dias; 1 ano depois nunca esteve abaixo; mediana +66%
  ≤ 0,8  barato        15% dos dias; 1 ano depois abaixo em 22% dos casos; 2 anos depois em 7%
  ≤ 1,0  abaixo da média 24% dos dias
  ≤ 1,5  normal        45% dos dias
  ≤ 2,4  esticado      13% dos dias
  > 2,4  caro           1% dos dias; 1 ano depois abaixo em 83% dos casos
Avisos: uma mensagem por dia — "MUDOU DE FAIXA" quando troca de faixa, "se mantém" quando continua.
O aporte mensal fixo continua sendo a base (esperar a queda para comprar rendeu MENOS no teste). As faixas
só sugerem reforçar (1,5× / 2×) ou reduzir (0,5×) o aporte do mês — isso baixou o preço médio em 2% a 9%.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# (limite superior do Mayer, código, nome, multiplicador sugerido do aporte)
ZONES = [
    (0.6, "muito_barato", "MUITO BARATO", 2.0),
    (0.8, "barato", "BARATO", 2.0),
    (1.0, "abaixo_media", "ABAIXO DA MÉDIA", 1.5),
    (1.5, "normal", "NORMAL", 1.0),
    (2.4, "esticado", "ESTICADO", 1.0),
    (float("inf"), "caro", "CARO", 0.5),
]
RANK = {z[1]: i for i, z in enumerate(ZONES)}          # menor = mais barato
CHEAP = {"muito_barato", "barato", "abaixo_media"}
HISTORY_DAYS = 180


def zone_of(mayer: float):
    for lim, code, name, mult in ZONES:
        if mayer <= lim:
            return code, name, mult
    return ZONES[-1][1:]


def levels(df) -> dict | None:
    """Números do último diário fechado. df = candles diários em ordem crescente (precisa de 200+)."""
    if len(df) < 200:
        return None
    close = float(df.close.iloc[-1])
    sma = float(df.close.iloc[-200:].mean())
    mayer = close / sma
    code, name, mult = zone_of(mayer)
    top = float(df.high.max())
    sma_series = df.close.rolling(200).mean()
    tail = df.assign(sma=sma_series).dropna(subset=["sma"]).tail(HISTORY_DAYS)
    history = [{"d": r.time.strftime("%Y-%m-%d"), "c": round(float(r.close), 2), "s": round(float(r.sma), 2)}
               for r in tail.itertuples()]
    return {
        "date": df.time.iloc[-1].strftime("%Y-%m-%d"), "close": close, "sma200": sma, "mayer": mayer,
        "zone": code, "zone_name": name, "mult": mult,
        "price_below_avg": sma * 1.0, "price_cheap": sma * 0.8, "price_very_cheap": sma * 0.6,
        "price_expensive": sma * 2.4,
        "off_high_pct": (close / top - 1) * 100, "high_days": int(len(df)),
        "history": history,          # fechamento e média de 200 dias, para o gráfico do painel
    }


def _fmt(x):
    return f"{x:,.0f}".replace(",", ".") if x >= 100 else f"{x:.4g}".replace(".", ",")


def _head(pair, lv):
    coin = pair.split("/")[0]
    m = f"{lv['mayer']:.2f}".replace(".", ",")
    return coin, f"{coin} {_fmt(lv['close'])} USD = {m}× a média de 200 dias ({_fmt(lv['sma200'])})."


def _advice(lv):
    mult = f"{lv['mult']:g}".replace(".", ",")
    if lv["zone"] in CHEAP:
        return f"✅ Vantajoso para compra. Aporte sugerido: {mult}× o valor normal do mês."
    if lv["zone"] == "caro":
        return f"⚠️ Caro. Aporte sugerido: {mult}× o valor normal; guarde a diferença."
    return (f"Não está vantajoso. Aporte sugerido: {mult}× (normal). "
            f"Fica vantajoso abaixo de {_fmt(lv['price_below_avg'])}; barato abaixo de {_fmt(lv['price_cheap'])}.")


def _icon(zone):
    return "🟢" if zone in CHEAP else "🟠" if zone == "caro" else "⚪"


def change_text(pair, lv, prev_name):
    """Mensagem quando o preço troca de faixa (ou na primeira leitura, prev_name=None)."""
    coin, head = _head(pair, lv)
    if prev_name is None:
        title = f"{_icon(lv['zone'])} APORTE {coin} — faixa atual: {lv['zone_name']}"
    else:
        title = f"{_icon(lv['zone'])} APORTE {coin} — MUDOU DE FAIXA: {prev_name} → {lv['zone_name']}"
    return f"{title}\n{head}\n{_advice(lv)}"


def stay_text(pair, lv, days):
    """Mensagem diária quando o preço continua na mesma faixa."""
    coin, head = _head(pair, lv)
    tempo = "desde ontem" if days <= 1 else f"há {days} dias"
    return f"{_icon(lv['zone'])} APORTE {coin} — se mantém em {lv['zone_name']} ({tempo})\n{head}\n{_advice(lv)}"


def reminder_text(levels_by_pair):
    lines = ["📅 Dia do aporte mensal (longo prazo)"]
    for pair, lv in levels_by_pair.items():
        coin = pair.split("/")[0]
        m = f"{lv['mayer']:.2f}".replace(".", ",")
        mult = f"{lv['mult']:g}".replace(".", ",")
        lines.append(f"{coin} {_fmt(lv['close'])} USD — {lv['zone_name']} ({m}× a média de 200 dias) → aporte sugerido: {mult}×")
        if lv["zone"] not in CHEAP:
            lines.append(f"  Fica vantajoso abaixo de {_fmt(lv['price_below_avg'])}; barato abaixo de {_fmt(lv['price_cheap'])}.")
    lines.append("O aporte fixo todo mês é a base; o multiplicador só reforça ou reduz.")
    return "\n".join(lines)


def update(st: dict, lv_by_pair: dict, cfg: dict, notify, now_ms: int) -> None:
    """Chamado 1× por dia (novo diário fechado). Manda UMA mensagem por moeda todo dia:
    'mudou de faixa' quando troca, 'se mantém' quando continua na mesma faixa."""
    a = cfg.get("aporte") or {}
    if not a.get("enabled", True) or not lv_by_pair:
        return
    ap = st.setdefault("aporte", {"pairs": {}, "last_reminder": None})
    for pair, lv in lv_by_pair.items():
        prev = ap["pairs"].get(pair) or {}
        if prev.get("date") == lv["date"]:                  # mesmo diário já avisado → só atualiza os números
            ap["pairs"][pair] = {**prev, **lv}
            continue
        changed = prev.get("zone") != lv["zone"]
        since = lv["date"] if changed else prev.get("zone_since", lv["date"])
        days = (datetime.strptime(lv["date"], "%Y-%m-%d") - datetime.strptime(since, "%Y-%m-%d")).days
        if changed:
            notify(change_text(pair, lv, prev.get("zone_name")))
            st.setdefault("events", []).append(
                {"ts": datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(timespec="seconds"),
                 "type": "aporte",
                 "text": f"Aporte {pair.split('/')[0]}: " + (f"{prev['zone_name']} → " if prev.get("zone_name") else "")
                         + f"{lv['zone_name']} ({lv['mayer']:.2f}× a média de 200 dias)".replace(".", ",")})
        elif a.get("notify_when_unchanged", True):
            notify(stay_text(pair, lv, days))
        ap["pairs"][pair] = {**lv, "zone_since": since, "zone_days": days}

    # lembrete mensal, no dia escolhido (horário de Brasília)
    dom = a.get("reminder_day")
    if dom:
        local = datetime.fromtimestamp(now_ms / 1000, timezone.utc) - timedelta(hours=3)
        key = local.strftime("%Y-%m")
        if local.day >= int(dom) and ap.get("last_reminder") != key:
            notify(reminder_text(lv_by_pair))
            ap["last_reminder"] = key


def summary_line(st: dict) -> str | None:
    ap = (st.get("aporte") or {}).get("pairs") or {}
    if not ap:
        return None
    parts = []
    for pair, lv in ap.items():
        m = f"{lv['mayer']:.2f}".replace(".", ",")
        mult = f"{lv['mult']:g}".replace(".", ",")
        parts.append(f"{pair.split('/')[0]} {lv['zone_name']} ({m}× média 200d, aporte {mult}×)")
    return "Aporte longo prazo: " + "; ".join(parts)

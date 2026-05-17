#!/usr/bin/env python3
"""
Market scan + verdict for BTC perps on Hyperliquid. V2.

Cambios v2 respecto al v1:
1.  Regla flex de cartas B y F (BULL-BIAS + RSI 1h > 60 para B,
    BEAR-BIAS + RSI 1h < 40 para F): activa retest del EMA20 1h sin
    exigir vela 15m de confirmación.
2.  detect_structure relajado: requiere 4 de 5 transiciones HH/HL o
    LH/LL en vez de las 5 estrictas. Evita el sesgo a "lateral".
3.  TP1 default 1:3 (antes 1:2). MIN_RR sube a 2.5 para mantener filtro.
4.  Cartas I y J de mean reversion en RSI extremo (oversold/overbought
    en bordes del rango 48h).
5.  vol_ratio_15m_norm: ratio normalizado por minutos transcurridos en
    la vela 15m actual (evita engaño cuando la vela arranca).
6.  support_flip y resistance_flip via pivotes reales (swing points con
    3 velas de confirmación a cada lado).
7.  Chequeo de eventos macro vía archivo macro_events.json. Si hay
    evento en menos de 1h, fuerza WAIT.
8.  Chequeo de loss streak vía trades.jsonl. Si las últimas 2 entradas
    son pérdidas, fuerza WAIT.
9.  reason del verdict marca oversold/overbought extremo y bear/bull
    trap risk cuando aplica.
10. decision_zone flag cuando precio está a menos de 0.2% de un
    trigger level.
11. next_check guidance estructurada (cuándo volver a chequear).
12. Modo backtest stub (TODO, se activa con --backtest desde CLI).

Usage: python3 market_scan_v2.py
       python3 market_scan_v2.py --backtest (no implementado aún)
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Config (paths relativos al script)
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_EVENTS_PATH = os.path.join(SCRIPT_DIR, "macro_events.json")
TRADES_PATH = os.path.join(SCRIPT_DIR, "..", "trades.jsonl")

# ntfy.sh push notifications
NTFY_TOPIC = "duke-btc-bullish"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
ALERT_STATE_PATH = os.path.join(SCRIPT_DIR, ".alert_state.json")  # evita alertas duplicadas

# Risk profile (ajustable desde acá)
# Profile activo: A (subido 2026-05-13). Capital $150, margin $75, leverage 3x.
# Risk per trade: 1.5% del capital ($2.25). Antes era 1% ($1.50).
NOTIONAL_USD = 225        # margin 75 * leverage 3
MAX_RISK_USD = 2.25       # 1.5% del capital de $150
MIN_SIGNALS = 3
MIN_RR = 2.5              # con TP 1:3, R/R nominal es 3.0
TP_RATIO = 3              # TP1 a 3x el stop
FUNDING_HOURLY_LIMIT = 0.01

# Early trigger cards (15m timeframe) y carta K momentum
MIN_RR_EARLY = 1.8        # cartas 15m con TP 1:2, R/R nominal 2.0
TP_RATIO_EARLY = 2        # TP1 a 2x el stop para cartas tempranas
DECISION_ZONE_PCT = 0.2   # precio dentro de este % activa preset_order


# ---------------------------------------------------------------------------
# HTTP + math helpers
# ---------------------------------------------------------------------------

def http_json(url, method="GET", body=None, headers=None):
    headers = headers or {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    out = [e]
    for v in values[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return [None] * len(closes)
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out = [None] * period
    rs = ag / al if al > 0 else 999
    out.append(100 - 100 / (1 + rs))
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rs = ag / al if al > 0 else 999
        out.append(100 - 100 / (1 + rs))
    return out


def atr(klines, period=14):
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return [None] * len(klines)
    a = sum(trs[:period]) / period
    out = [None] * period
    out.append(a)
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out.append(a)
    return out


def _normalize_kline_rows(raw, interval_minutes):
    return [
        {
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "vol": float(x[5]),
            "t_open_ms": int(x[0]),
            "t_close_ms": int(x[0]) + interval_minutes * 60000,
            "t": datetime.fromtimestamp(int(x[0]) / 1000, tz=timezone.utc).isoformat(),
        }
        for x in raw
    ]


def load_klines_from_bybit(interval_minutes, limit=100):
    url = (
        f"https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT"
        f"&interval={interval_minutes}&limit={limit}"
    )
    resp = http_json(url)
    if not isinstance(resp, dict) or "result" not in resp or "list" not in resp["result"]:
        raise RuntimeError(f"Bybit kline response shape inesperada: {resp}")
    raw = list(resp["result"]["list"])
    raw.reverse()
    return _normalize_kline_rows(raw, interval_minutes)


def load_klines_from_okx(interval_minutes, limit=100):
    bar_map = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1H", 120: "2H", 240: "4H"}
    bar = bar_map.get(interval_minutes)
    if bar is None:
        raise RuntimeError(f"OKX bar no soportado para interval_minutes={interval_minutes}")
    url = (
        f"https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP"
        f"&bar={bar}&limit={limit}"
    )
    resp = http_json(url)
    if not isinstance(resp, dict) or "data" not in resp:
        raise RuntimeError(f"OKX kline response shape inesperada: {resp}")
    raw = list(resp["data"])
    raw.reverse()
    return _normalize_kline_rows(raw, interval_minutes)


def load_klines_from_hyperliquid(interval_minutes, limit=100):
    interval_map = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 120: "2h", 240: "4h"}
    interval = interval_map.get(interval_minutes)
    if interval is None:
        raise RuntimeError(f"HL interval no soportado para interval_minutes={interval_minutes}")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (limit + 2) * interval_minutes * 60_000
    resp = http_json(
        "https://api.hyperliquid.xyz/info",
        method="POST",
        body={
            "type": "candleSnapshot",
            "req": {"coin": "BTC", "interval": interval, "startTime": start_ms, "endTime": now_ms},
        },
    )
    if not isinstance(resp, list):
        raise RuntimeError(f"HL kline response shape inesperada: {resp}")
    rows = [[c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]] for c in resp[-limit:]]
    return _normalize_kline_rows(rows, interval_minutes)


def load_klines(interval_minutes, limit=100):
    """Try Bybit -> OKX -> Hyperliquid (todos los exchanges asiaticos geo-bloquean GH Actions)."""
    last_err = None
    for fn in (load_klines_from_bybit, load_klines_from_okx, load_klines_from_hyperliquid):
        try:
            return fn(interval_minutes, limit)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"No se pudo obtener klines de ningun source: {last_err}")


# ---------------------------------------------------------------------------
# v2 helpers: structure, pivots, vol norm, macro, loss streak
# ---------------------------------------------------------------------------

def detect_structure(last6):
    """Relaxed: 4 de 5 transiciones confirman direccion."""
    highs = [x["high"] for x in last6]
    lows = [x["low"] for x in last6]
    n = len(highs) - 1
    if n < 5:
        return "lateral"
    hh = sum(1 for i in range(1, len(highs)) if highs[i] >= highs[i - 1])
    hl = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1])
    lh = sum(1 for i in range(1, len(highs)) if highs[i] <= highs[i - 1])
    ll = sum(1 for i in range(1, len(lows)) if lows[i] <= lows[i - 1])
    threshold = 4
    if hh >= threshold and hl >= threshold:
        return "alcista"
    if lh >= threshold and ll >= threshold:
        return "bajista"
    return "lateral"


def find_pivots(klines, n=3):
    """Detect swing highs/lows: candle is highest/lowest in window of n on each side."""
    pivot_highs = []
    pivot_lows = []
    for i in range(n, len(klines) - n):
        window = klines[i - n:i + n + 1]
        if klines[i]["high"] == max(c["high"] for c in window):
            pivot_highs.append((i, klines[i]["high"]))
        if klines[i]["low"] == min(c["low"] for c in window):
            pivot_lows.append((i, klines[i]["low"]))
    return pivot_highs, pivot_lows


def compute_flips(k1h_window, current_price, ema50_fallback):
    """
    support_flip: pivot high mas reciente que esta abajo del precio (resistencia ahora soporte).
    resistance_flip: pivot low mas reciente que esta arriba del precio (soporte ahora resistencia).
    """
    pivot_highs, pivot_lows = find_pivots(k1h_window, n=3)
    sup_candidates = [(i, p) for i, p in pivot_highs if p < current_price]
    res_candidates = [(i, p) for i, p in pivot_lows if p > current_price]
    support_flip = max(sup_candidates, key=lambda x: x[0])[1] if sup_candidates else ema50_fallback
    resistance_flip = min(res_candidates, key=lambda x: -x[0])[1] if res_candidates else None
    return support_flip, resistance_flip


def vol_ratio_15m_normalized(v15_last, avg20v_15, candle_open_ms):
    """Normaliza vol_ratio 15m por minutos transcurridos en la vela actual."""
    if avg20v_15 == 0:
        return 0
    now = datetime.now(timezone.utc)
    candle_open = datetime.fromtimestamp(candle_open_ms / 1000, tz=timezone.utc)
    elapsed_min = (now - candle_open).total_seconds() / 60
    elapsed_frac = max(min(elapsed_min / 15, 1.0), 0.05)  # piso 5% para evitar div by ~0
    return v15_last / (avg20v_15 * elapsed_frac)


def check_macro_events():
    """
    Lee macro_events.json (lista de {name, time_iso}) y devuelve (bool, str).
    True si hay evento en menos de 1h.
    """
    if not os.path.exists(MACRO_EVENTS_PATH):
        return False, None
    try:
        with open(MACRO_EVENTS_PATH) as f:
            events = json.load(f)
    except Exception:
        return False, None
    now = datetime.now(timezone.utc)
    for ev in events:
        try:
            ev_time = datetime.fromisoformat(ev["time_iso"].replace("Z", "+00:00"))
        except Exception:
            continue
        delta_s = (ev_time - now).total_seconds()
        if 0 < delta_s < 3600:
            return True, f"{ev['name']} en {int(delta_s / 60)} min"
    return False, None


def send_ntfy_alert(title, message, priority="default", tags=None):
    """Manda push notification via ntfy.sh. priority: min, low, default, high, urgent. tags: lista de emojis o keywords."""
    headers = {
        "Title": title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception as e:
        return False


def load_alert_state():
    if not os.path.exists(ALERT_STATE_PATH):
        return {}
    try:
        with open(ALERT_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_alert_state(state):
    try:
        with open(ALERT_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def maybe_send_alert(verdict_obj, mark):
    """
    Decide si mandar alerta a ntfy y la dispara.
    Evita repetir la misma alerta consecutiva (state file).
    """
    state = load_alert_state()
    last_alert = state.get("last_alert", "")
    last_card = state.get("last_card", "")
    now_iso = datetime.now(timezone.utc).isoformat()

    v = verdict_obj
    if v.get("verdict") == "GO":
        card = v.get("card", "?")
        params = v.get("params", {})
        alert_key = f"GO_{card}_{int(mark/100)}"  # cambia si BTC se mueve > $100
        if alert_key == last_alert:
            return False  # ya alertamos esto
        side = params.get("side", "?")
        entry = params.get("entry", 0)
        sl = params.get("sl", 0)
        tp = params.get("tp1", 0)
        risk = params.get("stop_real_loss_usd", 0)
        rr = params.get("rr", 0)
        flex = " FLEX" if v.get("flex_triggered") else ""
        early = " EARLY" if v.get("early_trigger") else ""
        title = f"GO {card}{flex}{early} | {side}"
        message = (
            f"BTC ${mark:.0f}\n"
            f"Entry ${entry}\nSL ${sl}\nTP ${tp}\n"
            f"Risk ${risk} | R/R 1:{rr}"
        )
        sent = send_ntfy_alert(title, message, priority="high", tags=["chart_with_upwards_trend" if side == "Long" else "chart_with_downwards_trend"])
        if sent:
            state["last_alert"] = alert_key
            state["last_card"] = card
            state["last_sent"] = now_iso
            save_alert_state(state)
        return sent

    if v.get("decision_zone") and v.get("preset_order"):
        po = v["preset_order"]
        card = v.get("nearest_card", "?")
        alert_key = f"DZ_{card}_{int(mark/100)}"
        if alert_key == last_alert:
            return False
        title = f"DECISION ZONE: {card} | {po['side']}"
        message = (
            f"BTC ${mark:.0f} a {v.get('nearest_distance_pct')}% del trigger\n"
            f"Preset {po['order_type']} @ ${po['trigger_price']}\n"
            f"SL ${po['sl']} | TP ${po['tp']} | Risk ${po['risk_usd']}"
        )
        sent = send_ntfy_alert(title, message, priority="high", tags=["warning"])
        if sent:
            state["last_alert"] = alert_key
            state["last_card"] = card
            state["last_sent"] = now_iso
            save_alert_state(state)
        return sent

    if v.get("hard_block"):
        alert_key = f"BLOCK_{v.get('hard_block')}"
        if alert_key == last_alert:
            return False
        title = f"BLOCKED: {v.get('hard_block')}"
        message = v.get("reason", "")
        sent = send_ntfy_alert(title, message, priority="default", tags=["no_entry"])
        if sent:
            state["last_alert"] = alert_key
            state["last_sent"] = now_iso
            save_alert_state(state)
        return sent

    return False


def check_loss_streak():
    """
    Lee trades.jsonl, devuelve True si las últimas 2 entradas son pérdidas DEL DÍA UTC ACTUAL.
    Si la última pérdida es de un día anterior, el circuit breaker ya expiró.
    """
    if not os.path.exists(TRADES_PATH):
        return False, "trades.jsonl no encontrado, sin chequeo de loss streak"
    try:
        with open(TRADES_PATH) as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception:
        return False, "trades.jsonl ilegible"
    if len(lines) < 2:
        return False, f"solo {len(lines)} trade(s) en journal, sin streak posible"
    try:
        last_two = [json.loads(l) for l in lines[-2:]]
    except Exception:
        return False, "trades.jsonl con JSON inválido en últimas entradas"
    losses = [t for t in last_two if (str(t.get("result") or t.get("pnl_status") or "")).lower() in ("loss", "sl")]
    if len(losses) != 2:
        return False, None
    # Las 2 últimas son pérdidas. ¿La más reciente es del día UTC actual?
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_trade_date = last_two[-1].get("date_utc", "")
    if last_trade_date == today_utc:
        return True, f"2 pérdidas consecutivas hoy ({today_utc})"
    return False, f"Loss streak previa expirada (último loss {last_trade_date}, hoy {today_utc})"


# ---------------------------------------------------------------------------
# Regime + signals + cards
# ---------------------------------------------------------------------------

def compute_regime(mkt):
    pos = mkt["range_48h"]["position_pct"]
    rsi1h = mkt["tf_1h"]["rsi14"]
    rsi1h_prev = mkt["tf_1h"]["rsi14_prev"]
    rsi15 = mkt["tf_15m"]["rsi14"]
    e20_1 = mkt["tf_1h"]["ema20"]
    e50_1 = mkt["tf_1h"]["ema50"]
    close_1h = mkt["tf_1h"]["close"]
    range_pct = mkt["range_48h"]["range_pct"]
    vol_ratio = mkt["tf_1h"]["vol_ratio"]

    chop_by_range = range_pct < 3.0
    chop_by_rsi = 40 <= rsi1h <= 60 and 40 <= rsi15 <= 60
    chop_by_vol = vol_ratio < 0.6

    if chop_by_range or chop_by_rsi or chop_by_vol:
        return "CHOP"
    if pos < 50 and 40 <= rsi1h <= 70 and e20_1 > e50_1:
        return "BULL-BIAS"
    if pos > 50 and rsi1h < rsi1h_prev and rsi1h_prev > 60:
        return "BEAR-BIAS"
    if e20_1 < e50_1 and close_1h < e20_1 and close_1h < e50_1:
        return "BEAR-BIAS"
    return "CHOP"


def extreme_rsi_flags(mkt):
    """Devuelve dict con flags de oversold/overbought extremo y trap risk."""
    rsi1h = mkt["tf_1h"]["rsi14"]
    rsi15 = mkt["tf_15m"]["rsi14"]
    pos = mkt["range_48h"]["position_pct"]
    return {
        "oversold_extreme": rsi1h < 30 and rsi15 < 30,
        "overbought_extreme": rsi1h > 70 and rsi15 > 70,
        "bear_trap_risk": rsi1h < 30 and pos < 5,
        "bull_trap_risk": rsi1h > 70 and pos > 95,
    }


def count_signals_long(mkt):
    signals = []
    t1 = mkt["tf_1h"]
    t15 = mkt["tf_15m"]
    if t1["ema20"] > t1["ema50"] and t1["close"] > t1["ema20"] and t1["close"] > t1["ema50"]:
        signals.append("trend_1h_bull")
    if t15["structure_last6"] == "alcista":
        signals.append("structure_15m_bull")
    if t15["rsi14"] > 50 and t15["rsi14"] > t15["rsi14_prev"] and 40 <= t1["rsi14"] <= 70:
        signals.append("rsi_momentum_bull")
    # señal extra: RSI extremo oversold (mean reversion long)
    if t1["rsi14"] < 30 and t15["rsi14"] < 30 and t15["rsi14"] > t15["rsi14_prev"]:
        signals.append("rsi_oversold_reversal")
    if t1["vol_ratio"] > 1.0 or t15["vol_ratio_norm"] > 1.0:
        signals.append("volume_confirm")
    if mkt["hyperliquid"]["funding_hourly_pct"] < FUNDING_HOURLY_LIMIT:
        signals.append("funding_ok_long")
    if mkt["range_48h"]["position_pct"] < 40:
        signals.append("position_range_long")
    return len(signals), signals


def count_signals_short(mkt):
    signals = []
    t1 = mkt["tf_1h"]
    t15 = mkt["tf_15m"]
    if (t1["ema20"] < t1["ema50"] and t1["close"] < t1["ema20"] and t1["close"] < t1["ema50"]) or \
       (t1["close"] < t1["ema50"] and t1["rsi14"] < 45):
        signals.append("trend_1h_bear")
    if t15["structure_last6"] == "bajista":
        signals.append("structure_15m_bear")
    if t15["rsi14"] < 50 and t15["rsi14"] < t15["rsi14_prev"] and 30 <= t1["rsi14"] <= 60:
        signals.append("rsi_momentum_bear")
    if t1["rsi14"] > 70 and t15["rsi14"] > 70 and t15["rsi14"] < t15["rsi14_prev"]:
        signals.append("rsi_overbought_reversal")
    if t1["vol_ratio"] > 1.0 or t15["vol_ratio_norm"] > 1.0:
        signals.append("volume_confirm")
    if mkt["hyperliquid"]["funding_hourly_pct"] > -FUNDING_HOURLY_LIMIT:
        signals.append("funding_ok_short")
    if mkt["range_48h"]["position_pct"] > 60:
        signals.append("position_range_short")
    return len(signals), signals


def near(price, level, tol_pct):
    if level is None or level == 0:
        return False
    return abs(price - level) / level * 100 <= tol_pct


def long_params(entry, sl, tp2_candidate, card_name, tp_ratio=None):
    if tp_ratio is None:
        tp_ratio = TP_RATIO
    sl_dist = entry - sl
    tp1 = entry + tp_ratio * sl_dist
    rr = (tp1 - entry) / sl_dist if sl_dist > 0 else 0
    stop_usd = sl_dist / entry * NOTIONAL_USD if entry else 0
    return {
        "side": "Long",
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2_candidate, 2) if tp2_candidate else None,
        "sl_dist_usd": round(sl_dist, 2),
        "sl_dist_pct": round(sl_dist / entry * 100, 3) if entry else 0,
        "stop_real_loss_usd": round(stop_usd, 2),
        "rr": round(rr, 2),
        "be_trigger": round(entry + 0.5 * (tp1 - entry), 2),
        "card": card_name,
    }


def short_params(entry, sl, tp2_candidate, card_name, tp_ratio=None):
    if tp_ratio is None:
        tp_ratio = TP_RATIO
    sl_dist = sl - entry
    tp1 = entry - tp_ratio * sl_dist
    rr = (entry - tp1) / sl_dist if sl_dist > 0 else 0
    stop_usd = sl_dist / entry * NOTIONAL_USD if entry else 0
    return {
        "side": "Short",
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2_candidate, 2) if tp2_candidate else None,
        "sl_dist_usd": round(sl_dist, 2),
        "sl_dist_pct": round(sl_dist / entry * 100, 3) if entry else 0,
        "stop_real_loss_usd": round(stop_usd, 2),
        "rr": round(rr, 2),
        "be_trigger": round(entry - 0.5 * (entry - tp1), 2),
        "card": card_name,
    }


def evaluate_cards(mkt, regime, ex_flags):
    t1 = mkt["tf_1h"]
    t15 = mkt["tf_15m"]
    lv = mkt["trigger_levels"]
    hl = mkt["hyperliquid"]
    close = t1["close"]
    atr_1h = t1["atr14"] or 0
    rango_alt = mkt["range_48h"]["high"] - mkt["range_48h"]["low"]

    candle_green = t1["close"] > t1["open"]
    candle_red = t1["close"] < t1["open"]

    cards = {}

    # Carta A. LONG retest support flip
    a_active = (
        near(close, lv["support_flip"], 0.2)
        and candle_green
        and t1["vol_ratio"] > 1.0
        and regime in ("BULL-BIAS",)
    )
    entry_a = lv["support_flip"] * 1.0015
    sl_a = lv["support_flip"] * 0.995
    cards["A"] = {
        "active": a_active,
        "description": "LONG retest support flip",
        "nearest_level": lv["support_flip"],
        "distance": round(close - lv["support_flip"], 2),
        "params": long_params(entry_a, sl_a, lv["breakout_level"], "A"),
    }

    # Carta B. LONG retest EMA20 1h con regla flex
    b_active_strict = (
        near(close, lv["ema20_1h"], 0.1)
        and t15["rsi14"] > 50
        and t15["rsi14"] > t15["rsi14_prev"]
        and regime == "BULL-BIAS"
    )
    b_active_flex = (
        near(close, lv["ema20_1h"], 0.15)
        and regime == "BULL-BIAS"
        and t1["rsi14"] > 60
    )
    b_active = b_active_strict or b_active_flex
    entry_b = lv["ema20_1h"]
    sl_b = lv["ema20_1h"] - atr_1h
    cards["B"] = {
        "active": b_active,
        "flex_active": b_active_flex and not b_active_strict,
        "description": "LONG retest EMA20 1h",
        "nearest_level": lv["ema20_1h"],
        "distance": round(close - lv["ema20_1h"], 2),
        "params": long_params(entry_b, sl_b, lv["breakout_level"], "B"),
    }

    # Carta C. LONG breakout
    c_active = close > lv["breakout_level"] and t1["vol_ratio"] > 2.0
    entry_c = lv["breakout_level"] * 1.001
    sl_c = lv["breakout_level"] * 0.995
    cards["C"] = {
        "active": c_active,
        "description": "LONG breakout swing_high_48h",
        "nearest_level": lv["breakout_level"],
        "distance": round(close - lv["breakout_level"], 2),
        "params": long_params(entry_c, sl_c, lv["breakout_level"] + rango_alt, "C"),
    }

    # Carta E. SHORT retest resistance flip
    resistance_flip = lv.get("resistance_flip") or lv["ema20_1h"]
    e_active = (
        near(close, resistance_flip, 0.2)
        and candle_red
        and t1["vol_ratio"] > 1.0
        and mkt["range_48h"]["position_pct"] > 60
        and regime == "BEAR-BIAS"
    )
    entry_e = resistance_flip * 0.9985
    sl_e = resistance_flip * 1.005
    cards["E"] = {
        "active": e_active,
        "description": "SHORT retest resistance flip",
        "nearest_level": resistance_flip,
        "distance": round(close - resistance_flip, 2),
        "params": short_params(entry_e, sl_e, mkt["range_48h"]["low"], "E"),
    }

    # Carta F. SHORT retest EMA20 desde abajo con regla flex
    f_active_strict = (
        near(close, lv["ema20_1h"], 0.1)
        and close < lv["ema20_1h"] * 1.001
        and t15["rsi14"] < 50
        and t15["rsi14"] < t15["rsi14_prev"]
        and regime in ("BEAR-BIAS", "CHOP")
    )
    f_active_flex = (
        near(close, lv["ema20_1h"], 0.15)
        and regime == "BEAR-BIAS"
        and t1["rsi14"] < 40
    )
    f_active = f_active_strict or f_active_flex
    entry_f = lv["ema20_1h"]
    sl_f = lv["ema20_1h"] + atr_1h
    cards["F"] = {
        "active": f_active,
        "flex_active": f_active_flex and not f_active_strict,
        "description": "SHORT retest EMA20 1h desde abajo",
        "nearest_level": lv["ema20_1h"],
        "distance": round(close - lv["ema20_1h"], 2),
        "params": short_params(entry_f, sl_f, mkt["range_48h"]["low"], "F"),
    }

    # Carta G. SHORT breakdown intradía
    g_active = (
        close < mkt["range_48h"]["low"]
        and t1["vol_ratio"] > 1.5
        and t15["rsi14"] < 40
    )
    entry_g = mkt["range_48h"]["low"] * 0.999
    sl_g = mkt["range_48h"]["low"] * 1.005
    cards["G"] = {
        "active": g_active,
        "description": "SHORT breakdown intradía",
        "nearest_level": mkt["range_48h"]["low"],
        "distance": round(close - mkt["range_48h"]["low"], 2),
        "params": short_params(entry_g, sl_g, mkt["range_48h"]["low"] - rango_alt, "G"),
    }

    # Carta H. SHORT quiebre estructural macro
    h_active = close < lv["structure_break_down"] and t1["vol_ratio"] > 2.0
    entry_h = lv["structure_break_down"] * 0.9995
    sl_h = lv["structure_break_down"] * 1.007
    cards["H"] = {
        "active": h_active,
        "description": "SHORT quiebre estructural macro",
        "nearest_level": lv["structure_break_down"],
        "distance": round(close - lv["structure_break_down"], 2),
        "params": short_params(entry_h, sl_h, lv["structure_break_down"] - rango_alt, "H"),
    }

    # Carta I. LONG mean reversion en oversold extremo
    i_active = (
        ex_flags["oversold_extreme"]
        and near(close, mkt["range_48h"]["low"], 0.3)
        and t15["rsi14"] > t15["rsi14_prev"]  # 15m girando arriba
        and hl["funding_hourly_pct"] > -FUNDING_HOURLY_LIMIT
    )
    entry_i = close * 1.0005
    sl_i = mkt["range_48h"]["low"] * 0.993
    cards["I"] = {
        "active": i_active,
        "description": "LONG mean reversion (RSI extremo oversold en swing_low_48h)",
        "nearest_level": mkt["range_48h"]["low"],
        "distance": round(close - mkt["range_48h"]["low"], 2),
        "params": long_params(entry_i, sl_i, lv["ema20_1h"], "I"),
    }

    # Carta J. SHORT mean reversion en overbought extremo
    j_active = (
        ex_flags["overbought_extreme"]
        and near(close, mkt["range_48h"]["high"], 0.3)
        and t15["rsi14"] < t15["rsi14_prev"]
        and hl["funding_hourly_pct"] < FUNDING_HOURLY_LIMIT
    )
    entry_j = close * 0.9995
    sl_j = mkt["range_48h"]["high"] * 1.007
    cards["J"] = {
        "active": j_active,
        "description": "SHORT mean reversion (RSI extremo overbought en swing_high_48h)",
        "nearest_level": mkt["range_48h"]["high"],
        "distance": round(close - mkt["range_48h"]["high"], 2),
        "params": short_params(entry_j, sl_j, lv["ema20_1h"], "J"),
    }

    # =====================================================================
    # Cartas EARLY TRIGGER (cierre 15m en vez de 1h, R/R 1:2)
    # =====================================================================
    c15 = mkt["candle_15m"]
    r15m24 = mkt["range_15m_24"]

    # Carta C15. LONG breakout 15m
    # Activa si: close_15m > swing_high_48h + vol_15m_norm > 2x (no espera cierre 1h)
    c15_active = (
        close > lv["breakout_level"]
        and t15["vol_ratio_norm"] > 2.0
        and c15["green"]
    )
    entry_c15 = lv["breakout_level"] * 1.0005
    sl_c15 = lv["breakout_level"] * 0.997  # 0.3% debajo, SL más ajustado para early
    cards["C15"] = {
        "active": c15_active,
        "description": "LONG breakout 15m (early trigger, no espera cierre 1h)",
        "nearest_level": lv["breakout_level"],
        "distance": round(close - lv["breakout_level"], 2),
        "params": long_params(entry_c15, sl_c15, lv["breakout_level"] + rango_alt * 0.5, "C15", tp_ratio=TP_RATIO_EARLY),
    }

    # Carta G15. SHORT breakdown 15m
    g15_active = (
        close < mkt["range_48h"]["low"]
        and t15["vol_ratio_norm"] > 2.0
        and t15["rsi14"] < 40
        and c15["red"]
    )
    entry_g15 = mkt["range_48h"]["low"] * 0.9995
    sl_g15 = mkt["range_48h"]["low"] * 1.003
    cards["G15"] = {
        "active": g15_active,
        "description": "SHORT breakdown 15m (early trigger, no espera cierre 1h)",
        "nearest_level": mkt["range_48h"]["low"],
        "distance": round(close - mkt["range_48h"]["low"], 2),
        "params": short_params(entry_g15, sl_g15, mkt["range_48h"]["low"] - rango_alt * 0.5, "G15", tp_ratio=TP_RATIO_EARLY),
    }

    # Carta H15. SHORT structure break 15m
    h15_active = (
        close < lv["structure_break_down"]
        and t15["vol_ratio_norm"] > 2.5
        and c15["red"]
    )
    entry_h15 = lv["structure_break_down"] * 0.9995
    sl_h15 = lv["structure_break_down"] * 1.005
    cards["H15"] = {
        "active": h15_active,
        "description": "SHORT structure break 15m (early trigger)",
        "nearest_level": lv["structure_break_down"],
        "distance": round(close - lv["structure_break_down"], 2),
        "params": short_params(entry_h15, sl_h15, lv["structure_break_down"] - rango_alt * 0.5, "H15", tp_ratio=TP_RATIO_EARLY),
    }

    # =====================================================================
    # Carta K MOMENTUM. Vol explosivo + dirección + estructura
    # SL en último swing 15m, NO en swing 48h. Entry inmediato.
    # =====================================================================
    k_long_active = (
        t15["vol_ratio_norm"] > 3.0
        and c15["pct_move"] > 0.4
        and c15["green"]
        and t15["rsi14"] > t15["rsi14_prev"]
        and t15["rsi14"] > 50
        and not ex_flags["overbought_extreme"]  # evitar entrar en techo
    )
    k_short_active = (
        t15["vol_ratio_norm"] > 3.0
        and c15["pct_move"] < -0.4
        and c15["red"]
        and t15["rsi14"] < t15["rsi14_prev"]
        and t15["rsi14"] < 50
        and not ex_flags["oversold_extreme"]  # evitar entrar en piso
    )
    if k_long_active:
        entry_k = close
        sl_k = r15m24["low"]  # último swing low 15m
        cards["K"] = {
            "active": True,
            "side_active": "Long",
            "description": "LONG momentum anticipado (vol 15m explosivo + direccional)",
            "nearest_level": close,
            "distance": 0,
            "params": long_params(entry_k, sl_k, lv["ema20_1h"], "K", tp_ratio=TP_RATIO_EARLY),
        }
    elif k_short_active:
        entry_k = close
        sl_k = r15m24["high"]
        cards["K"] = {
            "active": True,
            "side_active": "Short",
            "description": "SHORT momentum anticipado (vol 15m explosivo + direccional)",
            "nearest_level": close,
            "distance": 0,
            "params": short_params(entry_k, sl_k, lv["ema20_1h"], "K", tp_ratio=TP_RATIO_EARLY),
        }
    else:
        cards["K"] = {
            "active": False,
            "description": "Momentum anticipado (esperando vol_15m > 3x + dir > 0.4% + RSI alineado)",
            "nearest_level": close,
            "distance": 0,
            "params": None,
        }

    return cards


def select_verdict(mkt, regime, cards, long_sigs, short_sigs, ex_flags,
                   macro_block, macro_reason, loss_streak, loss_reason):
    funding = mkt["hyperliquid"]["funding_hourly_pct"]

    # Hard blocks: macro, loss streak
    if macro_block:
        return {
            "verdict": "WAIT",
            "card": None,
            "hard_block": "macro_event",
            "reason": f"Bloqueado por evento macro: {macro_reason}",
            "signals_long": long_sigs[0],
            "signals_short": short_sigs[0],
        }
    if loss_streak:
        return {
            "verdict": "WAIT",
            "card": None,
            "hard_block": "loss_streak",
            "reason": f"Circuit breaker activado: {loss_reason}. Parar hasta próximo día UTC.",
            "signals_long": long_sigs[0],
            "signals_short": short_sigs[0],
        }

    # Régimen filter, ahora con I, J, K y cartas 15m habilitadas
    if regime == "CHOP":
        allowed = ["C", "G", "H", "I", "J", "C15", "G15", "H15", "K"]
    elif regime == "BULL-BIAS":
        allowed = ["A", "B", "C", "J", "C15", "K"]
    elif regime == "BEAR-BIAS":
        allowed = ["E", "F", "G", "H", "I", "G15", "H15", "K"]
    else:
        allowed = list(cards.keys())

    # Cartas tempranas tienen MIN_RR más bajo
    EARLY_CARDS = {"C15", "G15", "H15", "K"}

    for name in allowed:
        card = cards[name]
        if not card["active"]:
            continue
        params = card["params"]
        if params is None:
            continue
        side = params["side"]
        sig_count = long_sigs[0] if side == "Long" else short_sigs[0]
        sig_names = long_sigs[1] if side == "Long" else short_sigs[1]
        # Cartas tempranas y K aceptan 2 señales (porque el vol explosivo ya es señal fuerte)
        min_sig = 2 if name in EARLY_CARDS else MIN_SIGNALS
        if sig_count < min_sig:
            continue
        min_rr_required = MIN_RR_EARLY if name in EARLY_CARDS else MIN_RR
        if params["rr"] < min_rr_required:
            continue
        if side == "Long" and funding > FUNDING_HOURLY_LIMIT:
            continue
        if side == "Short" and funding < -FUNDING_HOURLY_LIMIT:
            continue
        if params["stop_real_loss_usd"] > MAX_RISK_USD + 0.5:
            continue
        return {
            "verdict": "GO",
            "card": name,
            "flex_triggered": card.get("flex_active", False),
            "early_trigger": name in EARLY_CARDS,
            "signals_aligned": sig_count,
            "signal_names": sig_names,
            "params": params,
            "reason": f"Carta {name} activa en régimen {regime} con {sig_count} señales"
                      + (" (FLEX)" if card.get("flex_active") else "")
                      + (" (EARLY)" if name in EARLY_CARDS else ""),
        }

    # WAIT: encontrar trigger más cercano (en distancia absoluta porcentual)
    # Excluir K cuando inactiva (su distance es 0 pero no es un nivel real, es el precio actual)
    closest_card = None
    closest_dist_pct = float("inf")
    for name in allowed:
        if name == "K" and not cards[name]["active"]:
            continue
        if cards[name].get("params") is None:
            continue
        nl = cards[name]["nearest_level"]
        if not nl:
            continue
        d_pct = abs(cards[name]["distance"]) / nl * 100
        if d_pct < closest_dist_pct:
            closest_dist_pct = d_pct
            closest_card = name

    reason_parts = [f"Régimen {regime}"]
    if ex_flags["oversold_extreme"]:
        reason_parts.append("RSI extremo oversold (posible reversión)")
    if ex_flags["overbought_extreme"]:
        reason_parts.append("RSI extremo overbought (posible reversión)")
    if ex_flags["bear_trap_risk"]:
        reason_parts.append("riesgo bear trap")
    if ex_flags["bull_trap_risk"]:
        reason_parts.append("riesgo bull trap")
    if closest_card:
        card = cards[closest_card]
        reason_parts.append(
            f"Trigger más cerca: {closest_card} ({card['description']}) a ${card['distance']} del mark"
        )
    if long_sigs[0] >= short_sigs[0]:
        reason_parts.append(
            f"Señales LONG: {long_sigs[0]} ({', '.join(long_sigs[1]) if long_sigs[1] else 'ninguna'})"
        )
    else:
        reason_parts.append(
            f"Señales SHORT: {short_sigs[0]} ({', '.join(short_sigs[1]) if short_sigs[1] else 'ninguna'})"
        )

    # decision_zone: precio a < 0.2% de un trigger level
    decision_zone = closest_card is not None and closest_dist_pct < DECISION_ZONE_PCT

    # next_check guidance
    nl_str = f"${cards[closest_card]['nearest_level']:.2f}" if closest_card else "n/a"
    next_check = {
        "at_next_1h_close": True,
        "trigger_level_to_watch": nl_str,
        "what_to_watch": f"Confirmación de {closest_card} en {nl_str}" if closest_card else "Cambio de régimen",
    }

    # preset_orders: cuando decision_zone, armar la orden lista para Valiant
    preset_order = None
    if decision_zone and closest_card and cards[closest_card].get("params"):
        p = cards[closest_card]["params"]
        is_long = p["side"] == "Long"
        order_type = "Stop-Buy Market" if is_long else "Stop-Sell Market"
        preset_order = {
            "card": closest_card,
            "side": p["side"],
            "margin_mode": "Isolated",
            "leverage": 3,
            "order_type": order_type,
            "trigger_price": p["entry"],
            "sl": p["sl"],
            "tp": p["tp1"],
            "qty_usdc": NOTIONAL_USD,
            "risk_usd": p["stop_real_loss_usd"],
            "rr": p["rr"],
            "be_trigger": p["be_trigger"],
            "cancel_if": (
                f"Precio retrocede más de 0.5% del nivel ({'arriba' if not is_long else 'debajo'} de "
                f"${p['entry'] * (1.005 if not is_long else 0.995):.0f}) sin gatillar"
            ),
            "note": "Orden pre-armada activada por decision_zone. Pegar en Valiant y dejar. Se ejecuta solo si BTC toca el trigger.",
        }

    return {
        "verdict": "WAIT",
        "card": None,
        "nearest_card": closest_card,
        "nearest_distance_pct": round(closest_dist_pct, 3) if closest_card else None,
        "signals_long": long_sigs[0],
        "signals_short": short_sigs[0],
        "decision_zone": decision_zone,
        "preset_order": preset_order,
        "extreme_flags": ex_flags,
        "next_check": next_check,
        "reason": " | ".join(reason_parts),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Hyperliquid context
    hl = http_json(
        "https://api.hyperliquid.xyz/info",
        method="POST",
        body={"type": "metaAndAssetCtxs"},
    )
    universe = hl[0]["universe"]
    ctxs = hl[1]
    idx = next(i for i, u in enumerate(universe) if u["name"] == "BTC")
    btc = ctxs[idx]
    mark = float(btc["markPx"])
    oracle = float(btc["oraclePx"])
    funding = float(btc["funding"])
    prev_day = float(btc["prevDayPx"])
    oi = float(btc["openInterest"])
    day_vol_usd = float(btc["dayNtlVlm"])

    # Klines
    k1h = load_klines(60, 100)
    k15 = load_klines(15, 100)
    cl1 = [x["close"] for x in k1h]
    cl15 = [x["close"] for x in k15]

    e20_1 = ema(cl1, 20)[-1]
    e50_1 = ema(cl1, 50)[-1]
    r1 = rsi(cl1, 14)
    a1 = atr(k1h, 14)

    e20_15 = ema(cl15, 20)[-1]
    e50_15 = ema(cl15, 50)[-1]
    r15 = rsi(cl15, 14)
    a15 = atr(k15, 14)

    v1 = [x["vol"] for x in k1h]
    avg20v_1 = sum(v1[-21:-1]) / 20
    v15 = [x["vol"] for x in k15]
    avg20v_15 = sum(v15[-21:-1]) / 20

    vol_ratio_1h = v1[-1] / avg20v_1 if avg20v_1 else 0
    vol_ratio_15m_raw = v15[-1] / avg20v_15 if avg20v_15 else 0
    vol_ratio_15m_norm = vol_ratio_15m_normalized(v15[-1], avg20v_15, k15[-1]["t_open_ms"])

    last48 = k1h[-48:]
    swing_high_48 = max(x["high"] for x in last48)
    swing_low_48 = min(x["low"] for x in last48)
    pos = (cl1[-1] - swing_low_48) / (swing_high_48 - swing_low_48) * 100

    # Structure 15m relajado
    last6 = k15[-6:]
    struct_15m = detect_structure(last6)

    # Swing high/low 15m (24 velas = 6 horas) para cartas early-trigger y SL ajustado
    last24_15m = k15[-24:]
    swing_high_24_15m = max(x["high"] for x in last24_15m)
    swing_low_24_15m = min(x["low"] for x in last24_15m)
    # Última vela 15m (puede estar en formación)
    last15m_candle = k15[-1]
    candle_15m_pct_move = ((last15m_candle["close"] - last15m_candle["open"]) / last15m_candle["open"] * 100) if last15m_candle["open"] else 0
    candle_15m_green = last15m_candle["close"] > last15m_candle["open"]
    candle_15m_red = last15m_candle["close"] < last15m_candle["open"]

    breakout_level = swing_high_48
    structure_break_down = min(swing_low_48, e50_1 - (a1[-1] or 0))

    # Pivot-based flips
    support_flip, resistance_flip = compute_flips(last48, cl1[-1], e50_1)

    mkt = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "hyperliquid": {
            "mark": mark,
            "oracle": oracle,
            "funding_hourly_pct": funding * 100,
            "funding_annual_pct": funding * 24 * 365 * 100,
            "change_24h_pct": (mark - prev_day) / prev_day * 100,
            "open_interest_btc": oi,
            "day_volume_usd": day_vol_usd,
        },
        "tf_1h": {
            "close": cl1[-1],
            "open": k1h[-1]["open"],
            "high": k1h[-1]["high"],
            "low": k1h[-1]["low"],
            "ema20": e20_1,
            "ema50": e50_1,
            "rsi14": r1[-1],
            "rsi14_prev": r1[-2],
            "atr14": a1[-1],
            "vol_last": v1[-1],
            "vol_avg20": avg20v_1,
            "vol_ratio": vol_ratio_1h,
            "trend": "bull" if e20_1 > e50_1 else "bear",
        },
        "tf_15m": {
            "close": cl15[-1],
            "ema20": e20_15,
            "ema50": e50_15,
            "rsi14": r15[-1],
            "rsi14_prev": r15[-2],
            "atr14": a15[-1],
            "vol_last": v15[-1],
            "vol_avg20": avg20v_15,
            "vol_ratio_raw": vol_ratio_15m_raw,
            "vol_ratio_norm": vol_ratio_15m_norm,
            "structure_last6": struct_15m,
        },
        "range_48h": {
            "high": swing_high_48,
            "low": swing_low_48,
            "range_pct": (swing_high_48 - swing_low_48) / swing_low_48 * 100,
            "position_pct": pos,
        },
        "range_15m_24": {
            "high": swing_high_24_15m,
            "low": swing_low_24_15m,
        },
        "candle_15m": {
            "open": last15m_candle["open"],
            "high": last15m_candle["high"],
            "low": last15m_candle["low"],
            "close": last15m_candle["close"],
            "pct_move": candle_15m_pct_move,
            "green": candle_15m_green,
            "red": candle_15m_red,
        },
        "trigger_levels": {
            "support_flip": support_flip,
            "resistance_flip": resistance_flip,
            "ema20_1h": e20_1,
            "ema20_15m": e20_15,
            "breakout_level": breakout_level,
            "structure_break_down": structure_break_down,
        },
    }

    regime = compute_regime(mkt)
    ex_flags = extreme_rsi_flags(mkt)
    long_sigs = count_signals_long(mkt)
    short_sigs = count_signals_short(mkt)
    cards = evaluate_cards(mkt, regime, ex_flags)

    macro_block, macro_reason = check_macro_events()
    loss_streak, loss_reason = check_loss_streak()

    verdict = select_verdict(
        mkt, regime, cards, long_sigs, short_sigs, ex_flags,
        macro_block, macro_reason, loss_streak, loss_reason
    )

    mkt["regime"] = regime
    mkt["extreme_flags"] = ex_flags
    mkt["signals_long_count"] = long_sigs[0]
    mkt["signals_long"] = long_sigs[1]
    mkt["signals_short_count"] = short_sigs[0]
    mkt["signals_short"] = short_sigs[1]
    mkt["cards"] = cards
    mkt["macro_check"] = {"blocked": macro_block, "reason": macro_reason}
    mkt["loss_streak_check"] = {"blocked": loss_streak, "reason": loss_reason}
    mkt["risk_config"] = {
        "notional_usd": NOTIONAL_USD,
        "max_risk_usd": MAX_RISK_USD,
        "min_signals": MIN_SIGNALS,
        "min_rr": MIN_RR,
        "tp_ratio": TP_RATIO,
    }
    mkt["verdict"] = verdict

    # Mandar alerta push si GO o decision_zone (evita duplicados via state file)
    alert_sent = maybe_send_alert(verdict, mark)
    mkt["alert_sent"] = alert_sent

    print(json.dumps(mkt, indent=2, default=str))


if __name__ == "__main__":
    if "--backtest" in sys.argv:
        print(json.dumps({"error": "Modo backtest no implementado todavía (TODO)"}, indent=2))
        sys.exit(1)
    main()

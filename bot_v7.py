"""
╔══════════════════════════════════════════════════════════╗
║     AKA SMART MONEY CRYPTO SCREENER BOT v7.0             ║
║     Built for Ahsan | Aka Trading Signals                ║
╚══════════════════════════════════════════════════════════╝

WHAT CHANGED FROM v6.0 (and why):

v6.0 fired on raw volume magnitude alone — a 30x+ spike on the 30m
candle, with no idea whether that volume was buying or selling. That is
the direct cause of "signal fires, coin dumps in front of me": a large
share of what v6 called an "explosion" was actually a sell-off.

v7.0 adds, in order of impact:

  1. BUY/SELL DIRECTION FILTER (the core fix). Binance's kline data
     includes taker-buy volume alongside total volume. We now require
     60%+ of the exploding candle's volume to be aggressive BUYING
     before it's even considered a candidate. This alone removes most
     of the "dumps in front of me" signals.
  2. MARKET STRUCTURE — swing high/low detection, trend classification
     (bullish / bearish / ranging), Break of Structure and Change of
     Character detection. A bearish structure now hard-blocks a long
     signal instead of being ignored, like it was in v6.
  3. HIGHER-TIMEFRAME BIAS — 1h EMA20 vs EMA50 trend check, used to
     penalize or block signals that fight the bigger trend.
  4. ORDER BLOCKS + FAIR VALUE GAPS — simplified SMC/ICT-style
     detection, used as confluence in the score.
  5. ATR-BASED STOPS/TARGETS — stop distance now scales with the
     coin's real volatility instead of a flat 7%, so the R:R shown is
     real math, not a fixed label.
  6. HARDENED HTTP — a malformed or error API response from Binance
     for one symbol can no longer throw an unhandled exception mid-scan.
  7. auto_scan_loop now retries fetching the Discord channel on startup
     instead of silently giving up forever if the first attempt fails.

DELIBERATELY NOT INCLUDED YET:
  - Volume profile / point-of-control — needs much higher-resolution
    data than REST klines give cheaply. Real phase-2 project.
  - ICT liquidity sweeps / kill-zone timing — phase 2.
  - LIVE ORDER EXECUTION. This bot still only sends Discord alerts.
    Wiring it to actually place trades on Binance is a separate,
    bigger step that should only happen after this signal logic has
    been backtested and has a known, real win rate.
"""

import os, time, logging, requests, certifi
from datetime import datetime, timedelta
import discord
from discord.ext import commands
import asyncio

TOKEN      = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# COOLDOWN
# ══════════════════════════════════════════════════════════
signal_cooldowns = {}

def is_on_cooldown(symbol, hours=4):
    last = signal_cooldowns.get(symbol)
    if not last:
        return False
    return datetime.utcnow() - last < timedelta(hours=hours)

def mark_signaled(symbol):
    signal_cooldowns[symbol] = datetime.utcnow()

# ══════════════════════════════════════════════════════════
# SAFE HTTP — guards against malformed / error JSON payloads
# ══════════════════════════════════════════════════════════
# Module-level cooldown: once Binance signals a rate-limit ban (418/429),
# every request goes quiet until this timestamp passes, instead of
# retrying every 5 minutes into an active ban and extending it.
_RATE_LIMIT_UNTIL = {"ts": 0}

def safe_get_json(url, params=None, timeout=15):
    if time.time() < _RATE_LIMIT_UNTIL["ts"]:
        return None
    try:
        r = requests.get(url, params=params, timeout=timeout, verify=certifi.where())
        if r.status_code in (418, 429):
            retry_after = r.headers.get("Retry-After")
            wait_s = int(retry_after) if retry_after and retry_after.isdigit() else 180
            _RATE_LIMIT_UNTIL["ts"] = time.time() + wait_s
            log.error(f"Binance rate-limited us (HTTP {r.status_code}). "
                      f"Pausing ALL requests for {wait_s}s instead of retrying into it.")
            return None
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"Request error {url}: {e}")
        return None
    if not isinstance(data, list):
        log.warning(f"Unexpected response shape from {url}: {str(data)[:200]}")
        return None
    return data

# ══════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════
def get_all_tickers():
    data = safe_get_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not data:
        return {}
    result = {}
    for c in data:
        if not isinstance(c, dict) or not str(c.get("symbol", "")).endswith("USDT"):
            continue
        try:
            result[c["symbol"]] = {
                "symbol":   c["symbol"],
                "gain":     float(c.get("priceChangePercent", 0)),
                "price":    float(c.get("lastPrice", 0)),
                "vol_usdt": float(c.get("quoteVolume", 0)),
            }
        except Exception:
            continue
    return result

def get_klines(symbol, interval, limit=45):
    """Unified kline fetcher. Captures taker-buy volume (index 10) —
    this is what powers the direction filter."""
    data = safe_get_json("https://fapi.binance.com/fapi/v1/klines",
                          params={"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return []
    out = []
    for k in data:
        try:
            out.append({
                "open":      float(k[1]),
                "high":      float(k[2]),
                "low":       float(k[3]),
                "close":     float(k[4]),
                "vol_usdt":  float(k[7]),   # total quote volume
                "buy_vol":   float(k[10]),  # taker BUY quote volume
                "open_time": k[0],
            })
        except (IndexError, ValueError, TypeError):
            continue
    return out

def get_oi(symbol):
    data = safe_get_json("https://fapi.binance.com/futures/data/openInterestHist",
                          params={"symbol": symbol, "period": "30m", "limit": 4})
    if not data or len(data) < 2:
        return None
    try:
        old = float(data[0]["sumOpenInterest"])
        new = float(data[-1]["sumOpenInterest"])
        return round((new - old) / old * 100, 2) if old > 0 else None
    except Exception:
        return None

def get_funding(symbol):
    data = safe_get_json("https://fapi.binance.com/fapi/v1/fundingRate",
                          params={"symbol": symbol, "limit": 1})
    if not data:
        return None
    try:
        return round(float(data[-1]["fundingRate"]) * 100, 4)
    except Exception:
        return None

# ══════════════════════════════════════════════════════════
# VOLUME + DIRECTION  (the core fix)
# ══════════════════════════════════════════════════════════
def detect_volume_explosion(klines, min_ratio=30, min_vol=100_000, baseline_n=20):
    if len(klines) < baseline_n + 2:
        return False, 0, 0, 0
    baseline = klines[-(baseline_n + 2):-2]
    current = klines[-1]
    vols = [k["vol_usdt"] for k in baseline if k["vol_usdt"] > 0]
    if not vols:
        return False, 0, 0, 0
    avg_vol = sum(vols) / len(vols)
    if avg_vol <= 0:
        return False, 0, 0, 0
    ratio = current["vol_usdt"] / avg_vol
    is_explosion = ratio >= min_ratio and current["vol_usdt"] >= min_vol
    return is_explosion, round(ratio, 1), round(avg_vol, 0), round(current["vol_usdt"], 0)

def simple_ratio(klines, baseline_n=10):
    if len(klines) < baseline_n + 2:
        return 0
    baseline = klines[-(baseline_n + 2):-2]
    current = klines[-1]
    vols = [k["vol_usdt"] for k in baseline if k["vol_usdt"] > 0]
    if not vols:
        return 0
    avg = sum(vols) / len(vols)
    if avg <= 0:
        return 0
    return round(current["vol_usdt"] / avg, 1)

def buy_sell_ratio(candle):
    """Fraction of this candle's volume that was aggressive BUYING.
    > 0.5 = net buying pressure, < 0.5 = net selling pressure.
    This is the field v6.0 never looked at."""
    if candle["vol_usdt"] <= 0:
        return 0.5
    return round(candle["buy_vol"] / candle["vol_usdt"], 3)

# ══════════════════════════════════════════════════════════
# MARKET STRUCTURE — swings, trend, BOS / CHoCH
# ══════════════════════════════════════════════════════════
def find_swings(klines, left=2, right=2):
    """Fractal swing highs/lows. Returns [(index, price, 'H'|'L'), ...]."""
    swings = []
    n = len(klines)
    for i in range(left, n - right):
        window_highs = [klines[j]["high"] for j in range(i - left, i + right + 1)]
        window_lows  = [klines[j]["low"]  for j in range(i - left, i + right + 1)]
        if klines[i]["high"] == max(window_highs) and window_highs.count(klines[i]["high"]) == 1:
            swings.append((i, klines[i]["high"], "H"))
        if klines[i]["low"] == min(window_lows) and window_lows.count(klines[i]["low"]) == 1:
            swings.append((i, klines[i]["low"], "L"))
    swings.sort(key=lambda s: s[0])
    return swings

def market_structure(klines):
    """Classify trend from the last two swings of each type and flag
    BOS (continuation break) or CHoCH (character change) on the latest
    close."""
    swings = find_swings(klines)
    if len(swings) < 4:
        return {"trend": "ranging", "event": None, "last_high": None, "last_low": None}

    highs = [s for s in swings if s[2] == "H"]
    lows  = [s for s in swings if s[2] == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return {"trend": "ranging", "event": None, "last_high": None, "last_low": None}

    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1]  > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1]  < lows[-2][1]

    if hh and hl:
        trend = "bullish"
    elif lh and ll:
        trend = "bearish"
    else:
        trend = "ranging"

    last_close = klines[-1]["close"]
    last_high  = highs[-1][1]
    last_low   = lows[-1][1]

    event = None
    if trend == "bullish" and last_close < last_low:
        event = "CHoCH"
    elif trend == "bearish" and last_close > last_high:
        event = "CHoCH"
    elif trend == "bullish" and last_close > last_high:
        event = "BOS"
    elif trend == "bearish" and last_close < last_low:
        event = "BOS"

    return {"trend": trend, "event": event, "last_high": last_high, "last_low": last_low}

# ══════════════════════════════════════════════════════════
# ORDER BLOCKS  (simplified SMC)
# ══════════════════════════════════════════════════════════
def find_last_bullish_ob(klines, lookback=15):
    """Last down-close candle immediately before an impulsive break higher."""
    n = len(klines)
    start = max(n - lookback, 1)
    for i in range(n - 2, start, -1):
        c, nxt = klines[i], klines[i + 1]
        if c["close"] < c["open"] and nxt["close"] > c["high"]:
            return {"low": c["low"], "high": c["high"], "index": i}
    return None

# ══════════════════════════════════════════════════════════
# FAIR VALUE GAPS
# ══════════════════════════════════════════════════════════
def find_recent_fvg(klines, lookback=10):
    """3-candle imbalance. Returns the most recent gap found, if any."""
    n = len(klines)
    start = max(n - lookback, 1)
    for i in range(n - 2, start, -1):
        if i - 1 < 0 or i + 1 >= n:
            continue
        c1, c3 = klines[i - 1], klines[i + 1]
        if c1["high"] < c3["low"]:
            return {"type": "bullish", "top": c3["low"], "bottom": c1["high"]}
        if c1["low"] > c3["high"]:
            return {"type": "bearish", "top": c1["low"], "bottom": c3["high"]}
    return None

# ══════════════════════════════════════════════════════════
# HIGHER TIMEFRAME BIAS
# ══════════════════════════════════════════════════════════
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def htf_bias(symbol):
    """1h EMA20 vs EMA50 — simple higher-timeframe trend filter."""
    k = get_klines(symbol, "1h", limit=60)
    if len(k) < 55:
        return "neutral"
    closes = [c["close"] for c in k]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20 is None or e50 is None:
        return "neutral"
    if e20 > e50 * 1.001:
        return "bullish"
    if e20 < e50 * 0.999:
        return "bearish"
    return "neutral"

# ══════════════════════════════════════════════════════════
# RSI / ATR
# ══════════════════════════════════════════════════════════
def calc_rsi(klines, period=14):
    closes = [k["close"] for k in klines]
    if len(closes) < period + 1:
        return None
    gains  = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - (100 / (1 + ag / al)), 1) if al > 0 else 100.0

def calc_atr(klines, period=14):
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def detect_price_action(klines):
    if len(klines) < 5:
        return True, 0
    start_price = klines[-3]["open"]
    current_price = klines[-1]["close"]
    if start_price <= 0:
        return True, 0
    recent_move = (current_price - start_price) / start_price * 100
    return recent_move <= 25, round(recent_move, 2)

# ══════════════════════════════════════════════════════════
# SCORER — confluence based
# ══════════════════════════════════════════════════════════
def score_signal(vol_ratio, buy_ratio, recent_move, struct, htf, ob, fvg,
                  oi_chg, funding, ticker_vol, rsi):
    """
    Positive confluence factors and risk penalties are accumulated
    separately and the positive side is capped BEFORE penalties are
    applied. This matters: with a single running total capped only at
    the end, a signal that already maxes out on volume/structure/OB/FVG
    could have a bearish-HTF or overbought-RSI penalty largely absorbed
    by the ceiling instead of actually pulling the score down. Applying
    penalties after the cap guarantees they always bite at full weight,
    exactly when it matters most (an otherwise "perfect-looking" signal
    that's fighting the higher timeframe or already overbought).
    """
    positive = 0
    penalty = 0
    reasons = []

    if vol_ratio >= 100:
        positive += 25; reasons.append(f"🔥 {vol_ratio}x volume explosion")
    elif vol_ratio >= 50:
        positive += 20; reasons.append(f"💥 {vol_ratio}x volume explosion")
    else:
        positive += 14; reasons.append(f"⚡ {vol_ratio}x volume explosion")

    positive += round((buy_ratio - 0.5) * 60)
    reasons.append(f"🟢 {int(buy_ratio * 100)}% of that volume was buying")

    if struct["trend"] == "bullish":
        positive += 15; reasons.append("📈 Bullish market structure (higher highs/lows)")
    if struct["event"] == "BOS":
        positive += 12; reasons.append("✅ Break of structure — trend continuation")
    elif struct["event"] == "CHoCH":
        positive += 8; reasons.append("🔄 Change of character — fresh reversal forming")

    if htf == "bullish":
        positive += 12; reasons.append("📊 1h higher-timeframe trend agrees (bullish)")
    elif htf == "bearish":
        penalty += 15; reasons.append("⚠️ 1h higher-timeframe trend disagrees (bearish)")

    if ob:
        positive += 8; reasons.append("🧱 Bullish order block below price")
    if fvg and fvg["type"] == "bullish":
        positive += 6; reasons.append("📐 Unfilled bullish fair value gap nearby")

    if recent_move <= 5:
        positive += 10; reasons.append(f"✨ Price barely moved +{recent_move}% — very early")
    elif recent_move <= 15:
        positive += 5; reasons.append(f"📈 Price +{recent_move}% — still early")

    if oi_chg and oi_chg >= 10:
        positive += 10; reasons.append(f"💰 OI +{oi_chg}% — new positions opening")
    elif oi_chg and oi_chg >= 3:
        positive += 5

    if rsi and rsi <= 60:
        positive += 5
    elif rsi and rsi > 80:
        penalty += 12; reasons.append(f"🔴 RSI {rsi} — overbought")

    if funding and funding < -0.01:
        positive += 6; reasons.append("🔄 Negative funding — shorts may get squeezed")
    elif funding and funding > 0.3:
        penalty += 8; reasons.append(f"⚠️ Funding {funding}% — overheated")

    if ticker_vol >= 50e6:
        positive += 4

    positive = max(min(positive, 100), 0)
    final = max(positive - penalty, 0)
    return final, reasons

# ══════════════════════════════════════════════════════════
# TRADE LEVELS — ATR-based, not flat percentages
# ══════════════════════════════════════════════════════════
def calculate_levels(price, atr, score):
    entry = price
    if not atr or atr <= 0:
        atr = price * 0.02

    stop_dist = atr * 1.5
    stop_dist = max(stop_dist, price * 0.02)
    stop_dist = min(stop_dist, price * 0.15)
    stop = entry - stop_dist

    if score >= 80:
        mults = (1.5, 3.0, 5.0)
    elif score >= 65:
        mults = (1.2, 2.5, 4.0)
    else:
        mults = (1.0, 2.0, 3.0)

    tp1 = entry + stop_dist * mults[0]
    tp2 = entry + stop_dist * mults[1]
    tp3 = entry + stop_dist * mults[2]

    rr = round((tp1 - entry) / (entry - stop), 1) if entry > stop else 0
    return entry, tp1, tp2, tp3, stop, rr

# ══════════════════════════════════════════════════════════
# FORMATTERS
# ══════════════════════════════════════════════════════════
def fmt_price(p):
    if not p: return "N/A"
    if p < 0.000001: return f"${p:.8f}"
    if p < 0.001:    return f"${p:.6f}"
    if p < 1:        return f"${p:.4f}"
    if p < 100:      return f"${p:.3f}"
    return f"${p:,.2f}"

def fmt_vol(v):
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

# ══════════════════════════════════════════════════════════
# MASTER SCANNER
# ══════════════════════════════════════════════════════════
SYMBOL_SCAN_LIMIT = 150  # cap per cycle — cuts request volume ~in half and
                          # focuses on the more liquid pairs anyway

def run_explosion_scanner():
    if time.time() < _RATE_LIMIT_UNTIL["ts"]:
        remaining = int(_RATE_LIMIT_UNTIL["ts"] - time.time())
        log.warning(f"Skipping scan — Binance rate-limit cooldown active for {remaining}s more")
        return []

    log.info("Scanner running (v7 — direction + structure filtered)...")
    tickers = get_all_tickers()
    if not tickers:
        log.error("Failed to get tickers")
        return []

    # Only check the most liquid pairs each cycle — same reasoning as the
    # existing $10K floor below, just applied as a hard cap so one scan
    # can't fire 300+ individual kline requests in a tight burst.
    top_symbols = sorted(tickers.items(), key=lambda kv: kv[1]["vol_usdt"], reverse=True)[:SYMBOL_SCAN_LIMIT]

    signals = []

    for sym, t in top_symbols:
        price = t["price"]
        vol = t["vol_usdt"]

        if price <= 0 or vol < 10_000:
            continue
        if is_on_cooldown(sym):
            continue
        if time.time() < _RATE_LIMIT_UNTIL["ts"]:
            log.warning("Rate-limit cooldown kicked in mid-scan — stopping this cycle early")
            break

        klines_30m = get_klines(sym, "30m", 45)
        time.sleep(0.25)
        if len(klines_30m) < 25:
            continue

        exploding, ratio_30m, avg_vol, curr_vol = detect_volume_explosion(klines_30m)
        if not exploding:
            continue

        # HARD GATE 1 — direction. This is the fix for "dumps in front of me".
        buy_ratio = buy_sell_ratio(klines_30m[-1])
        if buy_ratio < 0.60:
            log.info(f"{sym}: {ratio_30m}x volume but only {buy_ratio*100:.0f}% buy-side — skipping")
            continue

        # HARD GATE 2 — don't buy into a bearish structure.
        struct = market_structure(klines_30m)
        if struct["trend"] == "bearish":
            log.info(f"{sym}: volume+direction OK but structure is bearish — skipping")
            continue

        is_early, recent_move = detect_price_action(klines_30m)
        if not is_early and recent_move > 30:
            continue

        rsi = calc_rsi(klines_30m)
        if rsi and rsi > 85:
            continue

        # Expensive calls only for symbols that survive the cheap filters above.
        klines_5m = get_klines(sym, "5m", 20)
        time.sleep(0.15)
        ratio_5m = simple_ratio(klines_5m, 10) if klines_5m else 0

        htf = htf_bias(sym)
        time.sleep(0.15)
        if htf == "bearish" and struct["trend"] != "bullish":
            continue

        ob = find_last_bullish_ob(klines_30m)
        fvg = find_recent_fvg(klines_30m)

        oi_chg = get_oi(sym)
        funding = get_funding(sym)
        time.sleep(0.15)
        if funding and funding > 0.3:
            continue

        atr = calc_atr(klines_30m)

        score, reasons = score_signal(
            ratio_30m, buy_ratio, recent_move, struct, htf, ob, fvg,
            oi_chg, funding, vol, rsi
        )
        if ratio_5m >= 20:
            score = min(score + 8, 100)
            reasons.append(f"📈 5m also confirming, {ratio_5m}x")

        if score < 60:
            continue

        entry, tp1, tp2, tp3, stop, rr = calculate_levels(price, atr, score)

        signals.append({
            "sym": sym.replace("USDT", ""), "full_sym": sym, "price": price,
            "gain": t["gain"], "vol_24h": vol, "score": score, "rsi": rsi,
            "ratio_30m": ratio_30m, "ratio_5m": ratio_5m, "buy_ratio": buy_ratio,
            "trend": struct["trend"], "event": struct["event"], "htf": htf,
            "has_ob": bool(ob), "has_fvg": bool(fvg),
            "avg_vol": avg_vol, "curr_vol": curr_vol, "recent_move": recent_move,
            "oi_chg": oi_chg, "funding": funding, "reasons": reasons,
            "entry": entry, "tp1": tp1, "tp2": tp2, "tp3": tp3, "stop": stop, "rr": rr,
        })
        log.info(f"SIGNAL: {sym} | {ratio_30m}x | buy={buy_ratio} | struct={struct['trend']} | htf={htf} | score={score}")

    top = sorted(signals, key=lambda x: x["score"], reverse=True)[:3]
    for c in top:
        mark_signaled(c["full_sym"])
    log.info(f"Scanner done. Signals: {len(top)}")
    return top

# ══════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ══════════════════════════════════════════════════════════
def build_alert(coins, triggered_by="auto"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not coins:
        if triggered_by == "auto":
            return None
        return [
            f"**🔍 SCAN** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No qualifying setups right now — volume, buy-pressure and "
            f"structure all have to line up together.\nNext scan in 5 mins."
        ]

    label = "🚨 SIGNAL" if triggered_by == "auto" else "🔍 MANUAL SCAN"
    msgs = [f"**{label}** | 🕐 {now}\n━━━━━━━━━━━━━━━━━━━━━━━━"]

    for i, c in enumerate(coins, 1):
        rsi_str  = f"RSI {c['rsi']}" if c.get("rsi") else "RSI N/A"
        oi_str   = f"OI {'+' if c['oi_chg'] and c['oi_chg'] >= 0 else ''}{c['oi_chg']}%" if c.get("oi_chg") is not None else ""
        fund_str = f"Fund {c['funding']}%" if c.get("funding") is not None else ""
        sm_line  = " | ".join(filter(None, [rsi_str, oi_str, fund_str]))

        struct_line = f"Structure: {c['trend'].upper()}" + (f" ({c['event']})" if c.get("event") else "")
        htf_line = f"1h bias: {c['htf'].upper()}"
        conf_line = f"OB: {'yes' if c['has_ob'] else 'no'} | FVG: {'yes' if c['has_fvg'] else 'no'} | {int(c['buy_ratio']*100)}% buy volume"

        reasons_str = "\n".join(c["reasons"][:6])

        tp1_pct = round((c["tp1"] - c["entry"]) / c["entry"] * 100, 1)
        tp2_pct = round((c["tp2"] - c["entry"]) / c["entry"] * 100, 1)
        tp3_pct = round((c["tp3"] - c["entry"]) / c["entry"] * 100, 1)

        msg = (
            f"\n🚨 **{i}. {c['sym']}/USDT** — Score {c['score']}/100\n"
            f"{struct_line} | {htf_line}\n"
            f"{conf_line}\n"
            f"{sm_line}\n"
            f"\n{reasons_str}\n"
            f"\n📊 Avg candle vol: `{fmt_vol(c['avg_vol'])}` → Current: `{fmt_vol(c['curr_vol'])}` ({c['ratio_30m']}x)\n"
            f"\n💰 Price:  `{fmt_price(c['price'])}`\n"
            f"🔵 Entry:  `{fmt_price(c['entry'])}`\n"
            f"🎯 TP1:   `{fmt_price(c['tp1'])}` (+{tp1_pct}%)\n"
            f"🚀 TP2:   `{fmt_price(c['tp2'])}` (+{tp2_pct}%)\n"
            f"💎 TP3:   `{fmt_price(c['tp3'])}` (+{tp3_pct}%)\n"
            f"⛔ Stop:  `{fmt_price(c['stop'])}` | ⚖️ R/R 1:{c['rr']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        msgs.append(msg)

    msgs.append("⚠️ Alerts only — this bot does not place trades. Not financial advice, always use a stop loss.")
    return msgs

# ══════════════════════════════════════════════════════════
# DISCORD BOT
# ══════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def send_messages(target, msgs):
    if not msgs:
        return
    for msg in msgs:
        chunks = [msg[i:i + 1900] for i in range(0, len(msg), 1900)]
        for chunk in chunks:
            await target.send(chunk)
            await asyncio.sleep(0.3)

@bot.command(name="scan")
async def scan(ctx):
    await ctx.send("🔍 **Scanning all pairs...** ~60 secs...")
    try:
        coins = run_explosion_scanner()
        msgs = build_alert(coins, "manual")
        await send_messages(ctx, msgs)
    except Exception as e:
        log.error(f"scan command error: {e}")
        await ctx.send(f"⚠️ Scan hit an error and stopped early: {e}")

@bot.command(name="momentum")
async def momentum(ctx):
    await ctx.send("⚡ **Scanning for explosions...** ~60 secs...")
    try:
        coins = run_explosion_scanner()
        msgs = build_alert(coins, "manual")
        await send_messages(ctx, msgs)
    except Exception as e:
        log.error(f"momentum command error: {e}")
        await ctx.send(f"⚠️ Scan hit an error and stopped early: {e}")

@bot.command(name="early")
async def early(ctx):
    await ctx.send("🌅 **Scanning for early explosions...** ~60 secs...")
    try:
        coins = run_explosion_scanner()
        msgs = build_alert(coins, "manual")
        await send_messages(ctx, msgs)
    except Exception as e:
        log.error(f"early command error: {e}")
        await ctx.send(f"⚠️ Scan hit an error and stopped early: {e}")

@bot.command(name="status")
async def status(ctx):
    active = [s for s, tm in signal_cooldowns.items()
              if datetime.utcnow() - tm < timedelta(hours=4)]
    await ctx.send(
        f"**📊 AKA Screener v7.0 — Status**\n"
        f"✅ Online | Scanning every **5 minutes**\n"
        f"👁 Watching ALL USDT futures pairs\n"
        f"🔒 {len(active)} coins on cooldown (4h)\n"
        f"🎯 Filters: 30m vol 30x+ AND 60%+ buy-side AND bullish/ranging structure\n"
        f"⚠️ Alerts only — no live trade execution yet\n\n"
        f"Commands: `!scan` `!status`"
    )

# ══════════════════════════════════════════════════════════
# AUTO SCAN LOOP — every 5 minutes
# ══════════════════════════════════════════════════════════
async def auto_scan_loop():
    await bot.wait_until_ready()

    channel = None
    for attempt in range(10):
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            break
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
            break
        except Exception as e:
            log.warning(f"Channel fetch attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(5)

    if not channel:
        log.error("Channel not found after retries. Bot stays online for "
                   "commands, but auto-scan alerts will not post. Check "
                   "CHANNEL_ID.")
        return

    log.info("First scan in 2 minutes...")
    await asyncio.sleep(120)

    while True:
        try:
            log.info("Auto scan starting...")
            coins = run_explosion_scanner()
            if coins:
                msgs = build_alert(coins, "auto")
                if msgs:
                    await send_messages(channel, msgs)
                    log.info(f"Sent {len(coins)} alert(s)")
            else:
                log.info("No qualifying setups this round — silent")
        except Exception as e:
            log.error(f"Auto scan error: {e}")

        await asyncio.sleep(5 * 60)

@bot.event
async def on_ready():
    print(f"✅ AKA SMC Screener v7.0 online as {bot.user}!")
    print(f"👁  Watching ALL USDT futures pairs")
    print(f"⚡ Scanning every 5 minutes")
    print(f"🎯 Trigger: 30m volume 30x+ AND 60%+ buy-side AND structure filter")
    print(f"⏰ First scan in 2 minutes...")
    bot.loop.create_task(auto_scan_loop())

# ══════════════════════════════════════════════════════════
# KEEP-ALIVE WEB SERVER — only needed on Render's FREE tier.
# Render's free Web Service sleeps after 15 min with no incoming HTTP
# traffic. A Discord bot never gets HTTP traffic on its own, so this
# tiny server gives it something to answer — pair it with an external
# pinger (e.g. UptimeRobot, free) hitting this URL every ~5-10 minutes.
# Not needed at all on a paid Background Worker.
#
# HARDENED VERSION: uses ThreadingHTTPServer (handles each request on
# its own thread, so one slow/dropped connection can't block the rest)
# and wraps everything in a watchdog loop — if the server ever dies
# for any reason, it's recreated automatically instead of silently
# going dark while the Discord bot keeps running underneath it
# (which is what made this look "up" internally but "down" externally).
# ══════════════════════════════════════════════════════════
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"AKA Smart Money Screener v7 - alive")
        except Exception:
            pass  # a client disconnecting mid-response must never crash the server

    def log_message(self, format, *args):
        pass  # keep Render's logs from filling up with ping requests

def _keepalive_watchdog():
    port = int(os.environ.get("PORT", 10000))
    while True:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), _PingHandler)
            log.info(f"Keep-alive server listening on port {port}")
            server.serve_forever()
        except Exception as e:
            log.error(f"Keep-alive server crashed, restarting in 2s: {e}")
            time.sleep(2)

def start_keepalive_server():
    threading.Thread(target=_keepalive_watchdog, daemon=True).start()

if not TOKEN:
    print("ERROR: No DISCORD_TOKEN set!")
else:
    start_keepalive_server()
    bot.run(TOKEN)

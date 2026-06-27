"""
╔══════════════════════════════════════════════════════════╗
║     AKA SMART MONEY CRYPTO SCREENER BOT v4.0            ║
║     Built for Ahsan | Aka Trading Signals                ║
║     Strategy: Structure-Based Entry + SMC                ║
╚══════════════════════════════════════════════════════════╝
"""

import os, time, logging, requests, threading, schedule, certifi
from datetime import datetime
import discord
from discord.ext import commands

TOKEN      = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
SCAN_HOURS = 2

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def safe_get(url, params=None, timeout=15):
    try:
        r = requests.get(url, params=params, timeout=timeout, verify=certifi.where())
        r.raise_for_status()
        return r
    except Exception as e:
        log.error(f"Request error {url}: {e}")
        return None

# ══════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════

def get_futures_tickers():
    r = safe_get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not r: return {}
    return {
        c["symbol"]: {
            "symbol":   c["symbol"],
            "gain":     float(c.get("priceChangePercent", 0)),
            "price":    float(c.get("lastPrice", 0)),
            "vol_usdt": float(c.get("quoteVolume", 0)),
            "high24":   float(c.get("highPrice", 0)),
            "low24":    float(c.get("lowPrice", 0)),
            "count":    int(c.get("count", 0)),
        }
        for c in r.json()
        if c["symbol"].endswith("USDT")
    }

def get_klines(symbol, interval="1h", limit=50):
    r = safe_get("https://fapi.binance.com/fapi/v1/klines",
                 params={"symbol": symbol, "interval": interval, "limit": limit})
    if not r: return []
    return [{
        "open":     float(k[1]),
        "high":     float(k[2]),
        "low":      float(k[3]),
        "close":    float(k[4]),
        "volume":   float(k[5]),
        "vol_usdt": float(k[7]),
    } for k in r.json()]

def get_oi_history(symbol):
    r = safe_get("https://fapi.binance.com/futures/data/openInterestHist",
                 params={"symbol": symbol, "period": "1h", "limit": 5})
    if not r: return None
    data = r.json()
    if len(data) < 2: return None
    old = float(data[0]["sumOpenInterest"])
    new = float(data[-1]["sumOpenInterest"])
    return round((new - old) / old * 100, 2) if old > 0 else None

def get_funding_rate(symbol):
    r = safe_get("https://fapi.binance.com/fapi/v1/fundingRate",
                 params={"symbol": symbol, "limit": 1})
    if not r: return None
    data = r.json()
    return round(float(data[-1]["fundingRate"]) * 100, 4) if data else None

# ══════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - (100 / (1 + ag/al)), 1) if al > 0 else 100.0

def find_support_resistance(klines):
    """
    Find key support levels from price structure.
    Support = recent lows that held multiple times.
    These are the REAL entry zones.
    """
    if len(klines) < 10: return [], []
    
    lows  = [k["low"]  for k in klines]
    highs = [k["high"] for k in klines]
    
    support_levels = []
    resistance_levels = []
    
    # Find swing lows (potential support)
    for i in range(2, len(lows)-2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support_levels.append(lows[i])
    
    # Find swing highs (potential resistance)
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance_levels.append(highs[i])
    
    return sorted(support_levels, reverse=True), sorted(resistance_levels)

def calc_fibonacci_levels(klines, lookback=20):
    """
    Fibonacci retracement of the most recent swing move.
    Price commonly retraces to 0.382 or 0.618 before continuing.
    These are high-probability entry zones.
    """
    if len(klines) < lookback: return {}
    
    recent = klines[-lookback:]
    swing_high = max(k["high"] for k in recent)
    swing_low  = min(k["low"]  for k in recent)
    diff = swing_high - swing_low
    
    if diff == 0: return {}
    
    return {
        "swing_high": swing_high,
        "swing_low":  swing_low,
        "fib_0":      swing_high,              # 0% (top)
        "fib_236":    swing_high - diff*0.236, # 23.6%
        "fib_382":    swing_high - diff*0.382, # 38.2% — common bounce
        "fib_500":    swing_high - diff*0.500, # 50%   — strong magnet
        "fib_618":    swing_high - diff*0.618, # 61.8% — golden ratio
        "fib_100":    swing_low,               # 100% (bottom)
    }

def find_order_block(klines):
    """
    SMC Order Block: Last bearish candle before a strong upward move.
    These zones are where banks/institutions placed buy orders.
    Price often returns to OB zone for a high-probability long entry.
    """
    if len(klines) < 5: return None, None
    
    for i in range(len(klines)-4, max(0, len(klines)-15), -1):
        candle = klines[i]
        is_bearish = candle["close"] < candle["open"]
        
        # Check if followed by strong up move
        subsequent = klines[i+1:]
        if not subsequent: continue
        
        max_close = max(k["close"] for k in subsequent)
        move_pct = (max_close - candle["close"]) / candle["close"] * 100
        
        if is_bearish and move_pct >= 3:
            # Order block zone = body of the bearish candle
            ob_high = candle["open"]   # top of bearish body
            ob_low  = candle["close"]  # bottom of bearish body
            
            current_price = klines[-1]["close"]
            # Only valid if price is above OB (OB is below current price)
            if ob_high < current_price * 0.98:
                return ob_low, ob_high
    
    return None, None

def find_fair_value_gap(klines):
    """
    SMC Fair Value Gap (FVG): Imbalance between candles.
    Smart money fills these gaps. Unfilled FVG above = target.
    Unfilled FVG below = support/entry zone.
    """
    if len(klines) < 3: return None, None, None, None
    
    bullish_fvg_low = bullish_fvg_high = None
    bearish_fvg_low = bearish_fvg_high = None
    
    for i in range(1, len(klines)-1):
        c1 = klines[i-1]
        c3 = klines[i+1]
        
        # Bullish FVG: gap between c1 high and c3 low (target above)
        if c3["low"] > c1["high"] and not bullish_fvg_high:
            bullish_fvg_low  = c1["high"]
            bullish_fvg_high = c3["low"]
        
        # Bearish FVG: gap between c1 low and c3 high (support below)
        if c3["high"] < c1["low"] and not bearish_fvg_low:
            bearish_fvg_low  = c3["high"]
            bearish_fvg_high = c1["low"]
    
    return bullish_fvg_low, bullish_fvg_high, bearish_fvg_low, bearish_fvg_high

def detect_market_structure(klines):
    """
    Determine if market is in uptrend, downtrend or ranging.
    Only trade longs in uptrend or at major reversal points.
    """
    if len(klines) < 10: return "unknown"
    
    closes = [k["close"] for k in klines[-10:]]
    highs  = [k["high"]  for k in klines[-10:]]
    lows   = [k["low"]   for k in klines[-10:]]
    
    # Higher highs and higher lows = uptrend
    hh = highs[-1] > highs[-5] > highs[-10]
    hl = lows[-1]  > lows[-5]  > lows[-10]
    
    # Lower highs and lower lows = downtrend
    lh = highs[-1] < highs[-5] < highs[-10]
    ll = lows[-1]  < lows[-5]  < lows[-10]
    
    if hh and hl: return "uptrend"
    if lh and ll: return "downtrend"
    return "ranging"

def detect_volume_accumulation(klines):
    """Volume rising while price is relatively flat = smart money accumulating"""
    if len(klines) < 9: return 1.0, False
    recent_vols   = [k["vol_usdt"] for k in klines[-3:]]
    previous_vols = [k["vol_usdt"] for k in klines[-9:-3]]
    recent_gains  = [abs(k["close"]-k["open"])/k["open"]*100 for k in klines[-3:]]
    avg_r = sum(recent_vols)/len(recent_vols) if recent_vols else 0
    avg_p = sum(previous_vols)/len(previous_vols) if previous_vols else 1
    avg_g = sum(recent_gains)/len(recent_gains) if recent_gains else 0
    spike = avg_r/avg_p if avg_p > 0 else 1
    return round(spike, 2), spike >= 2.0 and avg_g < 10

def detect_breakout(klines):
    """Price breaking above recent consolidation with volume"""
    if len(klines) < 12: return False, 0
    consol = klines[-12:-2]
    recent = klines[-2:]
    highs = [k["high"] for k in consol]
    lows  = [k["low"]  for k in consol]
    range_pct = (max(highs)-min(lows))/min(lows)*100 if min(lows) > 0 else 100
    was_tight = range_pct < 10
    broke_above = recent[-1]["close"] > max(highs)*1.005
    return was_tight and broke_above, round(recent[-1]["close"]/max(highs)*100-100, 2)

# ══════════════════════════════════════════════════════════
# SMART ENTRY CALCULATOR
# ══════════════════════════════════════════════════════════

def calculate_smart_entry(klines, current_price, rsi, fib_levels, supports, ob_low, ob_high, gain):
    """
    Calculate entry based on REAL price structure, not fixed percentages.
    Priority: Order Block > Fibonacci > Support Level > Minor dip
    """
    entry_zone = None
    entry_reason = ""
    
    # ── PRIORITY 1: Order Block (SMC) ──
    # Only use OB if it's within 8% below current price
    if ob_high and ob_low:
        dist = (current_price - ob_high) / current_price * 100
        if 1 <= dist <= 8:
            entry_zone = ob_high * 1.005  # slightly above OB high for confirmation
            entry_reason = f"📍 Order Block entry ({dist:.1f}% below)"

    # ── PRIORITY 2: Fibonacci Retracement ──
    if not entry_zone and fib_levels:
        fib382 = fib_levels.get("fib_382")
        fib500 = fib_levels.get("fib_500")
        
        # Use fib 38.2% if it's within 10% below current price
        if fib382:
            dist382 = (current_price - fib382) / current_price * 100
            if 1 <= dist382 <= 10:
                entry_zone = fib382
                entry_reason = f"📐 Fib 38.2% retracement ({dist382:.1f}% below)"
        
        # Use fib 50% if 38.2% is too far
        if not entry_zone and fib500:
            dist500 = (current_price - fib500) / current_price * 100
            if 1 <= dist500 <= 12:
                entry_zone = fib500
                entry_reason = f"📐 Fib 50% retracement ({dist500:.1f}% below)"

    # ── PRIORITY 3: Nearest Support Level ──
    if not entry_zone and supports:
        for sup in supports[:3]:  # top 3 nearest support levels
            dist = (current_price - sup) / current_price * 100
            if 1 <= dist <= 8:
                entry_zone = sup * 1.002  # slightly above support
                entry_reason = f"🔒 Key support level ({dist:.1f}% below)"
                break

    # ── PRIORITY 4: RSI-based minor dip ──
    # Only a small dip (1-4%) based on RSI — realistic
    if not entry_zone:
        if rsi and rsi <= 45:
            dip = 0.01  # RSI early — enter very close (1% below)
        elif rsi and rsi <= 55:
            dip = 0.02  # 2% below
        elif rsi and rsi <= 65:
            dip = 0.03  # 3% below
        else:
            dip = 0.04  # 4% max if RSI high
        entry_zone = current_price * (1 - dip)
        entry_reason = f"📊 RSI-based entry ({dip*100:.0f}% below current)"

    return entry_zone, entry_reason

def calculate_targets(entry, klines, fib_levels, resistances, current_price):
    """
    Calculate TP levels based on:
    1. Next resistance levels
    2. Fibonacci extension targets
    3. Risk/reward minimum 1:2
    """
    tp1 = tp2 = tp3 = None
    
    # ── From resistance levels ──
    res_above = [r for r in resistances if r > current_price * 1.02]
    if len(res_above) >= 1:
        tp1 = res_above[0]
    if len(res_above) >= 2:
        tp2 = res_above[1]
    if len(res_above) >= 3:
        tp3 = res_above[2]
    
    # ── From Fibonacci extensions ──
    if fib_levels:
        swing_low  = fib_levels.get("fib_100", entry)
        swing_high = fib_levels.get("fib_0", entry)
        diff = swing_high - swing_low
        
        ext_1618 = swing_high + diff * 0.618  # 1.618 extension
        ext_200  = swing_high + diff * 1.0    # 2.0 extension
        ext_2618 = swing_high + diff * 1.618  # 2.618 extension
        
        if not tp1 or ext_1618 < tp1:
            tp1 = ext_1618
        if not tp2 or ext_200 < tp2:
            tp2 = ext_200
        if not tp3:
            tp3 = ext_2618

    # ── Minimum R:R fallback ──
    stop = entry * 0.88  # 12% stop
    risk = entry - stop
    
    if not tp1: tp1 = entry + risk * 1.5   # min 1:1.5
    if not tp2: tp2 = entry + risk * 2.5   # min 1:2.5
    if not tp3: tp3 = entry + risk * 4.0   # min 1:4

    # Ensure TPs are above entry
    tp1 = max(tp1, entry * 1.06)
    tp2 = max(tp2, entry * 1.15)
    tp3 = max(tp3, entry * 1.25)

    stop_loss = entry * 0.88
    rr = round((tp1 - entry) / (entry - stop_loss), 1) if entry > stop_loss else 0

    return tp1, tp2, tp3, stop_loss, rr

# ══════════════════════════════════════════════════════════
# SCORING ENGINE
# ══════════════════════════════════════════════════════════

def compute_score(gain, vol, rsi, oi_chg, funding, vol_spike,
                  is_accum, is_breakout, structure, ob_found):
    score = 0
    reasons = []

    # Market structure — only trade in right direction
    if structure == "uptrend":
        score += 15
        reasons.append("📈 Market structure: Uptrend")
    elif structure == "downtrend":
        score -= 20
        reasons.append("📉 Downtrend — caution")
    else:
        score += 5
        reasons.append("↔️ Ranging market")

    # Gain — prefer early moves
    if 2 <= gain <= 12:
        score += 25
        reasons.append(f"✅ Early move +{gain}% (best entry zone)")
    elif 12 < gain <= 25:
        score += 15
        reasons.append(f"⚡ Mid move +{gain}%")
    elif 25 < gain <= 50:
        score += 8
        reasons.append(f"⚠️ +{gain}% — getting late")
    elif gain > 50:
        score += 2
        reasons.append(f"🔴 +{gain}% — very late, high risk")

    # Volume
    if vol >= 50e6:
        score += 15
        reasons.append(f"💎 Strong volume ${vol/1e6:.0f}M")
    elif vol >= 10e6:
        score += 10
        reasons.append(f"✅ Good volume ${vol/1e6:.0f}M")
    elif vol >= 2e6:
        score += 5
    else:
        score -= 10
        reasons.append("⚠️ Low volume — risky")

    # RSI
    if rsi:
        if 30 <= rsi <= 50:
            score += 20
            reasons.append(f"✨ RSI {rsi} — early entry zone (best)")
        elif 50 < rsi <= 60:
            score += 12
            reasons.append(f"📈 RSI {rsi} — momentum building")
        elif 60 < rsi <= 70:
            score += 5
            reasons.append(f"🟡 RSI {rsi} — getting hot")
        elif rsi > 70:
            score -= 15
            reasons.append(f"🔴 RSI {rsi} — overbought, wait for dip")
        elif rsi < 30:
            score += 10
            reasons.append(f"🟣 RSI {rsi} — oversold bounce possible")

    # Volume accumulation
    if is_accum and vol_spike >= 3:
        score += 20
        reasons.append(f"🔥 Volume accumulation {vol_spike}x (smart money)")
    elif is_accum:
        score += 12
        reasons.append(f"📦 Volume building {vol_spike}x")

    # Consolidation breakout
    if is_breakout:
        score += 15
        reasons.append("💥 Consolidation breakout")

    # Order block
    if ob_found:
        score += 10
        reasons.append("📍 Order Block identified (SMC)")

    # Open Interest
    if oi_chg is not None:
        if oi_chg >= 10:
            score += 15
            reasons.append(f"💰 OI +{oi_chg}% (big new positions)")
        elif oi_chg >= 5:
            score += 8
            reasons.append(f"📊 OI +{oi_chg}%")
        elif oi_chg < -5:
            score -= 8
            reasons.append(f"⚠️ OI falling {oi_chg}%")

    # Funding rate
    if funding is not None:
        if 0.001 <= funding <= 0.05:
            score += 8
            reasons.append(f"✅ Funding {funding}% (healthy)")
        elif funding > 0.1:
            score -= 12
            reasons.append(f"🔴 Funding {funding}% too high (squeeze risk)")
        elif funding < -0.005:
            score += 10
            reasons.append(f"🔄 Negative funding {funding}% (short squeeze possible)")

    return min(max(score, 0), 100), reasons

# ══════════════════════════════════════════════════════════
# MAIN SCREENER
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
    return f"${v/1e3:.0f}K"

def run_screener(min_gain=2, max_gain=60, min_vol=2e6, min_score=55):
    log.info("Running SMC screener v4...")
    tickers = get_futures_tickers()
    if not tickers:
        return []

    results = []
    processed = 0

    for sym, t in tickers.items():
        gain = t["gain"]
        vol  = t["vol_usdt"]
        price = t["price"]

        if not (min_gain <= gain <= max_gain): continue
        if vol < min_vol: continue
        if price <= 0: continue

        # Get candle data
        klines_1h  = get_klines(sym, "1h", 50)
        klines_4h  = get_klines(sym, "4h", 30)
        time.sleep(0.1)

        if not klines_1h: continue

        closes = [k["close"] for k in klines_1h]
        rsi = calc_rsi(closes)

        # Technical analysis
        structure         = detect_market_structure(klines_1h)
        vol_spike, is_acc = detect_volume_accumulation(klines_1h)
        is_brkout, bo_str = detect_breakout(klines_1h)
        ob_low, ob_high   = find_order_block(klines_1h)
        fib               = calc_fibonacci_levels(klines_4h if klines_4h else klines_1h)
        supports, resists = find_support_resistance(klines_1h)
        bfvg_l, bfvg_h, _, _ = find_fair_value_gap(klines_1h)

        # Smart money data
        oi_chg  = get_oi_history(sym)
        funding = get_funding_rate(sym)
        time.sleep(0.1)

        # Score
        score, reasons = compute_score(
            gain, vol, rsi, oi_chg, funding,
            vol_spike, is_acc, is_brkout, structure,
            ob_low is not None
        )

        if score < min_score: continue

        # Smart entry calculation
        entry, entry_reason = calculate_smart_entry(
            klines_1h, price, rsi, fib, supports, ob_low, ob_high, gain
        )

        # Target calculation
        tp1, tp2, tp3, stop, rr = calculate_targets(
            entry, klines_1h, fib, resists, price
        )

        # Skip if entry is unrealistically far from price
        entry_dist = (price - entry) / price * 100
        if entry_dist > 15:
            # Force entry closer to price
            entry = price * 0.97
            entry_dist = 3.0
            entry_reason = "📊 Near-market entry (3% dip)"
            tp1 = entry * 1.10
            tp2 = entry * 1.20
            tp3 = entry * 1.35
            stop = entry * 0.88
            rr = round((tp1 - entry) / (entry - stop), 1)

        signal = (
            "🟢 STRONG BUY" if score >= 75 and structure != "downtrend"
            else "🟡 WATCH" if score >= 60
            else "👀 ON RADAR"
        )

        results.append({
            "sym":          sym.replace("USDT",""),
            "gain":         gain,
            "vol":          vol,
            "price":        price,
            "score":        score,
            "signal":       signal,
            "rsi":          rsi,
            "structure":    structure,
            "oi_chg":       oi_chg,
            "funding":      funding,
            "vol_spike":    vol_spike,
            "is_acc":       is_acc,
            "is_brkout":    is_brkout,
            "ob_low":       ob_low,
            "ob_high":      ob_high,
            "fib_382":      fib.get("fib_382") if fib else None,
            "fib_500":      fib.get("fib_500") if fib else None,
            "fvg_target":   bfvg_h,
            "entry":        entry,
            "entry_reason": entry_reason,
            "entry_dist":   round(entry_dist, 1),
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "stop": stop, "rr": rr,
            "reasons":      reasons,
        })

        processed += 1
        if processed >= 100: break

    return sorted(results, key=lambda x: x["score"], reverse=True)

# ══════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ══════════════════════════════════════════════════════════

def build_messages(coins, mode="auto"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    label = "⏰ AUTO SCAN" if mode == "auto" else "🔍 MANUAL SCAN"

    if not coins:
        return [
            f"**{label}** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No quality signals right now.\n"
            f"Waiting for smart money to move..."
        ]

    msgs = [
        f"**{label}** | 🕐 {now}\n"
        f"✅ **{len(coins)} signal(s)** | Min score 55/100\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, c in enumerate(coins[:6], 1):
        rsi = c.get("rsi")
        if rsi:
            if rsi <= 35:   rsi_str = f"RSI {rsi} 🟣 Oversold"
            elif rsi <= 50: rsi_str = f"RSI {rsi} ✨ Early entry"
            elif rsi <= 65: rsi_str = f"RSI {rsi} 🟡 Building"
            else:           rsi_str = f"RSI {rsi} 🔴 Overbought"
        else:
            rsi_str = "RSI N/A"

        sm = []
        if c.get("oi_chg") is not None:
            sm.append(f"OI {'+' if c['oi_chg']>=0 else ''}{c['oi_chg']}%")
        if c.get("funding") is not None:
            sm.append(f"Fund {c['funding']}%")
        sm_line = " | ".join(sm)

        flags = []
        if c.get("is_acc"):   flags.append(f"📦 Accumulation {c['vol_spike']}x vol")
        if c.get("is_brkout"):flags.append("💥 Breakout")
        if c.get("ob_low"):   flags.append(f"📍 OB: {fmt_price(c['ob_low'])}–{fmt_price(c['ob_high'])}")
        if c.get("fvg_target"):flags.append(f"⬜ FVG target: {fmt_price(c['fvg_target'])}")

        rr_str = f"⚖️ R/R 1:{c['rr']}" if c.get("rr") else ""

        msg = (
            f"\n**{i}. {c['sym']}/USDT** | {c['signal']}\n"
            f"Score: **{c['score']}/100** | 📈 +{c['gain']}% | "
            f"Vol: {fmt_vol(c['vol'])} | Struct: {c['structure']}\n"
            f"💰 Current: `{fmt_price(c['price'])}`\n"
            f"{rsi_str}"
            + (f" | {sm_line}" if sm_line else "") + "\n"
            + ("\n".join(flags) + "\n" if flags else "")
            + f"\n**📊 {c['entry_reason']}**\n"
            f"🔵 Entry:  `{fmt_price(c['entry'])}` ({c['entry_dist']}% below)\n"
            f"🎯 TP1:   `{fmt_price(c['tp1'])}`  (+{round((c['tp1']-c['entry'])/c['entry']*100,1)}%)\n"
            f"🚀 TP2:   `{fmt_price(c['tp2'])}`  (+{round((c['tp2']-c['entry'])/c['entry']*100,1)}%)\n"
            f"💎 TP3:   `{fmt_price(c['tp3'])}`  (+{round((c['tp3']-c['entry'])/c['entry']*100,1)}%)\n"
            f"⛔ Stop:  `{fmt_price(c['stop'])}` (-12%)\n"
            + (rr_str + "\n" if rr_str else "")
            + "━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        msgs.append(msg)

    msgs.append("\n⚠️ *Not financial advice. Use stop losses. DYOR.*")
    return msgs

# ══════════════════════════════════════════════════════════
# DISCORD BOT
# ══════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

import asyncio

async def send_messages(target, msgs):
    for msg in msgs:
        chunks = [msg[i:i+1900] for i in range(0, len(msg), 1900)]
        for chunk in chunks:
            await target.send(chunk)
            await asyncio.sleep(0.3)

@bot.command(name="scan")
async def scan(ctx):
    await ctx.send("🔍 **Running SMC scan...** Analyzing structure, OI, funding & Fibonacci. ~60 secs...")
    coins = run_screener(min_gain=2, max_gain=60, min_score=55)
    await send_messages(ctx, build_messages(coins, "manual"))

@bot.command(name="early")
async def early(ctx):
    """Very early signals — tiny moves with big volume"""
    await ctx.send("🌅 **Early warning scan...** Looking for accumulation before pump. ~60 secs...")
    coins = run_screener(min_gain=1, max_gain=15, min_vol=1e6, min_score=50)
    await send_messages(ctx, build_messages(coins, "manual"))

@bot.command(name="help2")
async def help2(ctx):
    await ctx.send(
        "**📊 AKA Smart Money Screener v4.0**\n\n"
        "`!scan`  — Full SMC scan (2–60% gainers)\n"
        "`!early` — Very early signals (1–15% with accumulation)\n"
        "`!help2` — This message\n\n"
        "**Entry is based on:**\n"
        "1. Order Blocks (SMC)\n"
        "2. Fibonacci retracement (38.2%, 50%)\n"
        "3. Key support levels\n"
        "4. RSI-adjusted minor dip (max 4%)\n\n"
        "**Auto-scan every 2 hours** 🕐"
    )


@bot.command(name="momentum")
async def momentum(ctx):
    """Quick trade scan — 5-20% targets in hours"""
    await ctx.send("⚡ **Quick trade scan...** Finding momentum setups on 15m chart. ~45 secs...")
    coins = run_quick_screener()
    await send_messages(ctx, build_quick_messages(coins))


async def auto_quick_scan():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    try:
        coins = run_quick_screener()
        # Only auto-send if there are HIGH QUALITY signals (score >= 65)
        top_coins = [c for c in coins if c["score"] >= 65]
        if top_coins:
            await channel.send("⚡ **Auto Quick Trade Alert**")
            await send_messages(channel, build_quick_messages(top_coins))
        else:
            log.info("Quick scan: no high quality signals, skipping auto-send")
    except Exception as e:
        log.error(f"Auto quick scan error: {e}")

async def auto_scan():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        log.error("Channel not found!")
        return
    try:
        await channel.send("⏰ **Auto SMC scan starting...**")
        coins = run_screener(min_gain=2, max_gain=60, min_score=55)
        await send_messages(channel, build_messages(coins, "auto"))
    except Exception as e:
        log.error(f"Auto scan error: {e}")

def scheduler():
    def job():
        future = asyncio.run_coroutine_threadsafe(auto_scan(), bot.loop)
        try:
            future.result(timeout=300)
        except Exception as e:
            log.error(f"Scheduler error: {e}")

    def quick_job():
        future = asyncio.run_coroutine_threadsafe(auto_quick_scan(), bot.loop)
        try:
            future.result(timeout=180)
        except Exception as e:
            log.error(f"Quick scan error: {e}")

    # SMC scan every 4 hours
    schedule.every(4).hours.do(job)
    # Quick trade scan every 2 hours (offset by 2hrs)
    schedule.every(2).hours.do(quick_job)

    while True:
        schedule.run_pending()
        time.sleep(60)

@bot.event
async def on_ready():
    print(f"✅ AKA SMC Screener v4.0 online as {bot.user}!")
    print(f"📡 Binance Futures | SMC | OI | Funding | Fibonacci | Structure")
    print(f"⏰ Auto-scan every {SCAN_HOURS} hours | First scan in 5 mins")
    t = threading.Thread(target=scheduler, daemon=True)
    t.start()

if not TOKEN:
    print("ERROR: No DISCORD_TOKEN set!")
else:
    bot.run(TOKEN)

# ══════════════════════════════════════════════════════════
# QUICK TRADE / SCALP SCREENER
# ══════════════════════════════════════════════════════════

def get_klines_15m(symbol, limit=20):
    r = safe_get("https://fapi.binance.com/fapi/v1/klines",
                 params={"symbol": symbol, "interval": "15m", "limit": limit})
    if not r: return []
    return [{
        "open":     float(k[1]),
        "high":     float(k[2]),
        "low":      float(k[3]),
        "close":    float(k[4]),
        "volume":   float(k[5]),
        "vol_usdt": float(k[7]),
    } for k in r.json()]

def detect_momentum_candle(klines_15m):
    """
    Strong green candle in last 15 mins with high volume
    = momentum just started, get in fast
    """
    if len(klines_15m) < 3: return False, 0
    last = klines_15m[-1]
    prev = klines_15m[-2]
    
    body = (last["close"] - last["open"]) / last["open"] * 100
    vol_ratio = last["vol_usdt"] / prev["vol_usdt"] if prev["vol_usdt"] > 0 else 1
    
    is_strong_green = body >= 1.5 and last["close"] > last["open"]
    is_vol_spike    = vol_ratio >= 2.0
    
    return is_strong_green and is_vol_spike, round(body, 2)

def detect_15m_breakout(klines_15m):
    """Price just broke above last 10 candles high on 15m"""
    if len(klines_15m) < 12: return False
    prev_high = max(k["high"] for k in klines_15m[-12:-2])
    last_close = klines_15m[-1]["close"]
    return last_close > prev_high * 1.005

def run_quick_screener():
    """
    Quick trade screener — finds coins with:
    - Strong 15m momentum candle
    - Volume spike in last 15 mins
    - Already moving but NOT overbought
    - Entry within 1-2% of current price
    Target: 5-20% in hours, not days
    """
    log.info("Running quick trade screener...")
    tickers = get_futures_tickers()
    if not tickers: return []

    results = []
    processed = 0

    for sym, t in tickers.items():
        gain  = t["gain"]
        vol   = t["vol_usdt"]
        price = t["price"]

        # Quick trades: moving 3-35%, decent volume
        if not (3 <= gain <= 35): continue
        if vol < 3e6: continue
        if price <= 0: continue

        # Get 15m and 1h candles
        klines_15m = get_klines_15m(sym, 20)
        klines_1h  = get_klines(sym, "1h", 20)
        time.sleep(0.08)

        if not klines_15m or not klines_1h: continue

        # RSI on 1h
        closes_1h = [k["close"] for k in klines_1h]
        rsi = calc_rsi(closes_1h)

        # Skip overbought — too late for quick trade
        if rsi and rsi > 75: continue

        # Momentum signals
        mom_candle, body_pct = detect_momentum_candle(klines_15m)
        brkout_15m = detect_15m_breakout(klines_15m)

        # Need at least one strong signal
        if not mom_candle and not brkout_15m: continue

        # Volume trend on 15m
        recent_vols = [k["vol_usdt"] for k in klines_15m[-3:]]
        prev_vols   = [k["vol_usdt"] for k in klines_15m[-8:-3]]
        vol_trend   = sum(recent_vols)/len(recent_vols) / (sum(prev_vols)/len(prev_vols)) if prev_vols and sum(prev_vols) > 0 else 1

        # Score quick trade
        score = 0
        if mom_candle:  score += 35
        if brkout_15m:  score += 25
        if vol_trend >= 3: score += 20
        elif vol_trend >= 2: score += 12
        if rsi and rsi <= 55: score += 15
        elif rsi and rsi <= 65: score += 8
        if vol >= 20e6: score += 10
        elif vol >= 5e6: score += 5
        if 5 <= gain <= 20: score += 10  # sweet spot

        if score < 50: continue

        # Quick trade entry — RIGHT NOW or 1% dip max
        entry = price * 0.99  # 1% below = realistic quick entry

        # Quick targets based on momentum
        if score >= 75:
            tp1 = entry * 1.05   # 5%
            tp2 = entry * 1.10   # 10%
            tp3 = entry * 1.20   # 20%
        elif score >= 60:
            tp1 = entry * 1.04   # 4%
            tp2 = entry * 1.08   # 8%
            tp3 = entry * 1.15   # 15%
        else:
            tp1 = entry * 1.03   # 3%
            tp2 = entry * 1.06   # 6%
            tp3 = entry * 1.10   # 10%

        stop = entry * 0.95  # tight 5% stop for quick trades

        rr = round((tp1 - entry) / (entry - stop), 1)

        results.append({
            "sym":       sym.replace("USDT",""),
            "gain":      gain,
            "vol":       vol,
            "price":     price,
            "score":     score,
            "rsi":       rsi,
            "vol_trend": round(vol_trend, 1),
            "body_pct":  body_pct,
            "mom_candle":mom_candle,
            "brkout":    brkout_15m,
            "entry":     entry,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "stop": stop, "rr": rr,
        })

        processed += 1
        if processed >= 60: break

    return sorted(results, key=lambda x: x["score"], reverse=True)

def build_quick_messages(coins):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not coins:
        return [
            f"⚡ **QUICK TRADE SCAN** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No quick setups right now.\n"
            f"Market needs more momentum. Try again in 30 mins."
        ]

    msgs = [
        f"⚡ **QUICK TRADE SCAN** | 🕐 {now}\n"
        f"🎯 Target: **5–20% in hours** | Tight 5% stop\n"
        f"✅ **{len(coins)} setup(s) found**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, c in enumerate(coins[:5], 1):
        rsi = c.get("rsi")
        rsi_str = f"RSI {rsi}" if rsi else ""

        flags = []
        if c.get("mom_candle"): flags.append(f"🕯️ Strong 15m candle +{c['body_pct']}%")
        if c.get("brkout"):     flags.append("💥 15m breakout")
        if c.get("vol_trend", 1) >= 2: flags.append(f"📈 Vol trend {c['vol_trend']}x")

        msg = (
            f"\n**{i}. {c['sym']}/USDT** ⚡ QUICK TRADE\n"
            f"Score: **{c['score']}/100** | 📈 +{c['gain']}% 24h\n"
            f"Vol: {fmt_vol(c['vol'])} | {rsi_str}\n"
            + ("\n".join(flags) + "\n" if flags else "")
            + f"\n💰 Current: `{fmt_price(c['price'])}`\n"
            f"🔵 Entry:  `{fmt_price(c['entry'])}` **(enter now or on 1% dip)**\n"
            f"🎯 TP1:   `{fmt_price(c['tp1'])}` **(+{round((c['tp1']-c['entry'])/c['entry']*100,0):.0f}% — take 40%)**\n"
            f"🚀 TP2:   `{fmt_price(c['tp2'])}` **(+{round((c['tp2']-c['entry'])/c['entry']*100,0):.0f}% — take 40%)**\n"
            f"💎 TP3:   `{fmt_price(c['tp3'])}` **(+{round((c['tp3']-c['entry'])/c['entry']*100,0):.0f}% — take 20%)**\n"
            f"⛔ Stop:  `{fmt_price(c['stop'])}` **(-5% — exit fast if hit)**\n"
            f"⚖️ R/R:   1:{c['rr']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        msgs.append(msg)

    msgs.append(
        "\n⚡ *Quick trades: move fast, take profit fast.*\n"
        "⚠️ *Always set stop loss. Not financial advice.*"
    )
    return msgs


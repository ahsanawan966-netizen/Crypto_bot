"""
╔══════════════════════════════════════════════════════════╗
║     AKA SMART MONEY CRYPTO SCREENER BOT v3.0            ║
║     Built for Ahsan | Aka Trading Signals                ║
║     Strategy: Early Entry + Smart Money Concepts         ║
╚══════════════════════════════════════════════════════════╝

EARLY WARNING SIGNALS:
- Volume accumulation BEFORE price moves
- Funding rate shifts (smart money positioning)
- Open interest spikes (new money entering)
- Small price move + huge volume = accumulation
- Breakout from consolidation zones
- Liquidity sweep detection
"""

import os, time, logging, requests, threading, schedule
from datetime import datetime
import discord
from discord.ext import commands

TOKEN      = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))
SCAN_HOURS = 2   # scan every 2 hours for early signals

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# DATA FETCHERS
# ══════════════════════════════════════════════════════════

def get_futures_tickers():
    """All Binance Futures USDT pairs with 24h data"""
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15)
        r.raise_for_status()
        return {
            c["symbol"]: {
                "symbol":   c["symbol"],
                "gain":     float(c.get("priceChangePercent", 0)),
                "price":    float(c.get("lastPrice", 0)),
                "vol_usdt": float(c.get("quoteVolume", 0)),
                "high":     float(c.get("highPrice", 0)),
                "low":      float(c.get("lowPrice", 0)),
                "count":    int(c.get("count", 0)),  # number of trades
            }
            for c in r.json()
            if c["symbol"].endswith("USDT")
        }
    except Exception as e:
        log.error(f"Futures ticker error: {e}")
        return {}

def get_open_interest(symbol):
    """Open Interest — new money entering the market"""
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                         params={"symbol": symbol}, timeout=8)
        r.raise_for_status()
        return float(r.json().get("openInterest", 0))
    except:
        return None

def get_oi_history(symbol):
    """OI change over last 4 hours — rising OI = smart money entering"""
    try:
        r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                         params={"symbol": symbol, "period": "1h", "limit": 5}, timeout=8)
        r.raise_for_status()
        data = r.json()
        if len(data) < 2:
            return None
        old_oi = float(data[0]["sumOpenInterest"])
        new_oi = float(data[-1]["sumOpenInterest"])
        if old_oi == 0:
            return None
        return round((new_oi - old_oi) / old_oi * 100, 2)
    except:
        return None

def get_funding_rate(symbol):
    """Funding rate — positive = longs paying = bullish sentiment"""
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": symbol, "limit": 3}, timeout=8)
        r.raise_for_status()
        rates = [float(x["fundingRate"]) * 100 for x in r.json()]
        return round(rates[-1], 4) if rates else None
    except:
        return None

def get_klines(symbol, interval="1h", limit=24):
    """Price candles for volume and structure analysis"""
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         timeout=8)
        r.raise_for_status()
        return [{
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
            "vol_usdt": float(k[7]),
        } for k in r.json()]
    except:
        return []

def get_long_short_ratio(symbol):
    """Long/Short ratio — above 1.5 means more longs = bullish"""
    try:
        r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                         params={"symbol": symbol, "period": "1h", "limit": 2}, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[-1].get("longShortRatio", 1.0))
        return None
    except:
        return None

# ══════════════════════════════════════════════════════════
# ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag/al)), 1)

def detect_volume_accumulation(klines):
    """
    EARLY SIGNAL: Volume rising quietly while price is flat or slightly up
    This is smart money accumulating before the big move
    """
    if len(klines) < 6:
        return None, None
    
    recent_vols  = [k["vol_usdt"] for k in klines[-3:]]
    previous_vols= [k["vol_usdt"] for k in klines[-9:-3]]
    recent_gains = [abs(k["close"] - k["open"]) / k["open"] * 100 for k in klines[-3:]]
    
    avg_recent   = sum(recent_vols) / len(recent_vols) if recent_vols else 0
    avg_previous = sum(previous_vols) / len(previous_vols) if previous_vols else 1
    avg_gain     = sum(recent_gains) / len(recent_gains) if recent_gains else 0
    
    vol_spike = avg_recent / avg_previous if avg_previous > 0 else 1
    
    # Key signal: volume 2x+ but price move is small (accumulation)
    is_accumulation = vol_spike >= 2.0 and avg_gain < 8
    
    return round(vol_spike, 2), is_accumulation

def detect_consolidation_breakout(klines):
    """
    Detect if coin was consolidating (tight range) and now breaking out
    Consolidation breakouts often lead to 20-40% moves
    """
    if len(klines) < 10:
        return False, None
    
    # Check last 6 candles for tight range (consolidation)
    consol_candles = klines[-10:-2]
    recent_candles = klines[-2:]
    
    highs = [k["high"] for k in consol_candles]
    lows  = [k["low"]  for k in consol_candles]
    
    consol_range = (max(highs) - min(lows)) / min(lows) * 100 if min(lows) > 0 else 100
    
    # Consolidation: price range less than 8% for 8 candles
    was_consolidating = consol_range < 8
    
    # Breakout: recent candles breaking above consolidation high
    breakout_price = recent_candles[-1]["close"]
    consol_high    = max(highs)
    is_breaking    = breakout_price > consol_high * 1.01  # 1% above consolidation
    
    breakout_strength = (breakout_price - consol_high) / consol_high * 100 if consol_high > 0 else 0
    
    return was_consolidating and is_breaking, round(breakout_strength, 2)

def detect_liquidity_sweep(klines):
    """
    SMC: Detect liquidity sweep — price briefly dips below support
    then reverses strongly upward. This is smart money hunting stops
    before pumping. Perfect early entry signal.
    """
    if len(klines) < 5:
        return False
    
    prev_lows = [k["low"] for k in klines[-5:-1]]
    last_candle = klines[-1]
    prev_candle = klines[-2]
    
    support = min(prev_lows)
    
    # Price dipped below support (sweep) then closed above it
    swept   = last_candle["low"] < support * 0.99
    recovered = last_candle["close"] > support
    bullish_close = last_candle["close"] > last_candle["open"]  # green candle
    
    return swept and recovered and bullish_close

def detect_order_block(klines):
    """
    SMC: Find bullish order block — last bearish candle before a 
    strong up move. Price often returns to this zone before continuing up.
    """
    if len(klines) < 5:
        return None, None
    
    for i in range(len(klines)-3, len(klines)-6, -1):
        if i < 1: break
        candle = klines[i]
        next_candles = klines[i+1:]
        
        # Bearish candle followed by strong bullish move
        is_bearish = candle["close"] < candle["open"]
        subsequent_gain = (klines[-1]["close"] - candle["close"]) / candle["close"] * 100
        
        if is_bearish and subsequent_gain > 5:
            ob_high = candle["open"]
            ob_low  = candle["close"]
            return round(ob_low, 6), round(ob_high, 6)
    
    return None, None

def detect_fair_value_gap(klines):
    """
    SMC: Fair Value Gap (FVG) — price gap between candles that 
    smart money often fills. Unfilled FVG above = target zone.
    """
    if len(klines) < 3:
        return None, None
    
    for i in range(len(klines)-3, max(len(klines)-8, 0), -1):
        if i < 2: break
        c1 = klines[i-1]
        c2 = klines[i]
        c3 = klines[i+1] if i+1 < len(klines) else None
        
        if c3 is None: continue
        
        # Bullish FVG: gap between c1 high and c3 low
        if c3["low"] > c1["high"]:
            fvg_low  = c1["high"]
            fvg_high = c3["low"]
            # Check if FVG is still unfilled
            recent_low = min(k["low"] for k in klines[i+1:])
            if recent_low > fvg_low:  # unfilled
                return round(fvg_low, 6), round(fvg_high, 6)
    
    return None, None

# ══════════════════════════════════════════════════════════
# SCORING ENGINE
# ══════════════════════════════════════════════════════════

def compute_score(data):
    score = 0
    reasons = []

    gain   = data.get("gain", 0)
    vol    = data.get("vol_usdt", 0)
    rsi    = data.get("rsi")
    oi_chg = data.get("oi_change")
    fund   = data.get("funding")
    vol_spike    = data.get("vol_spike", 1)
    is_accum     = data.get("is_accumulation", False)
    is_breakout  = data.get("is_breakout", False)
    breakout_str = data.get("breakout_strength", 0)
    liq_sweep    = data.get("liquidity_sweep", False)
    ls_ratio     = data.get("ls_ratio")
    trade_count  = data.get("count", 0)

    # ── EARLY ENTRY SIGNALS (most important) ──

    # Volume accumulation (silent buildup before pump)
    if is_accum and vol_spike >= 3:
        score += 30
        reasons.append(f"🔥 Volume accumulation {vol_spike}x (smart money buying quietly)")
    elif is_accum and vol_spike >= 2:
        score += 20
        reasons.append(f"📈 Volume building {vol_spike}x (possible accumulation)")

    # Consolidation breakout
    if is_breakout:
        score += 25
        reasons.append(f"💥 Breaking out of consolidation (+{breakout_str}%)")

    # Liquidity sweep (SMC signal)
    if liq_sweep:
        score += 20
        reasons.append("🎯 Liquidity sweep detected (SMC — smart money hunted stops, reversal likely)")

    # ── MOMENTUM SIGNALS ──

    # Gain — prefer early (3-15%) over late (40%+)
    if 3 <= gain <= 15:
        score += 20
        reasons.append(f"✅ Early move +{gain}% (not overbought yet)")
    elif 15 < gain <= 30:
        score += 12
        reasons.append(f"⚡ Mid move +{gain}%")
    elif gain > 30:
        score += 5
        reasons.append(f"⚠️ Already pumped +{gain}% (late entry risk)")
    elif 1 <= gain < 3:
        score += 15
        reasons.append(f"👀 Tiny move +{gain}% with volume — very early signal")

    # ── SMART MONEY INDICATORS ──

    # Open Interest rising = new money entering
    if oi_chg is not None:
        if oi_chg >= 10:
            score += 15
            reasons.append(f"💰 OI surged +{oi_chg}% (big new positions opening)")
        elif oi_chg >= 5:
            score += 10
            reasons.append(f"📊 OI up +{oi_chg}% (new money entering)")
        elif oi_chg < -5:
            score -= 10
            reasons.append(f"⚠️ OI dropping {oi_chg}% (positions closing)")

    # Funding rate — slightly positive is healthy
    if fund is not None:
        if 0.005 <= fund <= 0.05:
            score += 10
            reasons.append(f"✅ Funding rate healthy +{fund}% (longs slightly dominant)")
        elif fund > 0.1:
            score -= 10
            reasons.append(f"⚠️ Funding rate too high {fund}% (overleveraged longs = dump risk)")
        elif fund < -0.01:
            score += 8
            reasons.append(f"🔄 Negative funding {fund}% (shorts paying = potential squeeze)")

    # RSI — reward early entry zone
    if rsi is not None:
        if 30 <= rsi <= 45:
            score += 15
            reasons.append(f"✨ RSI {rsi} — perfect early entry zone")
        elif 45 < rsi <= 55:
            score += 10
            reasons.append(f"📈 RSI {rsi} — momentum building")
        elif 55 < rsi <= 65:
            score += 5
            reasons.append(f"🟡 RSI {rsi} — getting hot")
        elif rsi > 70:
            score -= 10
            reasons.append(f"🔴 RSI {rsi} — overbought, wait for dip")
        elif rsi < 30:
            score += 12
            reasons.append(f"🟣 RSI {rsi} — oversold, bounce possible")

    # Long/Short ratio
    if ls_ratio is not None:
        if 1.3 <= ls_ratio <= 2.5:
            score += 8
            reasons.append(f"📊 L/S ratio {ls_ratio} (healthy bullish bias)")
        elif ls_ratio > 3:
            score -= 5
            reasons.append(f"⚠️ L/S ratio {ls_ratio} (too many longs = squeeze risk)")

    # Volume absolute value
    if vol >= 50e6:
        score += 10
        reasons.append(f"💎 High liquidity ${vol/1e6:.0f}M volume")
    elif vol >= 10e6:
        score += 6
    elif vol < 500000:
        score -= 10
        reasons.append(f"⚠️ Low volume ${vol/1e3:.0f}K (risky)")

    return min(score, 100), reasons

# ══════════════════════════════════════════════════════════
# MAIN SCREENER
# ══════════════════════════════════════════════════════════

def fmt_price(p):
    if not p: return "$0"
    if p < 0.000001: return f"${p:.8f}"
    if p < 0.001:    return f"${p:.6f}"
    if p < 1:        return f"${p:.5f}"
    if p < 100:      return f"${p:.3f}"
    return f"${p:,.2f}"

def fmt_vol(v):
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

def calc_entry_levels(price, rsi, score, ob_low=None):
    """Smart entry based on RSI and order block"""
    # If order block detected, use it as entry
    if ob_low and ob_low < price:
        entry = ob_low
    else:
        # Dynamic dip based on RSI
        if rsi and rsi <= 40:   dip = 0.02
        elif rsi and rsi <= 50: dip = 0.04
        elif rsi and rsi <= 60: dip = 0.06
        elif rsi and rsi > 70:  dip = 0.15
        else:                   dip = 0.05
        entry = price * (1 - dip)

    # TP levels based on score
    if score >= 75:
        tp1 = entry * 1.15
        tp2 = entry * 1.35
        tp3 = entry * 1.60
    elif score >= 60:
        tp1 = entry * 1.10
        tp2 = entry * 1.25
        tp3 = entry * 1.45
    else:
        tp1 = entry * 1.08
        tp2 = entry * 1.18
        tp3 = entry * 1.30

    stop = entry * 0.88  # 12% stop loss

    return {
        "entry": fmt_price(entry),
        "tp1":   fmt_price(tp1),
        "tp2":   fmt_price(tp2),
        "tp3":   fmt_price(tp3),
        "stop":  fmt_price(stop),
        "rr":    f"1:{round((tp1-entry)/(entry-stop), 1)}"  # risk/reward
    }

def run_screener(mode="early"):
    """
    mode='early'  — finds coins with volume accumulation BEFORE pump
    mode='momentum' — finds coins with strong momentum (current behavior)
    """
    log.info(f"Running {mode} screener...")
    tickers = get_futures_tickers()
    if not tickers:
        return []

    results = []
    processed = 0

    for sym, t in tickers.items():
        # Filter obvious non-candidates
        vol = t["vol_usdt"]
        gain = t["gain"]

        if mode == "early":
            # Early mode: look for ANY move with volume
            if vol < 1e6: continue       # min $1M volume
            if gain < 1 or gain > 40: continue  # 1-40% gain range
        else:
            if vol < 2e6: continue
            if gain < 8: continue

        # Get detailed data
        klines_1h = get_klines(sym, "1h", 24)
        klines_15m = get_klines(sym, "15m", 20)
        time.sleep(0.08)

        if not klines_1h: continue

        closes = [k["close"] for k in klines_1h]
        rsi = calc_rsi(closes)

        vol_spike, is_accum = detect_volume_accumulation(klines_1h)
        is_breakout, bo_str = detect_consolidation_breakout(klines_1h)
        liq_sweep = detect_liquidity_sweep(klines_15m) if klines_15m else False
        ob_low, ob_high = detect_order_block(klines_1h)
        fvg_low, fvg_high = detect_fair_value_gap(klines_1h)

        # Get smart money data
        oi_chg = get_oi_history(sym)
        funding = get_funding_rate(sym)
        ls_ratio = get_long_short_ratio(sym)
        time.sleep(0.08)

        data = {
            "gain": gain,
            "vol_usdt": vol,
            "rsi": rsi,
            "oi_change": oi_chg,
            "funding": funding,
            "vol_spike": vol_spike,
            "is_accumulation": is_accum,
            "is_breakout": is_breakout,
            "breakout_strength": bo_str,
            "liquidity_sweep": liq_sweep,
            "ls_ratio": ls_ratio,
            "count": t["count"],
        }

        score, reasons = compute_score(data)

        if score < 50: continue

        levels = calc_entry_levels(t["price"], rsi, score, ob_low)

        signal = "🟢 STRONG BUY" if score >= 75 else "🟡 WATCH CLOSELY" if score >= 60 else "👀 ON RADAR"

        results.append({
            "sym":      sym.replace("USDT", ""),
            "gain":     gain,
            "vol":      vol,
            "price":    fmt_price(t["price"]),
            "score":    score,
            "signal":   signal,
            "rsi":      rsi,
            "oi_chg":   oi_chg,
            "funding":  funding,
            "ls_ratio": ls_ratio,
            "vol_spike":vol_spike,
            "is_accum": is_accum,
            "is_brkout":is_breakout,
            "liq_sweep":liq_sweep,
            "ob_low":   fmt_price(ob_low) if ob_low else None,
            "ob_high":  fmt_price(ob_high) if ob_high else None,
            "fvg_low":  fmt_price(fvg_low) if fvg_low else None,
            "fvg_high": fmt_price(fvg_high) if fvg_high else None,
            "levels":   levels,
            "reasons":  reasons,
        })

        processed += 1
        if processed >= 80: break  # analyze top 80 candidates

    return sorted(results, key=lambda x: x["score"], reverse=True)

# ══════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ══════════════════════════════════════════════════════════

def build_message(coins, mode="early"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    header = "🔍 EARLY WARNING" if mode == "early" else "📊 MOMENTUM"

    if not coins:
        return (
            f"**{header} SCAN** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No signals detected right now.\n"
            f"Market is quiet — smart money not moving yet."
        )

    messages = []
    header_msg = (
        f"**{header} SCAN** | 🕐 {now}\n"
        f"✅ **{len(coins)} signal(s) found**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    messages.append(header_msg)

    for i, c in enumerate(coins[:6], 1):
        lv = c["levels"]

        # Build SMC notes
        smc_notes = []
        if c.get("is_accum"):
            smc_notes.append(f"📦 Accumulation detected ({c['vol_spike']}x vol)")
        if c.get("is_brkout"):
            smc_notes.append(f"💥 Consolidation breakout")
        if c.get("liq_sweep"):
            smc_notes.append(f"🎯 Liquidity sweep (SMC reversal)")
        if c.get("ob_low"):
            smc_notes.append(f"📍 Order Block: {c['ob_low']} – {c['ob_high']}")
        if c.get("fvg_low"):
            smc_notes.append(f"⬜ FVG target: {c['fvg_low']} – {c['fvg_high']}")

        smc_line = "\n".join(smc_notes) if smc_notes else ""

        # RSI label
        rsi = c.get("rsi")
        if rsi:
            if rsi <= 35:   rsi_label = f"RSI {rsi} 🟣 Oversold"
            elif rsi <= 50: rsi_label = f"RSI {rsi} ✨ Early entry"
            elif rsi <= 65: rsi_label = f"RSI {rsi} 🟡 Building"
            else:           rsi_label = f"RSI {rsi} 🔴 Overbought"
        else:
            rsi_label = ""

        # Smart money data line
        sm_parts = []
        if c.get("oi_chg") is not None:
            sm_parts.append(f"OI: {'+' if c['oi_chg']>=0 else ''}{c['oi_chg']}%")
        if c.get("funding") is not None:
            sm_parts.append(f"Fund: {c['funding']}%")
        if c.get("ls_ratio") is not None:
            sm_parts.append(f"L/S: {c['ls_ratio']}")
        sm_line = " | ".join(sm_parts) if sm_parts else ""

        coin_msg = (
            f"\n**{i}. {c['sym']}/USDT** | {c['signal']}\n"
            f"Score: **{c['score']}/100** | 📈 +{c['gain']}% | Vol: {fmt_vol(c['vol'])}\n"
            f"💰 Current: `{c['price']}`\n"
        )

        if rsi_label:
            coin_msg += f"{rsi_label}\n"
        if sm_line:
            coin_msg += f"📡 {sm_line}\n"
        if smc_line:
            coin_msg += f"{smc_line}\n"

        coin_msg += (
            f"\n**Entry Levels:**\n"
            f"🔵 Entry:  `{lv['entry']}`\n"
            f"🎯 TP1:    `{lv['tp1']}` (+10-15%)\n"
            f"🚀 TP2:    `{lv['tp2']}` (+25-35%)\n"
            f"💎 TP3:    `{lv['tp3']}` (+45-60%)\n"
            f"⛔ Stop:   `{lv['stop']}` (-12%)\n"
            f"⚖️ R/R:    {lv['rr']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        messages.append(coin_msg)

    messages.append("\n⚠️ *Not financial advice. Always use stop losses. DYOR.*")
    return messages

# ══════════════════════════════════════════════════════════
# DISCORD BOT
# ══════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def send_results(target, coins, mode="early"):
    messages = build_message(coins, mode)
    if isinstance(messages, str):
        await target.send(messages)
        return
    for msg in messages:
        if len(msg) > 1900:
            # split long messages
            chunks = [msg[i:i+1900] for i in range(0, len(msg), 1900)]
            for chunk in chunks:
                await target.send(chunk)
        else:
            await target.send(msg)
        await asyncio.sleep(0.5)

import asyncio

@bot.command(name="scan")
async def scan(ctx):
    """Early warning scan — finds coins BEFORE they pump"""
    await ctx.send("🔍 **Running early warning scan...**\nAnalyzing volume accumulation, OI, funding rates & SMC patterns. Takes ~60 seconds...")
    coins = run_screener(mode="early")
    await send_results(ctx, coins, "early")

@bot.command(name="momentum")
async def momentum(ctx):
    """Momentum scan — finds strong movers right now"""
    await ctx.send("⚡ **Running momentum scan...**\nFinding strong movers with smart money confirmation. Takes ~60 seconds...")
    coins = run_screener(mode="momentum")
    await send_results(ctx, coins, "momentum")

@bot.command(name="help2")
async def help2(ctx):
    await ctx.send(
        "**📊 AKA Smart Money Screener — Commands:**\n\n"
        "`!scan` — Early warning scan (finds coins BEFORE they pump)\n"
        "`!momentum` — Momentum scan (strong movers right now)\n"
        "`!help2` — This message\n\n"
        "**What we detect:**\n"
        "• Volume accumulation (quiet buying before pump)\n"
        "• Consolidation breakouts\n"
        "• Liquidity sweeps (SMC)\n"
        "• Order Blocks (SMC)\n"
        "• Fair Value Gaps (SMC)\n"
        "• Open Interest spikes\n"
        "• Funding rate shifts\n"
        "• RSI early entry zones\n"
        "• Long/Short ratio\n\n"
        f"⏰ Auto early scan every {SCAN_HOURS} hours"
    )

async def auto_scan():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        log.error("Channel not found!")
        return
    await channel.send("⏰ **Auto early warning scan starting...**")
    coins = run_screener(mode="early")
    await send_results(channel, coins, "early")
    log.info("Auto scan sent!")

def scheduler():
    def job():
        future = asyncio.run_coroutine_threadsafe(auto_scan(), bot.loop)
        try:
            future.result(timeout=180)
        except Exception as e:
            log.error(f"Auto scan error: {e}")
    schedule.every(SCAN_HOURS).hours.do(job)
    while True:
        schedule.run_pending()
        time.sleep(60)

@bot.event
async def on_ready():
    print(f"✅ AKA Smart Money Screener online as {bot.user}!")
    print(f"📡 Binance Futures | SMC | OI | Funding | RSI")
    print(f"⏰ Auto early scan every {SCAN_HOURS} hours")
    t = threading.Thread(target=scheduler, daemon=True)
    t.start()

if not TOKEN:
    print("ERROR: No DISCORD_TOKEN!")
else:
    bot.run(TOKEN)

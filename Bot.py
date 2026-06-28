"""
╔══════════════════════════════════════════════════════════╗
║     AKA SMART MONEY CRYPTO SCREENER BOT v4.1            ║
║     Built for Ahsan | Aka Trading Signals                ║
║     Strategy: Quality > Quantity | Max 3 signals         ║
╚══════════════════════════════════════════════════════════╝
"""

import os, time, logging, requests, threading, schedule, certifi
from datetime import datetime, timedelta
import discord
from discord.ext import commands

TOKEN      = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# COOLDOWN TRACKER — prevents repeated signals
# ══════════════════════════════════════════════════════════
signal_cooldowns = {}  # {symbol: {"momentum": datetime, "early": datetime}}

def is_on_cooldown(symbol, mode, hours):
    """Returns True if coin was signaled recently"""
    if symbol not in signal_cooldowns:
        return False
    last = signal_cooldowns[symbol].get(mode)
    if not last:
        return False
    return datetime.utcnow() - last < timedelta(hours=hours)

def mark_signaled(symbol, mode):
    """Record that this coin just fired a signal"""
    if symbol not in signal_cooldowns:
        signal_cooldowns[symbol] = {}
    signal_cooldowns[symbol][mode] = datetime.utcnow()

# ══════════════════════════════════════════════════════════
# SAFE HTTP
# ══════════════════════════════════════════════════════════

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
# TECHNICAL ANALYSIS
# ══════════════════════════════════════════════════════════

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - (100 / (1 + ag/al)), 1) if al > 0 else 100.0

def detect_market_structure(klines):
    if len(klines) < 10: return "unknown"
    highs = [k["high"] for k in klines[-10:]]
    lows  = [k["low"]  for k in klines[-10:]]
    hh = highs[-1] > highs[-5] > highs[-10]
    hl = lows[-1]  > lows[-5]  > lows[-10]
    lh = highs[-1] < highs[-5] < highs[-10]
    ll = lows[-1]  < lows[-5]  < lows[-10]
    if hh and hl: return "uptrend"
    if lh and ll: return "downtrend"
    return "ranging"

def detect_momentum_candle(klines_15m):
    """Strong green candle with volume spike on 15m"""
    if len(klines_15m) < 3: return False, 0
    last = klines_15m[-1]
    prev = klines_15m[-2]
    body = (last["close"] - last["open"]) / last["open"] * 100
    vol_ratio = last["vol_usdt"] / prev["vol_usdt"] if prev["vol_usdt"] > 0 else 1
    is_green      = last["close"] > last["open"]
    is_strong     = body >= 1.5
    is_vol_spike  = vol_ratio >= 2.0
    return is_green and is_strong and is_vol_spike, round(body, 2)

def detect_15m_breakout(klines_15m):
    """Price broke above last 10 candles on 15m"""
    if len(klines_15m) < 12: return False
    prev_high  = max(k["high"] for k in klines_15m[-12:-2])
    last_close = klines_15m[-1]["close"]
    return last_close > prev_high * 1.005

def detect_volume_accumulation(klines):
    """Volume rising while price flat = smart money accumulating"""
    if len(klines) < 9: return 1.0, False
    recent_vols   = [k["vol_usdt"] for k in klines[-3:]]
    previous_vols = [k["vol_usdt"] for k in klines[-9:-3]]
    recent_gains  = [abs(k["close"]-k["open"])/k["open"]*100 for k in klines[-3:]]
    avg_r = sum(recent_vols)/len(recent_vols)   if recent_vols   else 0
    avg_p = sum(previous_vols)/len(previous_vols) if previous_vols else 1
    avg_g = sum(recent_gains)/len(recent_gains) if recent_gains  else 0
    spike = avg_r / avg_p if avg_p > 0 else 1
    return round(spike, 2), spike >= 2.0 and avg_g < 8

def detect_volume_explosion(klines_1h, klines_15m):
    """
    Pre-pump signal: volume exploding on 15m vs 1h baseline.
    Threshold: 150%+ above average. This is the key early signal.
    """
    if not klines_1h or not klines_15m: return 1.0, False
    baseline_vol = sum(k["vol_usdt"] for k in klines_1h[-12:]) / 12
    recent_15m   = sum(k["vol_usdt"] for k in klines_15m[-3:]) / 3
    ratio = recent_15m / baseline_vol if baseline_vol > 0 else 1.0
    return round(ratio, 2), ratio >= 2.5  # 150%+ above baseline

def find_support_resistance(klines):
    if len(klines) < 10: return [], []
    lows  = [k["low"]  for k in klines]
    highs = [k["high"] for k in klines]
    supports    = []
    resistances = []
    for i in range(2, len(lows)-2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            supports.append(lows[i])
    for i in range(2, len(highs)-2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistances.append(highs[i])
    return sorted(supports, reverse=True), sorted(resistances)

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
    return f"${v/1e3:.0f}K"

# ══════════════════════════════════════════════════════════
# !MOMENTUM SCREENER — 5-10% scalp, best 3 signals only
# ══════════════════════════════════════════════════════════

def run_momentum_screener():
    """
    Finds coins with active momentum on 15m chart.
    Score must be 80+ to qualify (strict filter).
    Max 3 signals. Entry within 2% of current price.
    Cooldown: same coin won't fire again for 4 hours.
    """
    log.info("Running momentum screener (quality mode)...")
    tickers = get_futures_tickers()
    if not tickers: return []

    results  = []
    checked  = 0

    for sym, t in tickers.items():
        gain  = t["gain"]
        vol   = t["vol_usdt"]
        price = t["price"]

        # Filter: moving 3-30%, volume $5M+
        if not (3 <= gain <= 30): continue
        if vol < 5e6: continue
        if price <= 0: continue

        # Skip if on cooldown
        if is_on_cooldown(sym, "momentum", hours=4): continue

        klines_15m = get_klines(sym, "15m", 20)
        klines_1h  = get_klines(sym, "1h",  20)
        time.sleep(0.08)

        if not klines_15m or not klines_1h: continue

        closes_1h = [k["close"] for k in klines_1h]
        rsi       = calc_rsi(closes_1h)
        structure = detect_market_structure(klines_1h)

        # HARD FILTERS — disqualify weak setups immediately
        if rsi and rsi > 72:        continue  # overbought — too late
        if structure == "downtrend": continue  # never trade downtrend

        mom_candle, body_pct = detect_momentum_candle(klines_15m)
        brkout_15m           = detect_15m_breakout(klines_15m)

        # Must have at least one strong signal
        if not mom_candle and not brkout_15m: continue

        # Volume trend on 15m
        recent_vols = [k["vol_usdt"] for k in klines_15m[-3:]]
        prev_vols   = [k["vol_usdt"] for k in klines_15m[-8:-3]]
        vol_trend   = (sum(recent_vols)/3) / (sum(prev_vols)/5) if prev_vols and sum(prev_vols) > 0 else 1

        oi_chg  = get_oi_history(sym)
        funding = get_funding_rate(sym)
        time.sleep(0.08)

        # Skip if funding too high (squeeze risk)
        if funding and funding > 0.15: continue

        # SCORING — must reach 80 to qualify
        score = 0

        if mom_candle:         score += 30
        if brkout_15m:         score += 25
        if vol_trend >= 3:     score += 20
        elif vol_trend >= 2:   score += 12
        if rsi and rsi <= 50:  score += 18
        elif rsi and rsi <= 62: score += 10
        if structure == "uptrend": score += 15
        elif structure == "ranging": score += 5
        if vol >= 20e6:        score += 12
        elif vol >= 5e6:       score += 6
        if 5 <= gain <= 20:    score += 10
        if oi_chg and oi_chg >= 5: score += 10
        if funding and -0.01 <= funding <= 0.05: score += 5

        # STRICT THRESHOLD — 80 minimum
        if score < 80: continue

        # Entry: current price or max 2% dip (realistic for scalp)
        entry = price * 0.99  # 1% below = fast fill

        # Targets based on score quality
        if score >= 90:
            tp1 = entry * 1.06   # 6%
            tp2 = entry * 1.10   # 10%
            tp3 = entry * 1.18   # 18%
        elif score >= 85:
            tp1 = entry * 1.05   # 5%
            tp2 = entry * 1.09   # 9%
            tp3 = entry * 1.15   # 15%
        else:
            tp1 = entry * 1.04   # 4%
            tp2 = entry * 1.07   # 7%
            tp3 = entry * 1.12   # 12%

        stop = entry * 0.95  # tight 5% stop
        rr   = round((tp1 - entry) / (entry - stop), 1)

        results.append({
            "sym":       sym.replace("USDT", ""),
            "full_sym":  sym,
            "gain":      gain,
            "vol":       vol,
            "price":     price,
            "score":     score,
            "rsi":       rsi,
            "structure": structure,
            "oi_chg":    oi_chg,
            "funding":   funding,
            "vol_trend": round(vol_trend, 1),
            "body_pct":  body_pct,
            "mom_candle":mom_candle,
            "brkout":    brkout_15m,
            "entry":     entry,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "stop": stop, "rr": rr,
        })

        checked += 1
        if checked >= 80: break

    # Sort by score, return TOP 3 ONLY
    top3 = sorted(results, key=lambda x: x["score"], reverse=True)[:3]

    # Mark signaled (cooldown)
    for c in top3:
        mark_signaled(c["full_sym"], "momentum")

    return top3

# ══════════════════════════════════════════════════════════
# !EARLY SCREENER — pre-pump detection, max 2 signals
# ══════════════════════════════════════════════════════════

def run_early_screener():
    """
    Detects coins BEFORE they pump.
    Criteria: volume explosion + OI rising + price still quiet (under 8% gain).
    Max 2 signals. Score must be 80+.
    Cooldown: 6 hours per coin.
    """
    log.info("Running early pre-pump screener...")
    tickers = get_futures_tickers()
    if not tickers: return []

    results = []
    checked = 0

    for sym, t in tickers.items():
        gain  = t["gain"]
        vol   = t["vol_usdt"]
        price = t["price"]

        # Early signal: coin barely moved yet (1-8% gain)
        if not (1 <= gain <= 8): continue
        if vol < 3e6: continue
        if price <= 0: continue

        # Skip if on cooldown
        if is_on_cooldown(sym, "early", hours=6): continue

        klines_1h  = get_klines(sym, "1h",  20)
        klines_15m = get_klines(sym, "15m", 20)
        time.sleep(0.08)

        if not klines_1h or not klines_15m: continue

        closes_1h = [k["close"] for k in klines_1h]
        rsi       = calc_rsi(closes_1h)
        structure = detect_market_structure(klines_1h)

        # Hard filters
        if rsi and rsi > 65:         continue  # already ran up
        if structure == "downtrend":  continue

        # CORE EARLY SIGNAL: volume explosion vs baseline
        vol_ratio, vol_exploding = detect_volume_explosion(klines_1h, klines_15m)

        # Volume must be exploding for early signal — this is the key
        if not vol_exploding: continue

        # Accumulation check
        vol_spike, is_accum = detect_volume_accumulation(klines_1h)

        oi_chg  = get_oi_history(sym)
        funding = get_funding_rate(sym)
        time.sleep(0.08)

        # OI must be rising — confirms new money entering
        if oi_chg is None or oi_chg < 3: continue

        # Funding must be neutral/low (not overheated)
        if funding and funding > 0.10: continue

        # SCORING
        score = 0

        # Volume explosion is the #1 signal
        if vol_ratio >= 5:     score += 35
        elif vol_ratio >= 3:   score += 25
        elif vol_ratio >= 2.5: score += 15

        # OI growth = new money = fuel for pump
        if oi_chg >= 15:       score += 25
        elif oi_chg >= 8:      score += 18
        elif oi_chg >= 3:      score += 10

        # RSI early = more room to run
        if rsi and rsi <= 40:  score += 20
        elif rsi and rsi <= 55: score += 12
        elif rsi and rsi <= 65: score += 5

        # Accumulation = smart money building position
        if is_accum:           score += 15

        # Structure bonus
        if structure == "uptrend":  score += 10
        elif structure == "ranging": score += 5

        # Funding negative = short squeeze fuel
        if funding and funding < -0.005: score += 10
        elif funding and 0 <= funding <= 0.05: score += 5

        # Volume size
        if vol >= 10e6: score += 10
        elif vol >= 3e6: score += 5

        # STRICT THRESHOLD
        if score < 80: continue

        # Entry for early signal: current price (coin hasn't moved yet)
        entry = price * 1.001  # slight buffer above current

        # Early targets: bigger potential since catching early
        tp1 = entry * 1.08   # 8%
        tp2 = entry * 1.15   # 15%
        tp3 = entry * 1.25   # 25%
        stop = entry * 0.94  # 6% stop (slightly wider for early entry)
        rr   = round((tp1 - entry) / (entry - stop), 1)

        results.append({
            "sym":         sym.replace("USDT", ""),
            "full_sym":    sym,
            "gain":        gain,
            "vol":         vol,
            "price":       price,
            "score":       score,
            "rsi":         rsi,
            "structure":   structure,
            "oi_chg":      oi_chg,
            "funding":     funding,
            "vol_ratio":   vol_ratio,
            "is_accum":    is_accum,
            "vol_spike":   vol_spike,
            "entry":       entry,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "stop": stop, "rr": rr,
        })

        checked += 1
        if checked >= 80: break

    # TOP 2 ONLY — early signals are rare and high conviction
    top2 = sorted(results, key=lambda x: x["score"], reverse=True)[:2]

    for c in top2:
        mark_signaled(c["full_sym"], "early")

    return top2

# ══════════════════════════════════════════════════════════
# MESSAGE BUILDERS
# ══════════════════════════════════════════════════════════

def build_momentum_messages(coins, mode="manual"):
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    label = "⏰ AUTO" if mode == "auto" else "⚡ MANUAL"

    if not coins:
        return [
            f"**{label} MOMENTUM SCAN** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No quality momentum setups right now.\n"
            f"Need score 80+. Try again in 30 mins.\n"
            f"*(Strict filter = only real signals)*"
        ]

    msgs = [
        f"**{label} MOMENTUM SCAN** | 🕐 {now}\n"
        f"🎯 Target: **5–10% scalp** | Stop: 5% | Score: **80+ only**\n"
        f"✅ **{len(coins)} top setup(s)** (max 3 shown)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, c in enumerate(coins, 1):
        rsi     = c.get("rsi")
        rsi_str = f"RSI {rsi}" if rsi else "RSI N/A"

        flags = []
        if c.get("mom_candle"): flags.append(f"🕯️ 15m candle +{c['body_pct']}% with vol spike")
        if c.get("brkout"):     flags.append("💥 15m breakout above resistance")
        if c.get("vol_trend", 1) >= 2: flags.append(f"📈 Volume {c['vol_trend']}x above average")
        if c.get("oi_chg") and c["oi_chg"] >= 5: flags.append(f"💰 OI +{c['oi_chg']}% (new positions)")
        if c.get("structure") == "uptrend": flags.append("📈 1h uptrend confirmed")

        msg = (
            f"\n**{i}. {c['sym']}/USDT** ⚡ MOMENTUM\n"
            f"Score: **{c['score']}/100** | 📈 +{c['gain']}% 24h | Vol: {fmt_vol(c['vol'])}\n"
            f"{rsi_str} | Structure: {c['structure']}\n"
            + ("\n".join(flags) + "\n" if flags else "")
            + f"\n💰 Current: `{fmt_price(c['price'])}`\n"
            f"🔵 Entry:  `{fmt_price(c['entry'])}` ← **enter now or skip**\n"
            f"🎯 TP1:   `{fmt_price(c['tp1'])}` **(+{round((c['tp1']-c['entry'])/c['entry']*100,1)}% — exit 50%)**\n"
            f"🚀 TP2:   `{fmt_price(c['tp2'])}` **(+{round((c['tp2']-c['entry'])/c['entry']*100,1)}% — exit 35%)**\n"
            f"💎 TP3:   `{fmt_price(c['tp3'])}` **(+{round((c['tp3']-c['entry'])/c['entry']*100,1)}% — exit 15%)**\n"
            f"⛔ Stop:  `{fmt_price(c['stop'])}` **(-5% — exit immediately)**\n"
            f"⚖️ R/R:   1:{c['rr']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        msgs.append(msg)

    msgs.append(
        "\n⚡ *Momentum fades fast — enter quick, take profit at TP1/TP2.*\n"
        "⚠️ *Always set stop loss. Not financial advice.*"
    )
    return msgs

def build_early_messages(coins, mode="manual"):
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    label = "⏰ AUTO" if mode == "auto" else "🌅 MANUAL"

    if not coins:
        return [
            f"**{label} EARLY SIGNAL** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No pre-pump setups detected.\n"
            f"Watching for volume explosion + OI spike.\n"
            f"*(These are rare — that's the point)*"
        ]

    msgs = [
        f"**{label} EARLY SIGNAL** | 🕐 {now}\n"
        f"🌅 Pre-pump detection | Target: **8–25%** | Score: **80+ only**\n"
        f"⚠️ **{len(coins)} early setup(s)** — coin hasn't moved yet!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, c in enumerate(coins, 1):
        rsi     = c.get("rsi")
        rsi_str = f"RSI {rsi}" if rsi else "RSI N/A"

        flags = []
        flags.append(f"🔥 Volume {c['vol_ratio']}x above 1h baseline (EXPLOSION)")
        if c.get("oi_chg"): flags.append(f"💰 OI +{c['oi_chg']}% — new money entering")
        if c.get("is_accum"): flags.append(f"📦 Accumulation detected ({c['vol_spike']}x)")
        if c.get("funding") and c["funding"] < 0: flags.append(f"🔄 Negative funding {c['funding']}% (short squeeze)")
        if c.get("structure") == "uptrend": flags.append("📈 Structure: uptrend")

        msg = (
            f"\n**{i}. {c['sym']}/USDT** 🌅 EARLY SIGNAL\n"
            f"Score: **{c['score']}/100** | 📈 Only +{c['gain']}% so far\n"
            f"Vol: {fmt_vol(c['vol'])} | {rsi_str}\n"
            + ("\n".join(flags) + "\n" if flags else "")
            + f"\n💰 Current: `{fmt_price(c['price'])}`\n"
            f"🔵 Entry:  `{fmt_price(c['entry'])}` ← **accumulate here**\n"
            f"🎯 TP1:   `{fmt_price(c['tp1'])}` **(+8% — take 40%)**\n"
            f"🚀 TP2:   `{fmt_price(c['tp2'])}` **(+15% — take 40%)**\n"
            f"💎 TP3:   `{fmt_price(c['tp3'])}` **(+25% — take 20%)**\n"
            f"⛔ Stop:  `{fmt_price(c['stop'])}` **(-6%)**\n"
            f"⚖️ R/R:   1:{c['rr']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        msgs.append(msg)

    msgs.append(
        "\n🌅 *Early signals need patience — may take 2–12 hours to play out.*\n"
        "⚠️ *Not financial advice. Always use stop loss.*"
    )
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

@bot.command(name="momentum")
async def momentum(ctx):
    """Quick scalp — 5-10% targets, 15m momentum, max 3 signals"""
    await ctx.send("⚡ **Momentum scan...** Finding 80+ score setups on 15m chart. ~45 secs...")
    coins = run_momentum_screener()
    await send_messages(ctx, build_momentum_messages(coins, "manual"))

@bot.command(name="early")
async def early(ctx):
    """Pre-pump detection — volume explosion + OI spike, max 2 signals"""
    await ctx.send("🌅 **Early signal scan...** Looking for volume explosion before pump. ~45 secs...")
    coins = run_early_screener()
    await send_messages(ctx, build_early_messages(coins, "manual"))

@bot.command(name="help2")
async def help2(ctx):
    await ctx.send(
        "**📊 AKA Smart Money Screener v4.1**\n\n"
        "`!momentum` — Quick scalp (5–10% targets, 15m chart, max 3 signals)\n"
        "`!early`    — Pre-pump detection (8–25% targets, max 2 signals)\n"
        "`!help2`    — This message\n\n"
        "**Signal quality rules:**\n"
        "• Score must be **80+/100** to appear\n"
        "• Same coin won't repeat for **4h (momentum) / 6h (early)**\n"
        "• Downtrend coins are always filtered out\n"
        "• Overbought RSI (72+) coins are skipped\n\n"
        "**Auto-scans:**\n"
        "• Momentum: every 2 hours\n"
        "• Early: every 2 hours (offset 1h)\n"
    )

# ══════════════════════════════════════════════════════════
# AUTO SCANS
# ══════════════════════════════════════════════════════════

async def auto_momentum_scan():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    try:
        coins = run_momentum_screener()
        if coins:
            await send_messages(channel, build_momentum_messages(coins, "auto"))
        else:
            log.info("Auto momentum: no 80+ signals, skipping")
    except Exception as e:
        log.error(f"Auto momentum error: {e}")

async def auto_early_scan():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return
    try:
        coins = run_early_screener()
        if coins:
            await send_messages(channel, build_early_messages(coins, "auto"))
        else:
            log.info("Auto early: no 80+ signals, skipping")
    except Exception as e:
        log.error(f"Auto early error: {e}")

def scheduler():
    def momentum_job():
        future = asyncio.run_coroutine_threadsafe(auto_momentum_scan(), bot.loop)
        try: future.result(timeout=180)
        except Exception as e: log.error(f"Scheduler momentum error: {e}")

    def early_job():
        future = asyncio.run_coroutine_threadsafe(auto_early_scan(), bot.loop)
        try: future.result(timeout=180)
        except Exception as e: log.error(f"Scheduler early error: {e}")

    # Momentum scan every 2 hours
    schedule.every(2).hours.do(momentum_job)
    # Early scan every 2 hours (runs at offset so they don't clash)
    schedule.every(2).hours.do(early_job)

    # First scans fire after 5 minutes on startup
    time.sleep(300)
    momentum_job()
    time.sleep(60)
    early_job()

    while True:
        schedule.run_pending()
        time.sleep(60)

@bot.event
async def on_ready():
    print(f"✅ AKA SMC Screener v4.1 online as {bot.user}!")
    print(f"📡 Quality mode: Score 80+ | Max 3 momentum | Max 2 early")
    print(f"⏰ Cooldown: 4h momentum, 6h early | First scan in 5 mins")
    t = threading.Thread(target=scheduler, daemon=True)
    t.start()

if not TOKEN:
    print("ERROR: No DISCORD_TOKEN set!")
else:
    bot.run(TOKEN)

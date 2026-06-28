"""
╔══════════════════════════════════════════════════════════╗
║     AKA SMART MONEY CRYPTO SCREENER BOT v5.0            ║
║     Built for Ahsan | Aka Trading Signals                ║
║     Goal: Catch coins BEFORE they appear in top gainers  ║
╚══════════════════════════════════════════════════════════╝

AUTO SCAN LOGIC:
- Scans every 30 minutes automatically (no need to type commands)
- Catches coins in EARLY accumulation phase (before big pump)
- Catches coins with MOMENTUM building (second leg setups)
- Score 70+ required | Max 3 signals per scan
- Cooldown: 6h per coin to prevent repeats
- Filters: no downtrend, no RSI>75, no high funding
"""

import os, time, logging, requests, threading, certifi
from datetime import datetime, timedelta
import discord
from discord.ext import commands
import asyncio

TOKEN      = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# COOLDOWN SYSTEM
# ══════════════════════════════════════════════════════════
signal_cooldowns = {}

def is_on_cooldown(symbol, hours=6):
    last = signal_cooldowns.get(symbol)
    if not last: return False
    return datetime.utcnow() - last < timedelta(hours=hours)

def mark_signaled(symbol):
    signal_cooldowns[symbol] = datetime.utcnow()

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
                 params={"symbol": symbol, "period": "1h", "limit": 6})
    if not r: return None, None
    data = r.json()
    if len(data) < 2: return None, None
    old = float(data[0]["sumOpenInterest"])
    new = float(data[-1]["sumOpenInterest"])
    chg = round((new - old) / old * 100, 2) if old > 0 else None
    mid = float(data[len(data)//2]["sumOpenInterest"])
    accel = (new - mid) > (mid - old)
    return chg, accel

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

def calc_volume_profile(klines_1h, klines_15m):
    if not klines_1h or not klines_15m:
        return 1.0, 1.0, False
    baseline_1h = [k["vol_usdt"] for k in klines_1h[-26:-2]]
    avg_baseline = sum(baseline_1h) / len(baseline_1h) if baseline_1h else 1
    recent_1h = sum(k["vol_usdt"] for k in klines_1h[-2:]) / 2
    spike_1h = recent_1h / avg_baseline if avg_baseline > 0 else 1
    recent_15m  = [k["vol_usdt"] for k in klines_15m[-3:]]
    prev_15m    = [k["vol_usdt"] for k in klines_15m[-11:-3]]
    avg_r = sum(recent_15m) / len(recent_15m) if recent_15m else 0
    avg_p = sum(prev_15m)   / len(prev_15m)   if prev_15m   else 1
    spike_15m = avg_r / avg_p if avg_p > 0 else 1
    accelerating = False
    if len(klines_15m) >= 3:
        v1 = klines_15m[-3]["vol_usdt"]
        v2 = klines_15m[-2]["vol_usdt"]
        v3 = klines_15m[-1]["vol_usdt"]
        accelerating = v3 > v2 > v1
    return round(spike_1h, 2), round(spike_15m, 2), accelerating

def detect_price_compression(klines_1h):
    if len(klines_1h) < 20: return False, 0
    body_sizes = []
    for k in klines_1h[-12:-2]:
        body = abs(k["close"] - k["open"]) / k["open"] * 100
        body_sizes.append(body)
    avg_body = sum(body_sizes) / len(body_sizes) if body_sizes else 5
    recent_body = abs(klines_1h[-1]["close"] - klines_1h[-1]["open"]) / klines_1h[-1]["open"] * 100
    breakout_strength = recent_body / avg_body if avg_body > 0 else 1
    was_compressed = avg_body < 2.0
    is_breaking    = recent_body > avg_body * 2
    return was_compressed and is_breaking, round(breakout_strength, 1)

def detect_15m_momentum(klines_15m):
    if len(klines_15m) < 4: return False, 0, 0
    last = klines_15m[-1]
    prev = klines_15m[-2]
    body = (last["close"] - last["open"]) / last["open"] * 100
    vol_ratio = last["vol_usdt"] / prev["vol_usdt"] if prev["vol_usdt"] > 0 else 1
    is_momentum = (
        last["close"] > last["open"] and
        body >= 1.2 and
        vol_ratio >= 1.8
    )
    return is_momentum, round(body, 2), round(vol_ratio, 1)

def detect_15m_breakout(klines_15m):
    if len(klines_15m) < 14: return False, 0
    prev_highs = [k["high"] for k in klines_15m[-14:-2]]
    resistance = max(prev_highs)
    last_close = klines_15m[-1]["close"]
    broke = last_close > resistance * 1.003
    strength = round((last_close / resistance - 1) * 100, 2) if broke else 0
    return broke, strength

def detect_accumulation(klines_1h):
    if len(klines_1h) < 12: return False, 1.0
    closes = [k["close"] for k in klines_1h[-6:]]
    price_range = (max(closes) - min(closes)) / min(closes) * 100 if min(closes) > 0 else 100
    vol_recent = sum(k["vol_usdt"] for k in klines_1h[-3:]) / 3
    vol_prev   = sum(k["vol_usdt"] for k in klines_1h[-9:-3]) / 6
    vol_ratio  = vol_recent / vol_prev if vol_prev > 0 else 1
    is_accum = price_range < 5.0 and vol_ratio >= 1.5
    return is_accum, round(vol_ratio, 2)

def detect_second_leg(klines_1h, gain):
    if len(klines_1h) < 20 or gain < 15: return False
    highs  = [k["high"]  for k in klines_1h[-10:]]
    closes = [k["close"] for k in klines_1h[-10:]]
    recent_high = max(highs[:-2])
    current     = closes[-1]
    pullback    = (recent_high - current) / recent_high * 100
    vols = [k["vol_usdt"] for k in klines_1h[-8:]]
    avg_vol = sum(vols[:-1]) / len(vols[:-1])
    vol_returning = vols[-1] > avg_vol * 1.5
    return 3 <= pullback <= 20 and vol_returning

# ══════════════════════════════════════════════════════════
# MASTER SCORING ENGINE
# ══════════════════════════════════════════════════════════
def score_coin(sym, t, klines_1h, klines_15m, oi_chg, oi_accel, funding):
    gain     = t["gain"]
    vol      = t["vol_usdt"]
    score    = 0
    reasons  = []
    sig_type = "MOMENTUM"

    closes_1h = [k["close"] for k in klines_1h]
    rsi       = calc_rsi(closes_1h)
    structure = detect_market_structure(klines_1h)

    # Hard disqualifiers
    if structure == "downtrend":     return 0, None, []
    if rsi and rsi > 75:             return 0, None, []
    if funding and funding > 0.15:   return 0, None, []
    if vol < 2e6:                    return 0, None, []

    spike_1h, spike_15m, vol_accel = calc_volume_profile(klines_1h, klines_15m)
    is_mom, body_pct, vol_ratio    = detect_15m_momentum(klines_15m)
    broke_15m, bo_strength         = detect_15m_breakout(klines_15m)
    compressed, comp_strength      = detect_price_compression(klines_1h)
    is_accum, accum_ratio          = detect_accumulation(klines_1h)
    is_second_leg                  = detect_second_leg(klines_1h, gain)

    # ── EARLY SIGNAL (1-20% gain) ──
    if 1 <= gain <= 20:
        sig_type = "EARLY"

        if spike_15m >= 5:
            score += 35
            reasons.append(f"🔥 Volume EXPLOSION {spike_15m}x on 15m (pre-pump signal)")
        elif spike_15m >= 3:
            score += 25
            reasons.append(f"📈 Volume surge {spike_15m}x on 15m")
        elif spike_15m >= 2:
            score += 15
            reasons.append(f"📊 Volume building {spike_15m}x")

        if vol_accel:
            score += 10
            reasons.append("⚡ Volume accelerating each candle")

        if oi_chg and oi_chg >= 15:
            score += 25
            reasons.append(f"💰 OI +{oi_chg}% SURGING — big money entering NOW")
        elif oi_chg and oi_chg >= 8:
            score += 18
            reasons.append(f"💰 OI +{oi_chg}% rising fast")
        elif oi_chg and oi_chg >= 3:
            score += 10
            reasons.append(f"📊 OI +{oi_chg}% increasing")

        if oi_accel:
            score += 8
            reasons.append("⚡ OI accelerating in last hour")

        if is_accum:
            score += 15
            reasons.append(f"📦 Smart money accumulating ({accum_ratio}x vol, price flat)")

        if compressed:
            score += 12
            reasons.append(f"🌀 Coiled spring breakout ({comp_strength}x expansion)")

        if rsi and rsi <= 40:
            score += 20
            reasons.append(f"✨ RSI {rsi} — very early, massive room to run")
        elif rsi and rsi <= 55:
            score += 12
            reasons.append(f"📈 RSI {rsi} — early entry zone")
        elif rsi and rsi <= 65:
            score += 5
            reasons.append(f"🟡 RSI {rsi} — momentum building")

        if funding and funding < -0.005:
            score += 12
            reasons.append(f"🔄 Negative funding {funding}% — short squeeze fuel")
        elif funding and 0 <= funding <= 0.05:
            score += 5
            reasons.append(f"✅ Funding {funding}% — healthy")

        if structure == "uptrend":
            score += 10
            reasons.append("📈 1h uptrend — with the trend")
        elif structure == "ranging":
            score += 5
            reasons.append("↔️ Ranging — breakout possible")

        if vol >= 50e6:   score += 10; reasons.append(f"💎 Volume ${vol/1e6:.0f}M — massive")
        elif vol >= 10e6: score += 7;  reasons.append(f"✅ Volume ${vol/1e6:.0f}M — strong")
        elif vol >= 2e6:  score += 3

    # ── MOMENTUM / SECOND LEG (15-50% gain) ──
    elif 15 <= gain <= 50:
        sig_type = "MOMENTUM"

        if is_mom:
            score += 30
            reasons.append(f"🕯️ Strong 15m candle +{body_pct}% with {vol_ratio}x volume")
        if broke_15m:
            score += 25
            reasons.append(f"💥 15m breakout +{bo_strength}% above resistance")
        if is_second_leg:
            score += 20
            reasons.append("🔁 Second leg setup — pulled back, volume returning")

        if spike_15m >= 4:
            score += 20
            reasons.append(f"🔥 15m volume {spike_15m}x — momentum igniting")
        elif spike_15m >= 2.5:
            score += 12
            reasons.append(f"📈 15m volume {spike_15m}x — building")

        if vol_accel:
            score += 8
            reasons.append("⚡ Volume accelerating")

        if oi_chg and oi_chg >= 8:
            score += 15
            reasons.append(f"💰 OI +{oi_chg}% — new money still entering")
        elif oi_chg and oi_chg >= 3:
            score += 8
            reasons.append(f"📊 OI +{oi_chg}%")

        if rsi and rsi <= 55:
            score += 18
            reasons.append(f"✨ RSI {rsi} — not overbought, fuel remaining")
        elif rsi and rsi <= 65:
            score += 10
            reasons.append(f"🟡 RSI {rsi} — momentum building")
        elif rsi and 65 < rsi <= 72:
            score += 3
            reasons.append(f"⚠️ RSI {rsi} — getting hot but acceptable")

        if structure == "uptrend":
            score += 12
            reasons.append("📈 Uptrend confirmed")

        if vol >= 20e6:  score += 10; reasons.append(f"💎 Volume ${vol/1e6:.0f}M")
        elif vol >= 5e6: score += 5

        if funding and funding < -0.005:
            score += 10
            reasons.append(f"🔄 Negative funding — short squeeze possible")

    has_signal = is_mom or broke_15m or is_accum or compressed or (spike_15m >= 2) or is_second_leg
    if not has_signal:
        return 0, None, []

    return min(score, 100), sig_type, reasons

# ══════════════════════════════════════════════════════════
# ENTRY + TARGET CALCULATOR
# ══════════════════════════════════════════════════════════
def calculate_trade_levels(price, sig_type, score, rsi):
    if sig_type == "EARLY":
        entry = price * 1.001
        stop  = entry * 0.94
        if score >= 85:
            tp1 = entry * 1.10; tp2 = entry * 1.20; tp3 = entry * 1.35
        elif score >= 75:
            tp1 = entry * 1.08; tp2 = entry * 1.15; tp3 = entry * 1.25
        else:
            tp1 = entry * 1.06; tp2 = entry * 1.12; tp3 = entry * 1.20
    else:
        entry = price * 0.99
        stop  = entry * 0.95
        if score >= 85:
            tp1 = entry * 1.07; tp2 = entry * 1.12; tp3 = entry * 1.20
        elif score >= 75:
            tp1 = entry * 1.05; tp2 = entry * 1.09; tp3 = entry * 1.15
        else:
            tp1 = entry * 1.04; tp2 = entry * 1.07; tp3 = entry * 1.12

    rr = round((tp1 - entry) / (entry - stop), 1) if entry > stop else 0
    return entry, tp1, tp2, tp3, stop, rr

# ══════════════════════════════════════════════════════════
# MASTER SCREENER
# ══════════════════════════════════════════════════════════
def run_master_screener(min_score=70, max_signals=3, triggered_by="auto"):
    log.info(f"Master screener running [{triggered_by}]...")
    tickers = get_futures_tickers()
    if not tickers: return []

    candidates = []
    checked = 0

    for sym, t in tickers.items():
        gain  = t["gain"]
        vol   = t["vol_usdt"]
        price = t["price"]

        if not (1 <= gain <= 50): continue
        if vol < 2e6: continue
        if price <= 0: continue
        if is_on_cooldown(sym, hours=6): continue

        klines_1h  = get_klines(sym, "1h",  50)
        klines_15m = get_klines(sym, "15m", 20)
        time.sleep(0.08)

        if not klines_1h or not klines_15m: continue

        oi_chg, oi_accel = get_oi_history(sym)
        funding          = get_funding_rate(sym)
        time.sleep(0.08)

        score, sig_type, reasons = score_coin(
            sym, t, klines_1h, klines_15m,
            oi_chg, oi_accel, funding
        )

        if score < min_score: continue

        closes_1h = [k["close"] for k in klines_1h]
        rsi = calc_rsi(closes_1h)

        entry, tp1, tp2, tp3, stop, rr = calculate_trade_levels(
            price, sig_type, score, rsi
        )

        candidates.append({
            "sym":      sym.replace("USDT", ""),
            "full_sym": sym,
            "gain":     gain,
            "vol":      vol,
            "price":    price,
            "score":    score,
            "sig_type": sig_type,
            "rsi":      rsi,
            "oi_chg":   oi_chg,
            "funding":  funding,
            "reasons":  reasons,
            "entry":    entry,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "stop": stop, "rr": rr,
        })

        checked += 1
        if checked >= 150: break

    top = sorted(candidates, key=lambda x: x["score"], reverse=True)[:max_signals]

    for c in top:
        mark_signaled(c["full_sym"])

    log.info(f"Screener done: {len(candidates)} qualified, sending {len(top)}")
    return top

# ══════════════════════════════════════════════════════════
# MESSAGE BUILDER
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

def build_signal_messages(coins, triggered_by="auto"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not coins:
        if triggered_by == "auto":
            return None
        return [
            f"**🔍 SCAN** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No quality signals (score 70+) right now.\n"
            f"Bot auto-scans every 30 mins. Stay tuned."
        ]

    label = "⏰ AUTO SIGNAL" if triggered_by == "auto" else "🔍 MANUAL SCAN"
    msgs = [
        f"**{label}** | 🕐 {now}\n"
        f"🎯 Score 70+ | 6h cooldown | Max 3 signals\n"
        f"✅ **{len(coins)} signal(s) found**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, c in enumerate(coins, 1):
        rsi = c.get("rsi")
        if rsi:
            if rsi <= 35:   rsi_str = f"RSI {rsi} 🟣 Oversold"
            elif rsi <= 50: rsi_str = f"RSI {rsi} ✨ Early"
            elif rsi <= 65: rsi_str = f"RSI {rsi} 🟡 Building"
            else:           rsi_str = f"RSI {rsi} 🔴 Hot"
        else:
            rsi_str = "RSI N/A"

        sig_emoji  = "🌅" if c["sig_type"] == "EARLY" else "⚡"
        entry_note = "accumulate here" if c["sig_type"] == "EARLY" else "enter now or on dip"

        tp1_pct = round((c["tp1"]-c["entry"])/c["entry"]*100, 1)
        tp2_pct = round((c["tp2"]-c["entry"])/c["entry"]*100, 1)
        tp3_pct = round((c["tp3"]-c["entry"])/c["entry"]*100, 1)

        oi_str   = f"OI {'+' if c['oi_chg'] and c['oi_chg']>=0 else ''}{c['oi_chg']}%" if c.get("oi_chg") else ""
        fund_str = f"Fund {c['funding']}%" if c.get("funding") is not None else ""
        sm_line  = " | ".join(filter(None, [oi_str, fund_str]))

        reasons_str = "\n".join(c["reasons"][:4])

        msg = (
            f"\n{sig_emoji} **{i}. {c['sym']}/USDT** — {c['sig_type']} SIGNAL\n"
            f"Score: **{c['score']}/100** | 📈 +{c['gain']:.1f}% | Vol: {fmt_vol(c['vol'])}\n"
            f"{rsi_str}" + (f" | {sm_line}" if sm_line else "") + "\n"
            f"\n{reasons_str}\n"
            f"\n💰 Price:  `{fmt_price(c['price'])}`\n"
            f"🔵 Entry:  `{fmt_price(c['entry'])}` ← **{entry_note}**\n"
            f"🎯 TP1:   `{fmt_price(c['tp1'])}` (+{tp1_pct}%) — exit 50%\n"
            f"🚀 TP2:   `{fmt_price(c['tp2'])}` (+{tp2_pct}%) — exit 35%\n"
            f"💎 TP3:   `{fmt_price(c['tp3'])}` (+{tp3_pct}%) — exit 15%\n"
            f"⛔ Stop:  `{fmt_price(c['stop'])}` | ⚖️ R/R 1:{c['rr']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        msgs.append(msg)

    msgs.append("⚠️ *Not financial advice. Always use stop loss. DYOR.*")
    return msgs

# ══════════════════════════════════════════════════════════
# DISCORD BOT
# ══════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def send_messages(target, msgs):
    if not msgs: return
    for msg in msgs:
        chunks = [msg[i:i+1900] for i in range(0, len(msg), 1900)]
        for chunk in chunks:
            await target.send(chunk)
            await asyncio.sleep(0.3)

@bot.command(name="scan")
async def scan(ctx):
    await ctx.send("🔍 **Full scan...** ~60 secs...")
    coins = run_master_screener(min_score=70, max_signals=3, triggered_by="manual")
    msgs  = build_signal_messages(coins, "manual")
    await send_messages(ctx, msgs)

@bot.command(name="momentum")
async def momentum(ctx):
    await ctx.send("⚡ **Momentum scan...** ~45 secs...")
    coins = run_master_screener(min_score=70, max_signals=3, triggered_by="manual")
    msgs  = build_signal_messages(coins, "manual")
    await send_messages(ctx, msgs)

@bot.command(name="early")
async def early(ctx):
    await ctx.send("🌅 **Early signal scan...** ~45 secs...")
    coins = run_master_screener(min_score=65, max_signals=3, triggered_by="manual")
    msgs  = build_signal_messages(coins, "manual")
    await send_messages(ctx, msgs)

@bot.command(name="status")
async def status(ctx):
    active = [s for s, t in signal_cooldowns.items()
              if datetime.utcnow() - t < timedelta(hours=6)]
    await ctx.send(
        f"**📊 Bot Status — AKA Screener v5.0**\n"
        f"✅ Online | Auto-scan every **30 minutes**\n"
        f"🔒 {len(active)} coins on cooldown (6h)\n"
        f"📡 Scanning 1-50% gainers | Score 70+ required\n\n"
        f"Commands: `!scan` `!momentum` `!early` `!status`"
    )

@bot.command(name="help2")
async def help2(ctx):
    await ctx.send(
        "**📊 AKA Smart Money Screener v5.0**\n\n"
        "**Auto-scans every 30 minutes** — no commands needed!\n\n"
        "Manual commands:\n"
        "`!scan` / `!momentum` / `!early` — trigger scan now\n"
        "`!status` — bot health + cooldown info\n\n"
        "**Signal types:**\n"
        "🌅 EARLY — coin at 1-20%, accumulation detected\n"
        "⚡ MOMENTUM — coin at 15-50%, second leg setup\n\n"
        "**Filters:**\n"
        "• Score 70+/100 required\n"
        "• Same coin silent for 6 hours\n"
        "• Downtrend coins always skipped\n"
        "• RSI 75+ skipped (overbought)\n"
        "• High funding rate skipped\n"
    )

# ── AUTO SCANNER — runs every 30 mins ──
async def auto_scan_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        log.error("Channel not found!")
        return

    log.info("First auto-scan in 3 minutes...")
    await asyncio.sleep(180)

    while True:
        try:
            log.info("Auto scan starting...")
            coins = run_master_screener(min_score=70, max_signals=3, triggered_by="auto")
            if coins:
                msgs = build_signal_messages(coins, "auto")
                if msgs:
                    await send_messages(channel, msgs)
                    log.info(f"Auto scan sent {len(coins)} signal(s)")
            else:
                log.info("Auto scan: no 70+ signals this round")
        except Exception as e:
            log.error(f"Auto scan error: {e}")

        await asyncio.sleep(30 * 60)  # 30 minutes

@bot.event
async def on_ready():
    print(f"✅ AKA SMC Screener v5.0 online as {bot.user}!")
    print(f"📡 Auto-scan every 30 mins | Score 70+ | Max 3 signals | 6h cooldown")
    print(f"⏰ First scan in 3 minutes...")
    bot.loop.create_task(auto_scan_loop())

if not TOKEN:
    print("ERROR: No DISCORD_TOKEN set!")
else:
    bot.run(TOKEN)

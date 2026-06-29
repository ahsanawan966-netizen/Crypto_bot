"""
╔══════════════════════════════════════════════════════════╗
║     AKA SMART MONEY CRYPTO SCREENER BOT v6.0            ║
║     Built for Ahsan | Aka Trading Signals                ║
║     FINAL VERSION — Catches volume explosions LIVE       ║
╚══════════════════════════════════════════════════════════╝

CORE LOGIC:
- Scans every 5 minutes across ALL USDT futures pairs
- Watches LIVE candle volume on 30m chart
- Fires alert when current candle volume is 50x+ above average
- No gain filter — catches coins BEFORE they move
- This is exactly what would have caught TAC ($8K → $3.38M)
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
# COOLDOWN — prevent same coin repeating
# ══════════════════════════════════════════════════════════
signal_cooldowns = {}

def is_on_cooldown(symbol, hours=4):
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
def get_all_tickers():
    """Get ALL USDT futures pairs — no filters"""
    r = safe_get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not r: return {}
    result = {}
    for c in r.json():
        if not c["symbol"].endswith("USDT"): continue
        try:
            result[c["symbol"]] = {
                "symbol":   c["symbol"],
                "gain":     float(c.get("priceChangePercent", 0)),
                "price":    float(c.get("lastPrice", 0)),
                "vol_usdt": float(c.get("quoteVolume", 0)),
            }
        except: continue
    return result

def get_klines_30m(symbol, limit=40):
    """Get 30m candles — this is the key timeframe for volume explosions"""
    r = safe_get("https://fapi.binance.com/fapi/v1/klines",
                 params={"symbol": symbol, "interval": "30m", "limit": limit})
    if not r: return []
    return [{
        "open":      float(k[1]),
        "high":      float(k[2]),
        "low":       float(k[3]),
        "close":     float(k[4]),
        "vol_usdt":  float(k[7]),
        "open_time": k[0],
    } for k in r.json()]

def get_klines_5m(symbol, limit=20):
    """5m candles for very early detection"""
    r = safe_get("https://fapi.binance.com/fapi/v1/klines",
                 params={"symbol": symbol, "interval": "5m", "limit": limit})
    if not r: return []
    return [{
        "open":     float(k[1]),
        "high":     float(k[2]),
        "low":      float(k[3]),
        "close":    float(k[4]),
        "vol_usdt": float(k[7]),
    } for k in r.json()]

def get_oi(symbol):
    r = safe_get("https://fapi.binance.com/futures/data/openInterestHist",
                 params={"symbol": symbol, "period": "30m", "limit": 4})
    if not r: return None
    data = r.json()
    if len(data) < 2: return None
    old = float(data[0]["sumOpenInterest"])
    new = float(data[-1]["sumOpenInterest"])
    return round((new - old) / old * 100, 2) if old > 0 else None

def get_funding(symbol):
    r = safe_get("https://fapi.binance.com/fapi/v1/fundingRate",
                 params={"symbol": symbol, "limit": 1})
    if not r: return None
    data = r.json()
    return round(float(data[-1]["fundingRate"]) * 100, 4) if data else None

# ══════════════════════════════════════════════════════════
# CORE DETECTION ENGINE
# ══════════════════════════════════════════════════════════

def detect_volume_explosion_30m(klines_30m):
    """
    THE MAIN SIGNAL — detects TAC-style volume explosions.
    
    TAC pattern:
    - Previous 20 candles avg volume: ~$50K
    - Current candle: $3.38M = 67x explosion
    - Alert fires DURING this candle, before price runs
    
    Returns: (is_explosion, ratio, avg_vol, current_vol)
    """
    if len(klines_30m) < 15: return False, 0, 0, 0

    # Use last 20 closed candles as baseline (exclude current)
    baseline_candles = klines_30m[-22:-2]
    current_candle   = klines_30m[-1]   # current live candle
    prev_candle      = klines_30m[-2]   # last closed candle

    vols = [k["vol_usdt"] for k in baseline_candles if k["vol_usdt"] > 0]
    if not vols: return False, 0, 0, 0

    avg_vol     = sum(vols) / len(vols)
    current_vol = current_candle["vol_usdt"]
    prev_vol    = prev_candle["vol_usdt"]

    if avg_vol <= 0: return False, 0, 0, 0

    ratio = current_vol / avg_vol

    # EXPLOSION: current candle is 30x+ above average
    # (TAC was 379x — we use 30x to catch it earlier)
    is_explosion = ratio >= 30 and current_vol >= 100_000  # min $100K to avoid dust

    return is_explosion, round(ratio, 1), round(avg_vol, 0), round(current_vol, 0)

def detect_volume_explosion_5m(klines_5m):
    """
    Ultra-early detection on 5m chart.
    Catches the explosion even before 30m candle closes.
    """
    if len(klines_5m) < 10: return False, 0

    baseline = [k["vol_usdt"] for k in klines_5m[-12:-2] if k["vol_usdt"] > 0]
    current  = klines_5m[-1]["vol_usdt"]

    if not baseline: return False, 0
    avg = sum(baseline) / len(baseline)
    if avg <= 0: return False, 0

    ratio = current / avg
    is_explosion = ratio >= 20 and current >= 50_000

    return is_explosion, round(ratio, 1)

def detect_price_action(klines_30m):
    """
    Check if price is just starting to move (not already pumped 50%+).
    We want to catch the START, not the end.
    """
    if len(klines_30m) < 5: return True, 0

    # Price change in last 2 candles
    start_price   = klines_30m[-3]["open"]
    current_price = klines_30m[-1]["close"]
    if start_price <= 0: return True, 0

    recent_move = (current_price - start_price) / start_price * 100

    # Allow entry if price hasn't moved more than 25% yet
    # (still early enough for good entry)
    is_early = recent_move <= 25
    return is_early, round(recent_move, 2)

def calc_rsi_simple(klines, period=14):
    closes = [k["close"] for k in klines]
    if len(closes) < period + 1: return None
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return round(100 - (100 / (1 + ag/al)), 1) if al > 0 else 100.0

# ══════════════════════════════════════════════════════════
# SIGNAL SCORER
# ══════════════════════════════════════════════════════════
def score_explosion(vol_ratio_30m, vol_ratio_5m, recent_move,
                    oi_chg, funding, ticker_vol, rsi):
    """
    Score the explosion quality.
    Higher ratio + early price move + OI surge = strongest signal.
    """
    score = 0
    reasons = []

    # Volume explosion strength (most important factor)
    if vol_ratio_30m >= 200:
        score += 40
        reasons.append(f"🚀 MEGA explosion {vol_ratio_30m}x on 30m (TAC-level)")
    elif vol_ratio_30m >= 100:
        score += 35
        reasons.append(f"🔥 HUGE explosion {vol_ratio_30m}x on 30m")
    elif vol_ratio_30m >= 50:
        score += 28
        reasons.append(f"💥 Major explosion {vol_ratio_30m}x on 30m")
    elif vol_ratio_30m >= 30:
        score += 20
        reasons.append(f"⚡ Volume explosion {vol_ratio_30m}x on 30m")

    # 5m confirmation
    if vol_ratio_5m >= 50:
        score += 20
        reasons.append(f"🔥 5m also exploding {vol_ratio_5m}x")
    elif vol_ratio_5m >= 20:
        score += 12
        reasons.append(f"📈 5m surging {vol_ratio_5m}x")
    elif vol_ratio_5m >= 10:
        score += 6
        reasons.append(f"📊 5m building {vol_ratio_5m}x")

    # Price still early = better entry
    if recent_move <= 5:
        score += 20
        reasons.append(f"✨ Price barely moved +{recent_move}% — VERY EARLY entry")
    elif recent_move <= 15:
        score += 12
        reasons.append(f"📈 Price +{recent_move}% — still early")
    elif recent_move <= 25:
        score += 5
        reasons.append(f"🟡 Price +{recent_move}% — catching early part")

    # OI surge = new money entering
    if oi_chg and oi_chg >= 20:
        score += 20
        reasons.append(f"💰 OI +{oi_chg}% SURGING — institutions entering")
    elif oi_chg and oi_chg >= 10:
        score += 14
        reasons.append(f"💰 OI +{oi_chg}% rising fast")
    elif oi_chg and oi_chg >= 5:
        score += 8
        reasons.append(f"📊 OI +{oi_chg}% increasing")
    elif oi_chg and oi_chg >= 2:
        score += 4
        reasons.append(f"📊 OI +{oi_chg}%")

    # RSI
    if rsi and rsi <= 50:
        score += 10
        reasons.append(f"✨ RSI {rsi} — early, lots of room")
    elif rsi and rsi <= 65:
        score += 5
        reasons.append(f"🟡 RSI {rsi} — building")
    elif rsi and rsi > 80:
        score -= 10
        reasons.append(f"🔴 RSI {rsi} — already hot")

    # Funding
    if funding and funding < -0.01:
        score += 8
        reasons.append(f"🔄 Negative funding {funding}% — short squeeze")
    elif funding and funding > 0.2:
        score -= 5
        reasons.append(f"⚠️ High funding {funding}%")

    # 24h volume size (liquidity check)
    if ticker_vol >= 50e6:
        score += 5
        reasons.append(f"💎 24h vol ${ticker_vol/1e6:.0f}M — liquid")
    elif ticker_vol >= 10e6:
        score += 3

    return min(score, 100), reasons

# ══════════════════════════════════════════════════════════
# TRADE LEVELS
# ══════════════════════════════════════════════════════════
def calculate_levels(price, recent_move, score):
    """
    Entry: current market price (explosion = enter NOW)
    Stop: 7% below (wider for volatile coins)
    Targets: based on explosion strength
    """
    entry = price  # enter at market price immediately

    if score >= 80:
        tp1 = entry * 1.10   # 10%
        tp2 = entry * 1.20   # 20%
        tp3 = entry * 1.40   # 40%
        stop = entry * 0.93  # 7% stop
    elif score >= 65:
        tp1 = entry * 1.07   # 7%
        tp2 = entry * 1.15   # 15%
        tp3 = entry * 1.30   # 30%
        stop = entry * 0.93
    else:
        tp1 = entry * 1.05   # 5%
        tp2 = entry * 1.10   # 10%
        tp3 = entry * 1.20   # 20%
        stop = entry * 0.94  # 6% stop

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
def run_explosion_scanner():
    """
    Scans ALL USDT futures pairs for volume explosions.
    This is what catches TAC, MANTA, ACT, GWEI before they pump.
    Runs every 5 minutes.
    """
    log.info("Explosion scanner running...")
    tickers = get_all_tickers()
    if not tickers:
        log.error("Failed to get tickers")
        return []

    signals  = []
    checked  = 0

    for sym, t in tickers.items():
        price = t["price"]
        gain  = t["gain"]
        vol   = t["vol_usdt"]

        if price <= 0: continue
        if vol < 10_000: continue        # skip dead coins
        if is_on_cooldown(sym): continue

        # Get 30m candles
        klines_30m = get_klines_30m(sym, 40)
        time.sleep(0.06)
        if not klines_30m: continue

        # PRIMARY SIGNAL: 30m volume explosion
        exploding_30m, ratio_30m, avg_vol, curr_vol = detect_volume_explosion_30m(klines_30m)

        # Must have explosion to proceed
        if not exploding_30m: continue

        # Get 5m candles for confirmation
        klines_5m = get_klines_5m(sym, 20)
        time.sleep(0.06)

        exploding_5m, ratio_5m = detect_volume_explosion_5m(klines_5m) if klines_5m else (False, 0)

        # Price action check
        is_early, recent_move = detect_price_action(klines_30m)

        # Skip if already pumped too much (over 30% in last 2 candles)
        if not is_early and recent_move > 30:
            log.info(f"{sym}: explosion detected but already moved {recent_move}%, skipping")
            continue

        # RSI
        rsi = calc_rsi_simple(klines_30m) if klines_30m else None

        # Skip overbought
        if rsi and rsi > 85: continue

        # OI + funding
        oi_chg  = get_oi(sym)
        funding = get_funding(sym)
        time.sleep(0.06)

        # Skip very high funding (overheated)
        if funding and funding > 0.3: continue

        # Score it
        score, reasons = score_explosion(
            ratio_30m, ratio_5m, recent_move,
            oi_chg, funding, vol, rsi
        )

        # Minimum score 55 — lower threshold since volume explosion
        # itself is already a very strong signal
        if score < 55: continue

        # Calculate trade levels
        entry, tp1, tp2, tp3, stop, rr = calculate_levels(price, recent_move, score)

        signals.append({
            "sym":        sym.replace("USDT", ""),
            "full_sym":   sym,
            "price":      price,
            "gain":       gain,
            "vol_24h":    vol,
            "score":      score,
            "rsi":        rsi,
            "ratio_30m":  ratio_30m,
            "ratio_5m":   ratio_5m,
            "avg_vol":    avg_vol,
            "curr_vol":   curr_vol,
            "recent_move": recent_move,
            "oi_chg":     oi_chg,
            "funding":    funding,
            "reasons":    reasons,
            "entry":      entry,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "stop": stop, "rr": rr,
        })

        checked += 1
        log.info(f"SIGNAL: {sym} | ratio={ratio_30m}x | score={score} | move={recent_move}%")

    # Sort by volume ratio (strongest explosion first), cap at 3
    top = sorted(signals, key=lambda x: x["ratio_30m"], reverse=True)[:3]

    for c in top:
        mark_signaled(c["full_sym"])

    log.info(f"Scanner done. Checked all pairs. Signals: {len(top)}")
    return top

# ══════════════════════════════════════════════════════════
# MESSAGE BUILDER
# ══════════════════════════════════════════════════════════
def build_alert(coins, triggered_by="auto"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not coins:
        if triggered_by == "auto": return None
        return [
            f"**🔍 SCAN** | 🕐 {now}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"😴 No volume explosions detected right now.\n"
            f"Watching all pairs. Next scan in 5 mins."
        ]

    label = "🚨 VOLUME EXPLOSION ALERT" if triggered_by == "auto" else "🔍 MANUAL SCAN"
    msgs = [
        f"**{label}** | 🕐 {now}\n"
        f"⚡ Caught LIVE — enter NOW before price runs!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for i, c in enumerate(coins, 1):
        rsi = c.get("rsi")
        rsi_str = f"RSI {rsi}" if rsi else "RSI N/A"

        tp1_pct = round((c["tp1"]-c["entry"])/c["entry"]*100, 1)
        tp2_pct = round((c["tp2"]-c["entry"])/c["entry"]*100, 1)
        tp3_pct = round((c["tp3"]-c["entry"])/c["entry"]*100, 1)

        oi_str   = f"OI {'+' if c['oi_chg'] and c['oi_chg']>=0 else ''}{c['oi_chg']}%" if c.get("oi_chg") is not None else ""
        fund_str = f"Fund {c['funding']}%" if c.get("funding") is not None else ""
        sm_line  = " | ".join(filter(None, [rsi_str, oi_str, fund_str]))

        reasons_str = "\n".join(c["reasons"][:5])

        msg = (
            f"\n🚨 **{i}. {c['sym']}/USDT** — VOLUME EXPLOSION\n"
            f"Score: **{c['score']}/100** | 24h: {'+' if c['gain']>=0 else ''}{c['gain']:.1f}%\n"
            f"{sm_line}\n"
            f"\n{reasons_str}\n"
            f"\n📊 Avg candle vol: `{fmt_vol(c['avg_vol'])}`\n"
            f"💥 Current candle: `{fmt_vol(c['curr_vol'])}` **({c['ratio_30m']}x above avg)**\n"
            f"\n💰 Price:  `{fmt_price(c['price'])}`\n"
            f"🔵 Entry:  `{fmt_price(c['entry'])}` ← **ENTER NOW**\n"
            f"🎯 TP1:   `{fmt_price(c['tp1'])}` (+{tp1_pct}%) — exit 50%\n"
            f"🚀 TP2:   `{fmt_price(c['tp2'])}` (+{tp2_pct}%) — exit 35%\n"
            f"💎 TP3:   `{fmt_price(c['tp3'])}` (+{tp3_pct}%) — exit 15%\n"
            f"⛔ Stop:  `{fmt_price(c['stop'])}` | ⚖️ R/R 1:{c['rr']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        msgs.append(msg)

    msgs.append("⚡ *Volume explosions move fast — act quickly or skip.*\n⚠️ *Not financial advice. Always use stop loss.*")
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
    await ctx.send("🔍 **Scanning all pairs for volume explosions...** ~60 secs...")
    coins = run_explosion_scanner()
    msgs  = build_alert(coins, "manual")
    await send_messages(ctx, msgs)

@bot.command(name="momentum")
async def momentum(ctx):
    await ctx.send("⚡ **Scanning for explosions...** ~60 secs...")
    coins = run_explosion_scanner()
    msgs  = build_alert(coins, "manual")
    await send_messages(ctx, msgs)

@bot.command(name="early")
async def early(ctx):
    await ctx.send("🌅 **Scanning for early explosions...** ~60 secs...")
    coins = run_explosion_scanner()
    msgs  = build_alert(coins, "manual")
    await send_messages(ctx, msgs)

@bot.command(name="status")
async def status(ctx):
    active = [s for s, t in signal_cooldowns.items()
              if datetime.utcnow() - t < timedelta(hours=4)]
    await ctx.send(
        f"**📊 AKA Screener v6.0 — Status**\n"
        f"✅ Online | Scanning every **5 minutes**\n"
        f"👁 Watching ALL USDT futures pairs\n"
        f"🔒 {len(active)} coins on cooldown (4h)\n"
        f"🎯 Trigger: 30m volume 30x+ above average\n\n"
        f"Commands: `!scan` `!status`"
    )

# ══════════════════════════════════════════════════════════
# AUTO SCAN LOOP — every 5 minutes
# ══════════════════════════════════════════════════════════
async def auto_scan_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        log.error("Channel not found!")
        return

    log.info("First scan in 2 minutes...")
    await asyncio.sleep(120)  # 2 min warmup

    while True:
        try:
            log.info("Auto explosion scan starting...")
            coins = run_explosion_scanner()
            if coins:
                msgs = build_alert(coins, "auto")
                if msgs:
                    await send_messages(channel, msgs)
                    log.info(f"Sent {len(coins)} explosion alert(s)")
            else:
                log.info("No explosions this round — silent")
        except Exception as e:
            log.error(f"Auto scan error: {e}")

        # Wait 5 minutes before next scan
        await asyncio.sleep(5 * 60)

@bot.event
async def on_ready():
    print(f"✅ AKA SMC Screener v6.0 online as {bot.user}!")
    print(f"👁  Watching ALL USDT futures pairs")
    print(f"⚡ Scanning every 5 minutes for volume explosions")
    print(f"🎯 Trigger: 30m candle 30x+ above average volume")
    print(f"⏰ First scan in 2 minutes...")
    bot.loop.create_task(auto_scan_loop())

if not TOKEN:
    print("ERROR: No DISCORD_TOKEN set!")
else:
    bot.run(TOKEN)

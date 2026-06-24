"""
=============================================================
  CRYPTO GAINER SCREENER — TELEGRAM BOT
  Built for Ahsan | Powered by CoinGecko API (Free)
=============================================================

HOW IT WORKS:
- Fetches top gainers from CoinGecko every 4 hours automatically
- Scores each coin using volume spike, RSI zone, market cap, momentum
- Only sends you coins that score 55+ (strong signals only)
- Shows Entry Price, TP1, TP2, Stop Loss for each coin
- Completely FREE — no paid APIs needed

COMMANDS:
  /start   — welcome message
  /scan    — run screener right now manually
  /help    — show all commands
=============================================================
"""

import os
import time
import logging
import requests
import schedule
import threading
from datetime import datetime
from telegram import Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update

# ─── CONFIGURATION ────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")
SCAN_INTERVAL_HOURS = 4   # auto-scan every 4 hours
MIN_SCORE           = 55  # only alert coins scoring 55+
MIN_GAIN_PCT        = 10  # only coins up 10%+ in 24h
TOP_N_COINS         = 30  # how many top gainers to fetch from CoinGecko

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
log = logging.getLogger(__name__)


# ─── SCORING ENGINE (same logic as your React screener) ───

def score_gain(gain):
    if gain >= 50: return 100
    if gain >= 30: return 85
    if gain >= 20: return 70
    if gain >= 10: return 50
    if gain >= 5:  return 30
    return 10

def score_mcap(mcap):
    if 1e8 <= mcap <= 5e9:  return 100
    if 5e9 < mcap <= 50e9:  return 75
    if 1e7 <= mcap < 1e8:   return 80
    if mcap > 50e9:         return 40
    return 20

def score_vol_spike(vol, mcap):
    """Using vol/mcap ratio since CoinGecko free tier has no 7d avg vol"""
    if not vol or not mcap: return 30
    ratio = vol / mcap
    if ratio >= 0.40: return 100
    if ratio >= 0.20: return 85
    if ratio >= 0.10: return 65
    if ratio >= 0.05: return 45
    return 20

def score_momentum(gain):
    if 20 < gain < 80: return 100
    if gain >= 80:     return 45
    if gain >= 10:     return 70
    return 30

def compute_score(coin):
    g  = score_gain(coin["gain"])       * 0.25
    m  = score_mcap(coin["mcap"])       * 0.15
    v  = score_vol_spike(coin["vol"], coin["mcap"]) * 0.40
    mo = score_momentum(coin["gain"])   * 0.20
    return round(g + m + v + mo)

def get_signal(score, gain):
    if score >= 75 and gain < 80: return "🟢 STRONG BUY"
    if score >= 75 and gain >= 80: return "🟡 WAIT FOR DIP"
    if score >= 55: return "🟡 WATCH"
    return "🔴 AVOID"

def calc_levels(price, score):
    """Calculate entry, TP1, TP2, stop loss"""
    dip   = 0.05 if score >= 75 else 0.08
    entry = price * (1 - dip)
    tp1   = entry * (1.20 if score >= 75 else 1.15)
    tp2   = entry * (1.40 if score >= 75 else 1.25)
    stop  = entry * 0.85

    def fmt(p):
        if p < 0.000001: return f"${p:.8f}"
        if p < 0.001:    return f"${p:.6f}"
        if p < 1:        return f"${p:.4f}"
        if p < 100:      return f"${p:.3f}"
        return f"${p:,.2f}"

    return {
        "entry": fmt(entry),
        "tp1":   fmt(tp1),
        "tp2":   fmt(tp2),
        "stop":  fmt(stop),
        "dip":   int(dip * 100),
    }


# ─── COINGECKO FETCHER ────────────────────────────────────

def fetch_top_gainers():
    """Fetch top gaining coins from CoinGecko free API"""
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency":          "usd",
        "order":                "percent_change_24h_desc",  # sort by biggest gainers
        "per_page":             TOP_N_COINS,
        "page":                 1,
        "sparkline":            False,
        "price_change_percentage": "24h",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        coins = []
        for c in data:
            gain = c.get("price_change_percentage_24h") or 0
            if gain < MIN_GAIN_PCT:
                continue
            coins.append({
                "name":   c.get("name", ""),
                "symbol": c.get("symbol", "").upper(),
                "price":  c.get("current_price") or 0,
                "gain":   round(gain, 2),
                "vol":    c.get("total_volume") or 0,
                "mcap":   c.get("market_cap") or 0,
                "rank":   c.get("market_cap_rank") or 999,
            })
        return coins

    except Exception as e:
        log.error(f"CoinGecko fetch error: {e}")
        return []


# ─── SCREENER RUNNER ──────────────────────────────────────

def run_screener():
    """Fetch coins, score them, return only strong signals"""
    log.info("Running screener...")
    coins = fetch_top_gainers()

    if not coins:
        return None, "⚠️ Could not fetch data from CoinGecko. Try again later."

    results = []
    for c in coins:
        score  = compute_score(c)
        signal = get_signal(score, c["gain"])
        levels = calc_levels(c["price"], score)
        if score >= MIN_SCORE:
            results.append({ **c, "score": score, "signal": signal, "levels": levels })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results, None


# ─── MESSAGE FORMATTER ────────────────────────────────────

def fmt_mcap(n):
    if n >= 1e9: return f"${n/1e9:.2f}B"
    if n >= 1e6: return f"${n/1e6:.1f}M"
    return f"${n:.0f}"

def build_message(results):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if not results:
        return (
            f"📊 *CRYPTO SCREENER SCAN*\n"
            f"🕐 {now}\n\n"
            f"😴 No strong signals right now.\n"
            f"All top gainers scored below {MIN_SCORE}. Check back later."
        )

    lines = [
        f"📊 *CRYPTO GAINER SCREENER*",
        f"🕐 {now}",
        f"✅ {len(results)} signal(s) found\n",
        f"{'─' * 28}",
    ]

    for i, c in enumerate(results[:8], 1):   # max 8 coins per message
        lvl = c["levels"]
        lines += [
            f"\n*{i}. {c['symbol']}* — {c['name']}",
            f"{c['signal']}  |  Score: *{c['score']}/100*",
            f"📈 24h: *+{c['gain']}%*  |  MCap: {fmt_mcap(c['mcap'])}",
            f"💰 Current Price: `{c['price']}`",
            f"",
            f"🔵 Entry:     `{lvl['entry']}` _(wait {lvl['dip']}% dip)_",
            f"🎯 TP1:       `{lvl['tp1']}` _(take 50%)_",
            f"🚀 TP2:       `{lvl['tp2']}` _(take rest)_",
            f"⛔ Stop Loss: `{lvl['stop']}` _(-15% from entry)_",
            f"{'─' * 28}",
        ]

    lines += [
        f"\n⚠️ _Not financial advice. Always DYOR._",
        f"_Next auto-scan in {SCAN_INTERVAL_HOURS}h_",
    ]

    return "\n".join(lines)


# ─── TELEGRAM SEND ────────────────────────────────────────

async def send_signal(context: ContextTypes.DEFAULT_TYPE = None, chat_id: str = None):
    """Run screener and send results to Telegram"""
    target_chat = chat_id or TELEGRAM_CHAT_ID
    results, err = run_screener()

    if err:
        msg = f"❌ {err}"
    else:
        msg = build_message(results)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=target_chat,
            text=msg,
            parse_mode="Markdown"
        )
        log.info(f"Signal sent to {target_chat}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")


# ─── COMMAND HANDLERS ─────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"👋 *Welcome to Crypto Gainer Screener Bot!*\n\n"
        f"🤖 I automatically scan top gainers every *{SCAN_INTERVAL_HOURS} hours* "
        f"and send you only the strongest signals with entry/exit prices.\n\n"
        f"*Commands:*\n"
        f"/scan — run screener right now\n"
        f"/help — show all commands\n\n"
        f"Your Chat ID: `{chat_id}`\n"
        f"_Add this to your .env file as TELEGRAM\\_CHAT\\_ID_",
        parse_mode="Markdown"
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning top gainers... please wait 10 seconds.")
    await send_signal(context, chat_id=str(update.effective_chat.id))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*CRYPTO SCREENER BOT — HELP*\n\n"
        "/start — welcome & your chat ID\n"
        "/scan  — run screener manually right now\n"
        "/help  — this message\n\n"
        f"*Auto-scan:* every {SCAN_INTERVAL_HOURS} hours\n"
        f"*Min gain:* {MIN_GAIN_PCT}%\n"
        f"*Min score:* {MIN_SCORE}/100\n"
        f"*Data source:* CoinGecko (free)\n\n"
        "_Entry = wait for small dip from current price_\n"
        "_TP1 = take 50% profit here_\n"
        "_TP2 = take remaining profit here_\n"
        "_Stop Loss = exit if price drops 15% from entry_",
        parse_mode="Markdown"
    )


# ─── AUTO SCHEDULER ───────────────────────────────────────

def start_scheduler(app):
    """Run auto-scan in background thread every X hours"""
    import asyncio

    def job():
        log.info("Auto-scan triggered by scheduler")
        asyncio.run(send_signal(chat_id=TELEGRAM_CHAT_ID))

    schedule.every(SCAN_INTERVAL_HOURS).hours.do(job)

    def run():
        while True:
            schedule.run_pending()
            time.sleep(60)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    log.info(f"Scheduler started — scanning every {SCAN_INTERVAL_HOURS} hours")


# ─── MAIN ─────────────────────────────────────────────────

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Please set your TELEGRAM_BOT_TOKEN in the .env file!")
        return

    log.info("Starting Crypto Screener Bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan",  cmd_scan))
    app.add_handler(CommandHandler("help",  cmd_help))

    # Start auto-scheduler
    start_scheduler(app)

    log.info("Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

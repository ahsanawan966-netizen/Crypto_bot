"""
CRYPTO GAINER SCREENER — TELEGRAM BOT
Compatible with Render.com free tier
"""

import os
import time
import logging
import requests
import threading
import schedule
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
import asyncio

# ── CONFIG ──────────────────────────────────────────────
TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL_HOURS = 4
MIN_SCORE = 55
MIN_GAIN  = 10

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── KEEP ALIVE WEB SERVER ───────────────────────────────
# Render requires a web server to keep the service alive

class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Crypto Screener Bot is running!")
    def log_message(self, format, *args):
        pass  # silence web server logs

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAlive)
    log.info(f"Web server running on port {port}")
    server.serve_forever()

# ── SCORING ─────────────────────────────────────────────

def score_coin(gain, vol, mcap):
    if gain >= 50:   gs = 100
    elif gain >= 30: gs = 85
    elif gain >= 20: gs = 70
    elif gain >= 10: gs = 50
    else:            gs = 20

    if 1e8 <= mcap <= 5e9:   ms = 100
    elif 5e9 < mcap <= 50e9: ms = 75
    elif 1e7 <= mcap < 1e8:  ms = 80
    elif mcap > 50e9:        ms = 40
    else:                    ms = 20

    ratio = vol / mcap if mcap > 0 else 0
    if ratio >= 0.4:    vs = 100
    elif ratio >= 0.2:  vs = 85
    elif ratio >= 0.1:  vs = 65
    elif ratio >= 0.05: vs = 45
    else:               vs = 20

    if 20 < gain < 80: mo = 100
    elif gain >= 80:   mo = 45
    elif gain >= 10:   mo = 70
    else:              mo = 30

    return round(gs*0.25 + ms*0.15 + vs*0.40 + mo*0.20)

def fmt_price(p):
    if p < 0.000001: return f"${p:.8f}"
    if p < 0.001:    return f"${p:.6f}"
    if p < 1:        return f"${p:.4f}"
    if p < 100:      return f"${p:.3f}"
    return f"${p:,.2f}"

def fmt_mcap(n):
    if n >= 1e9: return f"${n/1e9:.2f}B"
    if n >= 1e6: return f"${n/1e6:.1f}M"
    return f"${n:.0f}"

def calc_levels(price, score):
    dip   = 0.05 if score >= 75 else 0.08
    entry = price * (1 - dip)
    tp1   = entry * (1.20 if score >= 75 else 1.15)
    tp2   = entry * (1.40 if score >= 75 else 1.25)
    stop  = entry * 0.85
    return fmt_price(entry), fmt_price(tp1), fmt_price(tp2), fmt_price(stop), int(dip*100)

# ── COINGECKO ───────────────────────────────────────────

def fetch_gainers():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "percent_change_24h_desc",
        "per_page": 30,
        "page": 1,
        "sparkline": False,
        "price_change_percentage": "24h",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        results = []
        for c in r.json():
            gain = c.get("price_change_percentage_24h") or 0
            if gain < MIN_GAIN:
                continue
            score = score_coin(gain, c.get("total_volume") or 0, c.get("market_cap") or 0)
            if score < MIN_SCORE:
                continue
            price = c.get("current_price") or 0
            entry, tp1, tp2, stop, dip = calc_levels(price, score)
            results.append({
                "name":   c.get("name", ""),
                "symbol": c.get("symbol", "").upper(),
                "price":  fmt_price(price),
                "gain":   round(gain, 2),
                "mcap":   fmt_mcap(c.get("market_cap") or 0),
                "score":  score,
                "signal": "🟢 STRONG BUY" if score >= 75 else "🟡 WATCH",
                "entry":  entry,
                "tp1":    tp1,
                "tp2":    tp2,
                "stop":   stop,
                "dip":    dip,
            })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results
    except Exception as e:
        log.error(f"CoinGecko error: {e}")
        return None

# ── MESSAGE ─────────────────────────────────────────────

def build_message(coins):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if coins is None:
        return f"⚠️ Could not fetch data. Try again.\n🕐 {now}"
    if not coins:
        return f"😴 No strong signals right now.\n🕐 {now}"

    lines = [f"📊 *CRYPTO SCREENER*", f"🕐 {now}", f"✅ {len(coins)} signal(s)\n{'─'*26}"]
    for i, c in enumerate(coins[:8], 1):
        lines += [
            f"\n*{i}. {c['symbol']}* — {c['name']}",
            f"{c['signal']} | Score: *{c['score']}/100*",
            f"📈 *+{c['gain']}%* | MCap: {c['mcap']}",
            f"💰 Price: `{c['price']}`",
            f"🔵 Entry: `{c['entry']}` _(−{c['dip']}% dip)_",
            f"🎯 TP1:   `{c['tp1']}` _(take 50%)_",
            f"🚀 TP2:   `{c['tp2']}` _(take rest)_",
            f"⛔ Stop:  `{c['stop']}` _(−15%)_",
            f"{'─'*26}",
        ]
    lines.append("\n⚠️ _Not financial advice. DYOR._")
    return "\n".join(lines)

# ── COMMANDS ────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 *Crypto Screener Bot*\n\n"
        f"Auto-scans top gainers every *{INTERVAL_HOURS}h*\n\n"
        f"/scan — run now\n/help — commands\n\n"
        f"Your Chat ID: `{update.effective_chat.id}`",
        parse_mode="Markdown"
    )

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning... please wait.")
    coins = fetch_gainers()
    msg = build_message(coins)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands:*\n/scan — run screener now\n/help — this message\n\n"
        f"Auto-scan every {INTERVAL_HOURS}h\n"
        f"Min gain: {MIN_GAIN}% | Min score: {MIN_SCORE}/100",
        parse_mode="Markdown"
    )

# ── AUTO SCAN ───────────────────────────────────────────

async def auto_scan():
    if not CHAT_ID:
        return
    coins = fetch_gainers()
    msg = build_message(coins)
    bot = Bot(token=TOKEN)
    try:
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        log.info("Auto scan sent!")
    except Exception as e:
        log.error(f"Send error: {e}")

def run_scheduler():
    def job():
        asyncio.run(auto_scan())
    schedule.every(INTERVAL_HOURS).hours.do(job)
    while True:
        schedule.run_pending()
        time.sleep(60)

# ── MAIN ────────────────────────────────────────────────

def main():
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set!")
        return

    log.info("Starting bot...")

    # Start web server in background (required for Render)
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # Start scheduler in background
    sched_thread = threading.Thread(target=run_scheduler, daemon=True)
    sched_thread.start()

    # Start Telegram bot
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan",  cmd_scan))
    app.add_handler(CommandHandler("help",  cmd_help))

    log.info("Bot running!")
    app.run_polling()

if __name__ == "__main__":
    main()

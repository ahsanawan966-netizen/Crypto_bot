"""
CRYPTO GAINER SCREENER — TELEGRAM BOT
Compatible with Python 3.14 + Render.com
"""

import os
import time
import logging
import requests
import threading
import schedule
import asyncio
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# Use older compatible import style
import telegram
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL_HOURS = 4
MIN_SCORE = 55
MIN_GAIN  = 10

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── KEEP ALIVE ──────────────────────────────────────────
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, *args):
        pass

def run_web():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), KeepAlive).serve_forever()

# ── SCORING ─────────────────────────────────────────────
def score_coin(gain, vol, mcap):
    gs = 100 if gain>=50 else 85 if gain>=30 else 70 if gain>=20 else 50 if gain>=10 else 20
    ms = 100 if 1e8<=mcap<=5e9 else 75 if mcap<=50e9 else 80 if mcap>=1e7 else 40 if mcap>50e9 else 20
    r  = vol/mcap if mcap>0 else 0
    vs = 100 if r>=0.4 else 85 if r>=0.2 else 65 if r>=0.1 else 45 if r>=0.05 else 20
    mo = 100 if 20<gain<80 else 45 if gain>=80 else 70 if gain>=10 else 30
    return round(gs*0.25 + ms*0.15 + vs*0.40 + mo*0.20)

def fmt(p):
    if not p: return "$0"
    if p<0.000001: return f"${p:.8f}"
    if p<0.001: return f"${p:.6f}"
    if p<1: return f"${p:.4f}"
    if p<100: return f"${p:.3f}"
    return f"${p:,.2f}"

def fmt_mcap(n):
    if n>=1e9: return f"${n/1e9:.2f}B"
    if n>=1e6: return f"${n/1e6:.1f}M"
    return f"${n:.0f}"

def levels(price, score):
    dip = 0.05 if score>=75 else 0.08
    e = price*(1-dip)
    return fmt(e), fmt(e*(1.20 if score>=75 else 1.15)), fmt(e*(1.40 if score>=75 else 1.25)), fmt(e*0.85), int(dip*100)

# ── FETCH ───────────────────────────────────────────────
def fetch():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets", params={
            "vs_currency":"usd","order":"percent_change_24h_desc",
            "per_page":30,"page":1,"sparkline":False,
            "price_change_percentage":"24h"
        }, timeout=20)
        r.raise_for_status()
        out = []
        for c in r.json():
            gain = c.get("price_change_percentage_24h") or 0
            if gain < MIN_GAIN: continue
            sc = score_coin(gain, c.get("total_volume") or 0, c.get("market_cap") or 0)
            if sc < MIN_SCORE: continue
            e, t1, t2, sl, dip = levels(c.get("current_price") or 0, sc)
            out.append({
                "sym": c.get("symbol","").upper(), "name": c.get("name",""),
                "price": fmt(c.get("current_price") or 0),
                "gain": round(gain,2), "mcap": fmt_mcap(c.get("market_cap") or 0),
                "score": sc, "sig": "🟢 STRONG BUY" if sc>=75 else "🟡 WATCH",
                "e":e,"t1":t1,"t2":t2,"sl":sl,"dip":dip
            })
        return sorted(out, key=lambda x: x["score"], reverse=True)
    except Exception as ex:
        log.error(f"Fetch error: {ex}")
        return None

# ── MESSAGE ─────────────────────────────────────────────
def make_msg(coins):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if coins is None: return f"⚠️ Data fetch failed. Try again.\n🕐 {now}"
    if not coins: return f"😴 No strong signals now.\n🕐 {now}"
    lines = [f"📊 *CRYPTO SCREENER*", f"🕐 {now}", f"✅ {len(coins)} signal(s)\n{'─'*24}"]
    for i,c in enumerate(coins[:8],1):
        lines += [
            f"\n*{i}. {c['sym']}* — {c['name']}",
            f"{c['sig']} | Score: *{c['score']}/100*",
            f"📈 *+{c['gain']}%* | MCap: {c['mcap']}",
            f"💰 `{c['price']}`",
            f"🔵 Entry: `{c['e']}` _(−{c['dip']}% dip)_",
            f"🎯 TP1: `{c['t1']}` _(50%)_",
            f"🚀 TP2: `{c['t2']}` _(rest)_",
            f"⛔ Stop: `{c['sl']}` _(−15%)_",
            f"{'─'*24}",
        ]
    lines.append("\n⚠️ _Not financial advice. DYOR._")
    return "\n".join(lines)

# ── COMMANDS ────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 *Crypto Screener Bot*\n\nAuto-scans every *{INTERVAL_HOURS}h*\n\n/scan — run now\n/help — help\n\nYour ID: `{update.effective_chat.id}`",
        parse_mode="Markdown")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Scanning...")
    await update.message.reply_text(make_msg(fetch()), parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"*Commands:*\n/scan — run now\n/help — this\n\nEvery {INTERVAL_HOURS}h auto-scan\nMin gain: {MIN_GAIN}% | Min score: {MIN_SCORE}",
        parse_mode="Markdown")

# ── AUTO SCAN ───────────────────────────────────────────
async def auto_send():
    if not CHAT_ID: return
    try:
        bot = telegram.Bot(token=TOKEN)
        await bot.send_message(chat_id=CHAT_ID, text=make_msg(fetch()), parse_mode="Markdown")
        log.info("Auto scan sent!")
    except Exception as ex:
        log.error(f"Auto send error: {ex}")

def scheduler():
    schedule.every(INTERVAL_HOURS).hours.do(lambda: asyncio.run(auto_send()))
    while True:
        schedule.run_pending()
        time.sleep(60)

# ── MAIN ────────────────────────────────────────────────
def main():
    if not TOKEN:
        log.error("No token!")
        return

    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("help", cmd_help))
    log.info("Bot polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

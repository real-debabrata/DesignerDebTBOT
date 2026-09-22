"""
Telegram "Join + Refer to Unlock" bot
--------------------------------------
Flow:
1. User starts the bot (optionally via a referral link).
2. Bot shows "Join Channel" + "Verify" buttons.
3. On Verify, bot checks channel membership via the Telegram API.
4. Once joined, bot checks the user has referred enough friends
   (friends must also join + verify to count).
5. Once both conditions are met, the bot delivers the service
   (a text message, a link, or a file) directly in the chat.

Beginner notes:
- All secrets/config live in a .env file (see .env.example). Never put your
  bot token directly in this file if you plan to share/upload the code anywhere.
- Data is stored in a local SQLite file (bot_data.db) that is created automatically.
"""

import os
import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import sqlite3
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------
# Config (loaded from .env)
# --------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")                 # e.g. -1001234567890
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK")  # https://t.me/yourchannel or invite link
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
REQUIRED_REFERRALS = int(os.getenv("REQUIRED_REFERRALS", "2"))

SERVICE_TYPE = os.getenv("SERVICE_TYPE", "text")     # "text" or "file"  (legacy single-item fallback)
SERVICE_TEXT = os.getenv("SERVICE_TEXT", "Here is your service/link!")
SERVICE_FILE_PATH = os.getenv("SERVICE_FILE_PATH", "")

DB_PATH = "bot_data.db"


def parse_services():
    """
    Reads SERVICES from .env and turns it into a list of buttons.
    Format: Label|type|value ; Label|type|value ; ...
    type is one of: url, file, code, text

    Example:
      SERVICES=🔗 Premium Link|url|https://t.me/+abc123;📄 Bonus PDF|file|bonus.pdf;🎟 Promo Code|code|SAVE20NOW
    """
    raw = os.getenv("SERVICES", "")
    items = []
    for i, chunk in enumerate(raw.split(";")):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 3:
            logger.warning("Skipping malformed SERVICES entry: %r", chunk)
            continue
        label, s_type, value = parts
        items.append({"id": f"svc{i}", "label": label, "type": s_type, "value": value})
    return items


SERVICES = parse_services()

if not BOT_TOKEN or not CHANNEL_ID or not CHANNEL_INVITE_LINK:
    raise SystemExit(
        "Missing config. Make sure BOT_TOKEN, CHANNEL_ID and CHANNEL_INVITE_LINK "
        "are set in your .env file."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Tiny keep-alive web server
# (Only needed for free hosts like Render that require an open HTTP port.
#  Harmless to leave running even when you don't need it.)
# --------------------------------------------------------------------------
def start_keep_alive_server():
    port = int(os.environ.get("PORT", 8080))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is alive")

        def log_message(self, fmt, *args):
            pass  # keep the console clean

    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            referred_by INTEGER,
            referral_count INTEGER DEFAULT 0,
            verified_join INTEGER DEFAULT 0,
            got_service INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row  # (user_id, username, referred_by, referral_count, verified_join, got_service)


def add_user(user_id: int, username: str, referred_by: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, referred_by) VALUES (?, ?, ?)",
        (user_id, username, referred_by),
    )
    conn.commit()
    conn.close()


def set_verified(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET verified_join=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def increment_referral(referrer_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?",
        (referrer_id,),
    )
    conn.commit()
    conn.close()


def set_got_service(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET got_service=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------
def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_INVITE_LINK)],
            [InlineKeyboardButton("✅ Verify", callback_data="verify")],
        ]
    )


def recheck_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Check Again", callback_data="verify")]]
    )


def service_keyboard():
    """One tappable button per item in SERVICES. 'url' buttons open instantly;
    'file'/'code'/'text' buttons trigger the bot to send that item on tap."""
    rows = []
    for svc in SERVICES:
        if svc["type"] == "url":
            rows.append([InlineKeyboardButton(svc["label"], url=svc["value"])])
        else:
            rows.append(
                [InlineKeyboardButton(svc["label"], callback_data=f"get_{svc['id']}")]
            )
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    referred_by = None
    if args:
        payload = args[0]
        if payload.startswith("ref") and payload[3:].isdigit():
            candidate = int(payload[3:])
            if candidate != user.id:
                referred_by = candidate

    if get_user(user.id) is None:
        add_user(user.id, user.username or user.first_name, referred_by)

    text = (
        f"👋 Hey {user.first_name}!\n\n"
        f"To unlock the service, please:\n"
        f"1️⃣ Join our channel\n"
        f"2️⃣ Refer at least {REQUIRED_REFERRALS} friends (they must join & verify too)\n\n"
        f"Tap Join, then tap Verify 👇"
    )
    await update.message.reply_text(text, reply_markup=join_keyboard())


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    # 1. Check channel membership
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user.id)
        joined = member.status in ("member", "administrator", "creator")
    except TelegramError as e:
        logger.error("get_chat_member failed: %s", e)
        await query.answer(
            "⚠️ Couldn't verify right now. Make sure the bot is an admin "
            "of the channel, then try again.",
            show_alert=True,
        )
        return

    if not joined:
        await query.answer("❌ You haven't joined the channel yet!", show_alert=True)
        return

    # 2. Make sure the user exists in the DB (in case they clicked an old button)
    row = get_user(user.id)
    if row is None:
        add_user(user.id, user.username or user.first_name)
        row = get_user(user.id)

    _, _, referred_by, _, was_verified, _ = row

    # 3. First time verifying? Credit whoever referred them.
    if not was_verified:
        set_verified(user.id)
        if referred_by:
            increment_referral(referred_by)
            try:
                await context.bot.send_message(
                    referred_by,
                    "🎉 One of your referrals just joined and verified! "
                    "Keep sharing your link.",
                )
            except TelegramError:
                pass  # referrer may have blocked the bot

    # 4. Re-read fresh referral count and decide what to show
    row = get_user(user.id)
    _, _, _, referral_count, _, got_service = row

    if referral_count >= REQUIRED_REFERRALS:
        await query.edit_message_text(
            f"✅ Verified! You referred {referral_count}/{REQUIRED_REFERRALS} friends.\n\n"
            f"Delivering your service now… 👇"
        )
        if not got_service:
            set_got_service(user.id)
        await deliver_service(context, user.id)
    else:
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref{user.id}"
        text = (
            f"✅ Channel join verified!\n\n"
            f"👥 Referrals: {referral_count}/{REQUIRED_REFERRALS}\n\n"
            f"Share your personal link with friends, then tap Check Again:\n{ref_link}"
        )
        await query.edit_message_text(text, reply_markup=recheck_keyboard())


async def deliver_service(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Sends the actual product. Uses tappable buttons if SERVICES is set
    in .env, otherwise falls back to the old single text/file behavior."""
    try:
        if SERVICES:
            await context.bot.send_message(
                chat_id=user_id,
                text="🎉 You're all set! Tap below to access your service:",
                reply_markup=service_keyboard(),
            )
        elif SERVICE_TYPE == "file" and SERVICE_FILE_PATH:
            with open(SERVICE_FILE_PATH, "rb") as f:
                await context.bot.send_document(chat_id=user_id, document=f, caption=SERVICE_TEXT)
        else:
            await context.bot.send_message(
                chat_id=user_id, text=SERVICE_TEXT, parse_mode=ParseMode.HTML
            )
    except TelegramError as e:
        logger.error("Failed to deliver service to %s: %s", user_id, e)


async def get_service_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on a 'file' / 'code' / 'text' service button."""
    query = update.callback_query
    user = query.from_user

    # Defense in depth: re-check eligibility even though only this user
    # can tap buttons inside their own private chat with the bot.
    row = get_user(user.id)
    if row is None or not row[4] or row[3] < REQUIRED_REFERRALS:
        await query.answer("⚠️ You need to verify and complete your referrals first.", show_alert=True)
        return

    svc_id = query.data.removeprefix("get_")
    svc = next((s for s in SERVICES if s["id"] == svc_id), None)
    if svc is None:
        await query.answer("⚠️ That item isn't available anymore.", show_alert=True)
        return

    await query.answer()  # dismiss the loading spinner on the button

    try:
        if svc["type"] == "file":
            with open(svc["value"], "rb") as f:
                await context.bot.send_document(chat_id=user.id, document=f, caption=svc["label"])
        elif svc["type"] == "code":
            # Wrapped in <code> so Telegram shows a tap-to-copy monospace block.
            safe_value = html.escape(svc["value"])
            await context.bot.send_message(
                chat_id=user.id, text=f"<code>{safe_value}</code>", parse_mode=ParseMode.HTML
            )
        else:  # "text"
            await context.bot.send_message(chat_id=user.id, text=svc["value"])
    except (TelegramError, FileNotFoundError) as e:
        logger.error("Failed to deliver service item %s to %s: %s", svc_id, user.id, e)
        await context.bot.send_message(
            chat_id=user.id, text="⚠️ Something went wrong delivering that. Please try again shortly."
        )


async def my_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets an already-unlocked user re-summon their service buttons any time."""
    user = update.effective_user
    row = get_user(user.id)
    if row is None or not row[4] or row[3] < REQUIRED_REFERRALS:
        await update.message.reply_text("You haven't unlocked the service yet. Send /start to begin.")
        return
    await deliver_service(context, user.id)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: quick usage stats. Usage: /stats"""
    if update.effective_user.id != ADMIN_ID:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE verified_join=1")
    verified = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE got_service=1")
    unlocked = c.fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"📊 Stats\nTotal users: {total}\nVerified joins: {verified}\nUnlocked service: {unlocked}"
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    init_db()

    # Keep-alive server for hosts that require a bound port (e.g. Render free tier).
    threading.Thread(target=start_keep_alive_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("myservice", my_service))
    app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(get_service_item, pattern="^get_svc"))

    logger.info("Bot started. Polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""
Telegram "Join + Refer to Unlock" bot — DesignerDebBot
--------------------------------------------------------
Flow:
1. User starts the bot (optionally via a referral link).
2. Bot shows a greeting + task message with THREE buttons:
   📢 Channel | 🔗 Referral Link | ✅ Verify
3. On Verify, bot checks channel membership via the Telegram API.
4. Once joined, bot checks the user has referred enough friends
   (friends must also join + verify to count).
5. Once both conditions are met, the bot delivers the service
   (tappable reward buttons) directly in the chat.

Admin control panel:
- Send /admin (or tap the "🛠 Admin Panel" button that only you see on /start)
  to open a button-driven control panel. From it you can, without touching
  code or redeploying:
    - view stats
    - add/remove referrals for a user
    - change the channel link / channel ID
    - change the greeting message and the task message
    - change how many referrals are required
    - add, remove, or replace the reward buttons users unlock (links,
      files, tap-to-copy codes, or plain text)
  You never have to type a slash command with arguments again — every admin
  action is a button, and the bot asks you for the one piece of info it
  still needs (e.g. "send the new link").

Beginner notes:
- All secrets/config live in a .env file (see env.example). Never put your
  bot token directly in this file if you plan to share/upload the code
  anywhere.
- User data AND all the admin-editable settings above live in a local
  SQLite file (bot_data.db), created automatically. IMPORTANT if you're on
  Render's free plan: that file lives on an ephemeral disk, which is wiped
  every time the service redeploys or spins back up after going idle. See
  the note in the README about keeping data across restarts.
"""

import os
import html
import json
import logging
import secrets
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
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------
# Config (loaded from .env) — these are the STARTUP defaults. Everything
# marked "admin-editable" below can be changed later from inside Telegram
# without redeploying; the .env value is only used to seed it the first time.
# --------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

CHANNEL_ID_ENV = os.getenv("CHANNEL_ID")                       # admin-editable
CHANNEL_INVITE_LINK_ENV = os.getenv("CHANNEL_INVITE_LINK")     # admin-editable
REQUIRED_REFERRALS_ENV = int(os.getenv("REQUIRED_REFERRALS", "2"))  # admin-editable
SERVICES_ENV_RAW = os.getenv("SERVICES", "")                   # admin-editable

SERVICE_TYPE = os.getenv("SERVICE_TYPE", "text")     # legacy single-item fallback
SERVICE_TEXT = os.getenv("SERVICE_TEXT", "Here is your service/link!")
SERVICE_FILE_PATH = os.getenv("SERVICE_FILE_PATH", "")

DB_PATH = "bot_data.db"
BOT_DISPLAY_NAME = "DesignerDebBot"

DEFAULT_GREETING = f"👋 Hey {{first_name}}! Welcome to {BOT_DISPLAY_NAME}."
DEFAULT_TASK = (
    "Here's what you need to do to get your reward:\n"
    "1️⃣ Join our channel\n"
    "2️⃣ Refer at least {required} friends (they must join & verify too)\n\n"
    "Tap the buttons below 👇"
)

if not BOT_TOKEN or not CHANNEL_ID_ENV or not CHANNEL_INVITE_LINK_ENV:
    raise SystemExit(
        "Missing config. Make sure BOT_TOKEN, CHANNEL_ID and CHANNEL_INVITE_LINK "
        "are set in your .env file (these seed the admin-editable settings)."
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
    # Admin-editable settings, as simple key/value pairs. Anything not yet
    # here falls back to the .env-derived default (see get_field_value).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
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


def add_referral_count(user_id: int, amount: int):
    """Adds (or subtracts, if amount is negative) to a user's referral count.
    Never lets it go below 0."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET referral_count = MAX(referral_count + ?, 0) WHERE user_id=?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()


def set_got_service(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET got_service=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def build_stats_text() -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE verified_join=1")
    verified = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE got_service=1")
    unlocked = c.fetchone()[0]
    conn.close()
    return f"📊 Stats\nTotal users: {total}\nVerified joins: {verified}\nUnlocked service: {unlocked}"


# --------------------------------------------------------------------------
# Settings helpers (admin-editable config, stored in SQLite)
# --------------------------------------------------------------------------
def get_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row is not None else default


def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# Registry of the simple text/number fields an admin can edit from the panel.
# "setting_key" is where it's stored; "default_env" is what it falls back to
# until the admin changes it for the first time.
EDITABLE_FIELDS = {
    "channel_link": {
        "setting_key": "channel_invite_link",
        "label": "📢 Channel Link",
        "prompt": "Send the new channel invite link (e.g. https://t.me/yourchannel):",
        "default_env": CHANNEL_INVITE_LINK_ENV,
    },
    "channel_id": {
        "setting_key": "channel_id",
        "label": "🆔 Channel ID",
        "prompt": "Send the new numeric channel ID (e.g. -1001234567890):",
        "default_env": CHANNEL_ID_ENV,
    },
    "greeting": {
        "setting_key": "greeting_text",
        "label": "👋 Greeting Message",
        "prompt": "Send the new greeting. You can use {first_name} for the user's name:",
        "default_env": DEFAULT_GREETING,
    },
    "task": {
        "setting_key": "task_text",
        "label": "📋 Task Message",
        "prompt": "Send the new task/instructions message. You can use {required} for the referral count:",
        "default_env": DEFAULT_TASK,
    },
    "required": {
        "setting_key": "required_referrals",
        "label": "🔢 Required Referrals",
        "prompt": "Send the number of referrals required (whole number, e.g. 2):",
        "default_env": str(REQUIRED_REFERRALS_ENV),
    },
}


def get_field_value(field_key: str) -> str:
    field = EDITABLE_FIELDS[field_key]
    return get_setting(field["setting_key"], field["default_env"])


# --------------------------------------------------------------------------
# Reward-button ("services") helpers
# --------------------------------------------------------------------------
def parse_services_string(raw: str):
    """
    Turns a "Label|type|value;Label|type|value" string into a list of dicts.
    type is one of: url, file, code, text
      url  -> tapping opens the link immediately (no bot round-trip)
      file -> tapping sends a file. value is either a path in your repo, or
              (once an admin uploads a file through the panel) "tg:<file_id>"
      code -> tapping sends the value as a tap-to-copy code block
      text -> tapping sends the value as a plain message
    """
    items = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 3:
            logger.warning("Skipping malformed SERVICES entry: %r", chunk)
            continue
        label, s_type, value = parts
        if s_type not in ("url", "file", "code", "text"):
            logger.warning("Skipping SERVICES entry with unknown type: %r", chunk)
            continue
        items.append({"id": secrets.token_hex(3), "label": label, "type": s_type, "value": value})
    return items


def services_to_raw_string(services) -> str:
    return ";".join(f"{s['label']}|{s['type']}|{s['value']}" for s in services)


def get_services():
    raw = get_setting("services_json", "")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Corrupt services_json in settings — reseeding from .env SERVICES.")
    seeded = parse_services_string(SERVICES_ENV_RAW)
    save_services(seeded)
    return seeded


def save_services(services):
    set_setting("services_json", json.dumps(services))


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------
def start_keyboard(user_id: int):
    rows = [
        [InlineKeyboardButton("📢 Channel", url=get_field_value("channel_link"))],
        [InlineKeyboardButton("🔗 Referral Link", callback_data="show_ref")],
        [InlineKeyboardButton("✅ Verify", callback_data="verify")],
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def recheck_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Check Again", callback_data="verify")]]
    )


def service_keyboard(services):
    """One tappable button per reward item. 'url' buttons open instantly;
    'file'/'code'/'text' buttons trigger the bot to send that item on tap."""
    rows = []
    for svc in services:
        if svc["type"] == "url":
            rows.append([InlineKeyboardButton(svc["label"], url=svc["value"])])
        else:
            rows.append([InlineKeyboardButton(svc["label"], callback_data=f"get_{svc['id']}")])
    return InlineKeyboardMarkup(rows)


def admin_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Stats", callback_data="adm:stats")],
            [InlineKeyboardButton("➕ Add / Remove Referral", callback_data="adm:addref")],
            [
                InlineKeyboardButton("📢 Channel Link", callback_data="adm:edit:channel_link"),
                InlineKeyboardButton("🆔 Channel ID", callback_data="adm:edit:channel_id"),
            ],
            [
                InlineKeyboardButton("👋 Greeting", callback_data="adm:edit:greeting"),
                InlineKeyboardButton("📋 Task Text", callback_data="adm:edit:task"),
            ],
            [InlineKeyboardButton("🔢 Required Referrals", callback_data="adm:edit:required")],
            [InlineKeyboardButton("🎁 Reward Buttons", callback_data="adm:services")],
        ]
    )


def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel")]])


def services_menu_keyboard(services):
    rows = [
        [InlineKeyboardButton(f"🗑 {s['label']}", callback_data=f"adm:svcrm:{s['id']}")]
        for s in services
    ]
    rows.append([InlineKeyboardButton("➕ Add New Item", callback_data="adm:svcadd")])
    rows.append([InlineKeyboardButton("📋 Replace All (paste list)", callback_data="adm:svcreplace")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def svc_type_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 URL (opens instantly)", callback_data="adm:svctype:url")],
            [InlineKeyboardButton("📄 File", callback_data="adm:svctype:file")],
            [InlineKeyboardButton("🎟 Code (tap-to-copy)", callback_data="adm:svctype:code")],
            [InlineKeyboardButton("💬 Text", callback_data="adm:svctype:text")],
        ]
    )


def services_listing_text(services) -> str:
    return "\n".join(f"• {s['label']} ({s['type']})" for s in services) or "(none yet)"


# --------------------------------------------------------------------------
# User-facing handlers
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

    required = int(get_field_value("required"))

    try:
        greeting = get_field_value("greeting").format(first_name=user.first_name)
    except (KeyError, IndexError):
        greeting = get_field_value("greeting")  # admin text had a stray "{...}" — show it raw
    try:
        task = get_field_value("task").format(required=required)
    except (KeyError, IndexError):
        task = get_field_value("task")

    text = f"{greeting}\n\n{task}"
    await update.message.reply_text(text, reply_markup=start_keyboard(user.id))


async def show_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on the 'Referral Link' button — sends the user their
    personal link as a new message so it's easy to copy/forward."""
    query = update.callback_query
    user = query.from_user
    await query.answer()

    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref{user.id}"
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            f"🔗 Your personal referral link:\n{ref_link}\n\n"
            "Share it with friends — you get credit once they join the channel "
            "and tap Verify."
        ),
    )


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    channel_id = get_field_value("channel_id")

    # 1. Check channel membership
    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user.id)
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
    required = int(get_field_value("required"))

    if referral_count >= required:
        await query.edit_message_text(
            f"✅ Verified! You referred {referral_count}/{required} friends.\n\n"
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
            f"👥 Referrals: {referral_count}/{required}\n\n"
            f"Share your personal link with friends, then tap Check Again:\n{ref_link}"
        )
        await query.edit_message_text(text, reply_markup=recheck_keyboard())


async def deliver_service(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Sends the actual product. Uses tappable buttons if any reward items
    are configured, otherwise falls back to the old single text/file
    behavior. Returns True if it was sent successfully, False otherwise."""
    services = get_services()
    try:
        if services:
            await context.bot.send_message(
                chat_id=user_id,
                text="🎉 You're all set! Tap below to access your service:",
                reply_markup=service_keyboard(services),
            )
        elif SERVICE_TYPE == "file" and SERVICE_FILE_PATH:
            with open(SERVICE_FILE_PATH, "rb") as f:
                await context.bot.send_document(chat_id=user_id, document=f, caption=SERVICE_TEXT)
        else:
            await context.bot.send_message(
                chat_id=user_id, text=SERVICE_TEXT, parse_mode=ParseMode.HTML
            )
        return True
    except TelegramError as e:
        logger.error("Failed to deliver service to %s: %s", user_id, e)
        return False


async def get_service_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on a 'file' / 'code' / 'text' reward button."""
    query = update.callback_query
    user = query.from_user

    # Defense in depth: re-check eligibility even though only this user
    # can tap buttons inside their own private chat with the bot.
    row = get_user(user.id)
    required = int(get_field_value("required"))
    if row is None or not row[4] or row[3] < required:
        await query.answer("⚠️ You need to verify and complete your referrals first.", show_alert=True)
        return

    svc_id = query.data.removeprefix("get_")
    services = get_services()
    svc = next((s for s in services if s["id"] == svc_id), None)
    if svc is None:
        await query.answer("⚠️ That item isn't available anymore.", show_alert=True)
        return

    await query.answer()  # dismiss the loading spinner on the button

    try:
        if svc["type"] == "file":
            value = svc["value"]
            if value.startswith("tg:"):
                # Uploaded through the admin panel — Telegram hosts the file
                # itself, so this works even after the bot's disk is wiped.
                await context.bot.send_document(chat_id=user.id, document=value[3:], caption=svc["label"])
            else:
                with open(value, "rb") as f:
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
    required = int(get_field_value("required"))
    if row is None or not row[4] or row[3] < required:
        await update.message.reply_text("You haven't unlocked the service yet. Send /start to begin.")
        return
    await deliver_service(context, user.id)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: quick usage stats. Usage: /stats (or tap Stats in /admin)"""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(build_stats_text())


# --------------------------------------------------------------------------
# Admin control panel — dynamic buttons instead of typed commands
# --------------------------------------------------------------------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: opens the button-driven control panel. Usage: /admin"""
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data.clear()  # reset any half-finished edit flow
    await update.message.reply_text(
        "🛠 Admin Panel\n"
        "Tap a button below. Changes apply immediately — no redeploy needed.",
        reply_markup=admin_menu_keyboard(),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer()

    parts = query.data.split(":")
    action = parts[1]

    if action == "menu":
        context.user_data.clear()
        await query.edit_message_text("🛠 Admin Panel", reply_markup=admin_menu_keyboard())

    elif action == "cancel":
        context.user_data.clear()
        await query.edit_message_text("🛠 Admin Panel — cancelled.", reply_markup=admin_menu_keyboard())

    elif action == "stats":
        await query.edit_message_text(build_stats_text(), reply_markup=admin_menu_keyboard())

    elif action == "addref":
        context.user_data["awaiting"] = "addref"
        await query.edit_message_text(
            "Send: <user_id> <amount>\n"
            "Example: 123456789 2  (adds 2 referrals)\n"
            "Amount can be negative to subtract. Defaults to 1 if omitted.",
            reply_markup=cancel_keyboard(),
        )

    elif action == "edit":
        field_key = parts[2]
        field = EDITABLE_FIELDS.get(field_key)
        if not field:
            return
        context.user_data["awaiting"] = f"edit:{field_key}"
        current = get_field_value(field_key)
        await query.edit_message_text(
            f"Current {field['label']}:\n{current}\n\n{field['prompt']}\n\n"
            "(Send /cancel to abort.)",
            reply_markup=cancel_keyboard(),
        )

    elif action == "services":
        services = get_services()
        await query.edit_message_text(
            f"🎁 Reward Buttons\n{services_listing_text(services)}\n\n"
            "Tap an item to remove it, or use the buttons below.",
            reply_markup=services_menu_keyboard(services),
        )

    elif action == "svcrm":
        svc_id = parts[2]
        services = [s for s in get_services() if s["id"] != svc_id]
        save_services(services)
        await query.edit_message_text(
            f"🗑 Removed.\n\n🎁 Reward Buttons\n{services_listing_text(services)}",
            reply_markup=services_menu_keyboard(services),
        )

    elif action == "svcadd":
        context.user_data["svc_new"] = {}
        context.user_data["awaiting"] = "svc_label"
        await query.edit_message_text(
            "Send the button label for the new reward item (e.g. 🔗 Premium Link):\n\n"
            "(Send /cancel to abort.)",
            reply_markup=cancel_keyboard(),
        )

    elif action == "svctype":
        svc_type = parts[2]
        context.user_data.setdefault("svc_new", {})["type"] = svc_type
        context.user_data["awaiting"] = "svc_value"
        if svc_type == "url":
            prompt = "Send the URL this button should open:"
        elif svc_type == "file":
            prompt = (
                "Upload/forward the file right here in this chat, OR send a file "
                "path if it's already committed in your repo:"
            )
        elif svc_type == "code":
            prompt = "Send the code/text to deliver as a tap-to-copy block:"
        else:
            prompt = "Send the plain text message to deliver:"
        await query.edit_message_text(f"{prompt}\n\n(Send /cancel to abort.)", reply_markup=cancel_keyboard())

    elif action == "svcreplace":
        context.user_data["awaiting"] = "svc_replace_raw"
        raw_current = services_to_raw_string(get_services())
        await query.edit_message_text(
            "Paste the FULL replacement list, in this format:\n"
            "Label|type|value;Label|type|value\n"
            "(type = url, file, code, or text)\n\n"
            f"Current:\n{raw_current or '(none)'}\n\n"
            "(Send /cancel to abort.)",
            reply_markup=cancel_keyboard(),
        )


async def admin_flow_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures the admin's next text/file message when the panel is
    waiting on a value (e.g. after tapping 'Channel Link' or 'Add Item').
    Does nothing for non-admins or when no edit flow is in progress."""
    if update.effective_user.id != ADMIN_ID:
        return
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return

    message = update.message
    text = (message.text or message.caption or "").strip()

    if text == "/cancel":
        context.user_data.clear()
        await message.reply_text("Cancelled.", reply_markup=admin_menu_keyboard())
        return

    if awaiting == "addref":
        parts = text.split()
        if not parts or not parts[0].lstrip("-").isdigit():
            await message.reply_text("That doesn't look right. Send: <user_id> <amount>")
            return
        target_id = int(parts[0])
        amount = 1
        if len(parts) > 1:
            if not parts[1].lstrip("-").isdigit():
                await message.reply_text("Amount must be a whole number.")
                return
            amount = int(parts[1])

        if get_user(target_id) is None:
            add_user(target_id, "unknown")
        add_referral_count(target_id, amount)
        _, _, _, referral_count, verified_join, got_service = get_user(target_id)
        context.user_data.clear()
        await message.reply_text(
            f"✅ User {target_id} now has {referral_count} referral(s).",
            reply_markup=admin_menu_keyboard(),
        )

        required = int(get_field_value("required"))
        if verified_join and referral_count >= required and not got_service:
            set_got_service(target_id)
            delivered = await deliver_service(context, target_id)
            if delivered:
                await message.reply_text(f"🎉 Service auto-delivered to {target_id}.")
            else:
                await message.reply_text(
                    f"⚠️ Couldn't message {target_id} directly — they probably haven't "
                    f"started a chat with the bot yet."
                )
        return

    if awaiting.startswith("edit:"):
        field_key = awaiting.split(":", 1)[1]
        field = EDITABLE_FIELDS.get(field_key)
        if not field:
            context.user_data.clear()
            return
        if field_key == "required" and not text.isdigit():
            await message.reply_text("Please send a whole number (e.g. 2).")
            return
        set_setting(field["setting_key"], text)
        context.user_data.clear()
        await message.reply_text(f"✅ Updated {field['label']}.", reply_markup=admin_menu_keyboard())
        return

    if awaiting == "svc_label":
        context.user_data.setdefault("svc_new", {})["label"] = text
        context.user_data["awaiting"] = "svc_type"
        await message.reply_text("Now pick the type:", reply_markup=svc_type_keyboard())
        return

    if awaiting == "svc_type":
        await message.reply_text("Please tap one of the buttons above to pick a type.")
        return

    if awaiting == "svc_value":
        svc_new = context.user_data.get("svc_new", {})
        svc_type = svc_new.get("type")
        if svc_type == "file" and message.document:
            value = f"tg:{message.document.file_id}"
        elif not text:
            await message.reply_text("Please send a value (or upload the file, for file items).")
            return
        else:
            value = text
        svc_new["value"] = value
        svc_new["id"] = secrets.token_hex(3)
        services = get_services()
        services.append(svc_new)
        save_services(services)
        context.user_data.clear()
        await message.reply_text(
            f"✅ Added.\n\n🎁 Reward Buttons\n{services_listing_text(services)}",
            reply_markup=services_menu_keyboard(services),
        )
        return

    if awaiting == "svc_replace_raw":
        new_services = parse_services_string(text)
        save_services(new_services)
        context.user_data.clear()
        await message.reply_text(
            f"✅ Reward buttons replaced.\n\n🎁 Reward Buttons\n{services_listing_text(new_services)}",
            reply_markup=services_menu_keyboard(new_services),
        )
        return

    # Unknown state somehow — reset defensively rather than getting stuck.
    context.user_data.clear()


async def add_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: manually adjust a user's referral count.
    Usage: /addref <user_id> [amount]   (amount defaults to 1, can be negative to subtract)
    Kept as a direct command for power users — the admin panel does the same thing with buttons.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Usage: /addref <user_id> [amount]\n"
            "Example: /addref 123456789 2  (adds 2 referrals)\n"
            "Example: /addref 123456789 -1  (removes 1 referral)\n"
            "Tip: /admin gives you a button-based version of this."
        )
        return

    target_id = int(context.args[0])
    amount = 1
    if len(context.args) > 1:
        if not context.args[1].lstrip("-").isdigit():
            await update.message.reply_text("Amount must be a whole number, e.g. 2 or -1.")
            return
        amount = int(context.args[1])

    if get_user(target_id) is None:
        add_user(target_id, "unknown")

    add_referral_count(target_id, amount)
    _, _, _, referral_count, verified_join, got_service = get_user(target_id)

    await update.message.reply_text(f"✅ User {target_id} now has {referral_count} referral(s).")

    required = int(get_field_value("required"))
    if verified_join and referral_count >= required and not got_service:
        set_got_service(target_id)
        delivered = await deliver_service(context, target_id)
        if delivered:
            await update.message.reply_text(f"🎉 Service auto-delivered to {target_id}.")
        else:
            await update.message.reply_text(
                f"⚠️ Couldn't message {target_id} directly — they probably haven't started "
                f"a chat with the bot yet. They can grab it themselves with /myservice once they do."
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
    app.add_handler(CommandHandler("addref", add_referral))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(show_referral_link, pattern="^show_ref$"))
    app.add_handler(CallbackQueryHandler(get_service_item, pattern="^get_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm:"))
    # Must be last: catches the admin's plain-text/file replies while a panel
    # flow is waiting on a value. No-ops for everyone else / when idle.
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, admin_flow_input))

    logger.info("Bot started. Polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

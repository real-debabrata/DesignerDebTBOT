"""
Telegram "Join + Refer to Redeem" bot — DesignerDebBot
--------------------------------------------------------
Flow:
1. User starts the bot (optionally via a referral link).
2. Bot shows a greeting + task message with buttons:
   📢 Channel | 🔗 Referral Link | ✅ Verify | 🎁 Rewards
3. On Verify, bot checks channel membership via the Telegram API and
   credits whoever referred them. Each verified referral adds 1 to the
   user's referral BALANCE — nothing is auto-delivered anymore.
4. Rewards each have their own referral cost (e.g. "Canva Pro — 1 Year"
   might cost 4 referrals, a promo code might cost 0). The user opens
   🎁 Rewards any time to see their balance and what they can afford.
5. Tapping a reward:
   - Free/instant items (url/file/code/text with cost 0) deliver right
     away, same as before.
   - Anything with a cost checks the user's balance first. If they can
     afford it, the cost is deducted from their balance.
   - Items of type "redeem" (the new type — use this for anything you
     fulfill by hand, like Canva Pro) then ask the user for their email.
     The email + product + cost gets logged to your local redemptions
     table, appended to a Google Sheet if you've set one up (see
     GOOGLE_SHEETS_SETUP.md), AND sent to you instantly on Telegram —
     so you can go deliver it.

Admin control panel:
- Send /admin (or tap the "🛠 Admin Panel" button that only you see on /start)
  to open a button-driven control panel. From it you can, without touching
  code or redeploying:
    - view stats
    - add/remove referral balance for a user
    - change the channel link / channel ID
    - change the greeting message and the task message
    - add, remove, or replace the rewards users can redeem (links, files,
      tap-to-copy codes, plain text, or "redeem" items that collect an
      email for you to fulfill by hand) — each with its own referral cost
    - see pending redemptions and mark them fulfilled once you've sent
      the product
    - export every redemption ever logged as a CSV file, any time
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
  every time the service redeploys or spins back up after going idle.
  That's exactly why redemptions (the ones that matter — email + product)
  get sent to you on Telegram immediately AND (optionally) to a Google
  Sheet that lives outside Render entirely. See GOOGLE_SHEETS_SETUP.md.
"""

import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import sqlite3
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
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

import sheets

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
SERVICES_ENV_RAW = os.getenv("SERVICES", "")                   # admin-editable (this is the REWARDS list)

SERVICE_TYPE = os.getenv("SERVICE_TYPE", "text")     # legacy single-item fallback
SERVICE_TEXT = os.getenv("SERVICE_TEXT", "Here is your service/link!")
SERVICE_FILE_PATH = os.getenv("SERVICE_FILE_PATH", "")

DB_PATH = "bot_data.db"
BOT_DISPLAY_NAME = "DesignerDebBot"

DEFAULT_GREETING = f"👋 Hey {{first_name}}! Welcome to {BOT_DISPLAY_NAME}."
DEFAULT_TASK = (
    "Here's what you need to do:\n"
    "1️⃣ Join our channel\n"
    "2️⃣ Refer friends — they must join & verify too\n"
    "3️⃣ Every verified referral adds to your balance\n\n"
    "Once you've got enough, open 🎁 Rewards to redeem something!\n\n"
    "Tap the buttons below 👇"
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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
            verified_join INTEGER DEFAULT 0
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
    # One row per redemption request (the "needs email / manual fulfillment"
    # kind, plus a record of instant/free deliveries for the stats count).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            reward_id TEXT,
            reward_label TEXT,
            cost INTEGER,
            email TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_user(user_id: int):
    """Returns (user_id, username, referred_by, referral_count, verified_join)
    or None. Selects columns explicitly (not SELECT *) so this keeps working
    even against an older DB file that still has a leftover legacy column."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT user_id, username, referred_by, referral_count, verified_join "
        "FROM users WHERE user_id=?",
        (user_id,),
    )
    row = c.fetchone()
    conn.close()
    return row


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
    """Adds (or subtracts, if amount is negative) to a user's referral
    balance. Never lets it go below 0 — this is also how redeeming a
    reward deducts its cost."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET referral_count = MAX(referral_count + ?, 0) WHERE user_id=?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()


def build_stats_text() -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE verified_join=1")
    verified = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM redemptions")
    total_redemptions = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM redemptions WHERE status='pending'")
    pending = c.fetchone()[0]
    conn.close()
    return (
        "📊 Stats\n"
        f"Total users: {total}\n"
        f"Verified joins: {verified}\n"
        f"Total redemptions: {total_redemptions}\n"
        f"Pending fulfillment: {pending}"
    )


# --------------------------------------------------------------------------
# Redemption log helpers (the "needs email / you fulfill by hand" queue)
# --------------------------------------------------------------------------
def create_redemption(user_id, username, reward_id, reward_label, cost, email, status) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO redemptions (user_id, username, reward_id, reward_label, cost, email, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, reward_id, reward_label, cost, email, status, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return new_id


def get_recent_redemptions(limit: int = 10, status: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if status:
        c.execute("SELECT * FROM redemptions WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        c.execute("SELECT * FROM redemptions ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_redemptions():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM redemptions ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def set_redemption_status(redemption_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE redemptions SET status=? WHERE id=?", (status, redemption_id))
    conn.commit()
    conn.close()


def is_valid_email(text: str) -> bool:
    return bool(EMAIL_RE.match(text.strip()))


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


# Registry of the simple text fields an admin can edit from the panel.
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
        "prompt": "Send the new task/instructions message:",
        "default_env": DEFAULT_TASK,
    },
}


def get_field_value(field_key: str) -> str:
    field = EDITABLE_FIELDS[field_key]
    return get_setting(field["setting_key"], field["default_env"])


# --------------------------------------------------------------------------
# Reward helpers (stored under the same "services_json" setting key /
# SERVICES env var as before, just with an added "cost" field, so an
# already-deployed bot doesn't lose anything configured previously).
# --------------------------------------------------------------------------
def parse_rewards_string(raw: str):
    """
    Turns "Label|type|value|cost;Label|type|value|cost" into a list of dicts.
    cost is optional per item (defaults to 0) — old 3-part entries still work.
      type = url    -> tapping opens the link immediately (free items only;
                        anything with cost > 0 routes through the bot instead)
      type = file   -> tapping sends a file. value = repo path, or (once an
                        admin uploads a file through the panel) "tg:<file_id>"
      type = code   -> tapping sends the value as a tap-to-copy code block
      type = text   -> tapping sends the value as a plain message
      type = redeem -> tapping asks the user for their email, then logs the
                        request (email + product) for you to fulfill by hand
                        — use this for anything like Canva Pro that needs a
                        human to actually deliver it
      cost = how many referrals this item costs. 0 = free/instant.
    """
    items = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) not in (3, 4):
            logger.warning("Skipping malformed reward entry: %r", chunk)
            continue
        label, r_type, value = parts[0], parts[1], parts[2]
        cost_str = parts[3] if len(parts) == 4 else "0"
        if r_type not in ("url", "file", "code", "text", "redeem"):
            logger.warning("Skipping reward entry with unknown type: %r", chunk)
            continue
        if not cost_str.isdigit():
            logger.warning("Skipping reward entry with a bad cost: %r", chunk)
            continue
        items.append(
            {
                "id": secrets.token_hex(3),
                "label": label,
                "type": r_type,
                "value": value,
                "cost": int(cost_str),
            }
        )
    return items


def rewards_to_raw_string(rewards) -> str:
    return ";".join(f"{r['label']}|{r['type']}|{r['value']}|{r.get('cost', 0)}" for r in rewards)


def get_rewards():
    raw = get_setting("services_json", "")
    if raw:
        try:
            rewards = json.loads(raw)
            for r in rewards:
                r.setdefault("cost", 0)  # migrate any old entries saved with no cost field
            return rewards
        except json.JSONDecodeError:
            logger.error("Corrupt services_json in settings — reseeding from .env SERVICES.")
    seeded = parse_rewards_string(SERVICES_ENV_RAW)
    save_rewards(seeded)
    return seeded


def save_rewards(rewards):
    set_setting("services_json", json.dumps(rewards))


# --------------------------------------------------------------------------
# Keyboards
# --------------------------------------------------------------------------
def start_keyboard(user_id: int):
    rows = [
        [InlineKeyboardButton("📢 Channel", url=get_field_value("channel_link"))],
        [InlineKeyboardButton("🔗 Referral Link", callback_data="show_ref")],
        [InlineKeyboardButton("✅ Verify", callback_data="verify")],
        [InlineKeyboardButton("🎁 Rewards", callback_data="show_rewards")],
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def recheck_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Check Again", callback_data="verify")],
            [InlineKeyboardButton("🎁 Rewards", callback_data="show_rewards")],
        ]
    )


def reward_keyboard(rewards, balance: int):
    """One button per reward. A free 'url' item opens instantly with no bot
    round-trip; everything else (including any item with a cost) routes
    through the bot so it can check the user's balance first."""
    rows = []
    for r in rewards:
        cost = r.get("cost", 0)
        if r["type"] == "url" and cost == 0:
            rows.append([InlineKeyboardButton(r["label"], url=r["value"])])
            continue
        if cost > 0:
            if balance >= cost:
                text = f"🎁 {r['label']} — Redeem ({cost} 👥)"
            else:
                text = f"🔒 {r['label']} — needs {cost} 👥 (you have {balance})"
        else:
            text = f"🎁 {r['label']} (free)"
        rows.append([InlineKeyboardButton(text, callback_data=f"redeem_{r['id']}")])
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
            [InlineKeyboardButton("🎁 Rewards", callback_data="adm:services")],
            [
                InlineKeyboardButton("📜 Redemptions", callback_data="adm:redemptions"),
                InlineKeyboardButton("📁 Export CSV", callback_data="adm:export"),
            ],
        ]
    )


def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel")]])


def rewards_menu_keyboard(rewards):
    rows = [
        [InlineKeyboardButton(f"🗑 {r['label']} ({r.get('cost', 0)} 👥)", callback_data=f"adm:svcrm:{r['id']}")]
        for r in rewards
    ]
    rows.append([InlineKeyboardButton("➕ Add New Reward", callback_data="adm:svcadd")])
    rows.append([InlineKeyboardButton("📋 Replace All (paste list)", callback_data="adm:svcreplace")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def reward_type_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 URL (opens instantly if free)", callback_data="adm:svctype:url")],
            [InlineKeyboardButton("📄 File", callback_data="adm:svctype:file")],
            [InlineKeyboardButton("🎟 Code (tap-to-copy)", callback_data="adm:svctype:code")],
            [InlineKeyboardButton("💬 Text", callback_data="adm:svctype:text")],
            [InlineKeyboardButton("🎯 Redeem (collects email, you fulfill)", callback_data="adm:svctype:redeem")],
        ]
    )


def rewards_listing_text(rewards) -> str:
    lines = []
    for r in rewards:
        cost = r.get("cost", 0)
        cost_label = f"{cost} referrals" if cost else "free"
        lines.append(f"• {r['label']} — {r['type']}, {cost_label}")
    return "\n".join(lines) or "(none yet)"


def redemptions_panel_content(limit: int = 10):
    rows = get_recent_redemptions(limit=limit, status="pending")
    if not rows:
        return "📜 No pending redemptions right now.", admin_menu_keyboard()
    lines = ["📜 Pending Redemptions (most recent first):\n"]
    keyboard_rows = []
    for r in rows:
        rid, user_id, username, reward_id, reward_label, cost, email, status, created_at = r
        lines.append(f"#{rid} • @{username or user_id} • {reward_label} • {email}")
        keyboard_rows.append(
            [InlineKeyboardButton(f"✅ Mark #{rid} fulfilled", callback_data=f"adm:fulfill:{rid}")]
        )
    keyboard_rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:menu")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


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

    try:
        greeting = get_field_value("greeting").format(first_name=user.first_name)
    except (KeyError, IndexError):
        greeting = get_field_value("greeting")  # admin text had a stray "{...}" — show it raw
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

    _, _, referred_by, _, was_verified = row

    # 3. First time verifying? Credit whoever referred them.
    if not was_verified:
        set_verified(user.id)
        if referred_by:
            increment_referral(referred_by)
            try:
                await context.bot.send_message(
                    referred_by,
                    "🎉 One of your referrals just joined and verified! "
                    "Keep sharing your link — every one gets you closer to your next reward.",
                )
            except TelegramError:
                pass  # referrer may have blocked the bot

    # 4. Show the user their (fresh) balance and point them at the shop
    row = get_user(user.id)
    _, _, _, balance, _ = row
    text = (
        "✅ Channel join verified!\n\n"
        f"👥 Your referral balance: {balance}\n\n"
        "Tap 🎁 Rewards to see what you can redeem, or keep sharing your referral "
        "link to earn more."
    )
    await query.edit_message_text(text, reply_markup=recheck_keyboard())


async def render_rewards(user, target, edit: bool):
    """Shared by the 🎁 Rewards button and the /rewards command."""
    row = get_user(user.id)
    if row is None:
        add_user(user.id, user.username or user.first_name)
        row = get_user(user.id)
    _, _, _, balance, verified = row

    rewards = get_rewards()
    if not rewards:
        text = "🎁 No rewards are configured yet — check back soon!"
        markup = None
    else:
        lines = [f"👥 Your referral balance: {balance}"]
        if not verified:
            lines.append("⚠️ Join the channel and tap ✅ Verify first to unlock redeeming.")
        lines.append("\nTap a reward to redeem it:")
        text = "\n".join(lines)
        markup = reward_keyboard(rewards, balance)

    if edit:
        await target.edit_message_text(text, reply_markup=markup)
    else:
        await target.reply_text(text, reply_markup=markup)


async def show_rewards_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await render_rewards(query.from_user, query, edit=True)


async def rewards_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_rewards(update.effective_user, update.message, edit=False)


async def deliver_reward_item(context: ContextTypes.DEFAULT_TYPE, user_id: int, reward) -> bool:
    """Instantly delivers a url/file/code/text reward. Returns True on
    success. ('redeem' items never reach this — they go through the
    email-collection flow instead.)"""
    try:
        if reward["type"] == "file":
            value = reward["value"]
            if value.startswith("tg:"):
                # Uploaded through the admin panel — Telegram hosts the file
                # itself, so this works even after the bot's disk is wiped.
                await context.bot.send_document(chat_id=user_id, document=value[3:], caption=reward["label"])
            else:
                with open(value, "rb") as f:
                    await context.bot.send_document(chat_id=user_id, document=f, caption=reward["label"])
        elif reward["type"] == "code":
            # Wrapped in <code> so Telegram shows a tap-to-copy monospace block.
            safe_value = html.escape(reward["value"])
            await context.bot.send_message(
                chat_id=user_id, text=f"<code>{safe_value}</code>", parse_mode=ParseMode.HTML
            )
        elif reward["type"] == "text":
            await context.bot.send_message(chat_id=user_id, text=reward["value"])
        elif reward["type"] == "url":
            # Only reached here when cost > 0 — free url items open directly
            # as a link button and never hit this function.
            await context.bot.send_message(chat_id=user_id, text=f"🔗 {reward['label']}:\n{reward['value']}")
        return True
    except (TelegramError, FileNotFoundError) as e:
        logger.error("Failed to deliver reward %s to %s: %s", reward.get("id"), user_id, e)
        await context.bot.send_message(
            chat_id=user_id,
            text="⚠️ Something went wrong delivering that. Please try again shortly, or contact the admin.",
        )
        return False


async def redeem_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles taps on a reward button (anything routed through the bot —
    i.e. everything except a free 'url' item)."""
    query = update.callback_query
    user = query.from_user

    row = get_user(user.id)
    if row is None:
        await query.answer("Please tap /start first.", show_alert=True)
        return
    _, username, _, balance, verified = row
    if not verified:
        await query.answer("⚠️ Join the channel and tap ✅ Verify first.", show_alert=True)
        return

    reward_id = query.data.removeprefix("redeem_")
    rewards = get_rewards()
    reward = next((r for r in rewards if r["id"] == reward_id), None)
    if reward is None:
        await query.answer("⚠️ That reward isn't available anymore.", show_alert=True)
        return

    cost = reward.get("cost", 0)
    if balance < cost:
        await query.answer(
            f"🔒 You need {cost - balance} more referral(s) for this. You have {balance}/{cost}.",
            show_alert=True,
        )
        return

    await query.answer()

    if reward["type"] == "redeem":
        # Don't deduct yet — only once we actually have a valid email, so an
        # abandoned flow never costs the user anything.
        context.user_data["awaiting_email_for"] = reward_id
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"🎁 Redeeming: {reward['label']}\n"
                f"Cost: {cost} referral(s)\n\n"
                "📧 Please reply with the email address we should deliver it to.\n"
                "(Send /cancel to back out — nothing is deducted yet.)"
            ),
        )
        return

    # Instant items (url/file/code/text) — deliver right away, then deduct.
    delivered = await deliver_reward_item(context, user.id, reward)
    if delivered and cost > 0:
        add_referral_count(user.id, -cost)
        create_redemption(user.id, username, reward["id"], reward["label"], cost, "", "auto_delivered")


async def my_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy alias for /rewards, kept so anyone used to the old command
    name doesn't hit a dead end."""
    await rewards_command(update, context)


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


async def export_redemptions_csv(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    rows = get_all_redemptions()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "user_id", "username", "reward_id", "reward_label", "cost", "email", "status", "created_at"]
    )
    writer.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.name = "redemptions.csv"
    await context.bot.send_document(chat_id=chat_id, document=data, filename="redemptions.csv")


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
        rewards = get_rewards()
        await query.edit_message_text(
            f"🎁 Rewards\n{rewards_listing_text(rewards)}\n\n"
            "Tap an item to remove it, or use the buttons below.",
            reply_markup=rewards_menu_keyboard(rewards),
        )

    elif action == "svcrm":
        svc_id = parts[2]
        rewards = [r for r in get_rewards() if r["id"] != svc_id]
        save_rewards(rewards)
        await query.edit_message_text(
            f"🗑 Removed.\n\n🎁 Rewards\n{rewards_listing_text(rewards)}",
            reply_markup=rewards_menu_keyboard(rewards),
        )

    elif action == "svcadd":
        context.user_data["svc_new"] = {}
        context.user_data["awaiting"] = "svc_label"
        await query.edit_message_text(
            "Send the button label for the new reward (e.g. 🎨 Canva Pro — 1 Year):\n\n"
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
        elif svc_type == "redeem":
            prompt = (
                "Send a short product name/description (e.g. 'Canva Pro — 1 Year "
                "Subscription'). This is what you'll see in the spreadsheet and "
                "in the Telegram notification:"
            )
        else:
            prompt = "Send the plain text message to deliver:"
        await query.edit_message_text(f"{prompt}\n\n(Send /cancel to abort.)", reply_markup=cancel_keyboard())

    elif action == "svcreplace":
        context.user_data["awaiting"] = "svc_replace_raw"
        raw_current = rewards_to_raw_string(get_rewards())
        await query.edit_message_text(
            "Paste the FULL replacement list, in this format:\n"
            "Label|type|value|cost;Label|type|value|cost\n"
            "(type = url, file, code, text, or redeem — cost = referrals required, 0 = free)\n\n"
            f"Current:\n{raw_current or '(none)'}\n\n"
            "(Send /cancel to abort.)",
            reply_markup=cancel_keyboard(),
        )

    elif action == "redemptions":
        text, markup = redemptions_panel_content()
        await query.edit_message_text(text, reply_markup=markup)

    elif action == "fulfill":
        rid = int(parts[2])
        set_redemption_status(rid, "fulfilled")
        text, markup = redemptions_panel_content()
        await query.edit_message_text(text, reply_markup=markup)

    elif action == "export":
        await query.edit_message_text("📁 Building your CSV export…", reply_markup=admin_menu_keyboard())
        await export_redemptions_csv(context, query.from_user.id)


async def handle_admin_awaiting(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handles the admin's next text/file reply while a panel flow (edit,
    add referral, add/replace reward) is waiting on a value."""
    message = update.message
    awaiting = context.user_data.get("awaiting")

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
        _, _, _, balance, _ = get_user(target_id)
        context.user_data.clear()
        await message.reply_text(
            f"✅ User {target_id} now has {balance} referral(s).",
            reply_markup=admin_menu_keyboard(),
        )
        return

    if awaiting.startswith("edit:"):
        field_key = awaiting.split(":", 1)[1]
        field = EDITABLE_FIELDS.get(field_key)
        if not field:
            context.user_data.clear()
            return
        set_setting(field["setting_key"], text)
        context.user_data.clear()
        await message.reply_text(f"✅ Updated {field['label']}.", reply_markup=admin_menu_keyboard())
        return

    if awaiting == "svc_label":
        context.user_data.setdefault("svc_new", {})["label"] = text
        context.user_data["awaiting"] = "svc_type"
        await message.reply_text("Now pick the type:", reply_markup=reward_type_keyboard())
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
        context.user_data["awaiting"] = "svc_cost"
        prompt = (
            "How many referrals should this cost? Send a whole number (redeem "
            "items normally cost more than 0):"
            if svc_type == "redeem"
            else "How many referrals should this cost? Send a whole number (0 = free/instant delivery):"
        )
        await message.reply_text(prompt, reply_markup=cancel_keyboard())
        return

    if awaiting == "svc_cost":
        if not text.isdigit():
            await message.reply_text("Please send a whole number, e.g. 3.")
            return
        svc_new = context.user_data.get("svc_new", {})
        svc_new["cost"] = int(text)
        svc_new["id"] = secrets.token_hex(3)
        rewards = get_rewards()
        rewards.append(svc_new)
        save_rewards(rewards)
        context.user_data.clear()
        await message.reply_text(
            f"✅ Added.\n\n🎁 Rewards\n{rewards_listing_text(rewards)}",
            reply_markup=rewards_menu_keyboard(rewards),
        )
        return

    if awaiting == "svc_replace_raw":
        new_rewards = parse_rewards_string(text)
        save_rewards(new_rewards)
        context.user_data.clear()
        await message.reply_text(
            f"✅ Rewards replaced.\n\n🎁 Rewards\n{rewards_listing_text(new_rewards)}",
            reply_markup=rewards_menu_keyboard(new_rewards),
        )
        return

    # Unknown state somehow — reset defensively rather than getting stuck.
    context.user_data.clear()


async def handle_redeem_email(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handles a user's reply while the bot is waiting for their email to
    finish a 'redeem' reward. Deducts the balance, logs the request, and
    notifies the admin — only once we have a valid email in hand."""
    user = update.effective_user
    message = update.message
    reward_id = context.user_data.get("awaiting_email_for")

    if not is_valid_email(text):
        await message.reply_text("That doesn't look like a valid email address. Please try again, or send /cancel.")
        return

    email = text.strip()
    rewards = get_rewards()
    reward = next((r for r in rewards if r["id"] == reward_id), None)
    if reward is None:
        context.user_data.clear()
        await message.reply_text("⚠️ That reward isn't available anymore. Nothing was deducted.")
        return

    row = get_user(user.id)
    _, username, _, balance, _ = row
    cost = reward.get("cost", 0)
    if balance < cost:
        context.user_data.clear()
        await message.reply_text(
            f"⚠️ Your balance changed and you no longer have enough referrals for this "
            f"({balance}/{cost}). Nothing was deducted."
        )
        return

    add_referral_count(user.id, -cost)
    redemption_id = create_redemption(user.id, username, reward["id"], reward["label"], cost, email, "pending")
    context.user_data.clear()

    new_balance = balance - cost
    await message.reply_text(
        "✅ Request received!\n\n"
        f"🎁 {reward['label']}\n"
        f"📧 {email}\n\n"
        f"We'll deliver it to that email soon. Your remaining balance: {new_balance}."
    )

    # Best-effort: log to Google Sheets + ping the admin. Neither failing
    # ever loses the request — it's already safe in the local DB and
    # viewable from /admin -> Redemptions, or via /admin -> Export CSV.
    await asyncio.to_thread(sheets.append_redemption, user.id, username, email, reward["label"], cost)
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🎁 New redemption request (#{redemption_id})\n"
                    f"User: @{username} (ID: {user.id})\n"
                    f"Product: {reward['label']}\n"
                    f"Cost: {cost} referral(s)\n"
                    f"Email: {email}\n"
                    f"Their new balance: {new_balance}"
                ),
            )
        except TelegramError:
            pass


async def handle_free_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single dispatcher for all plain text/file replies:
    - the admin's config-edit flows (only when ADMIN_ID has one in progress)
    - a user's pending 'send me your email' reward redemption
    No-ops for everyone else / when nothing is in progress, so the bot
    doesn't reply to random chat messages."""
    user = update.effective_user
    message = update.message
    text = (message.text or message.caption or "").strip()

    if text == "/cancel":
        had_admin_flow = user.id == ADMIN_ID and bool(context.user_data.get("awaiting"))
        had_redeem_flow = bool(context.user_data.get("awaiting_email_for"))
        context.user_data.clear()
        if had_admin_flow:
            await message.reply_text("Cancelled.", reply_markup=admin_menu_keyboard())
        elif had_redeem_flow:
            await message.reply_text("Cancelled — nothing was deducted.")
        return

    if user.id == ADMIN_ID and context.user_data.get("awaiting"):
        await handle_admin_awaiting(update, context, text)
        return

    if context.user_data.get("awaiting_email_for"):
        await handle_redeem_email(update, context, text)
        return

    # No flow in progress for this user — stay quiet.


async def add_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: manually adjust a user's referral balance.
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
    _, _, _, balance, _ = get_user(target_id)

    await update.message.reply_text(f"✅ User {target_id} now has {balance} referral(s).")


# --------------------------------------------------------------------------
# Command menu (the "/" button next to the message box in Telegram)
# --------------------------------------------------------------------------
async def register_commands(application):
    """Populates Telegram's native command menu so people can tap a command
    instead of typing it. Admin sees extra entries; everyone else sees the
    plain user commands. Runs once automatically when the bot starts."""
    user_commands = [
        BotCommand("start", "Show the greeting & task"),
        BotCommand("rewards", "See your balance & redeem a reward"),
    ]
    await application.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    if ADMIN_ID:
        admin_commands = user_commands + [
            BotCommand("admin", "Open the admin control panel"),
            BotCommand("stats", "Quick usage stats"),
            BotCommand("addref", "Manually adjust a referral balance"),
        ]
        await application.bot.set_my_commands(
            admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID)
        )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    init_db()

    # Keep-alive server for hosts that require a bound port (e.g. Render free tier).
    threading.Thread(target=start_keep_alive_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(register_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("rewards", rewards_command))
    app.add_handler(CommandHandler("myservice", my_service))  # legacy alias
    app.add_handler(CommandHandler("addref", add_referral))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(show_referral_link, pattern="^show_ref$"))
    app.add_handler(CallbackQueryHandler(show_rewards_callback, pattern="^show_rewards$"))
    app.add_handler(CallbackQueryHandler(redeem_reward, pattern="^redeem_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm:"))
    # Must be last: catches free-text/file replies while a panel flow or a
    # redeem-email flow is waiting on a value. No-ops otherwise.
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_free_text_input))

    logger.info("Bot started. Polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""
Optional Google Sheets logging for reward redemptions.
--------------------------------------------------------
Totally optional. If you don't set GOOGLE_SERVICE_ACCOUNT_JSON and
GOOGLE_SHEET_ID (see env.example / GOOGLE_SHEETS_SETUP.md), the bot just
skips this quietly and still keeps every redemption safe via:
  - the local SQLite `redemptions` table (viewable from /admin -> Redemptions)
  - the one-tap CSV export in /admin -> Export CSV
  - an instant Telegram DM to the admin for every redemption request

Set the two env vars above and this module starts appending a row per
redemption to your sheet automatically — no code changes needed.
"""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_worksheet = None
_setup_attempted = False
_fulfilled_worksheet = None
_fulfilled_setup_attempted = False


def _connect():
    """Lazily connects to the configured Google Sheet. Returns the worksheet
    object, or None if it isn't configured or the connection failed. Only
    actually tries to connect once per process (cheap to call repeatedly)."""
    global _worksheet, _setup_attempted
    if _worksheet is not None:
        return _worksheet
    if _setup_attempted:
        return None
    _setup_attempted = True

    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not creds_json or not sheet_id:
        logger.info("Google Sheets not configured — redemptions will only be logged locally.")
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        info = json.loads(creds_json)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)

        worksheet_name = os.getenv("GOOGLE_SHEET_WORKSHEET", "Redemptions")
        try:
            ws = sh.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=10)
            ws.append_row(
                ["Timestamp (UTC)", "User ID", "Username", "Email", "Product", "Referral Cost"]
            )

        _worksheet = ws
        logger.info("Connected to Google Sheet %r, worksheet %r.", sheet_id, worksheet_name)
        return _worksheet
    except Exception:
        logger.exception(
            "Failed to connect to Google Sheets — redemptions will only be logged locally."
        )
        return None


def append_redemption(user_id: int, username: str, email: str, product: str, cost: int) -> bool:
    """Appends one row to the Google Sheet. Returns True on success, False if
    Sheets isn't configured or the write failed. This is BLOCKING (network
    I/O) — call it via asyncio.to_thread(...) from async handlers.
    A False return is never fatal: the local DB row + admin Telegram alert
    already have the request covered."""
    ws = _connect()
    if ws is None:
        return False
    try:
        ws.append_row(
            [
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                user_id,
                username or "",
                email,
                product,
                cost,
            ]
        )
        return True
    except Exception:
        logger.exception("Failed to append redemption row to Google Sheet.")
        return False


def _connect_fulfilled():
    """Lazily connects to a second tab in the same spreadsheet used for
    fulfillment + validity records (kept separate from the pending-request
    log above so neither format has to change). Returns the worksheet, or
    None if Sheets isn't configured or the connection failed."""
    global _fulfilled_worksheet, _fulfilled_setup_attempted
    if _fulfilled_worksheet is not None:
        return _fulfilled_worksheet
    if _fulfilled_setup_attempted:
        return None
    _fulfilled_setup_attempted = True

    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not creds_json or not sheet_id:
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        info = json.loads(creds_json)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)

        worksheet_name = os.getenv("GOOGLE_SHEET_FULFILLED_WORKSHEET", "Fulfilled")
        try:
            ws = sh.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=10)
            ws.append_row(
                ["Fulfilled At (UTC)", "User ID", "Email", "Product", "Duration", "Valid Until"]
            )

        _fulfilled_worksheet = ws
        logger.info("Connected to Google Sheet %r, worksheet %r.", sheet_id, worksheet_name)
        return _fulfilled_worksheet
    except Exception:
        logger.exception(
            "Failed to connect to the Fulfilled worksheet — fulfillment/validity "
            "dates will only be kept in the local DB."
        )
        return None


def append_fulfillment(user_id: int, email: str, product: str, duration_label: str, fulfilled_at, valid_until) -> bool:
    """Appends one row recording that a redemption was fulfilled, plus its
    computed validity end date if it has one (`duration_label` is a
    human-readable string like "1 year" or "no expiry"; `valid_until` is a
    date or None). Returns True on success, False if Sheets isn't configured
    or the write failed. This is BLOCKING (network I/O) — call it via
    asyncio.to_thread(...) from async handlers.
    A False return is never fatal: the local DB row already has the
    fulfilled_at/valid_until values, and this is only an extra safety net
    since Render's free plan wipes local storage on restart."""
    ws = _connect_fulfilled()
    if ws is None:
        return False
    try:
        ws.append_row(
            [
                fulfilled_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                user_id,
                email,
                product,
                duration_label,
                valid_until.strftime("%Y-%m-%d") if valid_until else "",
            ]
        )
        return True
    except Exception:
        logger.exception("Failed to append fulfillment row to Google Sheet.")
        return False


def is_configured() -> bool:
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and os.getenv("GOOGLE_SHEET_ID"))

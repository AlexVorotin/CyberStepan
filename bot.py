"""
bot.py — DTEK blackout monitor for С. Кощіївка, Вул. Лісова, 1а

- Monitoring starts automatically when the bot launches (no /start needed)
- /status  — show current schedule now
- /stop    — pause monitoring
- /resume  — resume monitoring

    export TELEGRAM_BOT_TOKEN="your_token"
    export CHAT_ID="your_telegram_chat_id"   # get from @userinfobot
    python bot.py
"""
import asyncio
import logging
import os
import datetime
import hashlib
import json
from pathlib import Path

from typing import Optional

HASH_FILE = Path("last_hash.json")

def _load_hashes() -> dict:
    try:
        return json.loads(HASH_FILE.read_text()) if HASH_FILE.exists() else {}
    except Exception:
        return {}

def _save_hashes(hashes: dict) -> None:
    try:
        HASH_FILE.write_text(json.dumps(hashes))
    except Exception as e:
        logger.warning("Could not save hashes: %s", e)
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from scraper import get_schedule

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
# Your personal Telegram chat ID — get it by messaging @userinfobot
# Or leave empty and use /start to register
CHAT_ID   = os.getenv("CHAT_ID", "")

# ── Hardcoded address ─────────────────────────────────────────────────────────
CITY     = "С. Кощіївка"
STREET   = "Вул. Лісова"
BUILDING = "1а"
CHECK_INTERVAL = 300  # 5 minutes

# In-memory state — loaded from file to survive restarts
_last_hash: dict = _load_hashes()

CHAT_IDS_FILE = Path("chat_ids.json")

def _load_chat_ids() -> set:
    try:
        return set(json.loads(CHAT_IDS_FILE.read_text())) if CHAT_IDS_FILE.exists() else set()
    except Exception:
        return set()

def _save_chat_ids(ids: set) -> None:
    try:
        CHAT_IDS_FILE.write_text(json.dumps(list(ids)))
    except Exception as e:
        logger.warning("Could not save chat_ids: %s", e)

_chat_ids: set = _load_chat_ids()


# ── Time helpers ──────────────────────────────────────────────────────────────

def _outage_ranges(slots: dict) -> list[str]:
    intervals = []
    for slot, status in slots.items():
        if status not in ("off", "first30", "last30"):
            continue
        try:
            h_start, h_end = int(slot.split("-")[0]), int(slot.split("-")[1])
        except Exception:
            continue
        if status == "first30":
            intervals.append((h_start * 60, h_start * 60 + 30))
        elif status == "last30":
            intervals.append((h_start * 60 + 30, h_end * 60))
        else:
            intervals.append((h_start * 60, h_end * 60))

    if not intervals:
        return []

    intervals.sort()
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    def fmt(m): return f"{m // 60:02d}:{m % 60:02d}"
    return [f"{fmt(s)}–{fmt(e)}" for s, e in merged]


MONTHS_UK = ["січня","лютого","березня","квітня","травня","червня",
             "липня","серпня","вересня","жовтня","листопада","грудня"]

def _date_uk(dt: datetime.datetime) -> str:
    return f"{dt.day} {MONTHS_UK[dt.month - 1]}"

DAYS = ["Понеділок","Вівторок","Середа","Четвер","П'ятниця","Субота","Неділя"]


# ── Formatting ────────────────────────────────────────────────────────────────

def _format_day_card(label: str, slots: dict) -> str:
    ranges = _outage_ranges(slots)
    maybe_slots = {s: v for s, v in slots.items() if v == "maybe"}
    maybe_ranges = _outage_ranges({s: "off" for s in maybe_slots})

    lines = [f"📅 *{label}*"]
    if ranges:
        for r in ranges:
            start, end = r.split("–")
            lines.append(f"🔴 `OFF`  з {start} до {end}")
    elif maybe_ranges:
        for r in maybe_ranges:
            start, end = r.split("–")
            lines.append(f"🟡 `~OFF`  з {start} до {end}")
    else:
        lines.append("🟢 `ON`  Світло буде весь день")
    return "\n".join(lines)


def _format_message(result: dict, prefix: str = "") -> str:
    now         = datetime.datetime.now()
    tomorrow_dt = now + datetime.timedelta(days=1)

    lines = []
    if prefix:
        lines.append(prefix)
        lines.append("")
    lines += [
        _format_day_card(
            f"Сьогодні ({_date_uk(now)}, {DAYS[now.weekday()]})",
            result.get("_today", {})
        ),
        "",
        _format_day_card(
            f"Завтра ({_date_uk(tomorrow_dt)}, {DAYS[tomorrow_dt.weekday()]})",
            result.get("_tomorrow", {})
        ),
        "",
        f"📍 _{CITY}, {STREET}, {BUILDING}_",
    ]
    return "\n".join(lines)


def _has_outages(result: dict) -> bool:
    """Return True if today or tomorrow has any real outage."""
    for key in ("_today", "_tomorrow"):
        slots = result.get(key, {})
        if any(v != "on" for v in slots.values()):
            return True
    return False


def _result_hash(result: dict) -> str:
    # If no outages, include date so midnight rollover triggers a notification
    # If there are outages, date is NOT included — only real schedule changes notify
    data = {
        "today":    result.get("_today", {}),
        "tomorrow": result.get("_tomorrow", {}),
    }
    if not _has_outages(result):
        data["date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()


# ── Fetch helper ──────────────────────────────────────────────────────────────

async def _fetch(retries: int = 3) -> Optional[dict]:
    """Fetch schedule with retries — DTEK site is sometimes slow to respond."""
    for attempt in range(1, retries + 1):
        try:
            result = await get_schedule(CITY, STREET, BUILDING)
            if result:
                logger.info("Fetched OK (attempt %d) — today non-on: %d, tomorrow non-on: %d",
                            attempt,
                            sum(1 for v in result.get("_today",{}).values() if v != "on"),
                            sum(1 for v in result.get("_tomorrow",{}).values() if v != "on"))
                return result
            else:
                logger.warning("get_schedule returned None (attempt %d/%d)", attempt, retries)
        except Exception as e:
            logger.error("Fetch error (attempt %d/%d): %s", attempt, retries, e)

        if attempt < retries:
            wait = attempt * 10  # 10s, 20s between retries
            logger.info("Retrying in %ds...", wait)
            await asyncio.sleep(wait)

    logger.error("All %d fetch attempts failed", retries)
    return None


# ── Monitor job ───────────────────────────────────────────────────────────────

async def _monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Monitor check running...")
    result = await _fetch()

    if not result:
        logger.warning("Monitor: no data, skipping")
        return

    new_hash = _result_hash(result)
    msg = _format_message(result, prefix="🔔 *Графік змінився!*")

    # Notify all registered chats where hash changed
    notified = 0
    for chat_id in list(_chat_ids):
        old_hash = _last_hash.get(chat_id, "")
        if new_hash != old_hash:
            _last_hash[chat_id] = new_hash
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=msg, parse_mode="Markdown"
                )
                notified += 1
                _save_hashes(_last_hash)
                logger.info("Change notification sent to %s", chat_id)
            except Exception as e:
                logger.error("Send error to %s: %s", chat_id, e)
        else:
            logger.info("No change for chat %s (hash=%s)", chat_id, new_hash[:8])

    if not _chat_ids:
        logger.warning("No registered chats — send /status to the bot to register")


# ── Command handlers ──────────────────────────────────────────────────────────

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _chat_ids.add(chat_id)   # register this chat for monitoring
    _save_chat_ids(_chat_ids)

    await update.message.reply_text("⏳ Завантажую графік…")
    result = await _fetch()

    if not result:
        await update.message.reply_text("❌ Не вдалося отримати дані. Спробуйте /status пізніше.")
        return

    _last_hash[chat_id] = _result_hash(result)
    _save_hashes(_last_hash)
    await update.message.reply_text(_format_message(result), parse_mode="Markdown")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same as /status — register and show schedule."""
    await status_cmd(update, context)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _chat_ids.discard(chat_id)


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _chat_ids.add(chat_id)
    _save_chat_ids(_chat_ids)
    # Reset hash so next check always sends current state
    _last_hash.pop(chat_id, None)
    result = await _fetch()
    if result:
        _last_hash[chat_id] = _result_hash(result)
        await update.message.reply_text(_format_message(result), parse_mode="Markdown")


async def on_startup(app: Application) -> None:
    """Called once when bot starts — register CHAT_ID if set and start monitor."""
    if CHAT_ID:
        _chat_ids.add(CHAT_ID)
        logger.info("Auto-registered CHAT_ID=%s", CHAT_ID)
        # Send startup message
        try:
            result = await _fetch()
            if result:
                _last_hash[CHAT_ID] = _result_hash(result)
                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    text=_format_message(result),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error("Startup message error: %s", e)
    else:
        logger.warning("CHAT_ID not set. Send /status to the bot to register your chat.")

    logger.info("Monitor job scheduled every %ds", CHECK_INTERVAL)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    # Schedule monitor job
    app.job_queue.run_repeating(
        _monitor_job,
        interval=CHECK_INTERVAL,
        first=60,   # first check 60s after startup
        name="global_monitor",
    )

    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop",   stop_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))

    logger.info("Bot started. Monitoring %s, %s, %s every %ds",
                CITY, STREET, BUILDING, CHECK_INTERVAL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

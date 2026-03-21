"""
bot.py — DTEK blackout monitor for С. Кощіївка, Вул. Лісова, 1а

Commands:
  /start   — show today + tomorrow schedule immediately
  /stop    — stop auto-monitoring
  /resume  — resume auto-monitoring

Auto-checks every 5 minutes and sends a message when schedule changes.

    export TELEGRAM_BOT_TOKEN="8219410842:AAFgD-VNpx_XrkcFSh6VpkxhVHkxaOeUfxo"
    python bot.py
"""
import logging
import os
import datetime
import hashlib
import json

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from scraper import get_schedule

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8219410842:AAFgD-VNpx_XrkcFSh6VpkxhVHkxaOeUfxo")

# ── Hardcoded address ─────────────────────────────────────────────────────────
CITY     = "С. Кощіївка"
STREET   = "Вул. Лісова"
BUILDING = "1а"
CHECK_INTERVAL = 300  # 5 minutes


# ── Time helpers ──────────────────────────────────────────────────────────────

def _outage_ranges(slots: dict) -> list[str]:
    """
    Convert slot dict to human-readable time ranges with 30-min precision.
    e.g. {18-19: last30, 19-20: off, 20-21: off, 21-22: off} → ["18:30–22:00"]
    """
    intervals = []
    for slot, status in slots.items():
        if status not in ("off", "first30", "last30"):
            continue
        try:
            h_start, h_end = int(slot.split("-")[0]), int(slot.split("-")[1])
        except Exception:
            continue
        if status == "first30":
            intervals.append((h_start * 60,       h_start * 60 + 30))
        elif status == "last30":
            intervals.append((h_start * 60 + 30,  h_end * 60))
        else:
            intervals.append((h_start * 60,       h_end * 60))

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


def _format_day(label: str, slots: dict) -> str:
    """Format a single day's schedule as a text block."""
    if not slots:
        return f"{label}\nℹ️ Дані недоступні"

    ranges = _outage_ranges(slots)
    maybe_slots = {s: v for s, v in slots.items() if v == "maybe"}
    maybe_ranges = _outage_ranges({s: "off" for s in maybe_slots})

    lines = [f"*{label}*"]

    if ranges:
        lines.append(f"🔴 Без світла: `{'`, `'.join(ranges)}`")
    elif maybe_ranges:
        lines.append(f"🟡 Можливо відключення: `{'`, `'.join(maybe_ranges)}`")
    else:
        lines.append("✅ Відключень не заплановано")

    return "\n".join(lines)


MONTHS_UK = ["січня","лютого","березня","квітня","травня","червня",
             "липня","серпня","вересня","жовтня","листопада","грудня"]


def _date_uk(dt: datetime.datetime) -> str:
    return f"{dt.day} {MONTHS_UK[dt.month - 1]}"


def _format_day_card(label: str, slots: dict) -> str:
    """Format a single day as a card matching DTEK channel style."""
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


def _format_message(result: dict) -> str:
    now         = datetime.datetime.now()
    tomorrow_dt = now + datetime.timedelta(days=1)
    DAYS = ["Понеділок","Вівторок","Середа","Четвер","П'ятниця","Субота","Неділя"]
    today_name    = DAYS[now.weekday()]
    tomorrow_name = DAYS[tomorrow_dt.weekday()]

    queue = result.get("_queue", "Черга 4.1")

    lines = [
        "⚡ *ДТЕК | Київщина*",
        f"📍 {CITY}, {STREET}, {BUILDING}",
        f"🔄 {queue}",
        f"🕐 {now.strftime('%H:%M')}  {_date_uk(now)}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        _format_day_card(f"Сьогодні — {_date_uk(now)} ({today_name})",
                         result.get("_today", {})),
        "",
        _format_day_card(f"Завтра — {_date_uk(tomorrow_dt)} ({tomorrow_name})",
                         result.get("_tomorrow", {})),
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "_dtek-krem.com.ua_",
    ]
    return "\n".join(lines)


def _result_hash(result: dict) -> str:
    data = {
        "today":    result.get("_today", {}),
        "tomorrow": result.get("_tomorrow", {}),
    }
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()


# ── Command handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Завантажую графік…")

    result = await get_schedule(CITY, STREET, BUILDING)
    if not result:
        await update.message.reply_text("❌ Не вдалося отримати дані. Спробуйте пізніше.")
        return

    context.bot_data.setdefault("last_hash", {})[chat_id] = _result_hash(result)
    await update.message.reply_text(_format_message(result), parse_mode="Markdown")

    # Start auto-monitoring for this chat
    _start_monitor(context, chat_id, update.effective_user.id)
    await update.message.reply_text(
        "🔔 Автоматичний моніторинг увімкнено — перевірка кожні 5 хв\n"
        "/stop — зупинити  |  /resume — відновити"
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _cancel_monitor(context, chat_id)
    await update.message.reply_text("⏹ Моніторинг зупинено. /resume — відновити")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _start_monitor(context, chat_id, update.effective_user.id)
    await update.message.reply_text("✅ Моніторинг відновлено — перевірка кожні 5 хв\n/stop — зупинити")


# ── Monitoring ────────────────────────────────────────────────────────────────

def _job_name(chat_id: int) -> str:
    return f"monitor_{chat_id}"


def _cancel_monitor(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    for job in context.job_queue.get_jobs_by_name(_job_name(chat_id)):
        job.schedule_removal()


def _start_monitor(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    _cancel_monitor(context, chat_id)
    context.job_queue.run_repeating(
        _monitor_job,
        interval=CHECK_INTERVAL,
        first=CHECK_INTERVAL,
        data={"chat_id": chat_id},
        name=_job_name(chat_id),
        chat_id=chat_id,
        user_id=user_id,
    )
    logger.info("Monitor started for chat %s", chat_id)


async def _monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    logger.info("Checking schedule for chat %s", chat_id)

    try:
        result = await get_schedule(CITY, STREET, BUILDING)
    except Exception as e:
        logger.error("Monitor fetch error: %s", e)
        return

    if not result:
        return

    new_hash = _result_hash(result)
    last_hash = context.bot_data.get("last_hash", {}).get(chat_id, "")

    if new_hash != last_hash:
        context.bot_data.setdefault("last_hash", {})[chat_id] = new_hash
        msg = "🔔 *Графік змінився!*\n\n" + _format_message(result)
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            logger.info("Change notification sent to chat %s", chat_id)
        except Exception as e:
            logger.error("Send error: %s", e)
    else:
        logger.info("No change for chat %s", chat_id)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("stop",   stop_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    logger.info("Bot started — monitoring %s, %s, %s", CITY, STREET, BUILDING)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

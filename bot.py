"""Telegram birthday bot.

Every day at 07:00 (Europe/Berlin) it checks geburtstage.csv and sends ONE message
with all of today's birthdays to everyone who subscribed with /start.

Test without Telegram:   python bot.py --check 02.01.
"""
import calendar
import csv
import io
import json
import logging
import os
import re
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("birthday-bot")

TZ = ZoneInfo("Europe/Berlin")
SEND_TIME = time(7, 0, tzinfo=TZ)

BASE_DIR = Path(__file__).parent
CSV_FILE = BASE_DIR / "geburtstage.csv"
CSV_URL = os.environ.get("CSV_URL")  # optional: load CSV from GitHub instead of local file
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # only for private repos + CSV_URL
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
STATE_FILE = DATA_DIR / "state.json"

# Your CSV stores names as "Nachname Vorname" -> shown as "Vorname Nachname"
LASTNAME_FIRST = os.environ.get("LASTNAME_FIRST", "true").lower() == "true"
SHOW_AGE = os.environ.get("SHOW_AGE", "true").lower() == "true"


# ---------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"subs": [], "last_sent": None}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


# ---------------------------------------------------------------- CSV
def read_csv_text() -> str:
    if CSV_URL:
        headers = {}
        if GITHUB_TOKEN:
            headers = {
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.raw+json",
            }
        r = requests.get(CSV_URL, headers=headers, timeout=20)
        r.raise_for_status()
        return r.content.decode("utf-8-sig")
    return CSV_FILE.read_text(encoding="utf-8-sig")


def parse_date(s: str):
    """'02.01.2008', '4.1.2010', '14.03.' or '2008-03-14' -> (day, month, year|None)."""
    s = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})?", s)
        if not m:
            return None
        d, mo, y = int(m[1]), int(m[2]), (int(m[3]) if m[3] else None)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return d, mo, y


def display_name(raw: str) -> str:
    parts = raw.split()
    if LASTNAME_FIRST and len(parts) > 1:
        parts = parts[1:] + parts[:1]
    return " ".join(parts)


def parse_rows(text: str):
    """Yield (name, (day, month, year)) for every usable row.

    Works with your layout (",1,Nachname Vorname,01.01.1995,31,Schwester,01011995")
    and also with a simple "name,birthday" file: the birthday is the first cell that
    looks like a date, the name is the nearest text cell before it. Everything else
    (row number, age, Bruder/Schwester, helper column, empty rows) is ignored.
    """
    delimiter = ";" if text.count(";") > text.count(",") else ","
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        cells = [c.strip() for c in row]
        for j, cell in enumerate(cells):
            parsed = parse_date(cell)
            if parsed:
                break
        else:
            continue
        name = next((c for c in reversed(cells[:j]) if re.search(r"[^\W\d_]", c)), None)
        if name:
            yield name, parsed


def birthdays_on(today: date) -> list[str]:
    targets = {(today.day, today.month)}
    if today.month == 2 and today.day == 28 and not calendar.isleap(today.year):
        targets.add((29, 2))  # 29 Feb birthdays are celebrated on 28 Feb in non-leap years

    result = []
    for name, (day, month, year) in parse_rows(read_csv_text()):
        if (day, month) in targets:
            label = display_name(name)
            if SHOW_AGE and year:
                label += f" ({today.year - year})"
            result.append(label)
    return sorted(result)


def build_message(names: list[str]) -> str:
    if len(names) == 1:
        return f"🎂 Heute hat Geburtstag:\n• {names[0]}\n\nGratuliert ihm/ihr! 🎉"
    return (
        "🎂 Heute haben Geburtstag:\n"
        + "\n".join(f"• {n}" for n in names)
        + "\n\nGratuliert ihnen! 🎉"
    )


# ---------------------------------------------------------------- Telegram
async def send_birthdays(bot) -> None:
    """Send today's message once per day (safe to call several times)."""
    today = datetime.now(TZ).date()
    state = load_state()
    if state["last_sent"] == today.isoformat():
        return
    try:
        names = birthdays_on(today)
    except Exception:
        log.exception("Could not load birthdays")
        return

    state["last_sent"] = today.isoformat()
    if names:
        from telegram.error import Forbidden

        text = build_message(names)
        for chat_id in list(state["subs"]):
            try:
                await bot.send_message(chat_id, text)
            except Forbidden:  # user blocked the bot -> unsubscribe
                state["subs"].remove(chat_id)
            except Exception:
                log.exception("Sending to %s failed", chat_id)
    save_state(state)


async def daily_job(context):
    await send_birthdays(context.bot)


async def startup_catchup(context):
    """If the bot was restarted/redeployed after 07:00, still send today's message."""
    if 7 <= datetime.now(TZ).hour < 12:
        await send_birthdays(context.bot)


async def cmd_start(update, context):
    state = load_state()
    chat_id = update.effective_chat.id
    if chat_id not in state["subs"]:
        state["subs"].append(chat_id)
        save_state(state)
    await update.message.reply_text(
        "Du bekommst jetzt jeden Morgen um 7 Uhr eine Nachricht, wenn jemand "
        "Geburtstag hat. 🎉\nMit /stop meldest du dich ab, mit /heute siehst du die "
        "heutigen Geburtstage."
    )


async def cmd_stop(update, context):
    state = load_state()
    chat_id = update.effective_chat.id
    if chat_id in state["subs"]:
        state["subs"].remove(chat_id)
        save_state(state)
    await update.message.reply_text("Du bist abgemeldet. Mit /start geht's wieder los.")


async def cmd_heute(update, context):
    names = birthdays_on(datetime.now(TZ).date())
    await update.message.reply_text(
        build_message(names) if names else "Heute hat niemand Geburtstag."
    )


def main() -> None:
    from telegram.ext import Application, CommandHandler

    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("heute", cmd_heute))
    app.job_queue.run_daily(
        daily_job, time=SEND_TIME, name="daily", job_kwargs={"misfire_grace_time": 3600}
    )
    app.job_queue.run_once(startup_catchup, when=10)
    app.run_polling()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        # python bot.py --check 02.01.   (or no date = today)
        if len(sys.argv) > 2:
            d, mo, _ = parse_date(sys.argv[2])
            day = date(2026, mo, d)
        else:
            day = datetime.now(TZ).date()
        names = birthdays_on(day)
        print(build_message(names) if names else f"Niemand am {day:%d.%m.}")
    else:
        main()

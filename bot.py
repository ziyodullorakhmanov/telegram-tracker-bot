"""
Daily Task & Goal Tracker Telegram Bot
----------------------------------------
Kunlik tasklar, maqsadlar va ish sifatini (1-10 reyting) kuzatib boradigan bot.

Ishga tushirish:
    1. .env fayl yarating va ichiga: BOT_TOKEN=xxxxx yozing
    2. pip install -r requirements.txt
    3. python bot.py
"""

import logging
import os
import sqlite3
from datetime import datetime, date, time as dtime, timedelta
from contextlib import closing

import hashlib
import hmac
import json
import threading
import time as time_module
from urllib.parse import parse_qsl

import httpx

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    TypeHandler,
    filters,
)

try:
    from anthropic import Anthropic
except ImportError:  # anthropic o'rnatilmagan bo'lsa ham bot ishlashda davom etsin
    Anthropic = None

try:
    import uvicorn
    from fastapi import FastAPI, Query
    from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:  # fastapi/uvicorn o'rnatilmagan bo'lsa, mini-app o'chiriladi
    uvicorn = None
    FastAPI = None

# ---------------------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "tracker.db")
DEFAULT_REMINDER_TIME = "21:00"  # HH:MM, foydalanuvchi mahalliy vaqti bo'yicha

# Server (Railway) doim UTC vaqtida ishlaydi. Bu offset orqali foydalanuvchi
# kiritgan mahalliy vaqt avtomatik UTC'ga o'giriladi. Toshkent = UTC+5 (standart).
TZ_OFFSET_HOURS = int(os.environ.get("TZ_OFFSET_HOURS", "5"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
ai_client = Anthropic(api_key=ANTHROPIC_API_KEY) if (Anthropic and ANTHROPIC_API_KEY) else None

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# AI_PROVIDER: "auto" (Gemini bo'lsa shuni, aks holda Anthropic), "gemini" yoki "anthropic"
AI_PROVIDER = os.environ.get("AI_PROVIDER", "auto").strip().lower()

# Railway'da Settings -> Networking -> Generate Domain orqali olingan havola,
# masalan: https://telegram-tracker-bot-production.up.railway.app
WEBAPP_BASE_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
if WEBAPP_BASE_URL and not WEBAPP_BASE_URL.startswith(("http://", "https://")):
    WEBAPP_BASE_URL = "https://" + WEBAPP_BASE_URL  # https:// unutilgan bo'lsa ham ishlasin
WEBAPP_URL = f"{WEBAPP_BASE_URL}/webapp" if WEBAPP_BASE_URL else None
PORT = int(os.environ.get("PORT", "8080"))

# Faqat shu Telegram foydalanuvchi ID'larga ruxsat berish uchun (vergul bilan ajratilgan
# bo'lishi mumkin, masalan "12345,67890"). Bo'sh bo'lsa — cheklov yo'q (hamma foydalana oladi).
ALLOWED_USER_IDS = {
    uid.strip() for uid in os.environ.get("ALLOWED_USER_ID", "").split(",") if uid.strip()
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                name TEXT,
                reminder_time TEXT DEFAULT '21:00',
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT,
                task_date TEXT,
                done INTEGER DEFAULT 0,
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT,
                log_date TEXT,
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                rating_date TEXT,
                rating INTEGER,
                note TEXT,
                UNIQUE(chat_id, rating_date)
            )"""
        )
        # Eski bazalarda deadline_time ustuni bo'lmasligi mumkin — mavjud bo'lmasa qo'shamiz
        try:
            conn.execute("ALTER TABLE goals ADD COLUMN deadline_time TEXT")
        except sqlite3.OperationalError:
            pass  # ustun allaqachon mavjud
        try:
            conn.execute("ALTER TABLE goals ADD COLUMN failed INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # ustun allaqachon mavjud


def escape_md(text: str) -> str:
    """Telegram Markdown (v1) maxsus belgilarini xavfsiz qilib escape qiladi.
    Foydalanuvchi kiritgan yoki AI generatsiya qilgan matnni Markdown bilan
    yuborishdan oldin ishlatiladi, aks holda 'Can't parse entities' xatosi chiqadi."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def today_str():
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# VAQT ZONASI YORDAMCHILARI
# ---------------------------------------------------------------------------
# Server har doim UTC vaqtida ishlaydi. Foydalanuvchi kiritadigan barcha
# vaqtlar (kechki eslatma, task muddati) MAHALLIY vaqt deb qabul qilinadi
# va rejalashtirishdan oldin UTC'ga o'giriladi.

def local_now() -> datetime:
    """Foydalanuvchining hozirgi mahalliy vaqti (server UTC vaqtiga offset qo'shilgan)."""
    return datetime.now() + timedelta(hours=TZ_OFFSET_HOURS)


def local_time_to_utc_time(local_t: dtime) -> dtime:
    """Faqat vaqt (HH:MM) uchun — kunlik takrorlanuvchi eslatmalarda ishlatiladi."""
    dummy = datetime.combine(date(2000, 1, 1), local_t) - timedelta(hours=TZ_OFFSET_HOURS)
    return dummy.time()


def local_datetime_to_utc(local_dt: datetime) -> datetime:
    """To'liq sana+vaqt uchun — bir martalik (deadline) eslatmalarda ishlatiladi."""
    return local_dt - timedelta(hours=TZ_OFFSET_HOURS)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def ensure_user(chat_id: int, name: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, name, reminder_time, created_at) VALUES (?,?,?,?)",
            (chat_id, name, DEFAULT_REMINDER_TIME, datetime.now().isoformat()),
        )


def add_goal(chat_id: int, text: str) -> int:
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO goals (chat_id, text, task_date, created_at) VALUES (?,?,?,?)",
            (chat_id, text, today_str(), datetime.now().isoformat()),
        )
        return cur.lastrowid


def set_goal_deadline(chat_id: int, goal_id: int, deadline_time: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE goals SET deadline_time=? WHERE id=? AND chat_id=?",
            (deadline_time, goal_id, chat_id),
        )


def get_goal(chat_id: int, goal_id: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM goals WHERE id=? AND chat_id=?", (goal_id, chat_id)
        ).fetchone()


def get_pending_deadlines_today(chat_id: int = None):
    """Bugungi, hali bajarilmagan va muddati bor tasklarni qaytaradi
    (bot qayta ishga tushganda eslatmalarni tiklash uchun)."""
    with closing(get_conn()) as conn:
        if chat_id is not None:
            return conn.execute(
                """SELECT * FROM goals WHERE chat_id=? AND task_date=? 
                   AND done=0 AND deadline_time IS NOT NULL""",
                (chat_id, today_str()),
            ).fetchall()
        return conn.execute(
            """SELECT * FROM goals WHERE task_date=? AND done=0 AND deadline_time IS NOT NULL""",
            (today_str(),),
        ).fetchall()


def get_today_goals(chat_id: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT * FROM goals WHERE chat_id=? AND task_date=? ORDER BY id",
            (chat_id, today_str()),
        ).fetchall()


def mark_done(chat_id: int, goal_id: int) -> bool:
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE goals SET done=1, failed=0 WHERE id=? AND chat_id=?", (goal_id, chat_id)
        )
        return cur.rowcount > 0


def mark_failed(chat_id: int, goal_id: int) -> bool:
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "UPDATE goals SET failed=1, done=0 WHERE id=? AND chat_id=?", (goal_id, chat_id)
        )
        return cur.rowcount > 0


def add_log(chat_id: int, text: str):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "INSERT INTO logs (chat_id, text, log_date, created_at) VALUES (?,?,?,?)",
            (chat_id, text, today_str(), datetime.now().isoformat()),
        )


def save_rating(chat_id: int, rating: int, note: str = ""):
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """INSERT INTO ratings (chat_id, rating_date, rating, note) VALUES (?,?,?,?)
               ON CONFLICT(chat_id, rating_date) DO UPDATE SET rating=excluded.rating, note=excluded.note""",
            (chat_id, today_str(), rating, note),
        )


def get_stats(chat_id: int, days: int = 7):
    with closing(get_conn()) as conn:
        goals = conn.execute(
            """SELECT SUM(done) as done, SUM(failed) as failed, COUNT(*) as total
               FROM goals WHERE chat_id=? AND task_date >= date('now', ?)""",
            (chat_id, f"-{days} days"),
        ).fetchone()
        ratings = conn.execute(
            "SELECT AVG(rating) as avg_r, COUNT(*) as n FROM ratings WHERE chat_id=? AND rating_date >= date('now', ?)",
            (chat_id, f"-{days} days"),
        ).fetchone()
        return goals, ratings


def get_today_logs(chat_id: int):
    with closing(get_conn()) as conn:
        return conn.execute(
            "SELECT text, created_at FROM logs WHERE chat_id=? AND log_date=? ORDER BY id",
            (chat_id, today_str()),
        ).fetchall()


def _active_ai_provider() -> str | None:
    """Qaysi AI provayder ishlatilishini aniqlaydi: 'gemini', 'anthropic' yoki None."""
    if AI_PROVIDER == "gemini":
        return "gemini" if GEMINI_API_KEY else None
    if AI_PROVIDER == "anthropic":
        return "anthropic" if ai_client else None
    # "auto": Gemini kaliti bo'lsa, shuni afzal ko'ramiz (bepul), aks holda Anthropic
    if GEMINI_API_KEY:
        return "gemini"
    if ai_client:
        return "anthropic"
    return None


async def _call_gemini(prompt: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            logger.error(f"Gemini API xatosi {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
    try:
        candidates = data["candidates"]
        parts = candidates[0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        logger.error(f"Gemini javobi kutilmagan formatda: {data}")
        return ""


async def _call_anthropic(prompt: str) -> str:
    response = ai_client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "\n".join(parts).strip()


async def get_ai_feedback(chat_id: int) -> str:
    """Bugungi tasklar va loglarni AI'ga yuborib, ish sifati bo'yicha
    qisqa tahlil va 1-10 taklif reytingini oladi (Gemini yoki Anthropic)."""
    provider = _active_ai_provider()
    if not provider:
        return (
            "⚠️ AI tahlil hozircha yoqilmagan. Buni yoqish uchun Railway'da "
            "`GEMINI_API_KEY` (bepul, tavsiya etiladi) yoki `ANTHROPIC_API_KEY` "
            "muhit o'zgaruvchisini qo'shing."
        )

    goals = get_today_goals(chat_id)
    logs = get_today_logs(chat_id)

    if not goals and not logs:
        return "Bugun hali hech qanday task yoki yozuv qo'shmadingiz — tahlil qilishga narsa yo'q."

    def goal_status(g):
        if g["done"]:
            return "bajarildi"
        if g["failed"]:
            return "bajarilmadi (qilolmadi)"
        return "hali belgilanmagan"

    goals_text = "\n".join(f"- [{goal_status(g)}] {g['text']}" for g in goals) or "(task qo'shilmagan)"
    logs_text = "\n".join(f"- {l['text']}" for l in logs) or "(yozuv yo'q)"

    prompt = (
        "Siz mehribon, lekin halol shaxsiy mahsuldorlik murabbiysiz. "
        "Quyida foydalanuvchining bugungi tasklari va u yozgan ish loglari berilgan. "
        "O'zbek tilida, 4-6 jumlada qisqa tahlil bering: nima yaxshi ketdi, "
        "nimani yaxshilash mumkin, va oxirida \"Taklif reyting: X/10\" formatida "
        "ish sifati bo'yicha o'z bahoingizni yozing.\n\n"
        f"Bugungi tasklar:\n{goals_text}\n\nBugungi yozuvlar (loglar):\n{logs_text}"
    )

    try:
        if provider == "gemini":
            result = await _call_gemini(prompt)
        else:
            result = await _call_anthropic(prompt)
        return result or "AI javob berolmadi, birozdan keyin qayta urinib ko'ring."
    except Exception as e:
        logger.error(f"AI feedback xatosi ({provider}): {e}")
        return "⚠️ AI tahlil olishda xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring."


def get_chart_data(chat_id: int, period: str = "daily"):
    """period: 'daily' (oxirgi 14 kun), 'weekly' (oxirgi 12 hafta),
    'monthly' (oxirgi 12 oy)."""
    if period == "weekly":
        goal_group = "strftime('%Y-W%W', task_date)"
        rating_group = "strftime('%Y-W%W', rating_date)"
        window = "-84 days"  # ~12 hafta
    elif period == "monthly":
        goal_group = "strftime('%Y-%m', task_date)"
        rating_group = "strftime('%Y-%m', rating_date)"
        window = "-365 days"  # ~12 oy
    else:
        goal_group = "task_date"
        rating_group = "rating_date"
        window = "-14 days"

    with closing(get_conn()) as conn:
        goal_rows = conn.execute(
            f"""SELECT {goal_group} as d, SUM(done) as done, SUM(failed) as failed, COUNT(*) as total
               FROM goals WHERE chat_id=? AND task_date >= date('now', ?)
               GROUP BY d ORDER BY d""",
            (chat_id, window),
        ).fetchall()
        rating_rows = conn.execute(
            f"""SELECT {rating_group} as d, AVG(rating) as rating FROM ratings
               WHERE chat_id=? AND rating_date >= date('now', ?)
               GROUP BY d ORDER BY d""",
            (chat_id, window),
        ).fetchall()
    return {
        "period": period,
        "goals": [
            {"date": r["d"], "done": r["done"], "failed": r["failed"], "total": r["total"]}
            for r in goal_rows
        ],
        "ratings": [{"date": r["d"], "rating": round(r["rating"], 1)} for r in rating_rows],
    }


# ---------------------------------------------------------------------------
# ASOSIY MENYU (reply keyboard)
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("➕ Task qo'shish"), KeyboardButton("📋 Bugungi tasklar")],
        [KeyboardButton("🌙 Kunni yakunlash"), KeyboardButton("📊 Statistika")],
        [KeyboardButton("🧠 AI tahlil"), KeyboardButton("⚙️ Vaqtni sozlash")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def day_progress_text(chat_id: int) -> str:
    goals = get_today_goals(chat_id)
    if not goals:
        return "Bugun hali maqsad qo'shmadingiz. /goal buyrug'i bilan qo'shing."
    done = sum(1 for g in goals if g["done"])
    failed = sum(1 for g in goals if g["failed"])
    lines = [f"📋 Bugungi progress: {done} bajarildi, {failed} bajarilmadi, {len(goals) - done - failed} kutilmoqda\n"]
    for idx, g in enumerate(goals, start=1):
        if g["done"]:
            mark = "✅"
        elif g["failed"]:
            mark = "❌"
        else:
            mark = "◻️"
        lines.append(f"{mark} #{idx} {g['text']}")
    return "\n".join(lines)


def get_today_goal_by_number(chat_id: int, display_number: int):
    """Kunlik ro'yxatdagi ko'rsatilgan raqam (1, 2, 3...) bo'yicha haqiqiy
    task yozuvini topadi (chunki bazadagi ID global, lekin foydalanuvchiga
    har doim 1 dan boshlanadigan raqam ko'rsatiladi)."""
    goals = get_today_goals(chat_id)
    if 1 <= display_number <= len(goals):
        return goals[display_number - 1]
    return None


# ---------------------------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# FOYDALANISHNI CHEKLASH — faqat egasi
# ---------------------------------------------------------------------------

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Sizning Telegram ID'ingiz: `{user.id}`\n\n"
        "Buni Railway'dagi `ALLOWED_USER_ID` muhit o'zgaruvchisiga qo'ying "
        "(bir nechta kishi uchun vergul bilan ajrating, masalan `12345,67890`), "
        "shunda bot faqat shu ID'larga xizmat qiladi.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def restrict_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har qanday yangilanishdan oldin ishlaydi (group=-1). ALLOWED_USER_IDS
    o'rnatilgan bo'lsa, faqat shu ID'larga ruxsat beradi, qolganlarini bloklaydi."""
    if not ALLOWED_USER_IDS:
        return  # cheklov o'rnatilmagan — hammaga ochiq
    user = update.effective_user
    if user is None or str(user.id) not in ALLOWED_USER_IDS:
        if update.effective_message:
            await update.effective_message.reply_text(
                "🔒 Kechirasiz, bu bot shaxsiy va faqat egasi uchun mo'ljallangan."
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "🔒 Bu bot shaxsiy va faqat egasi uchun mo'ljallangan.", show_alert=True
            )
        raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(update.effective_chat.id, user.first_name or "")
    safe_name = escape_md(user.first_name or "")
    text = (
        f"Salom, {safe_name}! 👋\n\n"
        "Men sizning kunlik tasklaringiz, maqsadlaringiz va ish sifatingizni kuzatib boraman.\n\n"
        "*Buyruqlar:*\n"
        "/goal <matn> — bugungi maqsad/task qo'shish\n"
        "/goals — bugungi tasklar ro'yxati (bajarish uchun tugmalar bilan)\n"
        "/done <raqam> — taskni bajarildi deb belgilash\n"
        "/report — kunni yakunlab, 1-10 baholash\n"
        "/feedback — bugungi ish sifati bo'yicha AI tahlil olish\n"
        "/stats — oxirgi 7 kunlik statistika\n"
        "/settime HH:MM — kechki eslatma vaqtini o'rnatish\n\n"
        "Task qo'shganingizda, bot sizdan uni tugatish uchun aniq vaqt so'raydi — "
        "shu vaqt kelganda alohida eslatma yuboradi.\n\n"
        "Shuningdek, menga oddiy xabar yozsangiz (masalan, nima qilganingiz haqida), "
        "men uni kundaligingizga yozib qo'yaman va joriy progressni ko'rsataman.\n\n"
        "Pastdagi tugmali menyudan ham foydalanishingiz mumkin 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_keyboard())
    await schedule_reminder(context, update.effective_chat.id, DEFAULT_REMINDER_TIME)


async def cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user(chat_id, update.effective_user.first_name or "")
    if not context.args:
        await update.message.reply_text("Foydalanish: /goal Kod yozib bo'lish")
        return
    text = " ".join(context.args)
    goal_id = add_goal(chat_id, text)
    context.user_data["awaiting_deadline_for_goal"] = goal_id
    await update.message.reply_text(
        f"✅ Maqsad qo'shildi: {text}\n\n"
        "Bu taskni tugatish uchun aniq vaqt belgilaysizmi? "
        "Vaqtni HH:MM formatida yozing (masalan 18:30), yoki \"yo'q\" deb yozing."
    )


def build_goal_buttons(chat_id: int) -> InlineKeyboardMarkup | None:
    """Hali bajarilmagan/bajarilmadi deb belgilanmagan tasklar uchun tugmalar yasaydi.
    Hech qanday pending task qolmasa, None qaytaradi (tugmalarni olib tashlash uchun)."""
    goals = get_today_goals(chat_id)
    buttons = [
        [
            InlineKeyboardButton(f"✅ {g['text'][:20]}", callback_data=f"done:{g['id']}"),
            InlineKeyboardButton("❌ Qilolmadim", callback_data=f"fail:{g['id']}"),
        ]
        for g in goals if not g["done"] and not g["failed"]
    ]
    return InlineKeyboardMarkup(buttons) if buttons else None


async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    goals = get_today_goals(chat_id)
    if not goals:
        await update.message.reply_text("Bugun hali maqsad qo'shmadingiz. /goal buyrug'i bilan qo'shing.")
        return
    text = day_progress_text(chat_id)
    markup = build_goal_buttons(chat_id)
    if markup:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text + "\n\n🎉 Barcha tasklar belgilandi!")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Foydalanish: /done 3  (bugungi ro'yxatdagi task raqami)")
        return
    display_number = int(context.args[0])
    goal = get_today_goal_by_number(chat_id, display_number)
    if goal and mark_done(chat_id, goal["id"]):
        await update.message.reply_text("✅ Bajarildi deb belgilandi!\n\n" + day_progress_text(chat_id))
    else:
        await update.message.reply_text("Bunday raqamli task topilmadi.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if query.data.startswith("done:"):
        goal_id = int(query.data.split(":")[1])
        mark_done(chat_id, goal_id)
        markup = build_goal_buttons(chat_id)
        try:
            if markup:
                await query.edit_message_text(day_progress_text(chat_id), reply_markup=markup)
            else:
                await query.edit_message_text(day_progress_text(chat_id) + "\n\n🎉 Barcha tasklar belgilandi!")
        except Exception:
            pass  # matn o'zgarmagan bo'lsa Telegram xato qaytarishi mumkin — e'tiborsiz qoldiramiz
    elif query.data.startswith("fail:"):
        goal_id = int(query.data.split(":")[1])
        mark_failed(chat_id, goal_id)
        markup = build_goal_buttons(chat_id)
        try:
            if markup:
                await query.edit_message_text(day_progress_text(chat_id), reply_markup=markup)
            else:
                await query.edit_message_text(day_progress_text(chat_id) + "\n\n🎉 Barcha tasklar belgilandi!")
        except Exception:
            pass
    elif query.data.startswith("rate:"):
        rating = int(query.data.split(":")[1])
        save_rating(chat_id, rating)
        await query.edit_message_text(
            f"Kun {rating}/10 deb baholandi. Rahmat! Ertaga ko'rishguncha 👋\n\n"
            "Xohlasangiz, izoh yozib qo'yishingiz mumkin — u ham saqlanadi.\n"
            "(AI tahlil olish uchun istalgan vaqt /feedback yozing)"
        )
        context.user_data["awaiting_rating_note"] = True


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_evening_prompt(update.effective_chat.id, context)


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thinking_msg = await update.message.reply_text("🤔 Tahlil qilyapman...")
    feedback = await get_ai_feedback(chat_id)
    # AI matni istalgan belgilarni o'z ichiga olishi mumkin, shuning uchun
    # Markdown formatlashsiz, oddiy matn sifatida yuboramiz (xavfsizroq)
    await thinking_msg.edit_text(f"🧠 AI tahlili\n\n{feedback}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    goals, ratings = get_stats(chat_id, days=7)
    done = goals["done"] or 0
    failed = goals["failed"] or 0
    total = goals["total"] or 0
    addressed = done + failed  # javob berilgan (bajarilgan yoki bajarilmagan) tasklar
    pct = round(100 * done / addressed) if addressed else 0
    avg_r = round(ratings["avg_r"], 1) if ratings and ratings["avg_r"] else None

    text = (
        "📊 *Oxirgi 7 kunlik statistika*\n\n"
        f"✅ Bajarildi: {done}\n"
        f"❌ Bajarilmadi: {failed}\n"
        f"◻️ Hali belgilanmagan: {total - addressed}\n"
        f"📈 Produktivlik: {pct}% (bajarilgan/(bajarilgan+bajarilmagan))\n"
        f"O'rtacha kun reytingi: {avg_r if avg_r is not None else 'hali baholanmagan'}/10\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Foydalanish: /settime 21:00")
        return
    try:
        hh, mm = context.args[0].split(":")
        t = dtime(int(hh), int(mm))
    except Exception:
        await update.message.reply_text("Noto'g'ri format. Masalan: /settime 21:00")
        return
    with closing(get_conn()) as conn, conn:
        conn.execute(
            "UPDATE users SET reminder_time=? WHERE chat_id=?", (context.args[0], chat_id)
        )
    await schedule_reminder(context, chat_id, context.args[0])
    await update.message.reply_text(f"⏰ Kechki eslatma vaqti {context.args[0]} ga o'rnatildi.")


# ---------------------------------------------------------------------------
# FREE-TEXT HANDLER — darhol javob berish
# ---------------------------------------------------------------------------

MENU_BUTTON_TEXTS = {
    "➕ Task qo'shish",
    "📋 Bugungi tasklar",
    "🌙 Kunni yakunlash",
    "🧠 AI tahlil",
    "⚙️ Vaqtni sozlash",
    "📊 Statistika",
}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Agar foydalanuvchi biror "kutish" holatida turib, boshqa menyu tugmasini bossa —
    # eski kutishni bekor qilamiz va yangi tugmani ishga tushiramiz (adashib bosishning oldini olish)
    if text in MENU_BUTTON_TEXTS:
        context.user_data["awaiting_goal_text"] = False
        context.user_data["awaiting_time_text"] = False
        context.user_data["awaiting_rating_note"] = False
        context.user_data["awaiting_deadline_for_goal"] = None

    # Agar oxirgi baholashdan keyin izoh kutilayotgan bo'lsa
    if context.user_data.get("awaiting_rating_note"):
        with closing(get_conn()) as conn, conn:
            conn.execute(
                "UPDATE ratings SET note=? WHERE chat_id=? AND rating_date=?",
                (text, chat_id, today_str()),
            )
        context.user_data["awaiting_rating_note"] = False
        await update.message.reply_text("Izoh saqlandi. Rahmat! 🙌")
        return

    # Menyu orqali "Task qo'shish" bosilgan bo'lsa, keyingi xabarni task sifatida kutamiz
    if context.user_data.get("awaiting_goal_text"):
        context.user_data["awaiting_goal_text"] = False
        goal_id = add_goal(chat_id, text)
        context.user_data["awaiting_deadline_for_goal"] = goal_id
        await update.message.reply_text(
            f"✅ Maqsad qo'shildi: {text}\n\n"
            "Bu taskni tugatish uchun aniq vaqt belgilaysizmi? "
            "Vaqtni HH:MM formatida yozing (masalan 18:30), yoki \"yo'q\" deb yozing."
        )
        return

    # Task uchun muddat (deadline) kiritilishini kutayotgan bo'lsak
    pending_goal_id = context.user_data.get("awaiting_deadline_for_goal")
    if pending_goal_id:
        context.user_data["awaiting_deadline_for_goal"] = None
        if text.strip().lower() in ("yo'q", "yoq", "yo'q.", "yoq.", "skip", "-"):
            await update.message.reply_text("Yaxshi, bu task uchun eslatma o'rnatilmadi.")
            return
        try:
            hh, mm = text.split(":")
            deadline_t = dtime(int(hh), int(mm))
        except Exception:
            await update.message.reply_text(
                "Noto'g'ri format, shuning uchun eslatma o'rnatilmadi. "
                "Keyinroq qayta task qo'shganda HH:MM formatida kiriting."
            )
            return
        set_goal_deadline(chat_id, pending_goal_id, text.strip())
        goal = get_goal(chat_id, pending_goal_id)
        scheduled = await schedule_deadline_reminder(context, chat_id, pending_goal_id, goal["text"], deadline_t)
        if scheduled:
            await update.message.reply_text(f"⏰ '{goal['text']}' uchun {text.strip()} da eslatma o'rnatildi.")
        else:
            await update.message.reply_text(
                f"⚠️ {text.strip()} vaqti allaqachon o'tib ketgan, "
                "shuning uchun eslatma o'rnatilmadi."
            )
        return

    # Menyu orqali "Vaqtni sozlash" bosilgan bo'lsa, keyingi xabarni vaqt sifatida kutamiz
    if context.user_data.get("awaiting_time_text"):
        context.user_data["awaiting_time_text"] = False
        try:
            hh, mm = text.split(":")
            dtime(int(hh), int(mm))
        except Exception:
            await update.message.reply_text("Noto'g'ri format. Masalan: 21:00 deb yozing.")
            return
        with closing(get_conn()) as conn, conn:
            conn.execute("UPDATE users SET reminder_time=? WHERE chat_id=?", (text, chat_id))
        await schedule_reminder(context, chat_id, text)
        await update.message.reply_text(f"⏰ Kechki eslatma vaqti {text} ga o'rnatildi.")
        return

    # --- Asosiy menyu tugmalari ---
    if text == "➕ Task qo'shish":
        context.user_data["awaiting_goal_text"] = True
        await update.message.reply_text("Nimani qo'shmoqchisiz? Task matnini yozing:")
        return

    if text == "📋 Bugungi tasklar":
        await cmd_goals(update, context)
        return

    if text == "🌙 Kunni yakunlash":
        await send_evening_prompt(chat_id, context)
        return

    if text == "🧠 AI tahlil":
        await cmd_feedback(update, context)
        return

    if text == "⚙️ Vaqtni sozlash":
        context.user_data["awaiting_time_text"] = True
        await update.message.reply_text("Kechki eslatma vaqtini yozing, masalan: 21:00")
        return

    if text == "📊 Statistika":
        if WEBAPP_URL:
            inline_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("📊 Statistikani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]
            )
            await update.message.reply_text(
                "Interaktiv statistikani ochish uchun bosing 👇", reply_markup=inline_kb
            )
        else:
            await cmd_stats(update, context)
        return

    # Boshqa har qanday matn — kundalik log sifatida saqlanadi
    ensure_user(chat_id, update.effective_user.first_name or "")
    add_log(chat_id, text)
    await update.message.reply_text(
        "📝 Yozib qo'ydim.\n\n" + day_progress_text(chat_id)
    )


# ---------------------------------------------------------------------------
# KECHKI ESLATMA (scheduled job)
# ---------------------------------------------------------------------------

async def send_evening_prompt(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    goals = get_today_goals(chat_id)
    done = sum(1 for g in goals if g["done"])
    failed = sum(1 for g in goals if g["failed"])
    total = len(goals)
    if total:
        summary = f"Bugun {done} ta task bajardingiz, {failed} ta bajarilmadi (jami {total} ta)."
    else:
        summary = "Bugun hech qanday task belgilamagansiz."

    buttons = [
        [InlineKeyboardButton(str(i), callback_data=f"rate:{i}") for i in range(1, 6)],
        [InlineKeyboardButton(str(i), callback_data=f"rate:{i}") for i in range(6, 11)],
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🌙 Kun yakunlandi.\n{summary}\n\nBugungi kuningizni 1 dan 10 gacha qanday baholaysiz?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await send_evening_prompt(chat_id, context)


async def schedule_reminder(context: ContextTypes.DEFAULT_TYPE, chat_id: int, hhmm: str):
    # avvalgi job bo'lsa, olib tashlaymiz
    for job in context.job_queue.get_jobs_by_name(f"reminder_{chat_id}"):
        job.schedule_removal()
    hh, mm = hhmm.split(":")
    utc_time = local_time_to_utc_time(dtime(int(hh), int(mm)))
    context.job_queue.run_daily(
        reminder_job,
        time=utc_time,
        chat_id=chat_id,
        name=f"reminder_{chat_id}",
    )


async def schedule_all_reminders(app: Application):
    with closing(get_conn()) as conn:
        users = conn.execute("SELECT chat_id, reminder_time FROM users").fetchall()
    for u in users:
        hh, mm = u["reminder_time"].split(":")
        utc_time = local_time_to_utc_time(dtime(int(hh), int(mm)))
        app.job_queue.run_daily(
            reminder_job,
            time=utc_time,
            chat_id=u["chat_id"],
            name=f"reminder_{u['chat_id']}",
        )


# ---------------------------------------------------------------------------
# TASK MUDDATI (deadline) ESLATMASI
# ---------------------------------------------------------------------------

async def deadline_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=f"⏰ Eslatma: \"{data['text']}\" vazifasini tugatish vaqti keldi!",
    )


async def schedule_deadline_reminder(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, goal_id: int, goal_text: str, deadline_t: dtime
) -> bool:
    """Bugungi sana + berilgan MAHALLIY vaqt uchun bir martalik eslatma rejalashtiradi.
    Vaqt allaqachon o'tib ketgan bo'lsa, False qaytaradi."""
    local_target = datetime.combine(local_now().date(), deadline_t)
    if local_target <= local_now():
        return False
    utc_target = local_datetime_to_utc(local_target)
    for job in context.job_queue.get_jobs_by_name(f"deadline_{chat_id}_{goal_id}"):
        job.schedule_removal()
    context.job_queue.run_once(
        deadline_reminder_job,
        when=utc_target,
        data={"chat_id": chat_id, "text": goal_text},
        name=f"deadline_{chat_id}_{goal_id}",
    )
    return True


async def reschedule_pending_deadlines(app: Application):
    """Bot qayta ishga tushganda, bugungi hali o'tmagan muddatli tasklar
    uchun eslatmalarni qayta rejalashtiradi (redeploy paytida yo'qolib qolmasin)."""
    goals = get_pending_deadlines_today()
    now_local = local_now()
    for g in goals:
        try:
            hh, mm = g["deadline_time"].split(":")
            local_target = datetime.combine(now_local.date(), dtime(int(hh), int(mm)))
        except Exception:
            continue
        if local_target <= now_local:
            continue
        utc_target = local_datetime_to_utc(local_target)
        app.job_queue.run_once(
            deadline_reminder_job,
            when=utc_target,
            data={"chat_id": g["chat_id"], "text": g["text"]},
            name=f"deadline_{g['chat_id']}_{g['id']}",
        )


# ---------------------------------------------------------------------------
# MINI APP (Telegram WebApp) — FastAPI server
# ---------------------------------------------------------------------------

def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400):
    """Telegram WebApp initData'ni rasmiy algoritm bo'yicha tekshiradi.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        logger.warning("initData tekshiruvi: 'hash' maydoni topilmadi")
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        logger.warning(
            f"initData tekshiruvi: hash mos kelmadi. "
            f"kutilgan_boshi={computed_hash[:8]} olingan_boshi={received_hash[:8]} "
            f"token_uzunligi={len(bot_token)}"
        )
        return None

    auth_date = int(parsed.get("auth_date", 0))
    if max_age and (time_module.time() - auth_date) > max_age:
        return None

    user_json = parsed.get("user")
    if not user_json:
        return None
    return json.loads(user_json)


web_app = FastAPI() if FastAPI else None

if web_app:

    @web_app.get("/webapp", response_class=HTMLResponse)
    async def webapp_page():
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()

    @web_app.get("/webapp/chart.umd.js")
    async def chart_js():
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "chart.umd.js")
        with open(js_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), media_type="application/javascript")

    @web_app.get("/api/stats")
    async def api_stats(initData: str = Query(default=""), period: str = Query(default="daily")):
        user = verify_init_data(initData, BOT_TOKEN)
        if not user:
            return JSONResponse({"error": "invalid_init_data"}, status_code=401)
        if ALLOWED_USER_IDS and str(user["id"]) not in ALLOWED_USER_IDS:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if period not in ("daily", "weekly", "monthly"):
            period = "daily"
        chat_id = user["id"]
        data = get_chart_data(chat_id, period=period)
        return JSONResponse(data)

    @web_app.get("/health")
    async def health():
        return {"status": "ok"}


def run_webserver():
    if not web_app:
        logger.warning("fastapi/uvicorn o'rnatilmagan — mini-app o'chirilgan.")
        return
    uvicorn.run(web_app, host="0.0.0.0", port=PORT, log_level="warning")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable o'rnatilmagan! .env fayliga qo'shing.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Cheklov handleri barcha boshqalardan OLDIN ishlashi kerak (group=-1)
    app.add_handler(TypeHandler(Update, restrict_access), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("feedback", cmd_feedback))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_once(lambda ctx: None, when=0)  # job_queue ishga tushishini ta'minlash
    async def _post_init(app: Application):
        await schedule_all_reminders(app)
        await reschedule_pending_deadlines(app)

    app.post_init = _post_init

    if web_app:
        threading.Thread(target=run_webserver, daemon=True).start()
        logger.info(f"Mini-app serveri {PORT} portda ishga tushdi...")

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

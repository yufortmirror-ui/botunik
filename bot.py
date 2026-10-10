import asyncio
import html
import json
import logging
import os
import re
import secrets
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()  # должен быть до импорта ai (там читается ключ)
try:
    from telethon import TelegramClient, events
except ImportError:  # без telethon бот работает, но только с файлами до 20 МБ
    TelegramClient = events = None
import ai  # noqa: E402
import db  # noqa: E402
import schemes  # noqa: E402

router = Router()
sessions: dict[str, dict] = {}  # активные тесты: sid -> состояние
pending_broadcasts: dict[str, dict] = {}  # рассылки, ждущие подтверждения админа
draft_ctrl: dict[int, int] = {}  # chat_id -> id сообщения с кнопкой «Подтвердить»
processing: dict[int, int] = {}  # chat_id -> сколько аудио сейчас расшифровывается
LETTERS = "АБВГ"
# Если задан адрес локального Bot API сервера — боту доступны файлы до 2 ГБ,
# иначе обычный лимит Telegram на скачивание: 20 МБ.
API_URL = os.getenv("TELEGRAM_API_URL", "").strip()
API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
CLOUD_LIMIT = 20 * 1024 * 1024  # лимит обычного Bot API на скачивание
MAX_FILE = 2000 * 1024 * 1024 if (API_URL or (API_ID and API_HASH)) else CLOUD_LIMIT
BOT: "Bot | None" = None  # заполняется в main()
tclient = None  # MTProto-клиент (Telethon) для файлов больше 20 МБ
ADMIN_ID = int(os.getenv("ADMIN_ID", "787140952"))

BTN_HISTORY = "📚 История лекций"
BTN_STATS = "📊 Статистика"

AUDIO_EXT = {
    ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".mp3": "audio/mpeg",
    ".wav": "audio/wav", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".flac": "audio/flac",
}
SCHEME_RE = re.compile(r"<pre>(.*?)</pre>", re.S)


# ---------------------------------------------------------------- helpers
def main_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу чата."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_STATS)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def fmt_dt(iso: str | None, with_time: bool = True) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso).astimezone(db.MSK)
    return dt.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def pick_audio(m: Message):
    """Возвращает (file_id, size, mime) или None, если в сообщении нет аудио."""
    if m.voice:
        return m.voice.file_id, m.voice.file_size, "audio/ogg"
    obj = m.audio or m.document
    if not obj:
        return None
    name = (obj.file_name or "").lower()
    ext = os.path.splitext(name)[1]
    mime = AUDIO_EXT.get(ext) or (obj.mime_type if (obj.mime_type or "").startswith("audio/") else None)
    if not mime:
        return None
    return obj.file_id, obj.file_size, mime


def split_text(text: str, limit: int = 4000) -> list[str]:
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks


async def send_long(target: Message, text: str) -> None:
    for chunk in split_text(text):
        try:
            await target.answer(chunk)
        except TelegramBadRequest:  # кривой HTML от нейросети -> шлём без разметки
            plain = re.sub(r"<[^>]+>", "", html.unescape(chunk))
            await target.answer(plain, parse_mode=None)


async def send_scheme(target: Message, raw: str) -> None:
    """Схему из <pre> — картинкой; если не вышло, обычным моноширинным текстом."""
    text = html.unescape(re.sub(r"</?(?:code|b|i)>", "", raw)).strip("\n")
    if not text.strip():
        return
    try:
        png = await asyncio.to_thread(schemes.render, text)
        await target.answer_photo(BufferedInputFile(png, filename="scheme.png"))
    except Exception:
        logging.exception("scheme render failed")
        await target.answer(f"<pre>{html.escape(text)}</pre>")


async def send_notes(target: Message, notes: str) -> None:
    """Конспект: текст — сообщениями, схемы — картинками на своих местах."""
    pos = 0
    for m in SCHEME_RE.finditer(notes):
        before = notes[pos : m.start()].strip()
        if before:
            await send_long(target, before)
        await send_scheme(target, m.group(1))
        pos = m.end()
    tail = notes[pos:].strip()
    if tail:
        await send_long(target, tail)


def extract_title(notes: str, transcript: str) -> str:
    plain = re.sub(r"<[^>]+>", "", html.unescape(notes))
    m = re.search(r"Тема\s*[:\-–—]?\s*\n?\s*([^\n]{3,100})", plain)
    title = m.group(1).strip() if m else transcript.strip()[:40] + "…"
    return title[:70]


def lecture_title(row) -> str:
    return row["title"] or (row["snippet"].strip()[:40] + "…")


def answer_kb(prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, letter in enumerate(LETTERS):
        b.button(text=letter, callback_data=f"{prefix}:{i}")
    b.adjust(4)
    return b.as_markup()


def render_q(q: dict, header: str, verdict: str = "") -> str:
    lines = [header, "", html.escape(q["question"]), ""]
    lines += [f"<b>{LETTERS[i]})</b> {html.escape(o)}" for i, o in enumerate(q["options"])]
    if verdict:
        lines += ["", verdict]
    return "\n".join(lines)


def verdict_text(q: dict, chosen: int) -> str:
    if chosen == q["correct"]:
        v = "✅ <b>Верно!</b>"
    else:
        v = f"❌ <b>Неверно.</b> Правильно: {LETTERS[q['correct']]}"
    if q.get("explanation"):
        v += f"\n<i>{html.escape(q['explanation'])}</i>"
    return v


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


class TouchMiddleware(BaseMiddleware):
    """Запоминает каждого, кто пишет боту (для статистики и рассылки)."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not user.is_bot:
            db.touch(user.id)
        return await handler(event, data)


# ---------------------------------------------------------------- меню, история, статистика
@router.message(CommandStart())
async def on_start(m: Message):
    await m.answer(
        "Привет! 🎧 Скинь мне запись лекции с диктофона (.m4a), "
        "а я расшифрую её и сделаю конспект или тест.\n\n"
        "Каждый вечер в 21:00 буду присылать «вопрос дня», а в 21:50 — итоги дня.\n"
        "Кнопки внизу — история лекций и статистика.",
        reply_markup=main_kb(),
    )


@router.message(Command("history"))
@router.message(F.text == BTN_HISTORY)
async def on_history(m: Message):
    rows = db.list_lectures(m.chat.id, limit=10)
    if not rows:
        await m.answer("Пока нет ни одной лекции. Пришли аудиозапись 🎧", reply_markup=main_kb())
        return
    b = InlineKeyboardBuilder()
    for r in rows:
        label = f"{fmt_dt(r['created_at'], False)[:5]} · {lecture_title(r)}"
        b.button(text=label[:60], callback_data=f"hist:{r['id']}")
    b.adjust(1)
    await m.answer("📚 <b>История лекций</b>\nПоследние 10, выбери нужную:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("hist:"))
async def on_history_pick(cb: CallbackQuery):
    lec = db.get_lecture(int(cb.data.split(":")[1]))
    if not lec or lec["chat_id"] != cb.message.chat.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    t = lec["transcript"].strip()
    preview = t[:300] + ("…" if len(t) > 300 else "")
    marks = []
    if lec["notes_html"]:
        marks.append("📝 конспект готов")
    if lec["quiz_json"]:
        marks.append("🧠 тест готов")
    b = InlineKeyboardBuilder()
    b.button(text="📝 Конспект", callback_data=f"mode:notes:{lec['id']}")
    b.button(text="🧠 Тест", callback_data=f"mode:quiz:{lec['id']}")
    b.adjust(2)
    title = lec["title"] or "Лекция"
    await cb.message.answer(
        f"📚 <b>{html.escape(title)}</b>\n📅 {fmt_dt(lec['created_at'])}"
        + (f"\n{' · '.join(marks)}" if marks else "")
        + f"\n\n<i>{html.escape(preview)}</i>",
        reply_markup=b.as_markup(),
    )
    await cb.answer()


@router.message(Command("stats"))
@router.message(F.text == BTN_STATS)
async def on_stats(m: Message):
    s = db.stats(m.chat.id)
    await m.answer(
        "📊 <b>Твоя статистика</b>\n\n"
        f"🎧 Лекций загружено: <b>{s['lectures']}</b>\n"
        f"📝 Конспектов составлено: <b>{s['notes']}</b>\n"
        f"🧠 Тестов сгенерировано: <b>{s['quizzes']}</b>\n"
        f"✅ Тестов пройдено: <b>{s['tests_done']}</b>\n"
        f"🔥 Дней с активностью: <b>{s['active_days']}</b>\n\n"
        f"📅 Первое использование бота: <b>{fmt_dt(s['first'], False)}</b>\n"
        f"🕐 Последняя загрузка лекции: <b>{fmt_dt(s['last_lecture'])}</b>",
        reply_markup=main_kb(),
    )


# ---------------------------------------------------------------- аудио -> набор -> конспект / тест
def draft_kb(n: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Подтвердить ({n})", callback_data="draft:ok")
    b.button(text="🗑 Сбросить", callback_data="draft:reset")
    b.adjust(2)
    return b.as_markup()


@router.message(F.voice | F.audio | F.document)
async def on_audio(m: Message, bot: Bot):
    info = pick_audio(m)
    if not info:
        await m.answer("Пришли аудиофайл с диктофона (.m4a) 🎧")
        return
    file_id, size, mime = info
    if size and size > CLOUD_LIMIT and tclient is not None:
        return  # большой файл заберёт MTProto-обработчик (см. start_mtproto)
    limit_bytes = MAX_FILE if (API_URL or tclient is not None) else CLOUD_LIMIT
    if size and size > limit_bytes:
        await m.answer(f"Файл больше {limit_bytes // (1024 * 1024)} МБ — боту такой не скачать. Раздели запись на части 🙏")
        return

    async def fetch(path: str) -> None:
        await bot.download(file_id, destination=path)

    await process_audio(bot, m.chat.id, m.message_id, mime, size, fetch)


async def process_audio(bot: Bot, chat_id: int, message_id: int, mime: str, size: int | None, fetch) -> None:
    """Общий конвейер: скачать -> (сжать и нарезать) -> расшифровать -> добавить в набор.
    fetch(path) — корутина, которая скачивает файл по указанному пути."""
    big = bool(size and size > 24 * 1024 * 1024)
    status = await bot.send_message(
        chat_id,
        "🎧 Файл большой — разрежу на части и расшифрую, это займёт несколько минут…"
        if big
        else "🎧 Слушаю запись, это займёт пару минут…",
    )

    async def progress(i: int, n: int) -> None:
        if n > 1:
            try:
                await status.edit_text(f"🎧 Расшифровываю часть {i} из {n}…")
            except TelegramBadRequest:
                pass

    processing[chat_id] = processing.get(chat_id, 0) + 1
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "input_audio.m4a")  # тип определяет ffmpeg по содержимому
            await fetch(path)
            transcript = await ai.transcribe_path(path, mime, progress)
    except Exception:
        logging.exception("transcribe failed")
        await status.edit_text("😢 Не получилось расшифровать это аудио. Пришли его ещё раз чуть позже.")
        return
    finally:
        processing[chat_id] -= 1
    if len(transcript) < 50:
        await status.edit_text("🤔 Речи в этом аудио почти не нашлось. Проверь файл.")
        return

    n = db.add_draft(chat_id, message_id, transcript)
    preview = transcript[:250] + ("…" if len(transcript) > 250 else "")
    await status.edit_text(
        f"✅ <b>Аудио обработано</b> (в наборе: {n})\n\n<i>{html.escape(preview)}</i>\n\n"
        "Пришли ещё аудио или нажми «Подтвердить», когда всё загружено.",
        reply_markup=draft_kb(n),
    )
    # кнопки на прошлом сообщении набора убираем: активна только последняя
    prev = draft_ctrl.get(chat_id)
    if prev and prev != status.message_id:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=prev, reply_markup=None)
        except TelegramBadRequest:
            pass
    draft_ctrl[chat_id] = status.message_id


async def start_mtproto(token: str):
    """Второе подключение бота — по MTProto (Telethon). Оно умеет скачивать файлы до 2 ГБ,
    в отличие от обычного Bot API (20 МБ). Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH."""
    if TelegramClient is None:
        logging.warning("telethon не установлен: pip install telethon — иначе файлы больше 20 МБ недоступны")
        return None
    try:
        client = TelegramClient(
            str(Path(db.DB_PATH).resolve().parent / "mtproto_session"), int(API_ID), API_HASH
        )
        await client.start(bot_token=token)
    except Exception:
        logging.exception("MTProto не запустился, продолжаю только с файлами до 20 МБ")
        return None

    async def on_big(event) -> None:
        f = event.file
        ext = os.path.splitext((f.name or "").lower())[1]
        mime = AUDIO_EXT.get(ext) or (f.mime_type if (f.mime_type or "").startswith("audio/") else None)
        if not mime:
            return  # не аудио — ответит обычный обработчик
        db.touch(event.chat_id)
        if f.size > MAX_FILE:
            await BOT.send_message(event.chat_id, "Файл больше 2 ГБ — боту такой не скачать. Раздели запись на части 🙏")
            return

        async def fetch(path: str) -> None:
            await client.download_media(event.message, file=path)

        await process_audio(BOT, event.chat_id, event.id, mime, f.size, fetch)

    client.add_event_handler(
        on_big,
        events.NewMessage(
            incoming=True,
            func=lambda e: e.is_private and e.file is not None and (e.file.size or 0) > CLOUD_LIMIT,
        ),
    )
    logging.info("MTProto включён: файлы до 2 ГБ")
    return client


@router.callback_query(F.data.startswith("draft:"))
async def on_draft(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    action = cb.data.split(":")[1]

    if action == "reset":
        db.clear_drafts(chat_id)
        draft_ctrl.pop(chat_id, None)
        await cb.message.edit_text("🗑 Набор очищен. Пришли аудио заново 🎧")
        await cb.answer()
        return

    if processing.get(chat_id):
        await cb.answer(f"Ещё обрабатываю аудио ({processing[chat_id]}), подожди немного ⏳", show_alert=True)
        return
    parts = db.pop_drafts(chat_id)
    draft_ctrl.pop(chat_id, None)
    if not parts:
        await cb.answer("Набор пуст — пришли аудио 🎧", show_alert=True)
        return

    transcript = "\n\n".join(parts)
    lid = db.add_lecture(chat_id, transcript)
    preview = transcript[:400] + ("…" if len(transcript) > 400 else "")
    b = InlineKeyboardBuilder()
    b.button(text="📝 Конспект", callback_data=f"mode:notes:{lid}")
    b.button(text="🧠 Тест", callback_data=f"mode:quiz:{lid}")
    b.adjust(2)
    await cb.message.edit_text(
        f"✅ <b>Выбор подтверждён</b> — аудио: {len(parts)}\n\n<i>{html.escape(preview)}</i>\n\nЧто сделать?",
        reply_markup=b.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("mode:"))
async def on_mode(cb: CallbackQuery):
    _, mode, lid = cb.data.split(":")
    lec = db.get_lecture(int(lid))
    if not lec or lec["chat_id"] != cb.message.chat.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await cb.answer("Готовлю…")
    chat_msg = cb.message

    try:
        if mode == "notes":
            if lec["notes_html"]:  # уже делали — показываем сохранённый, без нового запроса к ИИ
                await send_notes(chat_msg, lec["notes_html"])
                return
            wait = await chat_msg.answer("✍️ Делаю конспект…")
            notes = await ai.make_notes(lec["transcript"])
            db.set_notes(lec["id"], notes, extract_title(notes, lec["transcript"]))
            db.log(chat_msg.chat.id, "notes")
            await wait.delete()
            await send_notes(chat_msg, notes)
        else:
            if lec["quiz_json"]:
                quiz = json.loads(lec["quiz_json"])
            else:
                wait = await chat_msg.answer("🧠 Придумываю вопросы…")
                quiz = await ai.make_quiz(lec["transcript"])
                db.set_quiz(lec["id"], quiz)
                await wait.delete()
            sid = secrets.token_hex(4)
            sessions[sid] = {"qs": quiz, "i": 0, "score": 0, "chat": chat_msg.chat.id}
            await send_question(chat_msg, sid)
    except Exception:
        logging.exception("generation failed")
        await chat_msg.answer("😢 Нейросеть не справилась. Нажми кнопку ещё раз чуть позже.")


async def send_question(target: Message, sid: str) -> None:
    s = sessions[sid]
    i = s["i"]
    header = f"<b>Вопрос {i + 1}/{len(s['qs'])}</b>"
    await target.answer(render_q(s["qs"][i], header), reply_markup=answer_kb(f"q:{sid}:{i}"))


@router.callback_query(F.data.startswith("q:"))
async def on_quiz_answer(cb: CallbackQuery):
    _, sid, i, opt = cb.data.split(":")
    i, opt = int(i), int(opt)
    s = sessions.get(sid)
    if not s or s["i"] != i:
        await cb.answer("Этот вопрос уже неактуален", show_alert=True)
        return

    q = s["qs"][i]
    n = len(s["qs"])
    s["score"] += opt == q["correct"]
    s["i"] += 1  # сразу, чтобы двойной клик не засчитался дважды

    await cb.message.edit_text(
        render_q(q, f"<b>Вопрос {i + 1}/{n}</b>", verdict_text(q, opt)), reply_markup=None
    )
    await cb.answer()

    if s["i"] < n:
        await send_question(cb.message, sid)
        return

    del sessions[sid]
    db.log(s["chat"], "test")
    pct = round(s["score"] * 100 / n)
    emoji = "🏆" if pct >= 90 else "👏" if pct >= 70 else "💪" if pct >= 50 else "📚"
    await cb.message.answer(
        f"{emoji} <b>Тест завершён!</b>\n\nПравильных ответов: <b>{s['score']} из {n}</b>\n"
        f"Результат: <b>{pct}%</b>"
    )


@router.callback_query(F.data.startswith("dq:"))
async def on_daily_answer(cb: CallbackQuery):
    _, did, opt = cb.data.split(":")
    row = db.get_daily(int(did))
    if not row or not db.close_daily(row["id"], "answered"):
        await cb.answer("Время вышло 🙈", show_alert=True)
        return
    q = json.loads(row["question_json"])
    await cb.message.edit_text(
        render_q(q, "<b>🌙 Вопрос дня</b>", verdict_text(q, int(opt))), reply_markup=None
    )
    await cb.answer()


# ---------------------------------------------------------------- админ: рассылка
@router.message(Command("admin_broadcast"), F.from_user.id == ADMIN_ID)
async def on_broadcast(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    src = m.reply_to_message
    if not text and not src:
        await m.answer(
            "📣 <b>Рассылка</b>\n\n"
            "• <code>/admin_broadcast текст</code> — разослать текст (без форматирования)\n"
            "• или ответь этой командой на сообщение — оно уйдёт копией, с форматированием и медиа."
        )
        return
    token = secrets.token_hex(4)
    pending_broadcasts[token] = {"text": text} if text else {"chat": src.chat.id, "msg": src.message_id}
    n = len(db.users())
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Отправить ({n})", callback_data=f"bc:ok:{token}")
    b.button(text="❌ Отмена", callback_data=f"bc:no:{token}")
    b.adjust(2)
    shown = text[:500] if text else "(копия сообщения, на которое ты ответил)"
    await m.answer(
        f"📣 <b>Предпросмотр рассылки</b>\nПолучателей: <b>{n}</b>\n\n{html.escape(shown)}",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("bc:"), F.from_user.id == ADMIN_ID)
async def on_broadcast_confirm(cb: CallbackQuery, bot: Bot):
    _, action, token = cb.data.split(":")
    item = pending_broadcasts.pop(token, None)
    if not item:
        await cb.answer("Рассылка устарела", show_alert=True)
        return
    if action == "no":
        await cb.message.edit_text("❌ Рассылка отменена.")
        await cb.answer()
        return

    await cb.answer()
    await cb.message.edit_text("📤 Отправляю…")
    ok = failed = 0
    for chat_id in db.users():
        for attempt in range(2):
            try:
                if "text" in item:
                    await bot.send_message(chat_id, item["text"], parse_mode=None)
                else:
                    await bot.copy_message(chat_id, item["chat"], item["msg"])
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:  # пользователь заблокировал бота
                failed += 1
                break
            except Exception:
                logging.exception("broadcast to %s failed", chat_id)
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек, в пределах лимитов Telegram
    await cb.message.edit_text(f"✅ Рассылка завершена.\nДоставлено: <b>{ok}</b>\nНе доставлено: <b>{failed}</b>")


# ---------------------------------------------------------------- scheduler jobs
async def job_daily_question(bot: Bot):
    for chat_id in db.users():
        q = db.random_question(chat_id)
        if not q:
            continue  # у пользователя ещё нет ни одного теста
        did = db.create_daily(chat_id, q, hours=1)
        try:
            msg = await bot.send_message(
                chat_id,
                render_q(q, "<b>🌙 Вопрос дня</b>"),
                reply_markup=answer_kb(f"dq:{did}"),
            )
            db.set_daily_message(did, msg.message_id)
        except Exception:
            logging.exception("daily question failed for %s", chat_id)


async def job_expire_daily(bot: Bot):
    for row in db.expired_daily():
        if not db.close_daily(row["id"], "expired"):
            continue
        try:
            await bot.edit_message_text(
                "Прошел час, ответ не поступил. Ты лениви попи😝",
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                reply_markup=None,
            )
        except TelegramBadRequest:
            pass


async def job_digest(bot: Bot):
    for chat_id in db.users():
        notes = db.count_today(chat_id, "notes")
        tests = db.count_today(chat_id, "test")
        if notes == 0 and tests == 0:
            text = "Сегодня конспектов и тестов не было. Ничего страшного, завтра наверстаем, котёнок 🐾"
        else:
            text = (
                f"Сегодня ты сделала {notes} {plural(notes, 'конспект', 'конспекта', 'конспектов')}, "
                f"прошла {tests} {plural(tests, 'тест', 'теста', 'тестов')}. "
                "Умничка, котёнок, горжусь тобой!"
            )
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            logging.exception("digest failed for %s", chat_id)


# ---------------------------------------------------------------- main
async def setup_menu(bot: Bot) -> None:
    """Кнопка «Меню» рядом с полем ввода со списком команд."""
    user_cmds = [
        BotCommand(command="history", description="📚 История лекций"),
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="start", description="🏠 Начать / главное меню"),
    ]
    await bot.set_my_commands(user_cmds)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    try:  # админская команда видна только админу
        admin_cmds = user_cmds + [BotCommand(command="admin_broadcast", description="📣 Рассылка (админ)")]
        await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except TelegramBadRequest:
        logging.warning("Не удалось задать меню админа — напиши боту /start с админского аккаунта и перезапусти")


async def move_to_local_server(token: str) -> None:
    """Один раз «выписывает» бота с облачного сервера Telegram — это требуется для перехода
    на локальный Bot API. Отметка о переносе хранится рядом с базой данных."""
    marker = Path(db.DB_PATH).resolve().parent / ".moved_to_local_api"
    if marker.exists():
        return
    cloud = Bot(token)
    try:
        await cloud.log_out()
        logging.info("Бот выписан с облачного сервера Telegram")
    except Exception as e:  # уже выписан или первый запуск — не страшно
        logging.info("log_out пропущен: %s", e)
    finally:
        await cloud.session.close()
    marker.write_text("ok")


async def main():
    logging.basicConfig(level=logging.INFO)
    db.init()
    token = os.environ["BOT_TOKEN"]
    session = None
    if API_URL:
        await move_to_local_server(token)
        session = AiohttpSession(api=TelegramAPIServer.from_base(API_URL, is_local=True), timeout=600)
        logging.info("Работаю через локальный Bot API: %s (файлы до 2 ГБ)", API_URL)
    bot = Bot(token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    global BOT, tclient
    BOT = bot
    if not API_URL and API_ID and API_HASH:
        tclient = await start_mtproto(token)
    dp = Dispatcher()
    dp.message.outer_middleware(TouchMiddleware())
    dp.callback_query.outer_middleware(TouchMiddleware())
    dp.include_router(router)
    await setup_menu(bot)

    sch = AsyncIOScheduler(timezone=db.MSK)
    sch.add_job(job_daily_question, CronTrigger(hour=21, minute=0), args=[bot])
    sch.add_job(job_digest, CronTrigger(hour=21, minute=50), args=[bot])
    sch.add_job(job_expire_daily, "interval", minutes=1, args=[bot])
    sch.start()

    try:
        await dp.start_polling(bot)
    finally:
        if tclient is not None:
            await tclient.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

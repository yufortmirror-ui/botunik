import os
import re
import random
import logging
import tempfile
import subprocess
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from groq import Groq

from PIL import Image, ImageDraw, ImageFont
from matplotlib import font_manager

# ============ НАСТРОЙКИ ============
BOT_TOKEN = "123"
GROQ_API_KEY = "123"
# ===================================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY)

CHAT_MODELS = [
    "llama-3.1-8b-instant",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "openai/gpt-oss-120b",
    "qwen/qwen3-32b",
]

# user_id -> список распознанных кусков
user_transcripts: dict[int, list[str]] = {}

MAX_FILE_SIZE = 20 * 1024 * 1024   # жёсткий лимит Telegram Bot API
MAX_PARTS_PER_LECTURE = 30         # защита от бесконечного добавления

WELCOME_TEXT = (
    "🎓 Привет! Я бот-помощник для конспектов, которого создал Васька для тебя!\n\n"
    "📤 Отправь мне запись диктофона с лекции и я помогу тебе:\n\n"
    "✅ Сделать хороший краткий конспект через нейросеть\n"
    "✅ Проверить свои знания через тест\n"
    "✅ Поднять настроение\n\n"
    "💡 Можешь отправлять лекцию по частям — после каждого файла я предложу "
    "добавить ещё или завершить.\n\n"
    "📦 Максимум на один файл: 20 МБ\n"
    "🚀 Жду твой файл!"
)

MOTIVATION_PHRASES = [
    "Ты - босс. Ты - просто начальник нафик!",
    "Сегодня ты либо победишь, либо научишься. Второе — тоже победа!",
    "Не сдавайся, до дедлайна ещё есть время!",
    "Ты умнее, чем думаешь, и сильнее, чем кажешься.",
    "Каждая лекция — это шаг к твоей мечте. Вперёд!",
    "Учёба — это марафон, а не спринт. Ты справишься!",
    "Отдохни, но не сдавайся. Ты на правильном пути!",
    "Ошибки — это не провал, а обратная связь от вселенной.",
    "Твоя будущая версия уже гордится тобой!",
    "Сделай это сейчас — потом скажешь себе спасибо!",
    "Пока другие спят — ты становишься легендой.",
    "Ты не устал. Ты просто на пути к успеху. Дыши и иди дальше!",
]


# ============ КЛАВИАТУРЫ ============
def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Поднять настроение", callback_data="motivate")]
    ])


def continue_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_more")],
        [InlineKeyboardButton(text="✅ Завершить", callback_data="finish")],
    ])


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Сделать конспект", callback_data="summary")],
        [InlineKeyboardButton(text="🧠 Пройти тест", callback_data="quiz")],
    ])


def chunk_text(text: str, size: int = 4000):
    for i in range(0, len(text), size):
        yield text[i:i + size]


# ============ ХЕЛПЕР ДЛЯ НЕЙРОСЕТИ ============
def ask_llm(system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
    last_error = None
    for model in CHAT_MODELS:
        try:
            resp = groq_client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"Модель {model} не сработала: {e}")
            last_error = e
            continue
    raise RuntimeError(f"Ни одна модель не ответила. Последняя ошибка: {last_error}")


# ============ ОТРИСОВКА ТЕКСТА В КАРТИНКИ ============
LATEX_MAP = {
    r'\pi': 'π', r'\phi': 'φ', r'\varphi': 'φ', r'\alpha': 'α', r'\beta': 'β',
    r'\gamma': 'γ', r'\delta': 'δ', r'\epsilon': 'ε', r'\varepsilon': 'ε',
    r'\theta': 'θ', r'\lambda': 'λ', r'\mu': 'μ', r'\nu': 'ν', r'\rho': 'ρ',
    r'\sigma': 'σ', r'\tau': 'τ', r'\omega': 'ω', r'\Omega': 'Ω',
    r'\approx': '≈', r'\ge': '≥', r'\geq': '≥', r'\le': '≤', r'\leq': '≤',
    r'\neq': '≠', r'\cdot': '·', r'\times': '×', r'\pm': '±',
    r'\to': '→', r'\rightarrow': '→', r'\leftarrow': '←',
    r'\infty': '∞', r'\sqrt': '√', r'\sum': '∑', r'\int': '∫',
    r'\%': '%', r'\_': '_', r'\&': '&', r'\#': '#',
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    prop = font_manager.FontProperties(
        family='DejaVu Sans',
        weight='bold' if bold else 'normal',
    )
    path = font_manager.findfont(prop)
    return ImageFont.truetype(path, size)


def _clean_latex(s: str) -> str:
    for k, v in LATEX_MAP.items():
        s = s.replace(k, v)
    s = re.sub(r'\\text\{([^}]*)\}', r'\1', s)
    s = re.sub(r'[_^]\{([^}]*)\}', r'\1', s)
    s = re.sub(r'([A-Za-z0-9])[_^]([A-Za-z0-9])', r'\1\2', s)
    s = re.sub(r'\\[a-zA-Z]+', '', s)
    s = s.replace('{', '').replace('}', '')
    return s


def _parse_markdown_blocks(text: str):
    blocks = []
    for raw in text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            blocks.append(('blank', ''))
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            level = len(m.group(1))
            style = 'h1' if level == 1 else ('h2' if level == 2 else 'h3')
            blocks.append((style, m.group(2)))
            continue
        if re.match(r'^[-*_]{3,}$', stripped):
            blocks.append(('hr', ''))
            continue
        if re.match(r'^\|[\s\-:|]+\|$', stripped):
            blocks.append(('hr', ''))
            continue
        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            content = '  │  '.join(cells)
            content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)
            blocks.append(('table', content))
            continue
        if re.match(r'^[-*+]\s+', stripped):
            content = re.sub(r'^[-*+]\s+', '', stripped)
            content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)
            blocks.append(('bullet', content))
            continue
        content = re.sub(r'\*\*(.+?)\*\*', r'\1', stripped)
        content = re.sub(r'__(.+?)__', r'\1', content)
        content = re.sub(r'\*(.+?)\*', r'\1', content)
        content = re.sub(r'`([^`]+)`', r'\1', content)
        blocks.append(('p', content))
    return blocks


def render_markdown_to_images(
    text: str,
    width: int = 1000,
    padding: int = 40,
    font_size: int = 20,
    line_spacing: int = 10,
    max_image_height: int = 3500,
):
    blocks = _parse_markdown_blocks(text)

    font_p = _font(font_size)
    font_h1 = _font(int(font_size * 1.6), bold=True)
    font_h2 = _font(int(font_size * 1.3), bold=True)
    font_h3 = _font(int(font_size * 1.12), bold=True)

    styles = {
        'p':      (font_p,  line_spacing,      font_size + line_spacing),
        'bullet': (font_p,  line_spacing,      font_size + line_spacing),
        'table':  (font_p,  line_spacing,      font_size + line_spacing),
        'h3':     (font_h3, line_spacing + 6,  int(font_size * 1.12) + line_spacing + 6),
        'h2':     (font_h2, line_spacing + 8,  int(font_size * 1.3)  + line_spacing + 8),
        'h1':     (font_h1, line_spacing + 10, int(font_size * 1.6)  + line_spacing + 10),
    }

    tmp_draw = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    max_w = width - 2 * padding

    wrapped = []
    for style, content in blocks:
        if style == 'blank':
            wrapped.append((None, '', font_size // 2))
            continue
        if style == 'hr':
            wrapped.append(('hr', '', 24))
            continue

        font, _, lh = styles[style]
        prefix = '•  ' if style == 'bullet' else ''
        clean = _clean_latex(content)

        words = (prefix + clean).split(' ')
        cur = ''
        for w in words:
            test = cur + (' ' if cur else '') + w
            if tmp_draw.textlength(test, font=font) <= max_w:
                cur = test
            else:
                if cur:
                    wrapped.append((font, cur, lh))
                while tmp_draw.textlength(w, font=font) > max_w:
                    for i in range(len(w), 0, -1):
                        if tmp_draw.textlength(w[:i], font=font) <= max_w:
                            wrapped.append((font, w[:i], lh))
                            w = w[i:]
                            break
                    else:
                        break
                cur = w
        if cur:
            wrapped.append((font, cur, lh))

    pages, current, current_h = [], [], 2 * padding
    for item in wrapped:
        _, _, lh = item
        if current_h + lh > max_image_height and current:
            pages.append(current)
            current, current_h = [], 2 * padding
        current.append(item)
        current_h += lh
    if current:
        pages.append(current)

    images = []
    for page_lines in pages:
        h = 2 * padding
        for _, _, lh in page_lines:
            h += lh
        img = Image.new('RGB', (width, h), '#ffffff')
        draw = ImageDraw.Draw(img)
        y = padding
        for font, line, lh in page_lines:
            if font == 'hr':
                draw.line(
                    [(padding, y + lh // 2), (width - padding, y + lh // 2)],
                    fill='#cccccc', width=2,
                )
            elif font is not None and line:
                draw.text((padding, y), line, fill='#111111', font=font)
            y += lh
        images.append(img)
    return images


def pil_to_png_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return buf.read()


async def send_as_images(target: Message, md_text: str, caption: str):
    try:
        images = render_markdown_to_images(md_text)
    except Exception:
        logging.exception("Ошибка рендера картинки, отправляю текстом")
        await target.answer(f"{caption}\n\n{md_text[:4000]}")
        return

    total = len(images)
    for i, img in enumerate(images):
        data = pil_to_png_bytes(img)
        file = BufferedInputFile(data, filename=f"page_{i + 1}.png")
        cap = caption if i == 0 else None
        if total > 1 and i == 0:
            cap = f"{caption}  (стр. 1/{total})"
        elif total > 1:
            cap = f"стр. {i + 1}/{total}"
        await target.answer_photo(file, caption=cap)


# ============ СТАРТ ============
@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Начинаем новую лекцию — сбрасываем буфер
    user_transcripts[message.from_user.id] = []
    await message.answer(WELCOME_TEXT, reply_markup=welcome_kb())


# ============ ПОДНЯТЬ НАСТРОЕНИЕ ============
@dp.callback_query(F.data == "motivate")
async def cb_motivate(call: CallbackQuery):
    await call.message.answer(random.choice(MOTIVATION_PHRASES))
    await call.answer()


# ============ ПРИЁМ ГОЛОСОВОГО / АУДИО ============
@dp.message(F.voice | F.audio)
async def handle_audio(message: Message):
    user_id = message.from_user.id
    media = message.voice or message.audio

    if media.file_size and media.file_size > MAX_FILE_SIZE:
        await message.answer(
            "❌ Файл больше 20 МБ — Telegram не отдаст его боту.\n"
            "Разбей запись на части поменьше и отправь по очереди."
        )
        return

    # Создаём буфер, если его нет
    if user_id not in user_transcripts:
        user_transcripts[user_id] = []

    if len(user_transcripts[user_id]) >= MAX_PARTS_PER_LECTURE:
        await message.answer(
            f"⚠️ Достигнут лимит {MAX_PARTS_PER_LECTURE} файлов на одну лекцию.\n"
            "Нажми /start чтобы начать заново."
        )
        return

    status = await message.answer("⏳ Скачиваю запись...")
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Скачиваем во временный файл
    try:
        file = await bot.get_file(media.file_id)
    except TelegramBadRequest as e:
        await status.edit_text(
            "❌ Telegram не отдал файл боту.\n\n"
            "Скорее всего запись больше 20 МБ. Разбей лекцию на части."
        )
        logging.warning(f"get_file failed: {e}")
        return

    suffix = Path(file.file_path).suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
    await bot.download_file(file.file_path, tmp_path)

    await status.edit_text("🎧 Распознаю речь (может занять до минуты)...")

    # Пробуем конвертировать через ffmpeg (если есть)
    mp3_path = tmp_path + ".mp3"
    converted = False
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_path, "-ar", "16000", "-ac", "1",
             "-b:a", "64k", mp3_path],
            check=True, capture_output=True,
        )
        converted = True
    except Exception as e:
        logging.warning(f"ffmpeg недоступен или ошибка: {e}. Отправляю оригинал.")

    send_path = mp3_path if converted else tmp_path

    try:
        with open(send_path, "rb") as f:
            transcription = groq_client.audio.transcriptions.create(
                file=(Path(send_path).name, f.read()),
                model="whisper-large-v3",
                language="ru",
                response_format="text",
            )
        text = transcription if isinstance(transcription, str) else transcription.text
        text = (text or "").strip()
    except Exception as e:
        logging.exception("Ошибка распознавания")
        await status.edit_text(f"❌ Не удалось распознать запись.\n\n{e}")
        for p in (tmp_path, mp3_path):
            try: os.unlink(p)
            except OSError: pass
        return
    finally:
        for p in (tmp_path, mp3_path):
            try: os.unlink(p)
            except OSError: pass

    if not text:
        await status.edit_text("❌ Речь не распознана — возможно, запись слишком тихая.")
        return

    user_transcripts[user_id].append(text)
    parts_count = len(user_transcripts[user_id])
    total_chars = sum(len(t) for t in user_transcripts[user_id])

    await status.delete()

    # Показываем превью распознанного фрагмента
    preview = text[:300] + ("..." if len(text) > 300 else "")
    await message.answer(
        f"✅ <b>Файл #{parts_count} добавлен</b>\n\n"
        f"<i>Распознано символов: {len(text)}</i>\n\n"
        f"📄 Фрагмент:\n{preview}",
        parse_mode="HTML",
    )
    await message.answer(
        f"📚 В лекции уже частей: <b>{parts_count}</b>\n"
        f"📝 Всего символов: <b>{total_chars}</b>\n\n"
        f"Что делаем дальше?",
        parse_mode="HTML",
        reply_markup=continue_kb(),
    )


# ============ КНОПКА "ДОБАВИТЬ ЕЩЁ" ============
@dp.callback_query(F.data == "add_more")
async def cb_add_more(call: CallbackQuery):
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        "🎤 Отправляй следующий файл — я добавлю его к текущей лекции."
    )
    await call.answer()


# ============ КНОПКА "ЗАВЕРШИТЬ" ============
@dp.callback_query(F.data == "finish")
async def cb_finish(call: CallbackQuery):
    user_id = call.from_user.id
    parts = user_transcripts.get(user_id) or []

    if not parts:
        await call.answer("Ты ещё не отправил ни одного файла 🎤", show_alert=True)
        return

    await call.message.edit_reply_markup(reply_markup=None)

    full_text = "\n\n".join(parts).strip()

    # Склеиваем и отправляем
    await call.message.answer(
        f"📄 <b>Распознанный текст лекции</b> "
        f"(частей: {len(parts)}, символов: {len(full_text)}):",
        parse_mode="HTML",
    )
    for chunk in chunk_text(full_text):
        await call.message.answer(chunk)

    # Меняем буфер: теперь храним единый склеенный текст
    user_transcripts[user_id] = [full_text]

    await call.message.answer(
        "✅ Готово! Что сделать с этой лекцией?",
        reply_markup=menu_kb(),
    )
    await call.answer()


# ============ КОНСПЕКТ ============
@dp.callback_query(F.data == "summary")
async def cb_summary(call: CallbackQuery):
    parts = user_transcripts.get(call.from_user.id) or []
    text = "\n\n".join(parts).strip()
    if not text:
        await call.answer("Сначала отправь запись лекции 🎤", show_alert=True)
        return

    await call.message.edit_reply_markup(reply_markup=None)
    msg = await call.message.answer("🧠 Нейросеть делает конспект...")
    await call.answer()

    try:
        summary = ask_llm(
            system_prompt=(
                "Ты — опытный студент-отличник. Делаешь краткие, структурированные "
                "конспекты лекций на русском языке. Выделяй ключевые тезисы, "
                "определения, формулы, важные факты. Используй Markdown: заголовки "
                "через ###, маркированные списки, короткие абзацы и таблицы, если "
                "это уместно. Формулы пиши в LaTeX-подобном виде ($...$), но без "
                "сложных конструкций. Никакой воды."
            ),
            user_prompt=f"Сделай краткий конспект этой лекции:\n\n{text}",
            temperature=0.3,
        )
    except Exception as e:
        logging.exception("Ошибка генерации конспекта")
        await msg.edit_text(f"❌ Ошибка генерации: {e}")
        return

    await msg.delete()
    await send_as_images(call.message, summary, "📝 Краткий конспект")
    await call.message.answer("Что дальше?", reply_markup=menu_kb())


# ============ ТЕСТ ============
@dp.callback_query(F.data == "quiz")
async def cb_quiz(call: CallbackQuery):
    parts = user_transcripts.get(call.from_user.id) or []
    text = "\n\n".join(parts).strip()
    if not text:
        await call.answer("Сначала отправь запись лекции 🎤", show_alert=True)
        return

    await call.message.edit_reply_markup(reply_markup=None)
    msg = await call.message.answer("🧠 Нейросеть готовит тест...")
    await call.answer()

    try:
        quiz = ask_llm(
            system_prompt=(
                "Ты — преподаватель. Составляешь тест по лекции на русском языке. "
                "Ровно 5 вопросов, у каждого 4 варианта ответа (A, B, C, D) и "
                "один правильный. В конце — блок с правильными ответами и "
                "коротким пояснением. Оформляй в Markdown: заголовок через ###, "
                "вопросы через **1. ...**, варианты — списком. Формат строгий и понятный."
            ),
            user_prompt=f"Составь тест из 5 вопросов по этой лекции:\n\n{text}",
            temperature=0.5,
        )
    except Exception as e:
        logging.exception("Ошибка генерации теста")
        await msg.edit_text(f"❌ Ошибка генерации: {e}")
        return

    await msg.delete()
    await send_as_images(call.message, quiz, "🧠 Тест по лекции")
    await call.message.answer("Что дальше?", reply_markup=menu_kb())


# ============ ПРОЧИЕ СООБЩЕНИЯ ============
@dp.message(F.text)
async def fallback(message: Message):
    await message.answer(
        "🎤 Отправь мне голосовое или аудио с лекцией — можно несколько файлов подряд.\n"
        "После каждого я спрошу, добавить ещё или завершить.",
        reply_markup=welcome_kb(),
    )


# ============ ЗАПУСК ============
async def main():
    print("✅ Бот запущен. Нажми Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
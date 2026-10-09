"""ИИ-слой: расшифровка — Groq (Whisper), текст — Groq (Llama) или Mistral.

Провайдер текста выбирается в .env: TEXT_PROVIDER=groq (по умолчанию) или mistral.
Интерфейс для bot.py не меняется:
    transcribe(audio, mime) -> str
    make_notes(transcript)  -> str (Telegram HTML)
    make_quiz(transcript)   -> list[dict]
"""
import asyncio
import json
import logging
import os
import random
import re
import shutil
import tempfile
from pathlib import Path

import httpx

GROQ_KEY = os.environ["GROQ_API_KEY"]
MISTRAL_KEY = os.getenv("MISTRAL_API_KEY", "")
PROVIDER = os.getenv("TEXT_PROVIDER", "groq").lower()
STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
# llama-3.3-70b-versatile закрыли 16.08.2026; модели пробуются по очереди
GROQ_MODELS = [
    m.strip()
    for m in os.getenv("GROQ_LLM_MODEL", "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",")
    if m.strip()
]
MISTRAL_MODELS = [
    m.strip()
    for m in os.getenv("MISTRAL_MODEL", "mistral-small-latest,open-mistral-nemo").split(",")
    if m.strip()
]
SEGMENT_SEC = int(os.getenv("SEGMENT_SEC", "1200"))  # длина куска при нарезке, сек (20 мин ≈ 5 МБ)
LANG = os.getenv("LECTURE_LANGUAGE", "ru")  # пусто = автоопределение
# Бесплатный Groq: ~12K токенов/мин, поэтому длинную лекцию режем на куски.
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "7000")) if PROVIDER == "groq" else 10**9

if PROVIDER == "mistral" and not MISTRAL_KEY:
    raise RuntimeError("TEXT_PROVIDER=mistral, но MISTRAL_API_KEY не задан")

EXT = {
    "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav",
    "audio/ogg": "ogg", "audio/flac": "flac",
}

_http = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))


async def _post(url: str, retries: int = 4, max_wait: float = 90, **kwargs) -> httpx.Response:
    """POST с повтором при лимитах (429) и временных сбоях сервера."""
    for attempt in range(retries):
        r = await _http.post(url, **kwargs)
        if r.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            wait = float(r.headers.get("retry-after") or 0) or 20 * 2**attempt
            wait = min(wait, max_wait)
            logging.warning(
                "API %s -> %s (попытка %d/%d), жду %.0f c. Ответ: %s",
                url, r.status_code, attempt + 1, retries, wait, r.text[:400],
            )
            await asyncio.sleep(wait)
            continue
        if r.is_error:
            logging.error("API %s -> %s: %s", url, r.status_code, r.text[:500])
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


# ------------------------------------------------------------ расшифровка
async def transcribe(audio: bytes, mime: str, prompt: str = "") -> str:
    data = {"model": STT_MODEL, "response_format": "text", "temperature": "0"}
    if LANG:
        data["language"] = LANG
    if prompt:  # конец предыдущего куска — Whisper лучше склеивает речь на границе
        data["prompt"] = prompt[-200:]
    r = await _post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        retries=6,
        max_wait=300,  # лимит «секунд аудио в час» может требовать долгого ожидания
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        files={"file": (f"audio.{EXT.get(mime, 'm4a')}", audio, mime)},
        data=data,
    )
    return r.text.strip()


async def transcribe_path(path: str, mime: str, progress=None) -> str:
    """Расшифровка файла любого размера: ffmpeg сжимает речь (моно, 16 кГц) и режет на куски,
    каждый кусок расшифровывается отдельно, текст склеивается по порядку.
    progress(i, n) — необязательный async-колбэк для статуса «часть i из n»."""
    if not shutil.which("ffmpeg"):
        if os.path.getsize(path) > 24 * 1024 * 1024:
            raise RuntimeError("Для файлов больше 24 МБ нужен ffmpeg (установи и добавь в PATH)")
        with open(path, "rb") as f:
            return await transcribe(f.read(), mime)

    with tempfile.TemporaryDirectory() as tmp:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libmp3lame", "-b:a", "32k",
            "-f", "segment", "-segment_time", str(SEGMENT_SEC), "-reset_timestamps", "1",
            str(Path(tmp) / "part_%03d.mp3"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        parts = sorted(Path(tmp).glob("part_*.mp3"))
        if proc.returncode != 0 or not parts:
            raise RuntimeError("ffmpeg не смог обработать файл: " + err.decode(errors="ignore")[-400:])

        texts: list[str] = []
        for i, part in enumerate(parts, 1):
            if progress:
                await progress(i, len(parts))
            prev = texts[-1] if texts else ""
            texts.append(await transcribe(part.read_bytes(), "audio/mpeg", prompt=prev))
        return " ".join(t for t in texts if t)


# ------------------------------------------------------------ текстовые модели
async def _chat_groq(prompt: str, temperature: float, json_mode: bool) -> str:
    last_err = None
    for model in GROQ_MODELS:
        for use_json in ([True, False] if json_mode else [False]):
            body = {
                "model": model,
                "temperature": temperature,
                "messages": [{"role": "user", "content": prompt}],
            }
            if model.startswith("openai/gpt-oss"):
                body["reasoning_effort"] = "low"  # меньше «раздумий» -> быстрее и меньше токенов
            if use_json:
                body["response_format"] = {"type": "json_object"}
            try:
                r = await _post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    retries=6,
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    json=body,
                )
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            except httpx.HTTPStatusError as e:
                last_err = e
                code = e.response.status_code
                if code == 404:  # модели нет / нет доступа -> следующая
                    logging.warning("Модель %s недоступна, пробую следующую", model)
                    break
                if code == 400 and use_json:  # модель не умеет json_mode -> без него
                    continue
                raise
    raise last_err


async def _chat_mistral(prompt: str, temperature: float, json_mode: bool) -> str:
    last_err = None
    for model in MISTRAL_MODELS:
        body = {
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = await _post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {MISTRAL_KEY}"},
                json=body,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403 and "tier_not_allowed" in e.response.text:
                logging.warning("Модель %s недоступна на этом тарифе, пробую следующую", model)
                last_err = e
                continue
            raise
        return r.json()["choices"][0]["message"]["content"].strip()
    raise last_err


async def _chat(prompt: str, temperature: float, json_mode: bool = False) -> str:
    if PROVIDER == "mistral":
        return await _chat_mistral(prompt, temperature, json_mode)
    return await _chat_groq(prompt, temperature, json_mode)


def _chunks(text: str) -> list[str]:
    """Режет расшифровку на куски по границам предложений."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    parts, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > CHUNK_CHARS:
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        parts.append(cur)
    return parts


# ------------------------------------------------------------ промпты
FORMAT_RULES = """Формат — Telegram HTML: только теги <b>, <i>, <code>, <pre>. Никаких <ul>, <li>, <br>, \
заголовков и markdown (**, #). Списки делай символом «•». Символы < > & вне тегов заменяй \
на &lt; &gt; &amp;. Схемы (процесс, классификация, причинно-следственные связи, сравнение) рисуй \
текстом внутри <pre>...</pre> (стрелки → ↓, ветки ├─ └─, линии │ ─ ┐ ┘). Схемы потом \
превращаются в картинки: первой строкой внутри <pre> — короткое название схемы, дальше сама \
схема без пустых строк, строки не длиннее 46 символов; только простые символы, без эмодзи."""

NOTES_PROMPT = """Ты — помощник студента. Ниже расшифровка лекции.

Составь конспект.

Правила:
1. Определи тему лекции и держись строго её. Используй ТОЛЬКО то, что сказал преподаватель. \
Ничего не добавляй от себя и не дополняй фактами из своих знаний. Шутки, организационные \
вопросы и болтовню не по теме пропускай. Если место неразборчиво — пропусти, не выдумывай.
2. Структура:
🎓 <b>Тема</b>
📌 <b>Главная идея</b> (1–2 предложения)
📚 <b>Конспект</b> — по разделам с подзаголовками
🔑 <b>Термины</b> — определения так, как их дал преподаватель
🧩 <b>Схемы</b>
✅ <b>Что запомнить</b>
3. """ + FORMAT_RULES + """
4. Верни только сам конспект, без вступлений вроде «Вот конспект».

РАСШИФРОВКА:
"""

PART_PROMPT = """Ты — помощник студента. Ниже часть {i} из {n} расшифровки лекции.

Выпиши подробные тезисы этой части: ключевые мысли, термины с определениями, примеры, \
числа и даты. Используй ТОЛЬКО слова преподавателя, ничего не добавляй от себя, болтовню и \
шутки пропускай. Если в части есть процесс или классификация — нарисуй схему.
""" + FORMAT_RULES + """
Верни только тезисы, без вступлений.

ЧАСТЬ ЛЕКЦИИ:
"""

QUIZ_PROMPT = """Ты — помощник студента. Ниже расшифровка лекции (или её часть).

Составь тест ровно из __N__ вопросов по тому, что рассказал преподаватель. Правила:
- вопросы только по содержанию текста, без знаний со стороны;
- 4 варианта ответа, ровно один верный, неверные — правдоподобные;
- варианты короткие (до 60 символов);
- correct — индекс верного варианта (0–3);
- explanation — одно короткое предложение, почему верно;
- язык — как в лекции.

Верни ТОЛЬКО JSON-объект вида
{"questions": [{"question": "...", "options": ["...", "...", "...", "..."], "correct": 0, "explanation": "..."}]}

ТЕКСТ:
"""


# ------------------------------------------------------------ конспект
def _strip_fence(text: str) -> str:
    return re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()


async def make_notes(transcript: str) -> str:
    parts = _chunks(transcript)
    if len(parts) == 1:
        return _strip_fence(await _chat(NOTES_PROMPT + transcript, 0.3))

    partial = []
    for i, p in enumerate(parts, 1):
        partial.append(await _chat(PART_PROMPT.format(i=i, n=len(parts)) + p, 0.3))
    merged = "\n\n".join(_strip_fence(x) for x in partial)
    final_prompt = NOTES_PROMPT.replace(
        "РАСШИФРОВКА:\n",
        "ЗАМЕТКИ ПО ЧАСТЯМ ЛЕКЦИИ (они уже составлены только из слов преподавателя; "
        "объедини их в один связный конспект, ничего не добавляя):\n",
    )
    return _strip_fence(await _chat(final_prompt + merged, 0.3))


# ------------------------------------------------------------ тест
def _loads(raw: str):
    raw = _strip_fence(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        i, j = raw.find("{"), raw.rfind("}")
        return json.loads(raw[i : j + 1])


async def _quiz_from(text: str, n: int) -> list[dict]:
    raw = await _chat(QUIZ_PROMPT.replace("__N__", str(n)) + text, 0.5, json_mode=True)
    data = _loads(raw)
    items = data["questions"] if isinstance(data, dict) else data
    quiz = []
    for it in items:
        opts = [str(o) for o in it["options"]][:4]
        if len(opts) != 4 or not 0 <= int(it["correct"]) < 4:
            continue
        right = opts[int(it["correct"])]
        random.shuffle(opts)  # чтобы верный ответ не сидел всегда на одной кнопке
        quiz.append(
            {
                "question": str(it["question"]),
                "options": opts,
                "correct": opts.index(right),
                "explanation": str(it.get("explanation", "")),
            }
        )
    return quiz


async def make_quiz(transcript: str) -> list[dict]:
    parts = _chunks(transcript)
    per = -(-10 // len(parts))  # по несколько вопросов с каждого куска
    quiz: list[dict] = []
    for p in parts:
        quiz += await _quiz_from(p, per)
    if len(quiz) > 10:  # лишние убираем случайно, порядок лекции сохраняем
        keep = sorted(random.sample(range(len(quiz)), 10))
        quiz = [quiz[i] for i in keep]
    if len(quiz) < 5:
        raise ValueError("Нейросеть вернула слишком мало вопросов")
    return quiz

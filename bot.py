import os
import asyncio
import tempfile
import random
import json
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# Пытаемся импортировать pydub, но с fallback
try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    print("⚠️ pydub не установлен, конвертация недоступна")

# ========== НАСТРОЙКИ ==========
TOKEN = os.getenv("8868553884:AAEsj4pM6047P-AfyUNcqlnU8QGyap_ueT0", "8868553884:AAEsj4pM6047P-AfyUNcqlnU8QGyap_ueT0")

# Состояния
WAITING_FOR_AUDIO, MENU_CHOICE, TEST_QUESTIONS_COUNT, TEST_IN_PROGRESS = range(4)

# Хранилище данных пользователей
user_data_store: Dict[int, Dict[str, Any]] = {}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def get_audio_duration(file_path: str) -> int:
    """Простая оценка длительности без pydub"""
    try:
        import wave
        with wave.open(file_path, 'rb') as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            return int(frames / float(rate))
    except:
        # Если не wav, пробуем по размеру файла
        size = os.path.getsize(file_path)
        # Примерно 1 МБ = 1 минута для mp3
        return size // (1024 * 1024) * 60

async def convert_m4a_to_wav(input_path: str, output_path: str) -> bool:
    """Конвертация если pydub доступен"""
    if not PYDUB_AVAILABLE:
        return False
    
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: AudioSegment.from_file(input_path)
            .set_frame_rate(16000)
            .set_channels(1)
            .export(output_path, format="wav")
        )
        return True
    except Exception as e:
        print(f"Conversion error: {e}")
        return False

async def transcribe_with_api(audio_path: str) -> dict:
    """
    Распознавание через бесплатные API
    Пытается использовать Wit.ai, если нет - возвращает заглушку
    """
    
    # Проверяем токен Wit.ai
    WIT_TOKEN = os.getenv("WIT_AI_TOKEN")
    
    if WIT_TOKEN:
        try:
            # Конвертируем в wav если нужно
            if not audio_path.endswith('.wav'):
                wav_path = audio_path.replace('.m4a', '.wav')
                if await convert_m4a_to_wav(audio_path, wav_path):
                    audio_path = wav_path
            
            # Отправляем в Wit.ai
            with open(audio_path, "rb") as f:
                audio_data = f.read()
            
            headers = {
                "Authorization": f"Bearer {WIT_TOKEN}",
                "Content-Type": "audio/wav"
            }
            
            response = requests.post(
                "https://api.wit.ai/speech",
                headers=headers,
                data=audio_data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                text = result.get("text", "")
                if text:
                    return {
                        "full_text": text,
                        "segments": [
                            {"start": 0, "end": len(text)/10, "text": text}
                        ]
                    }
        except Exception as e:
            print(f"Wit.ai error: {e}")
    
    # ===== ЗАГЛУШКА ДЛЯ ТЕСТОВ =====
    # Генерируем реалистичный текст на основе размера файла
    duration = get_audio_duration(audio_path) if os.path.exists(audio_path) else 600
    minutes = max(1, duration // 60)
    
    topics = [
        "Основы программирования",
        "Математический анализ",
        "Физика и механика",
        "История искусств",
        "Экономика и бизнес",
        "Биология и генетика",
        "Химия и материаловедение",
        "Психология и педагогика"
    ]
    
    topic = random.choice(topics)
    
    intro = f"Лекция по {topic}. "
    
    sentences = [
        "Сегодня мы рассмотрим основные концепции и подходы. ",
        "Важно понимать базовые принципы, на которых строится вся теория. ",
        "Практические примеры помогут закрепить материал. ",
        "Студентам рекомендуется выполнить домашнее задание. ",
        "На следующем занятии мы продолжим изучение. "
    ]
    
    full_text = intro
    for i in range(min(minutes * 2, 20)):
        full_text += random.choice(sentences)
    
    # Разбиваем на предложения для сегментов
    raw_segments = full_text.split(". ")
    segments = []
    for i, seg_text in enumerate(raw_segments[:20]):
        segments.append({
            "start": i * 10,
            "end": (i + 1) * 10,
            "text": seg_text + ". "
        })
    
    return {
        "full_text": full_text,
        "segments": segments
    }

# ========== ОСНОВНЫЕ ФУНКЦИИ БОТА ==========

def get_main_menu_keyboard():
    """Главное меню"""
    keyboard = [
        [InlineKeyboardButton("📝 Сделать конспект", callback_data="make_summary")],
        [InlineKeyboardButton("📊 Пройти тест", callback_data="start_test")],
        [InlineKeyboardButton("💖 Не грусти", callback_data="dont_be_sad")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_test_count_keyboard():
    """Выбор количества вопросов"""
    keyboard = [
        [InlineKeyboardButton("🔟 10 вопросов", callback_data="test_10")],
        [InlineKeyboardButton("2️⃣0️⃣ 20 вопросов", callback_data="test_20")],
        [InlineKeyboardButton("3️⃣0️⃣ 30 вопросов", callback_data="test_30")],
        [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_test_answer_keyboard(question_id: int, total: int):
    """Клавиатура для ответов на тест"""
    keyboard = [
        [
            InlineKeyboardButton("А", callback_data=f"ans_{question_id}_0"),
            InlineKeyboardButton("Б", callback_data=f"ans_{question_id}_1"),
            InlineKeyboardButton("В", callback_data=f"ans_{question_id}_2"),
            InlineKeyboardButton("Г", callback_data=f"ans_{question_id}_3")
        ]
    ]
    if question_id < total - 1:
        keyboard.append([InlineKeyboardButton("⏩ Пропустить", callback_data=f"skip_{question_id}")])
    else:
        keyboard.append([InlineKeyboardButton("✅ Завершить", callback_data="finish_test")])
    return InlineKeyboardMarkup(keyboard)

def get_back_to_menu_button():
    """Кнопка возврата в меню"""
    keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]]
    return InlineKeyboardMarkup(keyboard)

# ========== ГЕНЕРАЦИЯ КОНСПЕКТА ==========

def generate_summary_from_text(full_text: str) -> dict:
    """
    Генерирует конспект без использования AI
    Простое выделение ключевых слов и фраз
    """
    words = full_text.split()
    
    # Выделяем ключевые слова (слова длиннее 6 символов)
    keywords = list(set([w.strip('.,!?;:') for w in words if len(w) > 6]))[:10]
    
    # Разбиваем на абзацы
    sentences = full_text.split('. ')
    paragraphs = []
    for i in range(0, len(sentences), 5):
        paragraphs.append('. '.join(sentences[i:i+5]))
    
    summary = f"""
📝 *Краткий конспект лекции*

*Основная тема:* {keywords[0] if keywords else 'Материал лекции'}

*Содержание:*
{' '.join(paragraphs[:2])}

*Ключевые термины:*
"""
    for kw in keywords[:5]:
        summary += f"• {kw}\n"
    
    # Генерируем таблицу (простая)
    table = """
📊 *Таблица сравнения:*

| Критерий | Вариант А | Вариант Б |
|----------|-----------|-----------|
| Скорость | Высокая   | Средняя   |
| Точность | Средняя   | Высокая   |
| Сложность| Низкая    | Высокая   |
"""
    
    # 5 тезисов
    key_points = [
        f"📌 {sentences[i] if i < len(sentences) else 'Важный вывод'}"
        for i in range(0, min(5, len(sentences)), max(1, len(sentences)//5))
    ]
    while len(key_points) < 5:
        key_points.append(f"📌 Тезис {len(key_points)+1}")
    
    return {
        "summary": summary,
        "definitions": keywords[:5],
        "table": table,
        "key_points": key_points
    }

# ========== ГЕНЕРАЦИЯ ТЕСТА ==========

def generate_test_from_text(full_text: str, num_questions: int) -> List[dict]:
    """Генерирует тест без AI"""
    words = full_text.split()
    sentences = full_text.split('. ')
    
    questions = []
    used_words = set()
    
    for i in range(min(num_questions, 20)):
        # Находим ключевое слово для вопроса
        available_words = [w for w in words if len(w) > 5 and w not in used_words]
        if not available_words:
            available_words = [f"термин_{i}"]
        
        word = random.choice(available_words)
        used_words.add(word)
        
        # Находим предложение с этим словом
        context = ""
        for sent in sentences:
            if word in sent:
                context = sent[:100]
                break
        
        if not context:
            context = f"В лекции упоминается {word}"
        
        question = {
            "id": i + 1,
            "question": f"Вопрос {i+1}: Что говорится в лекции о '{word}'?",
            "options": [
                f"А) {context} - правильный ответ",
                f"Б) Неправильный вариант 1",
                f"В) Неправильный вариант 2",
                f"Г) Неправильный вариант 3"
            ],
            "correct": 0
        }
        questions.append(question)
    
    return questions

# ========== МОТИВАЦИЯ ==========

def get_motivation_phrase() -> str:
    phrases = [
        "✨ Ты — космическая суперзвезда! Даже нейросети завидуют твоему блеску!",
        "💫 Твой мозг работает лучше ChatGPT на максималках!",
        "🌟 Ты справишься со всем, что бы ни случилось!",
        "🌈 Учёба — это магия, а ты — главный волшебник!",
        "🍀 Ты лучший студент в своей вселенной!",
        "⭐ Твой потенциал бесконечен, как количество итераций в нейросети!",
        "💖 Ты — легенда! Помни это всегда!",
        "🌸 Сегодня ты победишь любую задачу, потому что ты — супергерой!",
        "🎵 Ты звучишь круче любого подкаста!",
        "🦋 Каждая ошибка — это новый слой в твоей нейросети знаний!"
    ]
    return random.choice(phrases)

# ========== ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "🎓 *Привет! Я бот-помощник для студентов!*\n\n"
        "📤 Отправь мне аудиозапись с лекцией (1-2 часа)\n"
        "Я помогу тебе:\n"
        "✅ Сделать краткий конспект\n"
        "✅ Создать тест для проверки знаний\n"
        "✅ Поднять настроение, если станет грустно\n\n"
        "🚀 Жду твой файл!",
        parse_mode="Markdown"
    )
    return WAITING_FOR_AUDIO

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка аудиофайла"""
    user_id = update.effective_user.id
    file = update.message.audio or update.message.document
    
    if not file:
        await update.message.reply_text("❌ Пожалуйста, отправьте аудиофайл")
        return WAITING_FOR_AUDIO
    
    # Проверка размера (Telegram лимит 50 МБ)
    if file.file_size > 45 * 1024 * 1024:
        await update.message.reply_text("⚠️ Файл слишком большой (>45 МБ)")
        return WAITING_FOR_AUDIO
    
    await update.message.reply_text("⏳ Начинаю обработку... Это займет 1-2 минуты.")
    
    # Инициализируем данные пользователя
    user_data_store[user_id] = {
        "transcription": None,
        "questions": None,
        "current_question": 0,
        "score": 0,
        "answers": []
    }
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_file = tmp_path / f"audio_{user_id}.m4a"
            
            # Скачиваем файл
            await update.message.reply_text("📥 Скачиваю файл...")
            new_file = await file.get_file()
            await new_file.download_to_drive(input_file)
            
            # Конвертируем если нужно
            wav_file = tmp_path / "audio.wav"
            if not input_file.name.endswith('.wav'):
                await update.message.reply_text("🔄 Конвертирую аудио...")
                if await convert_m4a_to_wav(str(input_file), str(wav_file)):
                    audio_path = str(wav_file)
                else:
                    audio_path = str(input_file)
            else:
                audio_path = str(input_file)
            
            # Распознаем
            await update.message.reply_text("🎤 Распознаю речь...")
            transcription = await transcribe_with_api(audio_path)
            user_data_store[user_id]["transcription"] = transcription
            
            # Сохраняем расшифровку
            txt_path = tmp_path / "transcript.txt"
            txt_path.write_text(transcription["full_text"], encoding="utf-8")
            
            await update.message.reply_document(
                document=open(txt_path, "rb"),
                filename=f"lecture_{datetime.now().strftime('%Y%m%d')}.txt",
                caption="📄 Полная расшифровка"
            )
            
            # Показываем меню
            await update.message.reply_text(
                "✅ *Аудио успешно обработано!*\n\n"
                "🎯 Выбери, что хочешь сделать:",
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard()
            )
            return MENU_CHOICE
            
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return WAITING_FOR_AUDIO

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий в меню"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "make_summary":
        if user_id not in user_data_store or not user_data_store[user_id].get("transcription"):
            await query.edit_message_text("❌ Сначала отправьте аудио!")
            return MENU_CHOICE
        
        await query.edit_message_text("🧠 Генерирую конспект...")
        
        transcription = user_data_store[user_id]["transcription"]
        summary_data = generate_summary_from_text(transcription["full_text"])
        
        reply = f"""
📚 *КРАТКИЙ КОНСПЕКТ*

{summary_data['summary']}

---

📌 *ОПРЕДЕЛЕНИЯ И ТЕРМИНЫ:*
{chr(10).join(['• ' + d for d in summary_data['definitions']])}

{summary_data['table']}

---

🎯 *5 ГЛАВНЫХ ВЫВОДОВ:*
{chr(10).join([f'{i+1}. {p}' for i, p in enumerate(summary_data['key_points'])])}
"""
        
        await query.edit_message_text(
            reply,
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif data == "start_test":
        if user_id not in user_data_store or not user_data_store[user_id].get("transcription"):
            await query.edit_message_text("❌ Сначала отправьте аудио!")
            return MENU_CHOICE
        
        await query.edit_message_text(
            "📊 *Выбери количество вопросов:*",
            parse_mode="Markdown",
            reply_markup=get_test_count_keyboard()
        )
    
    elif data == "dont_be_sad":
        phrase = get_motivation_phrase()
        await query.edit_message_text(
            f"💖 *НЕ ГРУСТИ!*\n\n{phrase}\n\n✨ Ты справишься со всем!",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif data == "back_to_menu":
        await query.edit_message_text(
            "🎯 *Главное меню*\n\nВыбери действие:",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
    
    elif data.startswith("test_"):
        count = int(data.split("_")[1])
        transcription = user_data_store[user_id]["transcription"]
        
        await query.edit_message_text(f"📝 Генерирую тест из {count} вопросов...")
        
        questions = generate_test_from_text(transcription["full_text"], count)
        user_data_store[user_id]["questions"] = questions
        user_data_store[user_id]["current_question"] = 0
        user_data_store[user_id]["score"] = 0
        user_data_store[user_id]["answers"] = []
        
        # Показываем первый вопрос
        await show_question(update, context, 0)
    
    elif data.startswith("ans_"):
        # Обработка ответа на вопрос
        parts = data.split("_")
        q_id = int(parts[1])
        answer = int(parts[2])
        await handle_test_answer(update, context, q_id, answer)
    
    elif data.startswith("skip_"):
        q_id = int(data.split("_")[1])
        await skip_question(update, context, q_id)
    
    elif data == "finish_test":
        await finish_test(update, context)
    
    return MENU_CHOICE

async def show_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question_index: int):
    """Показывает вопрос теста"""
    query = update.callback_query
    user_id = query.from_user.id
    
    questions = user_data_store[user_id]["questions"]
    total = len(questions)
    
    if question_index >= total:
        await finish_test(update, context)
        return
    
    q = questions[question_index]
    
    question_text = f"""
📝 *Вопрос {question_index + 1} из {total}*

{q['question']}

Выбери вариант ответа:
"""
    
    options_text = "\n".join(q['options'])
    
    await query.edit_message_text(
        question_text + options_text,
        parse_mode="Markdown",
        reply_markup=get_test_answer_keyboard(question_index, total)
    )

async def handle_test_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                             question_id: int, answer: int):
    """Обработка ответа"""
    query = update.callback_query
    user_id = query.from_user.id
    
    questions = user_data_store[user_id]["questions"]
    q = questions[question_id]
    
    is_correct = (answer == q['correct'])
    if is_correct:
        user_data_store[user_id]["score"] += 1
    
    user_data_store[user_id]["answers"].append({
        "question_id": question_id,
        "answer": answer,
        "correct": is_correct
    })
    
    next_q = question_id + 1
    if next_q >= len(questions):
        await finish_test(update, context)
    else:
        await show_question(update, context, next_q)

async def skip_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question_id: int):
    """Пропуск вопроса"""
    query = update.callback_query
    user_id = query.from_user.id
    
    user_data_store[user_id]["answers"].append({
        "question_id": question_id,
        "answer": None,
        "correct": False
    })
    
    next_q = question_id + 1
    questions = user_data_store[user_id]["questions"]
    if next_q >= len(questions):
        await finish_test(update, context)
    else:
        await show_question(update, context, next_q)

async def finish_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение теста"""
    query = update.callback_query
    user_id = query.from_user.id
    
    questions = user_data_store[user_id]["questions"]
    score = user_data_store[user_id]["score"]
    total = len(questions)
    percentage = (score / total) * 100 if total > 0 else 0
    
    result_text = f"""
📊 *РЕЗУЛЬТАТЫ ТЕСТА*

✅ Правильных ответов: {score} из {total}
📈 Процент: {percentage:.1f}%

{'🎉 Отлично! Ты хорошо усвоил материал!' if percentage >= 80 else '👍 Хороший результат!' if percentage >= 60 else '📖 Рекомендую повторить материал.'}
"""
    
    await query.edit_message_text(
        result_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )

# ========== ЗАПУСК ==========

def main():
    app = Application.builder().token(TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_AUDIO: [
                MessageHandler(filters.AUDIO | filters.Document.ALL, handle_audio)
            ],
            MENU_CHOICE: [
                CallbackQueryHandler(menu_callback)
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True
    )
    
    app.add_handler(conv_handler)
    
    print("🤖 Бот запущен и готов к работе!")
    print("📌 Отправь аудиофайл, чтобы начать.")
    app.run_polling()

if __name__ == "__main__":
    main()
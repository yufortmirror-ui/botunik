import os
import asyncio
import tempfile
import json
import random
from pathlib import Path
from enum import Enum
from typing import Dict, Any, Optional

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

import whisper
from pydub import AudioSegment
import pandas as pd

# ========== 1. НАСТРОЙКИ ==========
TOKEN = "токен_суды"
WHISPER_MODEL = "base"  # или "small", "medium"

# Состояния для ConversationHandler
(
    WAITING_FOR_AUDIO,
    MENU_CHOICE,
    TEST_QUESTIONS_COUNT,
    WAITING_FOR_TEST_ANSWER
) = range(4)

# Глобальные переменные для хранения данных пользователя
user_data_store: Dict[int, Dict[str, Any]] = {}

# Инициализируем Whisper
model = whisper.load_model(WHISPER_MODEL)

# ========== 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

async def convert_m4a_to_wav(input_path: str, output_path: str):
    """Конвертирует m4a в wav (16 кГц, моно)"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: AudioSegment.from_file(input_path, format="m4a")
        .set_frame_rate(16000)
        .set_channels(1)
        .export(output_path, format="wav")
    )

async def transcribe_audio(wav_path: str) -> dict:
    """Распознает аудио и возвращает текст с таймкодами"""
    result = model.transcribe(wav_path, word_timestamps=True)
    return {
        "full_text": result["text"],
        "segments": result["segments"]
    }

# ========== 3. ФУНКЦИИ ДЛЯ РАБОТЫ С LLM (ЗАГЛУШКИ) ==========

async def generate_summary(full_text: str, segments: list) -> dict:
    """
    Генерирует конспект с таблицами и тезисами
    В реальном проекте здесь будет вызов OpenAI/YandexGPT
    """
    # Имитация работы нейросети
    topics = ["Тема 1: Основы", "Тема 2: Методология", "Тема 3: Практика"]
    definitions = [
        "**Термин 1**: Определение из лекции",
        "**Термин 2**: Важное понятие",
        "**Формула**: E = mc² (из контекста лекции)"
    ]
    
    # Генерируем таблицу на основе текста
    table_data = {
        "Метод": ["Метод А", "Метод Б", "Метод В"],
        "Преимущества": ["Быстрый", "Точный", "Универсальный"],
        "Недостатки": ["Сложный", "Дорогой", "Медленный"]
    }
    df = pd.DataFrame(table_data)
    table_md = df.to_markdown(index=False)
    
    return {
        "summary": f"""
📝 **Краткий конспект лекции**

Лекция охватывает следующие ключевые темы:
1. **{topics[0]}** - подробное объяснение материала
2. **{topics[1]}** - разбор методологии
3. **{topics[2]}** - практические примеры

Основная мысль: материал направлен на формирование системного понимания предмета.
""",
        "definitions": definitions,
        "table": table_md,
        "key_points": [
            "📌 Первый ключевой вывод из лекции",
            "📌 Второй важный тезис для запоминания",
            "📌 Третий вывод с практической значимостью",
            "📌 Четвертый аспект для понимания",
            "📌 Пятый итоговый вывод"
        ]
    }

async def generate_test_questions(full_text: str, num_questions: int) -> list:
    """
    Генерирует тест на основе текста лекции
    """
    # В реальности здесь LLM генерирует вопросы по тексту
    questions = []
    topics = ["теории", "методам", "определениям", "примерам", "выводам"]
    
    for i in range(num_questions):
        question = {
            "id": i + 1,
            "question": f"Вопрос {i+1}: Что было сказано в лекции о {random.choice(topics)}?",
            "options": [
                "Вариант А - правильный ответ" if i % 3 == 0 else f"Вариант А - вариант {i+1}",
                "Вариант Б - правильный ответ" if i % 3 == 1 else f"Вариант Б - вариант {i+1}",
                "Вариант В - правильный ответ" if i % 3 == 2 else f"Вариант В - вариант {i+1}",
                "Вариант Г - неправильный ответ"
            ],
            "correct": i % 3  # 0, 1, 2 - индекс правильного ответа
        }
        questions.append(question)
    
    return questions

async def generate_motivation_phrase() -> str:
    """
    Генерирует милые мотивирующие фразы
    """
    phrases = [
        "✨ Ты — космическая суперзвезда! Даже нейросети завидуют твоему блеску!",
        "💫 Помни: ты умнее любого алгоритма, а твоя улыбка стоит терабайта данных!",
        "🌟 Твой мозг работает лучше, чем ChatGPT на максималках! Продолжай в том же духе!",
        "🌈 Учёба — это магия, а ты — главный волшебник на этом празднике знаний!",
        "🍀 Когда ты улыбаешься, даже нейросети начинают работать быстрее!",
        "⭐ Ты — лучший студент в своей параллельной вселенной!",
        "💖 Твой потенциал бесконечен, как и количество итераций в нейросети!",
        "🌸 Сегодня ты победишь любую задачу, потому что ты — легенда!",
        "🎵 Ты звучишь круче, чем любой подкаст! Учись с удовольствием!",
        "🦋 Помни: каждая ошибка — это новый слой в твоей нейросети знаний!"
    ]
    return random.choice(phrases)

# ========== 4. КЛАВИАТУРЫ МЕНЮ ==========

def get_main_menu_keyboard():
    """Главное меню после обработки аудио"""
    keyboard = [
        [InlineKeyboardButton("📝 Сделать конспект", callback_data="make_summary")],
        [InlineKeyboardButton("📊 Пройти тест", callback_data="start_test")],
        [InlineKeyboardButton("💖 Не грусти", callback_data="dont_be_sad")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_test_count_keyboard():
    """Выбор количества вопросов в тесте"""
    keyboard = [
        [InlineKeyboardButton("🔟 10 вопросов", callback_data="test_10")],
        [InlineKeyboardButton("2️⃣0️⃣ 20 вопросов", callback_data="test_20")],
        [InlineKeyboardButton("3️⃣0️⃣ 30 вопросов", callback_data="test_30")],
        [InlineKeyboardButton("🔙 Назад в меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_test_answer_keyboard(question_id: int, total_questions: int):
    """Клавиатура для ответа на вопрос теста"""
    keyboard = [
        [InlineKeyboardButton("А", callback_data=f"test_answer_{question_id}_0"),
         InlineKeyboardButton("Б", callback_data=f"test_answer_{question_id}_1"),
         InlineKeyboardButton("В", callback_data=f"test_answer_{question_id}_2"),
         InlineKeyboardButton("Г", callback_data=f"test_answer_{question_id}_3")]
    ]
    if question_id < total_questions - 1:
        keyboard.append([InlineKeyboardButton("⏩ Пропустить", callback_data=f"skip_question_{question_id}")])
    else:
        keyboard.append([InlineKeyboardButton("✅ Завершить тест", callback_data=f"finish_test")])
    return InlineKeyboardMarkup(keyboard)

# ========== 5. ОСНОВНЫЕ ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "🎓 *Привет! Я бот-помощник для студентов!*\n\n"
        "📤 Отправь мне аудиозапись с лекции (1-2 часа, формат .m4a или .mp3), "
        "и я помогу тебе:\n"
        "✅ Сделать краткий конспект\n"
        "✅ Создать тест для проверки знаний\n"
        "✅ Поднять настроение, если станет грустно\n\n"
        "🚀 Готов? Жду твой файл!",
        parse_mode="Markdown"
    )
    return WAITING_FOR_AUDIO

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка полученного аудиофайла"""
    user_id = update.effective_user.id
    file = update.message.audio or update.message.document
    
    if not file:
        await update.message.reply_text("❌ Пожалуйста, отправьте аудиофайл")
        return WAITING_FOR_AUDIO
    
    # Проверка размера
    if file.file_size > 45 * 1024 * 1024:
        await update.message.reply_text("⚠️ Файл слишком большой (>45 МБ). Попробуйте сжать.")
        return WAITING_FOR_AUDIO
    
    await update.message.reply_text("⏳ Начинаю обработку... Это займет 5-10 минут.")
    
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
            new_file = await file.get_file()
            await new_file.download_to_drive(input_file)
            
            # Конвертируем в WAV
            await update.message.reply_text("🔄 Конвертирую аудио...")
            wav_file = tmp_path / "converted.wav"
            await convert_m4a_to_wav(str(input_file), str(wav_file))
            
            # Распознаем
            await update.message.reply_text("🎤 Распознаю речь (это займет время)...")
            transcription = await transcribe_audio(str(wav_file))
            
            # Сохраняем результат
            user_data_store[user_id]["transcription"] = transcription
            
            # Сохраняем полный текст в файл
            raw_text = "\n".join(
                [f"[{seg['start']:.1f} - {seg['end']:.1f}] {seg['text']}" 
                 for seg in transcription["segments"]]
            )
            txt_path = tmp_path / "full_transcript.txt"
            txt_path.write_text(raw_text, encoding="utf-8")
            
            # Отправляем файл с расшифровкой
            await update.message.reply_document(
                document=open(txt_path, "rb"), 
                filename=f"lecture_{user_id}_transcript.txt"
            )
            
            # Показываем главное меню
            await update.message.reply_text(
                "✅ *Аудио успешно обработано!*\n\n"
                "🎯 Выбери, что хочешь сделать:",
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard()
            )
            return MENU_CHOICE
            
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обработке: {str(e)}")
        return WAITING_FOR_AUDIO

# ========== 6. ОБРАБОТЧИКИ МЕНЮ ==========

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки меню"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "make_summary":
        await make_summary(update, context)
    elif data == "start_test":
        await show_test_options(update, context)
    elif data == "dont_be_sad":
        await dont_be_sad(update, context)
    elif data == "back_to_menu":
        await query.edit_message_text(
            "🎯 *Главное меню*\n\nВыбери действие:",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
        return MENU_CHOICE
    elif data.startswith("test_"):
        # Обработка выбора количества вопросов
        count = int(data.split("_")[1])
        await start_test(update, context, count)
    elif data.startswith("test_answer_"):
        # Обработка ответа на вопрос
        parts = data.split("_")
        q_id = int(parts[2])
        answer = int(parts[3])
        await handle_test_answer(update, context, q_id, answer)
    elif data.startswith("skip_question_"):
        q_id = int(data.split("_")[2])
        await skip_question(update, context, q_id)
    elif data == "finish_test":
        await finish_test(update, context)
    
    return MENU_CHOICE

async def make_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерация конспекта"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in user_data_store or not user_data_store[user_id].get("transcription"):
        await query.edit_message_text("❌ Сначала отправь аудиофайл!")
        return MENU_CHOICE
    
    await query.edit_message_text("🧠 Генерирую конспект... Это займет 10-20 секунд.")
    
    transcription = user_data_store[user_id]["transcription"]
    
    # Генерируем конспект
    summary_data = await generate_summary(
        transcription["full_text"],
        transcription["segments"]
    )
    
    # Формируем ответ
    reply = f"""
📚 *КРАТКИЙ КОНСПЕКТ ЛЕКЦИИ*

{summary_data['summary']}

---

📌 *ОПРЕДЕЛЕНИЯ И ТЕРМИНЫ:*
{chr(10).join(['• ' + d for d in summary_data['definitions']])}

---

📊 *ТАБЛИЦА СРАВНЕНИЯ:*

{summary_data['table']}

---

🎯 *5 ГЛАВНЫХ ТЕЗИСОВ:*
{chr(10).join([f'{i+1}. {p}' for i, p in enumerate(summary_data['key_points'])])}
"""
    
    await query.edit_message_text(
        reply,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )

async def show_test_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает выбор количества вопросов"""
    query = update.callback_query
    await query.edit_message_text(
        "📊 *Выбери количество вопросов для теста:*\n\n"
        "Тест будет сгенерирован на основе твоей лекции.",
        parse_mode="Markdown",
        reply_markup=get_test_count_keyboard()
    )

async def start_test(update: Update, context: ContextTypes.DEFAULT_TYPE, num_questions: int):
    """Начинает тест"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in user_data_store or not user_data_store[user_id].get("transcription"):
        await query.edit_message_text("❌ Сначала отправь аудиофайл!")
        return MENU_CHOICE
    
    await query.edit_message_text(f"📝 Генерирую тест из {num_questions} вопросов...")
    
    transcription = user_data_store[user_id]["transcription"]
    
    # Генерируем вопросы
    questions = await generate_test_questions(
        transcription["full_text"],
        num_questions
    )
    
    # Сохраняем в данные пользователя
    user_data_store[user_id]["questions"] = questions
    user_data_store[user_id]["current_question"] = 0
    user_data_store[user_id]["score"] = 0
    user_data_store[user_id]["answers"] = []
    
    # Показываем первый вопрос
    await show_question(update, context, 0)

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
    
    # Формируем текст вопроса
    question_text = f"""
📝 *Вопрос {question_index + 1} из {total}*

{q['question']}

Выбери вариант ответа:
"""
    
    options_text = "\n".join([f"{chr(65+i)}) {opt}" for i, opt in enumerate(q['options'])])
    
    await query.edit_message_text(
        question_text + options_text,
        parse_mode="Markdown",
        reply_markup=get_test_answer_keyboard(question_index, total)
    )

async def handle_test_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                             question_id: int, answer: int):
    """Обработка ответа на вопрос"""
    query = update.callback_query
    user_id = query.from_user.id
    
    questions = user_data_store[user_id]["questions"]
    q = questions[question_id]
    
    # Проверяем правильность
    is_correct = (answer == q['correct'])
    if is_correct:
        user_data_store[user_id]["score"] += 1
    
    user_data_store[user_id]["answers"].append({
        "question_id": question_id,
        "answer": answer,
        "correct": is_correct
    })
    
    # Переходим к следующему вопросу
    next_q = question_id + 1
    user_data_store[user_id]["current_question"] = next_q
    
    if next_q >= len(questions):
        await finish_test(update, context)
    else:
        await show_question(update, context, next_q)

async def skip_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question_id: int):
    """Пропускает вопрос"""
    query = update.callback_query
    user_id = query.from_user.id
    
    user_data_store[user_id]["answers"].append({
        "question_id": question_id,
        "answer": None,
        "correct": False
    })
    
    next_q = question_id + 1
    user_data_store[user_id]["current_question"] = next_q
    
    questions = user_data_store[user_id]["questions"]
    if next_q >= len(questions):
        await finish_test(update, context)
    else:
        await show_question(update, context, next_q)

async def finish_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершает тест и показывает результаты"""
    query = update.callback_query
    user_id = query.from_user.id
    
    questions = user_data_store[user_id]["questions"]
    score = user_data_store[user_id]["score"]
    total = len(questions)
    percentage = (score / total) * 100
    
    # Генерируем результат
    result_text = f"""
📊 *РЕЗУЛЬТАТЫ ТЕСТА*

✅ Правильных ответов: {score} из {total}
📈 Процент: {percentage:.1f}%

{'🎉 Отлично! Ты хорошо усвоил материал!' if percentage >= 80 else '👍 Хороший результат! Есть над чем поработать.' if percentage >= 60 else '📖 Рекомендую повторить материал.'}

---
"""
    
    # Показываем правильные ответы
    result_text += "\n*Разбор ответов:*\n"
    for i, q in enumerate(questions):
        user_answer = user_data_store[user_id]["answers"][i] if i < len(user_data_store[user_id]["answers"]) else None
        if user_answer and user_answer['answer'] is not None:
            is_correct = user_answer['correct']
            emoji = "✅" if is_correct else "❌"
            answer_text = q['options'][user_answer['answer']]
            result_text += f"{emoji} Вопрос {i+1}: {answer_text[:50]}...\n"
        else:
            result_text += f"⏭️ Вопрос {i+1}: Пропущен\n"
    
    await query.edit_message_text(
        result_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )

async def dont_be_sad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует мотивирующие фразы"""
    query = update.callback_query
    
    phrase = await generate_motivation_phrase()
    
    await query.edit_message_text(
        f"💖 *НЕ ГРУСТИ!*\n\n{phrase}\n\n✨ Ты справишься со всем, что бы ни случилось!",
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
    )

# ========== 7. ЗАПУСК БОТА ==========

def main():
    app = Application.builder().token(TOKEN).build()
    
    # Создаем ConversationHandler
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
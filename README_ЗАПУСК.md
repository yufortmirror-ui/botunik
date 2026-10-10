# Запуск бота

## Вариант 1. Просто на компьютере (файлы до 20 МБ)
1. `pip install -r requirements.txt`
2. Поставь ffmpeg (Windows: `winget install ffmpeg`, потом перезапусти терминал).
3. Заполни `.env` по образцу `.env.example` и запусти `python bot.py`.

## Вариант 2. Файлы до 2 ГБ, всё автоматически (Docker)
Нужно один раз настроить, дальше пользователь просто присылает запись любого размера.

1. Поставь Docker Desktop (на Windows он использует WSL2).
2. Зайди на https://my.telegram.org/apps, создай приложение и возьми `api_id` и `api_hash`.
3. В `.env` (рядом с `docker-compose.yml`) добавь строки:
   ```
   BOT_TOKEN=...
   GROQ_API_KEY=...
   TELEGRAM_API_ID=...
   TELEGRAM_API_HASH=...
   ```
4. Если у бота уже есть история — скопируй старый `bot.db` в папку `data/`.
5. Останови бота, если он запущен в обычном режиме, и выполни:
   ```
   docker compose up -d --build
   ```
6. Смотреть логи: `docker compose logs -f bot`. Остановить: `docker compose down`.

Компьютер (или сервер), где запущен бот, должен быть включён: иначе не придут
«вопрос дня» в 21:00 и дайджест в 21:50.

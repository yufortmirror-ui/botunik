FROM python:3.11-slim

# ffmpeg нужен, чтобы сжимать и резать длинные записи
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY fonts ./fonts

CMD ["python", "bot.py"]

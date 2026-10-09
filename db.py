import json
import os
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
DB_PATH = os.getenv("DB_PATH", "bot.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(MSK).date().isoformat()


def init() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with closing(_conn()) as c, c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(chat_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS lectures(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, transcript TEXT, quiz_json TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS activity(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, kind TEXT, day TEXT);
            CREATE TABLE IF NOT EXISTS drafts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, msg_id INTEGER, transcript TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS daily(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER, message_id INTEGER, question_json TEXT,
                expires_at TEXT, status TEXT DEFAULT 'open');
            """
        )
        # миграции для уже существующей bot.db
        _add_column(c, "users", "first_seen", "TEXT")
        _add_column(c, "lectures", "title", "TEXT")
        _add_column(c, "lectures", "notes_html", "TEXT")


def _add_column(c: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# ---------- пользователи ----------
def add_user(chat_id: int) -> None:
    with closing(_conn()) as c, c:
        c.execute("INSERT OR IGNORE INTO users(chat_id) VALUES (?)", (chat_id,))


def touch(chat_id: int) -> None:
    """Регистрирует пользователя и запоминает дату первого использования."""
    now = _now()
    with closing(_conn()) as c, c:
        c.execute("INSERT OR IGNORE INTO users(chat_id) VALUES (?)", (chat_id,))
        c.execute(
            "UPDATE users SET first_seen=COALESCE(first_seen, ?) WHERE chat_id=?",
            (now, chat_id),
        )


def users() -> list[int]:
    with closing(_conn()) as c:
        return [r["chat_id"] for r in c.execute("SELECT chat_id FROM users")]


# ---------- лекции ----------
def add_lecture(chat_id: int, transcript: str) -> int:
    with closing(_conn()) as c, c:
        cur = c.execute(
            "INSERT INTO lectures(chat_id, transcript, created_at) VALUES (?,?,?)",
            (chat_id, transcript, _now()),
        )
        return cur.lastrowid


def get_lecture(lecture_id: int):
    with closing(_conn()) as c:
        return c.execute("SELECT * FROM lectures WHERE id=?", (lecture_id,)).fetchone()


def set_notes(lecture_id: int, notes_html: str, title: str | None) -> None:
    with closing(_conn()) as c, c:
        c.execute(
            "UPDATE lectures SET notes_html=?, title=? WHERE id=?",
            (notes_html, title, lecture_id),
        )


def list_lectures(chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with closing(_conn()) as c:
        return c.execute(
            "SELECT id, created_at, title, substr(transcript, 1, 80) AS snippet, "
            "notes_html IS NOT NULL AS has_notes, quiz_json IS NOT NULL AS has_quiz "
            "FROM lectures WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()


def stats(chat_id: int) -> dict:
    with closing(_conn()) as c:
        one = lambda q, *a: c.execute(q, a).fetchone()[0]  # noqa: E731
        first_candidates = [
            x
            for x in (
                one("SELECT first_seen FROM users WHERE chat_id=?", chat_id),
                one("SELECT MIN(created_at) FROM lectures WHERE chat_id=?", chat_id),
            )
            if x
        ]
        return {
            "lectures": one("SELECT COUNT(*) FROM lectures WHERE chat_id=?", chat_id),
            "notes": one("SELECT COUNT(*) FROM activity WHERE chat_id=? AND kind='notes'", chat_id),
            "quizzes": one(
                "SELECT COUNT(*) FROM lectures WHERE chat_id=? AND quiz_json IS NOT NULL", chat_id
            ),
            "tests_done": one("SELECT COUNT(*) FROM activity WHERE chat_id=? AND kind='test'", chat_id),
            "active_days": one(
                "SELECT COUNT(DISTINCT day) FROM activity WHERE chat_id=?", chat_id
            ),
            "first": min(first_candidates) if first_candidates else None,
            "last_lecture": one("SELECT MAX(created_at) FROM lectures WHERE chat_id=?", chat_id),
        }


def set_quiz(lecture_id: int, quiz: list[dict]) -> None:
    with closing(_conn()) as c, c:
        c.execute(
            "UPDATE lectures SET quiz_json=? WHERE id=?",
            (json.dumps(quiz, ensure_ascii=False), lecture_id),
        )


def random_question(chat_id: int) -> dict | None:
    """Случайный вопрос из всех уже сгенерированных тестов пользователя."""
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT quiz_json FROM lectures WHERE chat_id=? AND quiz_json IS NOT NULL",
            (chat_id,),
        ).fetchall()
    if not rows:
        return None
    quiz = json.loads(random.choice(rows)["quiz_json"])
    return random.choice(quiz)


# ---------- черновик: несколько аудио -> одна лекция ----------
def add_draft(chat_id: int, msg_id: int, transcript: str) -> int:
    """Сохраняет расшифровку очередного аудио; возвращает, сколько частей в наборе."""
    with closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO drafts(chat_id, msg_id, transcript, created_at) VALUES (?,?,?,?)",
            (chat_id, msg_id, transcript, _now()),
        )
        return c.execute("SELECT COUNT(*) FROM drafts WHERE chat_id=?", (chat_id,)).fetchone()[0]


def draft_count(chat_id: int) -> int:
    with closing(_conn()) as c:
        return c.execute("SELECT COUNT(*) FROM drafts WHERE chat_id=?", (chat_id,)).fetchone()[0]


def pop_drafts(chat_id: int) -> list[str]:
    """Забирает все части набора (в порядке отправки аудио) и очищает набор."""
    with closing(_conn()) as c, c:
        rows = c.execute(
            "SELECT transcript FROM drafts WHERE chat_id=? ORDER BY msg_id, id", (chat_id,)
        ).fetchall()
        c.execute("DELETE FROM drafts WHERE chat_id=?", (chat_id,))
        return [r["transcript"] for r in rows]


def clear_drafts(chat_id: int) -> None:
    with closing(_conn()) as c, c:
        c.execute("DELETE FROM drafts WHERE chat_id=?", (chat_id,))


# ---------- статистика ----------
def log(chat_id: int, kind: str) -> None:
    with closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO activity(chat_id, kind, day) VALUES (?,?,?)",
            (chat_id, kind, today()),
        )


def count_today(chat_id: int, kind: str) -> int:
    with closing(_conn()) as c:
        return c.execute(
            "SELECT COUNT(*) FROM activity WHERE chat_id=? AND kind=? AND day=?",
            (chat_id, kind, today()),
        ).fetchone()[0]


# ---------- вопрос дня ----------
def create_daily(chat_id: int, question: dict, hours: int = 1) -> int:
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    with closing(_conn()) as c, c:
        cur = c.execute(
            "INSERT INTO daily(chat_id, question_json, expires_at) VALUES (?,?,?)",
            (chat_id, json.dumps(question, ensure_ascii=False), expires),
        )
        return cur.lastrowid


def set_daily_message(daily_id: int, message_id: int) -> None:
    with closing(_conn()) as c, c:
        c.execute("UPDATE daily SET message_id=? WHERE id=?", (message_id, daily_id))


def get_daily(daily_id: int):
    with closing(_conn()) as c:
        return c.execute("SELECT * FROM daily WHERE id=?", (daily_id,)).fetchone()


def close_daily(daily_id: int, status: str) -> bool:
    """Закрывает вопрос, только если он ещё открыт (защита от гонки ответ/истечение)."""
    with closing(_conn()) as c, c:
        cur = c.execute(
            "UPDATE daily SET status=? WHERE id=? AND status='open'",
            (status, daily_id),
        )
        return cur.rowcount > 0


def expired_daily() -> list[sqlite3.Row]:
    with closing(_conn()) as c:
        return c.execute(
            "SELECT * FROM daily WHERE status='open' AND message_id IS NOT NULL "
            "AND expires_at <= ?",
            (_now(),),
        ).fetchall()

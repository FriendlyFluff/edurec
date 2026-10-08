"""
database.py — Двойной адаптер: Supabase (PostgreSQL) в облаке, SQLite локально.

Логика выбора:
  - Если в st.secrets (или переменных окружения) есть SUPABASE_URL и SUPABASE_KEY
    → используется Supabase PostgreSQL (production / облако).
  - Иначе → используется локальный SQLite (разработка / отладка).

Публичный интерфейс НЕ меняется:
  init_db(), save_user(), get_user(), list_users(),
  save_event(), get_events_by_directions(), get_all_events()
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# ── Кодировка терминала ────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Определяем бэкенд ─────────────────────────────────────────────────────────

def _get_secrets() -> tuple[str, str]:
    """
    Возвращает (SUPABASE_URL, SUPABASE_KEY).
    Пробует: st.secrets → os.environ → возвращает пустые строки.
    """
    try:
        import streamlit as st
        url = st.secrets.get("SUPABASE_URL", "")
        key = st.secrets.get("SUPABASE_KEY", "")
        if url and key:
            return url, key
    except Exception:
        pass
    return os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")


SUPABASE_URL, SUPABASE_KEY = _get_secrets()
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_KEY)

# ── SQLite fallback (локальная разработка) ────────────────────────────────────

DB_PATH = Path(__file__).parent / "edu_events.db"


def _sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Supabase клиент ───────────────────────────────────────────────────────────

_supabase_client = None

def _supa():
    """Ленивая инициализация Supabase клиента."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


# ══════════════════════════════════════════════════════════════════════════════
# Инициализация
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    """
    Создаёт таблицы, если их нет.
    SQLite: через executescript.
    Supabase: таблицы создаются через дашборд Supabase один раз вручную.
              Эта функция только проверяет доступность соединения.
    """
    if USE_SUPABASE:
        try:
            _supa().table("events").select("event_id").limit(1).execute()
            print("[DB] Supabase: подключение успешно.")
        except Exception as e:
            print(f"[DB] Supabase: ошибка подключения — {e}")
            print("[DB] Проверьте SUPABASE_URL и SUPABASE_KEY в секретах.")
    else:
        with _sqlite_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    role                TEXT    NOT NULL,
                    base_points         INTEGER NOT NULL DEFAULT 0,
                    preferred_directions TEXT   NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    title         TEXT    NOT NULL,
                    url           TEXT    UNIQUE,
                    event_date    TEXT,
                    direction     TEXT,
                    profit_points INTEGER NOT NULL DEFAULT 5,
                    description   TEXT,
                    created_at    TEXT    DEFAULT (datetime('now'))
                );
            """)
        print(f"[DB] SQLite: {DB_PATH}")


# ══════════════════════════════════════════════════════════════════════════════
# Users
# ══════════════════════════════════════════════════════════════════════════════

def save_user(role: str, base_points: int, preferred_directions: str,
              user_id: int | None = None) -> int:
    if USE_SUPABASE:
        supa = _supa()
        if user_id:
            supa.table("users").update({
                "role": role,
                "base_points": base_points,
                "preferred_directions": preferred_directions,
            }).eq("user_id", user_id).execute()
            return user_id
        res = supa.table("users").insert({
            "role": role,
            "base_points": base_points,
            "preferred_directions": preferred_directions,
        }).execute()
        return res.data[0]["user_id"]
    else:
        with _sqlite_conn() as conn:
            if user_id:
                conn.execute(
                    "UPDATE users SET role=?, base_points=?, preferred_directions=? WHERE user_id=?",
                    (role, base_points, preferred_directions, user_id),
                )
                return user_id
            cur = conn.execute(
                "INSERT INTO users (role, base_points, preferred_directions) VALUES (?,?,?)",
                (role, base_points, preferred_directions),
            )
            return cur.lastrowid


def get_user(user_id: int) -> dict | None:
    if USE_SUPABASE:
        res = _supa().table("users").select("*").eq("user_id", user_id).execute()
        return res.data[0] if res.data else None
    else:
        with _sqlite_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    if USE_SUPABASE:
        res = _supa().table("users").select("*").execute()
        return res.data or []
    else:
        with _sqlite_conn() as conn:
            rows = conn.execute("SELECT * FROM users").fetchall()
        return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# Events
# ══════════════════════════════════════════════════════════════════════════════

def save_event(title: str, url: str, event_date: str,
               direction: str, profit_points: int, description: str) -> int:
    """Сохраняет мероприятие. Пропускает дубликаты по URL."""
    if USE_SUPABASE:
        supa = _supa()
        # Проверяем дубликат
        existing = supa.table("events").select("event_id").eq("url", url).execute()
        if existing.data:
            return existing.data[0]["event_id"]
        res = supa.table("events").insert({
            "title": title,
            "url": url,
            "event_date": event_date or None,
            "direction": direction,
            "profit_points": profit_points,
            "description": description,
        }).execute()
        return res.data[0]["event_id"]
    else:
        with _sqlite_conn() as conn:
            existing = conn.execute(
                "SELECT event_id FROM events WHERE url=?", (url,)
            ).fetchone()
            if existing:
                return existing["event_id"]
            cur = conn.execute(
                "INSERT INTO events (title, url, event_date, direction, profit_points, description) "
                "VALUES (?,?,?,?,?,?)",
                (title, url, event_date, direction, profit_points, description),
            )
            return cur.lastrowid


def get_events_by_directions(directions: list[str], today: str) -> list[dict]:
    """Возвращает актуальные мероприятия по направлениям."""
    if not directions:
        return []

    if USE_SUPABASE:
        supa = _supa()
        res = (
            supa.table("events")
            .select("*")
            .in_("direction", directions)
            .or_(f"event_date.is.null,event_date.eq.,event_date.gte.{today}")
            .order("profit_points", desc=True)
            .execute()
        )
        return res.data or []
    else:
        placeholders = ",".join("?" * len(directions))
        query = f"""
            SELECT * FROM events
            WHERE direction IN ({placeholders})
              AND (event_date IS NULL OR event_date = '' OR event_date >= ?)
            ORDER BY profit_points DESC
        """
        with _sqlite_conn() as conn:
            rows = conn.execute(query, (*directions, today)).fetchall()
        return [dict(r) for r in rows]


def get_all_events() -> list[dict]:
    if USE_SUPABASE:
        res = _supa().table("events").select("*").order("profit_points", desc=True).execute()
        return res.data or []
    else:
        with _sqlite_conn() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY profit_points DESC").fetchall()
        return [dict(r) for r in rows]


def get_last_updated() -> str | None:
    """
    Возвращает дату/время последней записи в таблице events.
    Используется в UI для отображения таймера свежести базы.
    """
    if USE_SUPABASE:
        try:
            res = (
                _supa().table("events")
                .select("created_at")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0]["created_at"] if res.data else None
        except Exception:
            return None
    else:
        try:
            with _sqlite_conn() as conn:
                row = conn.execute(
                    "SELECT created_at FROM events ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            return row["created_at"] if row else None
        except Exception:
            return None


# ── Точка входа (локальная отладка) ───────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    uid = save_user("старшеклассник", 120, "спорт,наука")
    print(f"[DB] Тестовый пользователь user_id={uid}")
    eid = save_event(
        title="Олимпиада по физике",
        url="https://example.com/phys",
        event_date="2026-11-01",
        direction="наука",
        profit_points=20,
        description="Региональный этап Всероссийской олимпиады школьников по физике.",
    )
    print(f"[DB] Тестовое мероприятие event_id={eid}")
    print(f"[DB] Последнее обновление: {get_last_updated()}")
    print(f"[DB] Всего событий: {len(get_all_events())}")

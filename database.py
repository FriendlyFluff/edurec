"""
database.py — Двойной адаптер: Supabase (PostgreSQL) в облаке, SQLite локально.

Логика выбора:
  - Если в st.secrets (или переменных окружения) есть SUPABASE_URL и SUPABASE_KEY
    → используется Supabase PostgreSQL (production / облако).
  - Иначе → используется локальный SQLite (разработка / отладка).

Публичный интерфейс:
  init_db(),
  save_event(), get_events_by_directions(), get_all_events(), get_last_updated(),
  get_active_sources(), save_source(), mark_source_dead(), update_source_audited()
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
    Supabase: таблицы создаются через дашборд один раз вручную (см. docs/supabase_schema.sql).
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
                CREATE TABLE IF NOT EXISTS events (
                    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    title         TEXT    NOT NULL,
                    url           TEXT    UNIQUE,
                    event_date    TEXT,
                    direction     TEXT,
                    target_audience TEXT,
                    profit_points INTEGER NOT NULL DEFAULT 5,
                    description   TEXT,
                    created_at    TEXT    DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS trusted_sources (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    url          TEXT    NOT NULL UNIQUE,
                    status       TEXT    NOT NULL DEFAULT 'active',
                    last_audited TEXT
                );
            """)
        print(f"[DB] SQLite: {DB_PATH}")


# ══════════════════════════════════════════════════════════════════════════════
# Trusted Sources
# ══════════════════════════════════════════════════════════════════════════════

def get_active_sources() -> list[dict]:
    """Возвращает все активные Trusted Sources."""
    if USE_SUPABASE:
        res = _supa().table("trusted_sources").select("*").eq("status", "active").execute()
        return res.data or []
    else:
        with _sqlite_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trusted_sources WHERE status='active'"
            ).fetchall()
        return [dict(r) for r in rows]


def save_source(url: str) -> int | None:
    """Сохраняет новый Trusted Source со статусом 'active'. Пропускает дубликаты."""
    if USE_SUPABASE:
        supa = _supa()
        existing = supa.table("trusted_sources").select("id").eq("url", url).execute()
        if existing.data:
            return existing.data[0]["id"]
        res = supa.table("trusted_sources").insert({"url": url, "status": "active"}).execute()
        return res.data[0]["id"] if res.data else None
    else:
        with _sqlite_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM trusted_sources WHERE url=?", (url,)
            ).fetchone()
            if existing:
                return existing["id"]
            cur = conn.execute(
                "INSERT INTO trusted_sources (url, status) VALUES (?, 'active')", (url,)
            )
            return cur.lastrowid


def mark_source_dead(url: str) -> None:
    """Помечает Trusted Source как мертвый (dead). Вызывается Аудитором."""
    if USE_SUPABASE:
        _supa().table("trusted_sources").update({"status": "dead"}).eq("url", url).execute()
    else:
        with _sqlite_conn() as conn:
            conn.execute("UPDATE trusted_sources SET status='dead' WHERE url=?", (url,))


def update_source_audited(url: str) -> None:
    """Обновляет timestamp последней проверки Trusted Source."""
    if USE_SUPABASE:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        _supa().table("trusted_sources").update({"last_audited": ts}).eq("url", url).execute()
    else:
        with _sqlite_conn() as conn:
            conn.execute(
                "UPDATE trusted_sources SET last_audited=datetime('now') WHERE url=?", (url,)
            )




# ══════════════════════════════════════════════════════════════════════════════
# Events
# ══════════════════════════════════════════════════════════════════════════════

def save_event(title: str, url: str, event_date: str,
               direction: str, profit_points: int, description: str,
               target_audience: str = "") -> int:
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
            "target_audience": target_audience or None,
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
                "INSERT INTO events (title, url, event_date, direction, target_audience, profit_points, description) "
                "VALUES (?,?,?,?,?,?,?)",
                (title, url, event_date, direction, target_audience, profit_points, description),
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

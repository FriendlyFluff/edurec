-- ============================================================
-- Запустить один раз в Supabase Dashboard → SQL Editor
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    user_id              SERIAL PRIMARY KEY,
    role                 TEXT    NOT NULL,
    base_points          INTEGER NOT NULL DEFAULT 0,
    preferred_directions TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    event_id      SERIAL PRIMARY KEY,
    title         TEXT    NOT NULL,
    url           TEXT    UNIQUE,
    event_date    TEXT,
    direction     TEXT,
    profit_points INTEGER NOT NULL DEFAULT 5,
    description   TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Индекс для быстрого поиска по направлению
CREATE INDEX IF NOT EXISTS idx_events_direction ON events(direction);
CREATE INDEX IF NOT EXISTS idx_events_date      ON events(event_date);

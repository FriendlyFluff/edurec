-- ============================================================
-- EduRec Supabase Schema v2 (SPEC-002: Autonomous Harvester)
-- Запустить в Supabase Dashboard → SQL Editor
-- ============================================================

-- Таблица мероприятий (Events / Canonical Events)
CREATE TABLE IF NOT EXISTS events (
    event_id        SERIAL PRIMARY KEY,
    title           TEXT    NOT NULL,
    url             TEXT    UNIQUE,
    event_date      TEXT,
    direction       TEXT,
    target_audience TEXT,                        -- Для дедупликации: "7-8 класс", "учителя" и т.д.
    profit_points   INTEGER NOT NULL DEFAULT 5,
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Таблица надежных источников (Trusted Sources)
CREATE TABLE IF NOT EXISTS trusted_sources (
    id           SERIAL PRIMARY KEY,
    url          TEXT    NOT NULL UNIQUE,
    status       TEXT    NOT NULL DEFAULT 'active', -- 'active' | 'dead'
    last_audited TIMESTAMPTZ
);

-- Индексы для производительности
CREATE INDEX IF NOT EXISTS idx_events_direction     ON events(direction);
CREATE INDEX IF NOT EXISTS idx_events_date          ON events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_audience      ON events(target_audience);
CREATE INDEX IF NOT EXISTS idx_sources_status       ON trusted_sources(status);

-- Добавить колонку target_audience к существующей таблице events (если уже создана ранее)
-- Раскомментируйте если таблица events уже существует:
-- ALTER TABLE events ADD COLUMN IF NOT EXISTS target_audience TEXT;

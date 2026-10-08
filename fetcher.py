"""
fetcher.py — Autonomous Harvester (Scout + Harvester).

Архитектура (SPEC-002 / ADR-0002):
  Scout   — ИИ-Разведчик. Два режима:
              discover_sources() — находит новые Trusted Sources через Tavily API,
                                   валидирует через Groq LLM, сохраняет в БД.
              audit_sources()    — проверяет жизнеспособность существующих источников.
  Harvester — Сборщик. run_harvester() читает активные Trusted Sources,
              тащит текст через Jina Reader API, извлекает Events через Groq LLM,
              сохраняет с семантической дедупликацией (по title + date + audience).
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import urljoin

import requests
from openai import OpenAI

from database import (
    get_active_sources,
    save_source,
    mark_source_dead,
    update_source_audited,
    save_event,
    get_all_events,
)

# ── Кодировка терминала ────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Константы ──────────────────────────────────────────────────────────────────
GROQ_MODEL = "llama3-8b-8192"
JINA_BASE = "https://r.jina.ai/"

# Поисковые запросы для Scout-Discovery
SCOUT_QUERIES = [
    "олимпиады для школьников государственные 2026 site:.ru",
    "форумы конференции для педагогов учителей 2026",
    "конкурсы образовательные для учащихся государственные инициативы",
    "всероссийские олимпиады школьников официальные сайты",
    "образовательные мероприятия для школ министерство просвещения",
]

# ── Настройка клиентов ─────────────────────────────────────────────────────────

def _get_secret(key: str) -> str:
    try:
        import streamlit as st
        val = st.secrets.get(key, "")
        if val:
            return val
    except Exception:
        pass
    return os.getenv(key, "")


def _groq_client() -> OpenAI:
    api_key = _get_secret("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY не найден в секретах!")
    return OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")


def _tavily_key() -> str:
    key = _get_secret("TAVILY_API_KEY")
    if not key:
        raise ValueError("TAVILY_API_KEY не найден в секретах!")
    return key


# ══════════════════════════════════════════════════════════════════════════════
# SCOUT — Mode 1: Discovery (Поиск новых источников)
# ══════════════════════════════════════════════════════════════════════════════

def _tavily_search(query: str, max_results: int = 5) -> list[dict]:
    """Поиск через Tavily API. Возвращает список {'url': ..., 'content': ...}."""
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": _tavily_key(),
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_domains": [],
                "exclude_domains": [],
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        print(f"[Scout] Ошибка Tavily: {e}")
        return []


_SCOUT_VALIDATION_PROMPT = """Ты — строгий ИИ-аналитик образовательных ресурсов.
Тебе дан URL и краткое описание сайта. Определи, является ли этот сайт надежным источником государственных или академических образовательных мероприятий (олимпиад, конкурсов, форумов) для школьников или педагогов.

СТРОГИЕ ПРАВИЛА ОТКЛОНЕНИЯ (REJECT):
- Коммерческие курсы, платные подписки, EdTech-стартапы.
- Новостные агрегаторы (vc.ru, habr.com, rbc.ru) — они пишут О мероприятиях, но не ведут их реестр.
- Одиночные статьи о конкретном мероприятии (не портал-агрегатор).
- Сайты без явной образовательной специализации.

ПРИНИМАТЬ (VALID_PORTAL):
- Официальные государственные порталы (edu.ru, olimpiada.ru, минпросвещения.рф и подобные).
- Агрегаторы олимпиад и конкурсов, которые регулярно обновляют базу событий.
- Региональные/муниципальные образовательные порталы с разделом "мероприятия" или "олимпиады".

Верни строго JSON:
{"verdict": "VALID_PORTAL" или "REJECT", "reason": "одно предложение почему"}
Ничего кроме JSON."""


def _validate_source_via_llm(url: str, snippet: str, client: OpenAI) -> bool:
    """Просит Groq решить, является ли URL надежным образовательным порталом."""
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _SCOUT_VALIDATION_PROMPT},
                {"role": "user", "content": f"URL: {url}\nОписание: {snippet[:500]}"},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        verdict = data.get("verdict", "REJECT")
        reason = data.get("reason", "")
        print(f"  [Scout] {verdict}: {url} — {reason}")
        return verdict == "VALID_PORTAL"
    except Exception as e:
        print(f"  [Scout] Ошибка валидации LLM: {e}")
        return False


def discover_sources() -> int:
    """
    Scout Mode 1: Поиск новых Trusted Sources.
    Возвращает количество добавленных источников.
    """
    print("=== РАЗВЕДЧИК: ПОИСК НОВЫХ ИСТОЧНИКОВ ===")
    client = _groq_client()
    added = 0

    for query in SCOUT_QUERIES:
        print(f"\n[Scout] Запрос: {query}")
        results = _tavily_search(query, max_results=5)

        for r in results:
            url = r.get("url", "")
            snippet = r.get("content", "") or r.get("snippet", "")
            if not url:
                continue

            # Оставляем только корневой домен (не отдельные страницы)
            from urllib.parse import urlparse
            parsed = urlparse(url)
            root_url = f"{parsed.scheme}://{parsed.netloc}"

            if _validate_source_via_llm(root_url, snippet, client):
                source_id = save_source(root_url)
                if source_id:
                    print(f"  [+] Добавлен источник: {root_url} (ID: {source_id})")
                    added += 1

        time.sleep(1)  # Уважаем rate limits

    if added == 0:
        print("[Scout] Ничего не найдено, добавляю базовый агрегатор olimpiada.ru как fallback.")
        sid = save_source("https://olimpiada.ru")
        if sid:
            added += 1

    print(f"\n=== РАЗВЕДКА ЗАВЕРШЕНА. Добавлено источников: {added} ===")
    return added


# ══════════════════════════════════════════════════════════════════════════════
# SCOUT — Mode 2: Audit (Проверка существующих источников)
# ══════════════════════════════════════════════════════════════════════════════

_AUDIT_PROMPT = """Ты — аудитор образовательных ресурсов.
Тебе дан текст главной страницы сайта (сырой markdown). Определи статус сайта.

ПРАВИЛА:
- "ALIVE": Сайт работает и по-прежнему посвящен образовательным мероприятиям для школ/педагогов. Отсутствие свежих мероприятий (не сезон) НЕ является признаком смерти.
- "DEAD": Сайт технически мертв (ошибка загрузки), или его тематика ПОЛНОСТЬЮ сменилась (теперь это магазин, казино, новостной сайт без образовательного контента).

Верни строго JSON:
{"status": "ALIVE" или "DEAD", "reason": "одно предложение"}
Ничего кроме JSON."""


def _fetch_via_jina(url: str) -> str | None:
    """Получает текстовое содержимое страницы через Jina Reader API."""
    try:
        resp = requests.get(
            f"{JINA_BASE}{url}",
            headers={"Accept": "text/plain"},
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.text[:8000]  # Ограничиваем объем для LLM
        print(f"  [Jina] HTTP {resp.status_code} для {url}")
        return None
    except Exception as e:
        print(f"  [Jina] Ошибка: {e}")
        return None


def _audit_source_via_llm(url: str, content: str, client: OpenAI) -> str:
    """Просит Groq оценить живость и тематику источника. Возвращает 'ALIVE' или 'DEAD'."""
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _AUDIT_PROMPT},
                {"role": "user", "content": f"URL: {url}\n\nСодержимое страницы:\n{content}"},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        status = data.get("status", "ALIVE")
        reason = data.get("reason", "")
        print(f"  [Audit] {status}: {url} — {reason}")
        return status
    except Exception as e:
        print(f"  [Audit] Ошибка LLM: {e}")
        return "ALIVE"  # При сомнении — не убиваем источник


def audit_sources() -> dict:
    """
    Scout Mode 2: Аудит существующих Trusted Sources.
    Возвращает {'alive': N, 'dead': N}.
    """
    print("=== РАЗВЕДЧИК: АУДИТ СУЩЕСТВУЮЩИХ ИСТОЧНИКОВ ===")
    client = _groq_client()
    sources = get_active_sources()

    if not sources:
        print("[Audit] Нет активных источников для проверки.")
        return {"alive": 0, "dead": 0}

    alive_count = 0
    dead_count = 0

    for src in sources:
        url = src["url"]
        print(f"\n[Audit] Проверяю: {url}")

        content = _fetch_via_jina(url)
        if content is None:
            # Jina не смогла получить страницу — возможно, сайт мертв
            print(f"  [Audit] Не удалось загрузить. Помечаю как DEAD.")
            mark_source_dead(url)
            dead_count += 1
            continue

        status = _audit_source_via_llm(url, content, client)
        update_source_audited(url)

        if status == "DEAD":
            mark_source_dead(url)
            dead_count += 1
        else:
            alive_count += 1

        time.sleep(1)

    print(f"\n=== АУДИТ ЗАВЕРШЕН. Живых: {alive_count}, Мертвых: {dead_count} ===")
    return {"alive": alive_count, "dead": dead_count}


# ══════════════════════════════════════════════════════════════════════════════
# HARVESTER — Сборщик Events из Trusted Sources
# ══════════════════════════════════════════════════════════════════════════════

_HARVEST_PROMPT = """Ты — строгий ИИ-экстрактор образовательных мероприятий.
Тебе дан текст страницы сайта (markdown). Извлеки ВСЕ образовательные мероприятия.

СТРОГИЕ ПРАВИЛА ФИЛЬТРАЦИИ:
1. Мероприятие ДОЛЖНО быть государственным или академическим (не коммерческим).
2. Мероприятие ДОЛЖНО иметь временные рамки (дата, период типа "октябрь-ноябрь", или дедлайн).
3. Отсекай: платные курсы, вебинары без ценности для школы, рекламные материалы.
4. target_audience — аудитория: например "7-8 класс", "учителя", "школьники 5-11 класс", "педагоги". ВАЖНО для дедупликации!

Верни JSON-массив (может быть пустым []):
[
  {
    "title": "Точное название мероприятия",
    "event_date": "Дата или период (например '15 ноября 2026' или 'октябрь-декабрь 2026')",
    "direction": "наука/it/искусство/спорт/педагогика/другое",
    "target_audience": "Для кого предназначено",
    "description": "1-2 предложения о мероприятии и его пользе для школы"
  }
]
Ничего кроме JSON-массива."""


def _extract_events_via_llm(url: str, content: str, client: OpenAI) -> list[dict]:
    """Извлекает Events из текста страницы через Groq LLM."""
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _HARVEST_PROMPT},
                {"role": "user", "content": f"URL источника: {url}\n\nТекст страницы:\n{content}"},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()
        # Вырезаем JSON-массив из ответа
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        return json.loads(raw[start:end])
    except Exception as e:
        print(f"  [Harvester] Ошибка LLM: {e}")
        return []


def _is_duplicate(title: str, event_date: str, target_audience: str) -> bool:
    """
    Семантическая дедупликация по составному ключу:
    название + дата + целевая аудитория.
    Два мероприятия с одним названием, но для разных классов — НЕ дубликаты.
    """
    existing = get_all_events()
    title_lower = title.lower().strip()
    date_lower = (event_date or "").lower().strip()
    audience_lower = (target_audience or "").lower().strip()

    for ev in existing:
        same_title = title_lower in ev.get("title", "").lower() or \
                     ev.get("title", "").lower() in title_lower
        same_date = date_lower == (ev.get("event_date") or "").lower().strip()
        same_audience = audience_lower == (ev.get("target_audience") or "").lower().strip()

        if same_title and same_date and same_audience:
            return True
    return False


def run_harvester() -> int:
    """
    Harvester: сбор Events из активных Trusted Sources.
    Возвращает количество добавленных мероприятий.
    """
    print("=== СБОРЩИК: ИЗВЛЕЧЕНИЕ МЕРОПРИЯТИЙ ===")
    client = _groq_client()
    sources = get_active_sources()

    if not sources:
        print("[Harvester] Нет активных источников. Запустите сначала Разведчика.")
        return 0

    print(f"[Harvester] Активных источников: {len(sources)}")
    added_count = 0

    for src in sources:
        url = src["url"]
        print(f"\n[Harvester] Читаю: {url}")

        content = _fetch_via_jina(url)
        if not content:
            print(f"  [Harvester] Не удалось получить контент. Пропускаю.")
            continue

        events = _extract_events_via_llm(url, content, client)
        print(f"  [Harvester] Найдено мероприятий на странице: {len(events)}")

        for ev in events:
            title = ev.get("title", "").strip()
            event_date = ev.get("event_date", "")
            target_audience = ev.get("target_audience", "")

            if not title:
                continue

            # Семантическая дедупликация
            if _is_duplicate(title, event_date, target_audience):
                print(f"  [~] Дубликат, пропускаю: {title[:60]}")
                continue

            eid = save_event(
                title=title,
                url=url,
                event_date=event_date,
                direction=ev.get("direction", "другое"),
                profit_points=15,
                description=ev.get("description", ""),
                target_audience=target_audience,
            )
            print(f"  [+] Добавлено: {title[:60]} (ID: {eid})")
            added_count += 1

        time.sleep(1)

    print(f"\n=== СБОРЩИК ЗАВЕРШЁН. Добавлено мероприятий: {added_count} ===")
    return added_count


# Обратная совместимость — старый app.py вызывал run_fetcher()
def run_fetcher() -> int:
    """Алиас для run_harvester() — для совместимости с предыдущей версией app.py."""
    return run_harvester()


if __name__ == "__main__":
    print("Запуск в тестовом режиме. Используйте discover_sources() или run_harvester().")
    discover_sources()

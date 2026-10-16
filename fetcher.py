"""
fetcher.py — Autonomous Smart Harvester (SPEC-003 / ADR-0003).

Архитектура:
  Golden Seeds  — жестко зашитый список приоритетных государственных источников,
                  которые проверяются при каждом запуске обязательно.
  Smart Crawl   — перед парсингом контента ИИ ищет ссылку "Мероприятия/Новости"
                  на главной странице и делает один переход вглубь (глубина = 1).
  Harvester     — читает страницу через Jina Reader, извлекает Events через Groq
                  LLaMA-3-70B. Мероприятия без даты НЕ отбрасываются — они
                  сохраняются с флагом is_incomplete=True.
  Таксономия    — ФОП ДО: 7 категорий для советников по воспитанию.
  Лог-канал     — каждая функция принимает опциональный logger: Callable[[str], None],
                  чтобы UI мог выводить сообщения в «живой терминал» в реальном времени.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Callable
from urllib.parse import urljoin, urlparse

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
GROQ_MODEL_FAST = "llama3-8b-8192"     # Scout-валидация, Аудит (не требует глубокого анализа)
GROQ_MODEL_SMART = "llama3-70b-8192"   # Harvester (главный экстрактор — нужна точность)
JINA_BASE = "https://r.jina.ai/"

# Браузерные заголовки для прямых HTTP-запросов к гос. ресурсам.
# Без них многие .gov.ru и edu.ru возвращают 403 или пустую страницу.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Golden Seeds ───────────────────────────────────────────────────────────────
# Приоритетные государственные источники.
# Harvester проверяет их при каждом запуске ОБЯЗАТЕЛЬНО, независимо от БД.
GOLDEN_SEEDS: list[str] = [
    "https://будьвдвижении.рф",           # РДДМ «Движение Первых»
    "https://orlyatarussia.ru",            # «Орлята России»
    "https://mcvp.ru",                     # Международный центр воспитательных программ
    "https://rsv.ru",                      # Россия — страна возможностей
    "https://edu.gov.ru",                  # Министерство просвещения РФ
    "https://olimpiada.ru",                # Агрегатор олимпиад
    "https://bolshayaperemena.online",     # «Большая Перемена»
    "https://добровольцыроссии.рф",        # Волонтерство
]

# Ключевые слова для Smart Crawl — поиск раздела с мероприятиями на главной странице
CRAWL_KEYWORDS = [
    "мероприятия", "события", "анонсы", "конкурсы", "олимпиады",
    "новости", "calendar", "events", "афиша", "акции",
]

# ── Таксономия ФОП ДО ──────────────────────────────────────────────────────────
TAXONOMY = [
    "Патриотическое воспитание",
    "Добровольчество (волонтерство)",
    "Профориентация",
    "Проекты РДДМ (Движение Первых / Орлята)",
    "Экология и Краеведение",
    "Методическая поддержка (для педагогов)",
    "Другое",
]

# Поисковые запросы для Scout-Discovery (дополнение к Golden Seeds)
SCOUT_QUERIES = [
    "государственные конкурсы мероприятия для школьников 2026 воспитание",
    "форумы конференции для педагогов советников по воспитанию 2026",
    "всероссийские олимпиады патриотические мероприятия для школьников 2026",
]

# ── Утилиты ────────────────────────────────────────────────────────────────────

def _noop(msg: str) -> None:
    """Заглушка для logger, если вызов идет не из UI."""
    print(msg)


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


# ── Jina Reader + OpenGraph fallback ──────────────────────────────────────────

def _fetch_via_jina(url: str, log: Callable[[str], None] = _noop) -> str | None:
    """Получает текстовое содержимое страницы через Jina Reader API.

    Jina обходит большинство anti-bot защит и JS-рендеринг.
    """
    try:
        resp = requests.get(
            f"{JINA_BASE}{url}",
            headers={"Accept": "text/plain"},
            timeout=25,
        )
        if resp.status_code == 200 and len(resp.text.strip()) > 100:
            return resp.text[:10000]
        log(f"  ⚠️ Jina вернула пустой ответ для {url} (код {resp.status_code}). Пробую OpenGraph fallback...")
        return _fetch_opengraph_fallback(url, log)
    except Exception as e:
        log(f"  ⚠️ Jina ошибка для {url}: {e}. Пробую OpenGraph fallback...")
        return _fetch_opengraph_fallback(url, log)


def _fetch_opengraph_fallback(url: str, log: Callable[[str], None] = _noop) -> str | None:
    """Резервный метод: читает og:title и og:description из <head> напрямую.

    Государственные сайты всегда генерируют эти теги серверно для шеринга
    в соцсетях — они есть даже на SPA (Next.js/Vue).
    """
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        html = resp.text
        title_m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html)
        desc_m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', html)
        title = title_m.group(1) if title_m else ""
        desc = desc_m.group(1) if desc_m else ""
        if title or desc:
            result = f"Заголовок страницы: {title}\nОписание: {desc}"
            log(f"  ✅ OpenGraph fallback успешен для {url}")
            return result
    except Exception as e:
        log(f"  ❌ OpenGraph fallback тоже не удался для {url}: {e}")
    return None


# ── Smart Crawl ────────────────────────────────────────────────────────────────

def _smart_crawl(base_url: str, main_content: str, log: Callable[[str], None] = _noop) -> str:
    """Ищет в тексте главной страницы ссылку на раздел мероприятий.

    Делает ровно один переход вглубь (Smart Crawl, глубина = 1).
    Возвращает текст целевой страницы, либо исходный main_content.
    """
    # Ищем URL в тексте Jina (он обычно приводит ссылки в формате [текст](url))
    link_pattern = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')
    links = link_pattern.findall(main_content)

    for link_text, link_url in links:
        if any(kw in link_text.lower() for kw in CRAWL_KEYWORDS):
            # Убеждаемся, что ссылка с того же домена или относительная
            parsed_base = urlparse(base_url)
            parsed_link = urlparse(link_url)
            if parsed_link.netloc and parsed_link.netloc != parsed_base.netloc:
                continue  # Не уходим на другой домен

            log(f"  🔍 Smart Crawl: нашел раздел «{link_text}» → {link_url}")
            time.sleep(1.5)  # Вежливая пауза перед вторым запросом
            deep_content = _fetch_via_jina(link_url, log)
            if deep_content and len(deep_content) > len(main_content) * 0.3:
                log(f"  ✅ Smart Crawl успешен. Получено {len(deep_content)} символов.")
                return deep_content
            log(f"  ⚠️ Smart Crawl вернул мало контента, использую главную страницу.")
            break

    return main_content


# ══════════════════════════════════════════════════════════════════════════════
# SCOUT — Mode 1: Discovery (Поиск новых источников через Tavily)
# ══════════════════════════════════════════════════════════════════════════════

_SCOUT_VALIDATION_PROMPT = """Ты — строгий ИИ-аналитик образовательных ресурсов для советников по воспитательной работе в школах РФ.
Тебе дан URL и краткое описание сайта. Определи, является ли этот сайт надежным источником государственных образовательных мероприятий.

ПРИНИМАТЬ (VALID_PORTAL):
- Официальные государственные порталы и агрегаторы мероприятий (Движение Первых, Орлята, РСВ, Минпросвещения).
- Региональные/муниципальные образовательные порталы с разделом мероприятий или олимпиад.

СТРОГИЕ ПРАВИЛА ОТКЛОНЕНИЯ (REJECT):
- Коммерческие курсы, платные подписки, EdTech-стартапы.
- Новостные агрегаторы (vc.ru, habr.com, rbc.ru).
- Одиночные статьи о конкретном мероприятии.

Верни строго JSON:
{"verdict": "VALID_PORTAL" или "REJECT", "reason": "одно предложение почему"}
Ничего кроме JSON."""


def _tavily_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": _tavily_key(),
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        print(f"[Scout] Ошибка Tavily: {e}")
        return []


def _validate_source_via_llm(url: str, snippet: str, client: OpenAI,
                               log: Callable[[str], None] = _noop) -> bool:
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL_FAST,
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
        log(f"  [{verdict}] {url} — {reason}")
        return verdict == "VALID_PORTAL"
    except Exception as e:
        log(f"  ⚠️ Ошибка LLM-валидации: {e}")
        return False


def discover_sources(log: Callable[[str], None] = _noop) -> int:
    """Scout Mode 1: Поиск новых Trusted Sources через Tavily.

    Возвращает количество добавленных источников.
    """
    log("=== 🔍 РАЗВЕДЧИК: ПОИСК НОВЫХ ИСТОЧНИКОВ ===")
    client = _groq_client()
    added = 0

    for query in SCOUT_QUERIES:
        log(f"\n[Scout] Запрос: {query}")
        results = _tavily_search(query, max_results=5)

        for r in results:
            url = r.get("url", "")
            snippet = r.get("content", "") or r.get("snippet", "")
            if not url:
                continue
            parsed = urlparse(url)
            root_url = f"{parsed.scheme}://{parsed.netloc}"
            if _validate_source_via_llm(root_url, snippet, client, log):
                source_id = save_source(root_url)
                if source_id:
                    log(f"  [+] Добавлен источник: {root_url} (ID: {source_id})")
                    added += 1

        time.sleep(1.5)

    log(f"\n=== РАЗВЕДКА ЗАВЕРШЕНА. Добавлено источников: {added} ===")
    return added


# ══════════════════════════════════════════════════════════════════════════════
# SCOUT — Mode 2: Audit (Проверка существующих источников)
# ══════════════════════════════════════════════════════════════════════════════

_AUDIT_PROMPT = """Ты — аудитор образовательных ресурсов.
Тебе дан текст главной страницы сайта. Определи статус сайта.

ПРАВИЛА:
- "ALIVE": Сайт работает и посвящен образовательным мероприятиям для школ/педагогов. Отсутствие свежих мероприятий (не сезон) НЕ является признаком смерти.
- "DEAD": Сайт технически мертв, или тематика ПОЛНОСТЬЮ сменилась (магазин, казино, новости без образования).

Верни строго JSON:
{"status": "ALIVE" или "DEAD", "reason": "одно предложение"}
Ничего кроме JSON."""


def _audit_source_via_llm(url: str, content: str, client: OpenAI,
                            log: Callable[[str], None] = _noop) -> str:
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL_FAST,
            messages=[
                {"role": "system", "content": _AUDIT_PROMPT},
                {"role": "user", "content": f"URL: {url}\n\nСодержимое:\n{content[:3000]}"},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        status = data.get("status", "ALIVE")
        reason = data.get("reason", "")
        log(f"  [{status}] {url} — {reason}")
        return status
    except Exception as e:
        log(f"  ⚠️ Ошибка LLM-аудита: {e}")
        return "ALIVE"


def audit_sources(log: Callable[[str], None] = _noop) -> dict:
    """Scout Mode 2: Аудит существующих Trusted Sources.

    Возвращает {'alive': N, 'dead': N}.
    """
    log("=== 🩺 РАЗВЕДЧИК: АУДИТ ИСТОЧНИКОВ ===")
    client = _groq_client()
    sources = get_active_sources()

    if not sources:
        log("[Audit] Нет активных источников.")
        return {"alive": 0, "dead": 0}

    alive_count, dead_count = 0, 0
    for src in sources:
        url = src["url"]
        log(f"\n[Audit] Проверяю: {url}")
        content = _fetch_via_jina(url, log)
        if content is None:
            log(f"  ❌ Не удалось загрузить. Помечаю DEAD.")
            mark_source_dead(url)
            dead_count += 1
            continue

        status = _audit_source_via_llm(url, content, client, log)
        update_source_audited(url)
        if status == "DEAD":
            mark_source_dead(url)
            dead_count += 1
        else:
            alive_count += 1
        time.sleep(1.5)

    log(f"\n=== АУДИТ ЗАВЕРШЕН. Живых: {alive_count}, Мертвых: {dead_count} ===")
    return {"alive": alive_count, "dead": dead_count}


# ══════════════════════════════════════════════════════════════════════════════
# HARVESTER — Умный Сборщик Events из Golden Seeds + Trusted Sources
# ══════════════════════════════════════════════════════════════════════════════

_HARVEST_PROMPT = f"""Ты — ИИ-экстрактор образовательных мероприятий для советников по воспитательной работе в школах РФ.
Тебе дан текст страницы сайта (markdown). Извлеки ВСЕ государственные образовательные мероприятия.

ПРАВИЛА ФИЛЬТРАЦИИ:
1. Мероприятие должно быть государственным, патриотическим, добровольческим или официальным (не коммерческим).
2. Если дата не указана явно — НЕ ОТБРАСЫВАЙ мероприятие. Укажи event_date как null.
3. Если целевая аудитория не указана — укажи target_audience как null. Мероприятие всё равно сохраняй.
4. Отсекай только: платные коммерческие курсы, рекламу.
5. Если видишь сомнительное мероприятие — добавь его с пометкой в description: "Требует уточнения".

Классифицируй каждое мероприятие по одной из категорий таксономии ФОП ДО:
{json.dumps(TAXONOMY, ensure_ascii=False)}

Верни JSON-массив (может быть пустым []):
[
  {{
    "title": "Точное название мероприятия",
    "event_date": "Дата или период, либо null если не указана",
    "category": "Одна из категорий таксономии ФОП ДО",
    "target_audience": "Для кого предназначено, либо null",
    "description": "1-2 предложения о мероприятии и его пользе для школы"
  }}
]
Ничего кроме JSON-массива."""


def _extract_events_via_llm(source_url: str, content: str, client: OpenAI,
                              log: Callable[[str], None] = _noop) -> list[dict]:
    """Извлекает Events из текста страницы через Groq LLaMA-3-70B."""
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL_SMART,
            messages=[
                {"role": "system", "content": _HARVEST_PROMPT},
                {"role": "user", "content": f"URL источника: {source_url}\n\nТекст страницы:\n{content}"},
            ],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            log(f"  ⚠️ LLM вернула пустой список для {source_url}")
            return []
        extracted = json.loads(raw[start:end])
        log(f"  ✅ LLM извлекла {len(extracted)} мероприятий")
        return extracted
    except Exception as e:
        log(f"  ❌ Ошибка LLM-извлечения: {e}")
        return []


def _is_duplicate(title: str, event_date: str | None,
                  target_audience: str | None) -> bool:
    """Семантическая дедупликация.

    Два мероприятия считаются дублями ТОЛЬКО при совпадении всех трёх:
    название + дата + целевая аудитория.
    """
    existing = get_all_events()
    title_lower = title.lower().strip()
    date_lower = (event_date or "").lower().strip()
    audience_lower = (target_audience or "").lower().strip()

    for ev in existing:
        same_title = (
            title_lower in ev.get("title", "").lower()
            or ev.get("title", "").lower() in title_lower
        )
        same_date = date_lower == (ev.get("event_date") or "").lower().strip()
        same_audience = audience_lower == (ev.get("target_audience") or "").lower().strip()
        if same_title and same_date and same_audience:
            return True
    return False


def run_harvester(log: Callable[[str], None] = _noop) -> int:
    """Smart Harvester: сбор Events из Golden Seeds + активных Trusted Sources.

    Алгоритм:
    1. Объединяем Golden Seeds + Trusted Sources из БД (дедупликация URL).
    2. Для каждого источника: читаем главную страницу через Jina.
    3. Smart Crawl: ищем ссылку «Мероприятия/Анонсы» и уходим на глубину 1.
    4. LLaMA-3-70B извлекает Events; нет даты → is_incomplete=True.
    5. Семантическая дедупликация перед сохранением.

    Возвращает количество добавленных мероприятий.
    """
    log("=== 🌾 СБОРЩИК: УМНЫЙ АВТО-СБОР МЕРОПРИЯТИЙ ===")
    client = _groq_client()

    # Объединяем Golden Seeds с источниками из БД
    db_sources = [src["url"] for src in get_active_sources()]
    all_urls = list(dict.fromkeys(GOLDEN_SEEDS + db_sources))  # Golden Seeds всегда первые
    log(f"📋 Источников для обработки: {len(all_urls)} "
        f"({len(GOLDEN_SEEDS)} Golden Seeds + {len(db_sources)} из БД)")

    added_count = 0

    for url in all_urls:
        log(f"\n🌐 Читаю источник: {url}")
        time.sleep(1.5)  # Вежливая пауза между источниками

        # Шаг 1: Читаем главную страницу
        main_content = _fetch_via_jina(url, log)
        if not main_content:
            log(f"  ❌ Не удалось получить контент. Пропускаю.")
            continue

        log(f"  📄 Главная страница: {len(main_content)} символов")

        # Шаг 2: Smart Crawl — ищем раздел мероприятий внутри сайта
        content = _smart_crawl(url, main_content, log)

        # Шаг 3: LLM извлекает мероприятия
        log(f"  🤖 Передаю текст в LLaMA-3-70B для извлечения...")
        events = _extract_events_via_llm(url, content, client, log)

        if not events:
            log(f"  ℹ️ Мероприятий на странице не найдено (возможно, не сезон).")
            continue

        # Шаг 4: Сохраняем, проверяя дубликаты
        for ev in events:
            title = (ev.get("title") or "").strip()
            if not title:
                continue

            event_date = ev.get("event_date") or None
            target_audience = ev.get("target_audience") or None
            category = ev.get("category") or "Другое"

            # Валидируем категорию против таксономии
            if category not in TAXONOMY:
                category = "Другое"

            if _is_duplicate(title, event_date, target_audience):
                log(f"  [~] Дубликат, пропускаю: {title[:60]}")
                continue

            incomplete_mark = " (⚠️ дата уточняется)" if not event_date else ""
            log(f"  [+] Сохраняю: {title[:60]}{incomplete_mark}")

            save_event(
                title=title,
                url=url,
                event_date=event_date or "",
                direction=category,    # direction = category для обратной совместимости
                category=category,
                profit_points=20 if url in GOLDEN_SEEDS else 10,
                description=ev.get("description", ""),
                target_audience=target_audience or "",
            )
            added_count += 1

    log(f"\n✅ СБОРЩИК ЗАВЕРШЁН. Добавлено новых мероприятий: {added_count}")
    return added_count


def run_auto_harvest(log: Callable[[str], None] = _noop) -> dict:
    """Полный цикл авто-сбора: Discovery (опционально) + Harvest.

    Это главная функция, которую вызывает кнопка «Запустить Авто-Сбор» в UI.
    Возвращает {'discovered': N, 'harvested': N}.
    """
    log("🚀 ЗАПУСК ПОЛНОГО АВТО-СБОРА")
    log("─" * 50)

    # Шаг 1: Добавляем Golden Seeds в БД, если их там еще нет
    log("\n📌 Регистрирую Golden Seeds в базе данных...")
    for seed_url in GOLDEN_SEEDS:
        save_source(seed_url)
    log(f"  ✅ {len(GOLDEN_SEEDS)} Golden Seeds зарегистрированы.")

    # Шаг 2: Запускаем Harvester (Smart Crawl по всем источникам)
    log("\n")
    harvested = run_harvester(log)

    log("\n" + "─" * 50)
    log(f"🏁 АВТО-СБОР ЗАВЕРШЁН. Итого добавлено мероприятий: {harvested}")
    return {"harvested": harvested}


# Обратная совместимость
def run_fetcher() -> int:
    return run_harvester()


if __name__ == "__main__":
    run_auto_harvest()

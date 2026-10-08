"""
fetcher.py — Облачный сборщик мероприятий.
Использует duckduckgo_search для поиска и Groq API (LLaMA-3-8B) для извлечения данных.
Легковесный, не требует Playwright/браузера, идеально работает в Streamlit Cloud.
"""

import json
import os
import sys

from duckduckgo_search import DDGS
from openai import OpenAI

from database import save_event

# ── Кодировка терминала ────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Настройки ─────────────────────────────────────────────────────────────────
GROQ_MODEL = "llama3-8b-8192"

SEARCH_QUERIES = [
    "олимпиады для школьников 2026",
    "образовательные форумы для педагогов 2026",
    "конкурсы для учителей и школьников"
]

def _get_groq_key() -> str:
    try:
        import streamlit as st
        key = st.secrets.get("GROQ_API_KEY", "")
        if key: return key
    except Exception:
        pass
    return os.getenv("GROQ_API_KEY", "")

GROQ_API_KEY = _get_groq_key()

# ── Логика ────────────────────────────────────────────────────────────────────

def get_groq_client() -> OpenAI:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY не найден в секретах!")
    # Groq использует совместимый с OpenAI SDK API
    return OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1"
    )

def search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    """Ищет в DDG и возвращает список словарей с 'title', 'href', 'body'."""
    print(f"[*] Поиск в DDG: {query}")
    results = []
    try:
        with DDGS() as ddgs:
            # region='ru-ru' для российских результатов
            for r in ddgs.text(query, region='ru-ru', max_results=max_results):
                results.append(r)
    except Exception as e:
        print(f"[ERROR] Ошибка поиска DDG: {e}")
    return results

def extract_event_via_llm(snippet: dict, client: OpenAI) -> dict | None:
    """
    Передает заголовок и описание (сниппет) из поисковика в Groq,
    чтобы извлечь структурированные данные.
    """
    system_prompt = (
        "Ты — AI-ассистент, извлекающий данные об образовательных мероприятиях из поисковых сниппетов.\n"
        "Твоя задача — вернуть СТРОГИЙ JSON.\n"
        "Формат JSON:\n"
        "{\n"
        "  \"is_event\": true/false,\n"
        "  \"title\": \"Название\",\n"
        "  \"date\": \"Дата или 'Не указана'\",\n"
        "  \"direction\": \"наука, спорт, it, искусство или другое\",\n"
        "  \"description\": \"Краткая суть (1-2 предложения)\"\n"
        "}\n"
        "Если текст не описывает конкретное мероприятие (олимпиаду, форум, конкурс), ставь is_event: false.\n"
        "Не пиши ничего кроме JSON."
    )
    
    user_prompt = f"Заголовок: {snippet.get('title')}\nОписание: {snippet.get('body')}\nURL: {snippet.get('href')}"
    
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.0,
            response_format={"type": "json_object"}
        )
        
        raw_json = response.choices[0].message.content.strip()
        data = json.loads(raw_json)
        
        if data.get("is_event"):
            data["url"] = snippet.get("href")
            return data
            
    except Exception as e:
        print(f"[ERROR] Ошибка LLM для {snippet.get('href')}: {e}")
        
    return None

def run_fetcher():
    print("=== ЗАПУСК ОБЛАЧНОГО СБОРЩИКА (DDG + Groq) ===")
    
    try:
        client = get_groq_client()
    except ValueError as e:
        print(f"[FATAL] {e}")
        return

    added_count = 0
    for query in SEARCH_QUERIES:
        snippets = search_duckduckgo(query, max_results=5)
        for snip in snippets:
            event_data = extract_event_via_llm(snip, client)
            if event_data:
                # Сохраняем в Supabase (или SQLite)
                # Даем дефолтные очки
                eid = save_event(
                    title=event_data.get("title", "Без названия"),
                    url=event_data.get("url"),
                    event_date=event_data.get("date", ""),
                    direction=event_data.get("direction", "общее"),
                    profit_points=15,
                    description=event_data.get("description", "")
                )
                print(f"[+] Добавлено: {event_data.get('title')} (ID: {eid})")
                added_count += 1
                
    print(f"=== СБОР ЗАВЕРШЕН. Добавлено мероприятий: {added_count} ===")

if __name__ == "__main__":
    run_fetcher()

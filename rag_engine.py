"""
rag_engine.py — Облачный RAG.
FAISS + SentenceTransformers + Groq API (LLaMA-3-70B).

Архитектурное решение (ADR-0003):
  - get_recommendations() берет ВСЕ мероприятия из БД без SQL-фильтров.
  - Поиск релевантных мероприятий ведется исключительно через векторную
    семантическую близость (FAISS). Это устраняет проблему "пустой выдачи".
  - Если мероприятие помечено is_incomplete=True, к ответу добавляется
    предупреждение ⚠️ о необходимости уточнить детали по ссылке.
"""

from __future__ import annotations

import os
import faiss
import numpy as np
from openai import OpenAI
from sentence_transformers import SentenceTransformer

from database import get_all_events_for_rag

EMBED_MODEL = "all-MiniLM-L6-v2"
# Та же стабильная бесплатная модель что и в fetcher.py
GROQ_MODEL = "llama-3.1-8b-instant"

_embedder = None

def get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder

def _get_groq_key() -> str:
    try:
        import streamlit as st
        key = st.secrets.get("GROQ_API_KEY", "")
        if key: return key
    except Exception:
        pass
    return os.getenv("GROQ_API_KEY", "")

_cached_rag_model = None

def get_best_rag_model(client: OpenAI) -> str:
    global _cached_rag_model
    if _cached_rag_model:
        return _cached_rag_model

    candidates = [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
    ]
    try:
        available = [m.id for m in client.models.list().data]
        for c in candidates:
            if c in available:
                _cached_rag_model = c
                return _cached_rag_model
        for mid in available:
            if "whisper" not in mid and "guard" not in mid:
                _cached_rag_model = mid
                return _cached_rag_model
    except Exception:
        pass
    _cached_rag_model = "openai/gpt-oss-120b"
    return _cached_rag_model



def get_groq_client() -> OpenAI:
    key = _get_groq_key()
    if not key:
        raise ValueError("GROQ_API_KEY не найден в секретах!")
    return OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")

def build_faiss_index(events: list[dict]):
    if not events:
        return None, []
    
    texts = []
    for e in events:
        # Строим поисковый текст с учётом новых полей category и is_incomplete
        category_part = f" Категория: {e['category']}." if e.get("category") else ""
        date_part = e.get("event_date") or "дата уточняется"
        t = (
            f"Название: {e['title']}."
            f" Дата: {date_part}."
            f"{category_part}"
            f" Описание: {e.get('description', '')}."
        )
        texts.append(t)
        
    emb = get_embedder()
    vectors = emb.encode(texts, convert_to_numpy=True)
    
    dim = vectors.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(vectors)
    return index, texts

def _format_event_for_context(ev: dict) -> str:
    """Форматирует одно мероприятие для подачи в промпт LLM.
    
    Добавляет пометку ⚠️, если мероприятие неполное (is_incomplete=True).
    """
    date_str = ev.get("event_date") or "не указана"
    category_str = ev.get("category") or ev.get("direction") or "—"
    incomplete_flag = ev.get("is_incomplete") or ev.get("is_incomplete") == 1

    line = (
        f"- **{ev['title']}**\n"
        f"  Дата: {date_str} | Категория: {category_str}\n"
        f"  Ссылка: {ev['url']}\n"
        f"  {ev.get('description', '')}"
    )
    if incomplete_flag:
        line += "\n  ⚠️ *Данные неполные — точную дату и условия уточните по ссылке*"
    return line

def get_recommendations(role: str, directions: list[str], today: str) -> dict:
    """
    Возвращает словарь:
    {
       "answer": "Ответ LLM",
       "retrieved_context": ["сырой текст из БД", ...]
    }

    Изменение (ADR-0003): directions больше не используются для SQL-фильтрации.
    Параметр сохранен в сигнатуре для совместимости с app.py и используется
    только для формирования текста поискового запроса к FAISS.
    """
    # Берем ВСЕ мероприятия — FAISS сам найдет релевантные
    events = get_all_events_for_rag()
    
    if not events:
        return {"answer": "В базе данных пока нет мероприятий. Запустите Авто-Сбор в режиме администратора.", "retrieved_context": []}

    # 1. Поиск (Retrieval) — только векторная близость
    index, texts = build_faiss_index(events)
    if not index:
        return {"answer": "Ошибка построения индекса.", "retrieved_context": []}

    # Формируем поисковый запрос, используя role и directions как подсказки
    directions_hint = f", интересы: {', '.join(directions)}" if directions else ""
    query = f"Мероприятия для профиля: {role}{directions_hint}"
    q_vec = get_embedder().encode([query], convert_to_numpy=True)
    
    k = min(5, len(events))
    distances, indices = index.search(q_vec, k)
    
    context_texts = []
    for idx in indices[0]:
        if idx < len(events):
            ev = events[idx]
            context_texts.append(_format_event_for_context(ev))
            
    context_block = "\n\n".join(context_texts)
    
    # 2. Генерация (Augmented Generation)
    system_prompt = (
        "Ты — AI-рекомендательная система EduRec для советников по воспитательной работе. "
        "Пользователь хочет получить подборку подходящих мероприятий.\n"
        "ВАЖНО: Ты ДОЛЖЕН использовать ТОЛЬКО следующий контекст для ответа. "
        "Не выдумывай ссылки и даты, бери их строго из контекста.\n"
        "Если у мероприятия стоит пометка ⚠️ — обязательно упомяни в ответе, что детали нужно уточнить по ссылке.\n\n"
        f"--- КОНТЕКСТ ИЗ БАЗЫ ДАННЫХ ---\n{context_block}\n------------------------\n\n"
        "Напиши дружелюбный ответ, перечисли найденные мероприятия, укажи прямые ссылки и объясни, почему они подходят."
    )
    
    client = get_groq_client()
    rag_model = get_best_rag_model(client)
    try:
        resp = client.chat.completions.create(
            model=rag_model,
            messages=[{"role": "system", "content": system_prompt}],
            temperature=0.3
        )
        answer = resp.choices[0].message.content
    except Exception as e:
        answer = f"Ошибка генерации LLM: {e}"
        
    return {
        "answer": answer,
        "retrieved_context": context_texts
    }

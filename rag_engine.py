"""
rag_engine.py — Облачный RAG.
FAISS + SentenceTransformers + Groq API (LLaMA-3-70B).
"""

from __future__ import annotations

import os
import faiss
import numpy as np
from openai import OpenAI
from sentence_transformers import SentenceTransformer

from database import get_events_by_directions

EMBED_MODEL = "all-MiniLM-L6-v2"
GROQ_MODEL = "llama3-70b-8192"

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
        t = f"Название: {e['title']}. Дата: {e['event_date']}. Описание: {e.get('description','')}."
        texts.append(t)
        
    emb = get_embedder()
    vectors = emb.encode(texts, convert_to_numpy=True)
    
    dim = vectors.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(vectors)
    return index, texts

def get_recommendations(role: str, directions: list[str], today: str) -> dict:
    """
    Возвращает словарь:
    {
       "answer": "Ответ LLM",
       "retrieved_context": ["сырой текст из БД", ...]
    }
    """
    if not directions:
        return {"answer": "Не указаны теги для поиска.", "retrieved_context": []}

    events = get_events_by_directions(directions, today)
    
    if not events:
        return {"answer": "Подходящих мероприятий не найдено в базе.", "retrieved_context": []}

    # 1. Поиск (Retrieval)
    index, texts = build_faiss_index(events)
    if not index:
        return {"answer": "Ошибка построения индекса.", "retrieved_context": []}
        
    query = f"Лучшие мероприятия для профиля: {role}, интересы: {', '.join(directions)}"
    q_vec = get_embedder().encode([query], convert_to_numpy=True)
    
    k = min(3, len(events))
    distances, indices = index.search(q_vec, k)
    
    context_texts = []
    for idx in indices[0]:
        if idx < len(events):
            ev = events[idx]
            context_texts.append(
                f"- {ev['title']} ({ev['event_date']})\n  Ссылка: {ev['url']}\n  {ev.get('description','')}"
            )
            
    context_block = "\n\n".join(context_texts)
    
    # 2. Генерация (Augmented Generation)
    system_prompt = (
        "Ты — AI-рекомендательная система EduRec. "
        "Пользователь хочет получить подборку мероприятий.\n"
        "ВАЖНО: Ты ДОЛЖЕН использовать ТОЛЬКО следующий контекст (шпаргалку) для ответа. "
        "Не выдумывай ссылки и даты, бери их строго из контекста.\n\n"
        f"--- КОНТЕКСТ ИЗ FAISS БД ---\n{context_block}\n------------------------\n\n"
        "Напиши дружелюбный ответ, перечисли найденные мероприятия, укажи прямые ссылки и объясни, почему они подходят."
    )
    
    client = get_groq_client()
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
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

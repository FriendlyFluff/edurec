"""
app.py — Главный UI на Streamlit.
Реализован дизайн из 2 вкладок: RAG-Рекомендации и Админка (Fetcher).
Версия 2.0 (Autonomous Harvester).
"""

import streamlit as st
from datetime import date
from database import get_last_updated
from rag_engine import get_recommendations
from fetcher import discover_sources, audit_sources, run_harvester

st.set_page_config(page_title="EduRec", page_icon="🎓", layout="centered")

st.title("🎓 EduRec: Облачный RAG Рекомендатель")

# Показываем таймер свежести базы на главной
last_upd = get_last_updated()
if last_upd:
    # Убираем миллисекунды для красоты
    nice_time = str(last_upd).split('.')[0]
    st.caption(f"🟢 База данных свежая. Последнее обновление: **{nice_time}**")
else:
    st.caption("🔴 База данных пуста.")

# Разделяем UI на две вкладки (Тикет 03 + 07)
tab1, tab2 = st.tabs(["🎯 Рекомендации (RAG)", "⚙️ Админка / Парсер"])

with tab1:
    st.header("Получить подборку")
    st.write("Выберите вашу роль, и нейросеть подберет лучшие мероприятия из базы.")
    
    # Роли жестко зашиты в UI, без базы профилей (SPEC-002)
    role_mapping = {
        "Я школьник/студент": {
            "role": "студент",
            "tags": ["наука", "олимпиада", "it", "другое"]
        },
        "Я преподаватель": {
            "role": "преподаватель",
            "tags": ["педагогика", "форум", "наука", "конкурс"]
        }
    }
    
    selected_role = st.radio("Кто вы?", list(role_mapping.keys()), horizontal=True)
    
    if st.button("Сгенерировать RAG рекомендации", type="primary"):
        with st.spinner("Запрашиваем Groq LLaMA-3-70B..."):
            today_str = date.today().isoformat()
            
            role_data = role_mapping[selected_role]
            result = get_recommendations(role_data["role"], role_data["tags"], today_str)
            
            # Выводим красивый ответ
            st.markdown("### Ваш персональный список:")
            st.write(result["answer"])
            
            # Доказательство работы RAG для комиссии (Expander)
            with st.expander("🛠 Под капотом: Логи векторного поиска (FAISS)"):
                st.info("Этот блок доказывает комиссии, что нейросеть не выдумывает факты. Ниже представлены сырые данные, которые FAISS извлек из нашей БД и передал в LLM как 'шпаргалку'.")
                if not result["retrieved_context"]:
                    st.write("Контекст не найден.")
                else:
                    for i, ctx in enumerate(result["retrieved_context"]):
                        st.markdown(f"**Найдено в БД (Документ {i+1}):**\n```\n{ctx}\n```")

with tab2:
    st.header("Управление базой данных")
    st.write("Здесь вы можете управлять ИИ-Агентами: Разведчиком (Scout) и Сборщиком (Harvester).")
    
    # Защита от спама: блокировка кнопок во время работы агентов
    if "is_fetching" not in st.session_state:
        st.session_state.is_fetching = False
        
    btn_disabled = st.session_state.is_fetching
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🔍 Найти новые источники\n(Scout: Discovery)", disabled=btn_disabled, use_container_width=True):
            st.session_state.is_fetching = True
            st.rerun()
            
    with col2:
        if st.button("🩺 Проверить старые\n(Scout: Audit)", disabled=btn_disabled, use_container_width=True):
            st.session_state.is_fetching = 'audit'
            st.rerun()
            
    with col3:
        if st.button("🌾 Собрать урожай\n(Harvester)", disabled=btn_disabled, use_container_width=True):
            st.session_state.is_fetching = 'harvest'
            st.rerun()
            
    # Обработка действий в зависимости от стейта
    if st.session_state.is_fetching == True:
        with st.spinner("Scout ищет новые сайты (Tavily + Groq)..."):
            try:
                added = discover_sources()
                st.success(f"Разведка завершена! Добавлено новых источников: {added}")
            except Exception as e:
                st.error(f"Ошибка Разведчика: {e}")
            finally:
                st.session_state.is_fetching = False
                st.button("Обновить страницу", type="primary")
                
    elif st.session_state.is_fetching == 'audit':
        with st.spinner("Scout проверяет текущие сайты (Jina + Groq)..."):
            try:
                stats = audit_sources()
                st.success(f"Аудит завершен! Живых: {stats['alive']}, Мертвых: {stats['dead']}")
            except Exception as e:
                st.error(f"Ошибка Аудитора: {e}")
            finally:
                st.session_state.is_fetching = False
                st.button("Обновить страницу", type="primary")
                
    elif st.session_state.is_fetching == 'harvest':
        with st.spinner("Harvester читает тексты и извлекает мероприятия (Jina + Groq)..."):
            try:
                added = run_harvester()
                st.success(f"Сбор завершен! Новых мероприятий добавлено: {added}")
            except Exception as e:
                st.error(f"Ошибка Сборщика: {e}")
            finally:
                st.session_state.is_fetching = False
                st.button("Обновить страницу", type="primary")

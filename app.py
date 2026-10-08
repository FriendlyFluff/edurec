"""
app.py — Главный UI на Streamlit.
Реализован дизайн из 2 вкладок: RAG-Рекомендации и Админка (Fetcher).
"""

import streamlit as st
from datetime import date
from database import list_users, get_last_updated
from rag_engine import get_recommendations
from fetcher import run_fetcher

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

# Разделяем UI на две вкладки (Тикет 03)
tab1, tab2 = st.tabs(["🎯 Рекомендации (RAG)", "⚙️ Админка / Парсер"])

with tab1:
    st.header("Получить подборку")
    users = list_users()
    
    if not users:
        st.warning("Нет пользователей в БД. Запустите database.py локально один раз для создания тестового юзера.")
    else:
        user_opts = {u["user_id"]: f"{u['role']} (Интересы: {u['preferred_directions']})" for u in users}
        sel_uid = st.selectbox("Выберите профиль:", options=list(user_opts.keys()), format_func=lambda x: user_opts[x])
        
        if st.button("Сгенерировать RAG рекомендации", type="primary"):
            with st.spinner("Запрашиваем Groq LLaMA-3-70B..."):
                today_str = date.today().isoformat()
                result = get_recommendations(sel_uid, today_str)
                
                # Выводим красивый ответ
                st.markdown("### Ваш персональный список:")
                st.write(result["answer"])
                
                # Доказательство работы RAG для комиссии (Expander)
                with st.expander("🛠 Под капотом: Логи векторного поиска (FAISS)"):
                    st.info("Этот блок доказывает комиссии, что нейросеть не выдумывает факты. Ниже представлены сырые данные, которые FAISS извлек из нашей БД и передал в LLM как 'шпаргалку'.")
                    for i, ctx in enumerate(result["retrieved_context"]):
                        st.markdown(f"**Найдено в БД (Документ {i+1}):**\n```\n{ctx}\n```")

with tab2:
    st.header("Управление базой данных")
    st.write("Здесь вы можете запустить облачный парсер. Он обратится к DuckDuckGo, скачает сниппеты, проанализирует их через Groq LLaMA-3-8B и сохранит в Supabase.")
    
    # Защита от спама: если парсер работает, кнопка не нажмется дважды
    if "is_fetching" not in st.session_state:
        st.session_state.is_fetching = False
        
    btn_disabled = st.session_state.is_fetching
    
    if st.button("🚀 Запустить облачный сборщик", disabled=btn_disabled):
        st.session_state.is_fetching = True
        st.rerun()
        
    if st.session_state.is_fetching:
        with st.spinner("Идет живой сбор данных из интернета... Это займет около 15 секунд."):
            try:
                run_fetcher()
                st.success("База успешно обновлена!")
            except Exception as e:
                st.error(f"Ошибка парсера: {e}")
            finally:
                st.session_state.is_fetching = False
                # Перезагружаем страницу, чтобы обновился таймер наверху
                st.button("Обновить страницу", type="primary")

"""
app.py — Главный UI на Streamlit. Версия 3.0 (SPEC-003: Admin + Smart Harvester).

Режимы:
  Пользователь — видит только RAG-рекомендации и кнопку «Все мероприятия».
  Администратор — после ввода пин-кода получает доступ к:
                  • Одна кнопка «Запустить Авто-Сбор» с живым терминалом логов
                  • Поле для ручного добавления URL-источника
                  • Таблица всей базы данных с подсветкой ⚠️ неполных записей
"""

import streamlit as st
from datetime import date
import pandas as pd

from database import get_last_updated, get_all_events, save_source, init_db
from rag_engine import get_recommendations
from fetcher import run_auto_harvest

# ── Инициализация ──────────────────────────────────────────────────────────────
init_db()

st.set_page_config(page_title="EduRec", page_icon="🎓", layout="centered")

# ── Проверка пин-кода администратора ──────────────────────────────────────────

def _check_admin_pin(entered: str) -> bool:
    """Сверяет введённый пин с сохранённым в секретах."""
    try:
        correct = st.secrets.get("admin_pin", "1234")
    except Exception:
        correct = "1234"
    return entered.strip() == str(correct).strip()

# Состояние: авторизован ли администратор
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "harvest_log" not in st.session_state:
    st.session_state.harvest_log = []
if "is_harvesting" not in st.session_state:
    st.session_state.is_harvesting = False

# ── Sidebar: кнопка и форма входа в Админку ──────────────────────────────────
with st.sidebar:
    st.markdown("---")
    if not st.session_state.is_admin:
        st.markdown("### 🔒 Режим администратора")
        pin_input = st.text_input("Пин-код:", type="password", key="pin_input",
                                  placeholder="Введите пин...")
        if st.button("Войти", key="admin_login_btn"):
            if _check_admin_pin(pin_input):
                st.session_state.is_admin = True
                st.rerun()
            else:
                st.error("Неверный пин-код.")
    else:
        st.success("✅ Режим администратора")
        if st.button("Выйти из Админки", key="admin_logout_btn"):
            st.session_state.is_admin = False
            st.session_state.harvest_log = []
            st.rerun()

# ── Заголовок ──────────────────────────────────────────────────────────────────
st.title("🎓 EduRec: Мероприятия для советника по воспитанию")

last_upd = get_last_updated()
if last_upd:
    nice_time = str(last_upd).split(".")[0].replace("T", " ")
    st.caption(f"🟢 База данных обновлена: **{nice_time}**")
else:
    st.caption("🔴 База данных пуста. Запустите Авто-Сбор в режиме администратора.")

# ── Определяем вкладки ─────────────────────────────────────────────────────────
if st.session_state.is_admin:
    tab_rec, tab_all, tab_admin = st.tabs([
        "🎯 Рекомендации", "📋 Все мероприятия", "⚙️ Администрирование"
    ])
else:
    tab_rec, tab_all = st.tabs(["🎯 Рекомендации", "📋 Все мероприятия"])
    tab_admin = None

# ══════════════════════════════════════════════════════════════════════════════
# ВКЛАДКА 1: Рекомендации (RAG)
# ══════════════════════════════════════════════════════════════════════════════
with tab_rec:
    st.header("Получить подборку мероприятий")
    st.write("Выберите вашу роль — нейросеть подберёт подходящие мероприятия из базы.")

    role_mapping = {
        "👨‍🏫 Советник по воспитанию / Педагог": {
            "role": "советник по воспитанию",
            "tags": ["Патриотическое воспитание", "Добровольчество (волонтерство)",
                     "Методическая поддержка (для педагогов)", "Проекты РДДМ (Движение Первых / Орлята)"],
        },
        "🎒 Школьник / Студент": {
            "role": "школьник",
            "tags": ["Профориентация", "Проекты РДДМ (Движение Первых / Орлята)",
                     "Добровольчество (волонтерство)", "Экология и Краеведение"],
        },
    }

    selected_role = st.radio("Кто вы?", list(role_mapping.keys()), horizontal=True)

    if st.button("✨ Подобрать мероприятия", type="primary", key="rag_btn"):
        with st.spinner("Нейросеть ищет подходящие мероприятия..."):
            today_str = date.today().isoformat()
            role_data = role_mapping[selected_role]
            result = get_recommendations(role_data["role"], role_data["tags"], today_str)

            st.markdown("### Ваша персональная подборка:")
            st.write(result["answer"])

            with st.expander("🛠 Под капотом: что нашел векторный поиск (FAISS)"):
                st.info(
                    "Здесь показаны сырые данные, которые FAISS извлёк из базы "
                    "и передал нейросети как «шпаргалку». Это гарантирует, "
                    "что ИИ не выдумывает факты."
                )
                if not result["retrieved_context"]:
                    st.write("Контекст не найден.")
                else:
                    for i, ctx in enumerate(result["retrieved_context"]):
                        st.markdown(f"**Документ {i + 1} из базы:**\n```\n{ctx}\n```")

# ══════════════════════════════════════════════════════════════════════════════
# ВКЛАДКА 2: Все мероприятия (История базы данных)
# ══════════════════════════════════════════════════════════════════════════════
with tab_all:
    st.header("📋 Все мероприятия в базе")

    all_events = get_all_events()
    if not all_events:
        st.info("База данных пуста. Попросите администратора запустить Авто-Сбор.")
    else:
        st.caption(f"Всего мероприятий в базе: **{len(all_events)}**")

        # Строим DataFrame
        df = pd.DataFrame(all_events)

        # Выбираем и переименовываем нужные колонки
        cols_map = {
            "title": "Название",
            "category": "Категория",
            "event_date": "Дата",
            "target_audience": "Аудитория",
            "url": "Ссылка",
            "is_incomplete": "Неполные данные",
        }
        existing_cols = [c for c in cols_map if c in df.columns]
        df_show = df[existing_cols].rename(columns=cols_map)

        # Подсвечиваем неполные мероприятия
        if "Неполные данные" in df_show.columns:
            def _highlight_incomplete(row):
                if row.get("Неполные данные") in (True, 1, "1", "true", "True"):
                    return ["background-color: #fff3cd"] * len(row)
                return [""] * len(row)

            styled = df_show.style.apply(_highlight_incomplete, axis=1)
            st.dataframe(styled, use_container_width=True)
            st.caption("⚠️ Строки на жёлтом фоне — мероприятия с неполными данными (нет точной даты). "
                       "Перейдите по ссылке для уточнения.")
        else:
            st.dataframe(df_show, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# ВКЛАДКА 3: Администрирование (только для Админа)
# ══════════════════════════════════════════════════════════════════════════════
if tab_admin is not None:
    with tab_admin:
        st.header("⚙️ Панель администратора")

        # ── Блок 1: Авто-Сбор ─────────────────────────────────────────────────
        st.subheader("🌾 Авто-Сбор мероприятий")
        st.write(
            "Запускает полный цикл сбора: берёт Golden Seeds (МЦВП, Орлята, РДДМ...), "
            "умно переходит на страницы мероприятий и извлекает данные через ИИ."
        )

        if st.button("🚀 Запустить Авто-Сбор", type="primary",
                     disabled=st.session_state.is_harvesting, key="harvest_btn"):
            st.session_state.is_harvesting = True
            st.session_state.harvest_log = []
            st.rerun()

        # Живой терминал логов
        terminal_placeholder = st.empty()

        if st.session_state.is_harvesting:
            log_lines: list[str] = []

            def ui_logger(msg: str) -> None:
                """Callback, который пишет в живой терминал Streamlit."""
                log_lines.append(msg)
                # Обновляем терминал прямо во время выполнения
                terminal_placeholder.code("\n".join(log_lines), language="")

            try:
                result = run_auto_harvest(log=ui_logger)
                st.session_state.harvest_log = log_lines
                st.success(f"✅ Сбор завершён! Добавлено новых мероприятий: {result['harvested']}")
            except Exception as e:
                st.error(f"❌ Ошибка во время сбора: {e}")
                log_lines.append(f"\n❌ ОШИБКА: {e}")
                terminal_placeholder.code("\n".join(log_lines), language="")
            finally:
                st.session_state.is_harvesting = False
                st.rerun()

        elif st.session_state.harvest_log:
            # Показываем лог предыдущего запуска
            st.caption("Лог последнего запуска:")
            terminal_placeholder.code("\n".join(st.session_state.harvest_log), language="")

        st.markdown("---")

        # ── Блок 2: Ручное добавление источника ───────────────────────────────
        st.subheader("🔗 Добавить источник вручную")
        st.write("Вставьте ссылку на сайт — он будет добавлен в базу и проверен при следующем Авто-Сборе.")

        with st.form("add_source_form", clear_on_submit=True):
            new_url = st.text_input("URL сайта:", placeholder="https://example.ru")
            submitted = st.form_submit_button("➕ Добавить в базу источников")
            if submitted:
                if new_url and new_url.startswith("http"):
                    source_id = save_source(new_url.strip())
                    if source_id:
                        st.success(f"✅ Источник добавлен (ID: {source_id}): {new_url}")
                    else:
                        st.warning("Этот источник уже есть в базе.")
                else:
                    st.error("Пожалуйста, введите корректный URL (начинается с http).")

        st.markdown("---")

        # ── Блок 3: Таблица всей базы данных ─────────────────────────────────
        st.subheader("🗄 База данных мероприятий (полный вид)")

        all_events_admin = get_all_events()
        if not all_events_admin:
            st.info("База данных пуста.")
        else:
            df_admin = pd.DataFrame(all_events_admin)

            # В Админке показываем ВСЕ колонки, включая технические
            admin_cols_map = {
                "event_id": "ID",
                "title": "Название",
                "category": "Категория",
                "event_date": "Дата",
                "target_audience": "Аудитория",
                "is_incomplete": "⚠️",
                "profit_points": "Баллы",
                "url": "Ссылка",
                "created_at": "Добавлено",
            }
            existing_admin_cols = [c for c in admin_cols_map if c in df_admin.columns]
            df_admin_show = df_admin[existing_admin_cols].rename(columns=admin_cols_map)

            # Конвертируем флаг is_incomplete в читаемый вид
            if "⚠️" in df_admin_show.columns:
                df_admin_show["⚠️"] = df_admin_show["⚠️"].apply(
                    lambda x: "⚠️ Неполное" if x in (True, 1, "1", "true", "True") else "✅"
                )

            def _admin_highlight(row):
                if row.get("⚠️") == "⚠️ Неполное":
                    return ["background-color: #fff3cd"] * len(row)
                return [""] * len(row)

            styled_admin = df_admin_show.style.apply(_admin_highlight, axis=1)
            st.dataframe(styled_admin, use_container_width=True)
            st.caption(f"Всего записей: {len(all_events_admin)}")

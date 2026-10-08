@echo off
chcp 65001 >nul
title EduRec

echo.
echo  ====================================
echo   EduRec -- Рекомендации мероприятий
echo  ====================================
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python не найден!
    pause & exit /b 1
)

echo Обновить базу мероприятий? (ищет новые через Яндекс, ~3-5 мин)
choice /c YN /m "Обновить сейчас"
if errorlevel 2 goto :start_app
if errorlevel 1 (
    echo.
    echo [FETCHER] Запускаем поиск мероприятий...
    echo [INFO] LM Studio должен быть запущен на localhost:1234
    echo.
    py -3.13 fetcher.py
    echo.
)

:start_app
echo [APP] Запускаем интерфейс...
start "" http://localhost:8501
py -3.13 -m streamlit run app.py
pause

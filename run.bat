@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ERRORLEVEL="
chcp 65001 >nul
rem Запуск прототипа на Windows (Python 3.11+)
cd /d "%~dp0" || goto :error

py -3.11 -V >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3.11"
) else (
    py -3 -V >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
    ) else (
        set "PY=python"
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Создаю виртуальное окружение .venv ...
    %PY% -m venv .venv || goto :error
) else (
    echo [1/3] Использую существующее виртуальное окружение .venv.
)

rem Проверяем зависимости и в существующей среде: предыдущая установка могла прерваться.
rem pip не обновляет уже подходящие версии; интернет нужен только для недостающих пакетов.
echo [2/3] Проверяю зависимости ...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error

echo [3/3] Запускаю приложение ...
".venv\Scripts\python.exe" main.py
if not "%errorlevel%"=="0" goto :launch_error
goto :eof

:launch_error
echo.
echo Ошибка запуска. Скопируйте текст ошибки выше.
echo Если создан журнал, он находится здесь: "%USERPROFILE%\tsai_crash.log"
echo Перед отправкой отчёта удалите из него API-ключи, если они там есть.
pause
exit /b 1

:error
echo.
echo Ошибка установки. Проверьте, что установлен Python 3.11+ (python.org) и есть интернет.
pause
exit /b 1

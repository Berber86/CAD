@echo off
rem Запуск прототипа на Windows (Python 3.11+)
setlocal
chcp 65001 >nul
cd /d "%~dp0"

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
    echo [2/3] Устанавливаю зависимости ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

echo [3/3] Запускаю приложение ...
".venv\Scripts\python.exe" main.py
goto :eof

:error
echo.
echo Ошибка установки. Проверьте, что установлен Python 3.11+ (python.org) и есть интернет.
pause
exit /b 1

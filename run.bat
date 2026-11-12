@echo off
rem Запуск прототипа на Windows: py -3.11 run.bat
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    set "PY=python"
) else (
    set "PY=py -3.11"
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

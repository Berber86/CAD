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

rem Локальный OCR сканов — необязательный набор: без него сканы уходят в ИИ.
if exist "requirements-ocr.txt" (
    ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements-ocr.txt
    if errorlevel 1 echo [2/3] Локальный OCR не установлен - сканы пойдут в ИИ.
)
echo [3/3] Запускаю приложение ...
rem Проверка, что файлы программы не побиты обновлением: разбираем все модули в
rem папке программы (без записи .pyc заново, если файл не менялся). Ошибка видна
rem здесь же, в консоли, а ниже предлагается откат на резервную копию.
".venv\Scripts\python.exe" -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('.', maxlevels=0, quiet=1) else 1)" || goto :launch_error
rem Окно cmd закрывается сразу после запуска программы (4.41): GUI поднимается
rem отдельным процессом без консоли, cmd его не ждёт и не остаётся в панели задач.
rem Раньше cmd ждал завершения python.exe, а обновление заменяло run.bat прямо во
rem время его выполнения - старое окно зависало, и за несколько обновлений их
rem накапливалось несколько штук.
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" main.py
    goto :eof
)
rem Запасной путь (pythonw.exe не нашёлся): программа работает в этой консоли,
rem но прячет её, как только окно появится - см. console_window.py.
set "TSAI_HIDE_CONSOLE=1"
".venv\Scripts\python.exe" main.py
if not "%errorlevel%"=="0" goto :launch_error
goto :eof

:launch_error
echo.
echo Ошибка запуска. Скопируйте текст ошибки выше.
echo Если создан журнал, он находится здесь: "%USERPROFILE%\tsai_crash.log"
echo Перед отправкой отчёта удалите из него API-ключи, если они там есть.
if not exist "backups\*" goto :launch_pause
echo.
echo Есть резервная копия прежней версии (сохранена перед последним обновлением).
choice /c YN /n /m "Откатиться на неё? [Y/N] "
if errorlevel 2 goto :launch_pause
".venv\Scripts\python.exe" rollback_update.py
if errorlevel 1 goto :launch_pause
echo Откат выполнен. Закройте это окно и запустите run.bat снова.
pause
exit /b 0

:launch_pause
pause
exit /b 1

:error
echo.
echo Ошибка установки. Проверьте, что установлен Python 3.11+ (python.org) и есть интернет.
pause
exit /b 1

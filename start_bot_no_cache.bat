@echo off
set SCRIPT_DIR=%~dp0

set VENV_PYTHONW="%SCRIPT_DIR%.venv\Scripts\pythonw.exe"
set BOT_SCRIPT="%SCRIPT_DIR%src\music_bot\music_bot.pyw"

echo Checking if the bot is already running...
netstat -aon | findstr /R /C:":8000 .*LISTENING" > nul
if %errorlevel%==0 (
    echo.
    echo INFO: The Music Bot appears to be already running.
    echo A process is already LISTENING on port 8000.
    echo To restart the bot, please run 'stop_bot.bat' first.
    echo.
    pause
    exit /b
)

if not exist %VENV_PYTHONW% (
    echo.
    echo ERROR: The Python interpreter was not found in the virtual environment.
    echo Expected path: %VENV_PYTHONW%
    echo Please make sure you have created a virtual environment named ".venv".
    echo.
    pause
    exit /b
)

if not exist %BOT_SCRIPT% (
    echo.
    echo ERROR: The bot script was not found.
    echo Expected path: %BOT_SCRIPT%
    echo.
    pause
    exit /b
)

echo Starting the Music Bot in the background with new cache downloads disabled...
start "MusicBot" /B %VENV_PYTHONW% %BOT_SCRIPT% --no-cache

echo The bot has been started. You can close this window.
echo Existing cached songs will still be used; uncached songs will stream.
echo To check logs, visit http://localhost:8000
echo To shut down, send a POST request to http://localhost:8000/shutdown (or use stop_bot.bat)

timeout /t 5 > nul

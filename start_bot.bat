@echo off
REM =================================================================
REM  Batch script to run the Music Bot in the background
REM =================================================================

REM Get the directory where this script is located. This makes the script portable.
set SCRIPT_DIR=%~dp0

REM Define the paths to the virtual environment and the main python script.
set VENV_PYTHONW="%SCRIPT_DIR%.venv\Scripts\pythonw.exe"
set BOT_SCRIPT="%SCRIPT_DIR%music_bot.pyw"

REM --- Check if the bot is already running by checking the port ---
REM This looks for ":8000" followed by spaces and then "LISTENING".
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

REM --- Check if the required files exist ---
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

REM --- Run the bot ---
echo Starting the Music Bot in the background...
REM The 'start' command with the /B flag runs the program without creating a new window.
REM We give it a title "MusicBot" which is good practice but won't be visible.
start "MusicBot" /B %VENV_PYTHONW% %BOT_SCRIPT%

echo The bot has been started. You can close this window.
echo To check logs, visit http://localhost:8000
echo To shut down, visit http://localhost:8000/shutdown

REM A short pause so the user can read the message before the window closes.
timeout /t 5 > nul

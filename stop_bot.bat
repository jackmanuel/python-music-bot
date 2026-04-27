@echo off
set SHUTDOWN_URL=http://localhost:8000/shutdown

echo Sending graceful shutdown signal to the Music Bot...

curl -s -X POST -f %SHUTDOWN_URL% > nul

if errorlevel 1 (
    echo.
    echo Could not connect to the bot's server.
    echo It might already be stopped or was not started correctly.
) else (
    echo.
    echo Shutdown signal sent successfully.
    echo The bot will now terminate gracefully. Please allow a few seconds.
)

echo.
pause

@echo off
REM =================================================================
REM  Batch script to gracefully shut down the Music Bot
REM =================================================================

REM Define the URL for the shutdown endpoint.
set SHUTDOWN_URL=http://localhost:8000/shutdown

echo Sending graceful shutdown signal to the Music Bot...

REM Use curl to send a POST request to the shutdown URL.
REM -s makes it silent (no progress meter).
REM -f makes it fail silently on server errors (like if the bot isn't running).
curl -s -X POST -f %SHUTDOWN_URL% > nul

REM Check the result of the curl command.
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
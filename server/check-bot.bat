@echo off
cd /d "%~dp0"
echo === Server health ===
curl.exe -s http://127.0.0.1:8787/health
echo.
echo.
echo === Telegram bot ===
curl.exe -s http://127.0.0.1:8787/telegram/me
echo.
echo.
pause

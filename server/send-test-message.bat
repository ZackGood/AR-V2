@echo off
cd /d "%~dp0"
set /p CHAT_ID=Enter your Telegram chat ID: 
curl.exe -s -X POST http://127.0.0.1:8787/telegram/test -H "Content-Type: application/json" -d "{\"chat_id\":\"%CHAT_ID%\",\"text\":\"Local Telegram test: server is working.\"}"
echo.
pause

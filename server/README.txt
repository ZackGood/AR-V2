LOCAL OTP SERVER
================

1) Open server\.env in Notepad.
2) Replace PASTE_NEW_BOT_TOKEN_HERE with the NEW token from @BotFather.
3) Double-click start-server.bat.
4) In Telegram, open your bot and send /start.
5) Double-click check-bot.bat. It should show the bot info.
6) Run this in a Command Prompt to see your chat ID:
   curl -s http://127.0.0.1:8787/telegram/updates
7) Use that numeric chat_id as the Telegram ID in the extension.
8) Click GET OTP. The server sends a 6-digit OTP with Telegram sendMessage.
9) Enter the OTP and the extension receives a local session token.

Endpoints:
  GET  /health
  GET  /telegram/me
  GET  /telegram/updates
  POST /telegram/test
  POST /request-otp
  POST /verify-otp
  POST /validate-token

No database is needed for this local test server. Restarting the server clears OTPs and sessions.
The Telegram bot token stays on the server in .env and is not embedded in the extension.

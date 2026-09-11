import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DB_FILE = ROOT / "users.sqlite3"
HOST = "127.0.0.1"
PORT = 8787
OTP_TTL_SECONDS = 300
SESSION_TTL_SECONDS = 86400
POLL_INTERVAL_SECONDS = 1


def load_env(path: Path):
    env = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_cfg(name, default=""):
    env = load_env(ENV_FILE)
    return os.environ.get(name, env.get(name, default))


def telegram_call(method, payload=None):
    token = get_cfg("TELEGRAM_BOT_TOKEN")
    if not token or token == "PASTE_NEW_BOT_TOKEN_HERE":
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured in server/.env")
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {exc.code}: {text}") from exc
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data.get("result")


def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                plan TEXT NOT NULL DEFAULT 'Free',
                registered_at INTEGER NOT NULL,
                login_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


def upsert_user(telegram_id, username="", first_name="", increment_login=False):
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row:
            if increment_login:
                conn.execute("UPDATE users SET username=?, first_name=?, login_count=login_count+1 WHERE telegram_id=?",
                             (username, first_name, telegram_id))
            else:
                conn.execute("UPDATE users SET username=?, first_name=? WHERE telegram_id=?",
                             (username, first_name, telegram_id))
        else:
            conn.execute("INSERT INTO users(telegram_id, username, first_name, plan, registered_at, login_count) VALUES(?,?,?,?,?,?)",
                         (telegram_id, username, first_name, "Free", now, 1 if increment_login else 0))
        conn.commit()


def get_user(telegram_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()


def format_date(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


# In-memory OTP/session state; database stores registration/profile only.
OTPS = {}
SESSIONS = {}
OFFSET = None


def json_response(handler, status, payload):
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length > 32_000:
        raise ValueError("Request too large")
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode("utf-8"))


def send_bot_message(chat_id, text, **kwargs):
    payload = {"chat_id": chat_id, "text": text}
    payload.update(kwargs)
    return telegram_call("sendMessage", payload)


def bot_stats_text(user):
    return (
        "📊 *Your Stats — AR Hitter V2*\n\n"
        f"🆔 ID: `{user['telegram_id']}`\n"
        f"👤 Username: @{user['username'] if user['username'] else 'not_set'}\n"
        f"✅ Logins: **{user['login_count']}**\n"
        "📅 Registered: " + format_date(user["registered_at"]) + "\n"
        f"💳 Plan: **{user['plan']}**"
    )


def handle_bot_message(msg):
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = str(chat.get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    username = sender.get("username") or ""
    first_name = sender.get("first_name") or ""

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if command == "/start":
        user = get_user(chat_id)
        if user:
            send_bot_message(
                chat_id,
                "✅ You are already registered.\n\n"
                "Use /stats to view your account.\n"
                "Use /register to refresh your registration.\n"
                "Use the extension to request an OTP."
            )
        else:
            send_bot_message(
                chat_id,
                "👋 *AR HITTER V2*\n\n"
                "Welcome! You are not registered yet.\n\n"
                "Send /register to create your Free account.\n"
                "After registration, enter your Telegram ID in the extension to request an OTP.",
                parse_mode="Markdown"
            )
        return

    if command == "/register":
        existing = get_user(chat_id)
        upsert_user(chat_id, username, first_name, increment_login=False)
        if existing:
            send_bot_message(chat_id, "✅ Your registration is already active. Plan: *Free*.\n\nUse /stats to view your account.", parse_mode="Markdown")
        else:
            send_bot_message(chat_id, "✅ Registration complete!\n\nPlan: *Free*\nUse /stats to view your account, then enter your Telegram ID in the extension to request an OTP.", parse_mode="Markdown")
        return

    if command == "/stats":
        user = get_user(chat_id)
        if not user:
            send_bot_message(chat_id, "❌ You are not registered. Send /register first.")
        else:
            send_bot_message(chat_id, bot_stats_text(user), parse_mode="Markdown")
        return

    send_bot_message(chat_id, "Unknown command. Use /start, /register, or /stats.")


def telegram_poll_loop():
    global OFFSET
    print("Telegram bot polling enabled. Commands: /start /register /stats")
    while True:
        try:
            payload = {"limit": 100, "timeout": 10}
            if OFFSET is not None:
                payload["offset"] = OFFSET
            updates = telegram_call("getUpdates", payload) or []
            for update in updates:
                OFFSET = update.get("update_id", 0) + 1
                msg = update.get("message") or update.get("edited_message")
                if msg:
                    try:
                        handle_bot_message(msg)
                    except Exception as exc:
                        print(f"Bot handler error: {exc}")
        except Exception as exc:
            print(f"Telegram polling error: {exc}")
            time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    server_version = "ARHitterLocalOTP/2.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")

    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            json_response(self, 200, {"success": True, "service": "local-otp", "port": PORT})
            return
        if path == "/telegram/me":
            try:
                bot = telegram_call("getMe")
                json_response(self, 200, {"success": True, "bot": bot})
            except Exception as exc:
                json_response(self, 500, {"success": False, "error": str(exc)})
            return
        if path == "/telegram/updates":
            try:
                result = telegram_call("getUpdates", {"limit": 20, "timeout": 0})
                items = []
                for update in result or []:
                    msg = update.get("message") or update.get("edited_message") or {}
                    chat = msg.get("chat") or {}
                    user = msg.get("from") or {}
                    items.append({
                        "update_id": update.get("update_id"),
                        "chat_id": chat.get("id"),
                        "chat_type": chat.get("type"),
                        "username": user.get("username"),
                        "first_name": user.get("first_name"),
                        "text": msg.get("text"),
                    })
                json_response(self, 200, {"success": True, "updates": items})
            except Exception as exc:
                json_response(self, 500, {"success": False, "error": str(exc)})
            return
        json_response(self, 404, {"success": False, "error": "Not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = read_json(self)
        except Exception as exc:
            json_response(self, 400, {"success": False, "error": f"Invalid JSON: {exc}"})
            return

        if path == "/request-otp":
            self.request_otp(body)
            return
        if path == "/verify-otp":
            self.verify_otp(body)
            return
        if path == "/validate-token":
            self.validate_token(body)
            return
        if path == "/register":
            self.register(body)
            return
        if path == "/telegram/test":
            self.telegram_test(body)
            return
        json_response(self, 404, {"success": False, "error": "Not found"})

    def register(self, body):
        user_id = str(body.get("userId", "")).strip()
        if not user_id or not user_id.lstrip("-").isdigit():
            json_response(self, 400, {"success": False, "error": "userId must be numeric"})
            return
        # Registration is authoritative in Telegram via /register; this endpoint only reports status.
        user = get_user(user_id)
        if not user:
            json_response(self, 403, {"success": False, "registered": False, "error": "Telegram ID is not registered. Open the bot and send /register first."})
            return
        json_response(self, 200, {"success": True, "registered": True, "plan": user["plan"], "username": user["username"], "first_name": user["first_name"]})

    def request_otp(self, body):
        user_id = str(body.get("userId", "")).strip()
        if not user_id or not user_id.lstrip("-").isdigit():
            json_response(self, 400, {"success": False, "error": "Telegram ID must be numeric"})
            return
        user = get_user(user_id)
        if not user:
            json_response(self, 403, {"success": False, "error": "Telegram ID is not registered. Open the bot and send /register first."})
            return

        otp = f"{secrets.randbelow(1_000_000):06d}"
        now = int(time.time())
        OTPS[user_id] = {"otp": otp, "expires": now + OTP_TTL_SECONDS, "attempts": 0}
        text = (
            "🔐 *AR HITTER V2 — Verification Code*\n\n"
            f"Your OTP code: `{otp}`\n\n"
            "⏱ Valid for 5 minutes. Enter it in the extension to continue.\n"
            "⚠️ Do not share this code with anyone."
        )
        try:
            send_bot_message(user_id, text, parse_mode="Markdown")
        except Exception as exc:
            OTPS.pop(user_id, None)
            json_response(self, 502, {"success": False, "error": str(exc)})
            return

        json_response(self, 200, {"success": True, "message": "OTP sent to Telegram"})

    def verify_otp(self, body):
        user_id = str(body.get("userId", "")).strip()
        otp = str(body.get("otp", "")).strip()
        record = OTPS.get(user_id)
        now = int(time.time())

        if not record:
            json_response(self, 400, {"success": False, "error": "No OTP requested"})
            return
        if record["expires"] < now:
            OTPS.pop(user_id, None)
            json_response(self, 400, {"success": False, "error": "OTP expired"})
            return
        if record["attempts"] >= 5:
            OTPS.pop(user_id, None)
            json_response(self, 429, {"success": False, "error": "Too many attempts"})
            return
        if otp != record["otp"]:
            record["attempts"] += 1
            json_response(self, 401, {"success": False, "error": "Invalid OTP"})
            return

        OTPS.pop(user_id, None)
        session = secrets.token_urlsafe(18)
        SESSIONS[session] = {"user_id": user_id, "expires": now + SESSION_TTL_SECONDS}
        user = get_user(user_id)
        upsert_user(user_id, user["username"], user["first_name"], increment_login=True)
        user = get_user(user_id)
        json_response(self, 200, {
            "success": True,
            "token": session,
            "user_id": user_id,
            "username": user["username"],
            "first_name": user["first_name"],
            "pfp_url": "",
            "hits": 0,
            "global_hits": 0,
            "user_hits": 0,
            "attempts": user["login_count"],
            "plan": user["plan"],
        })

    def validate_token(self, body):
        token = str(body.get("token", "")).strip()
        record = SESSIONS.get(token)
        if not record or record["expires"] < int(time.time()):
            SESSIONS.pop(token, None)
            json_response(self, 401, {"success": False, "error": "Invalid token"})
            return
        user = get_user(record["user_id"])
        if not user:
            json_response(self, 401, {"success": False, "error": "User is not registered"})
            return
        json_response(self, 200, {
            "success": True,
            "token": token,
            "user_id": user["telegram_id"],
            "username": user["username"],
            "first_name": user["first_name"],
            "pfp_url": "",
            "hits": 0,
            "global_hits": 0,
            "user_hits": 0,
            "attempts": user["login_count"],
            "plan": user["plan"],
        })

    def telegram_test(self, body):
        chat_id = str(body.get("chat_id", "")).strip()
        text = str(body.get("text", "AR Hitter local Telegram test")).strip()
        if not chat_id or not chat_id.lstrip("-").isdigit():
            json_response(self, 400, {"success": False, "error": "chat_id must be numeric"})
            return
        try:
            result = send_bot_message(chat_id, text[:4096])
            json_response(self, 200, {"success": True, "message_id": result.get("message_id")})
        except Exception as exc:
            json_response(self, 502, {"success": False, "error": str(exc)})


if __name__ == "__main__":
    init_db()
    print(f"Local OTP server listening on http://{HOST}:{PORT}")
    print("Telegram bot commands: /start /register /stats")
    print("Put your NEW Telegram bot token in server/.env as TELEGRAM_BOT_TOKEN=...")
    print("Press Ctrl+C to stop.")
    threading.Thread(target=telegram_poll_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

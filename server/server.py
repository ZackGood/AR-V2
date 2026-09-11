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
HOST = os.environ.get("RAILWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
OTP_TTL_SECONDS = 300
SESSION_TTL_SECONDS = 86400
ADMIN_TELEGRAM_ID = 5765501461

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
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
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
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                plan TEXT DEFAULT 'free',
                premium_until INTEGER DEFAULT 0,
                registered_at INTEGER NOT NULL,
                login_count INTEGER DEFAULT 0,
                last_verified_at INTEGER DEFAULT 0,
                revoked INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id TEXT PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                role TEXT DEFAULT 'admin',
                added_at INTEGER NOT NULL,
                added_by TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS license_keys (
                key TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                duration_days INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                uses INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_by TEXT
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS key_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                redeemed_at INTEGER NOT NULL,
                premium_until INTEGER NOT NULL
            )
        """)
        
        conn.commit()
        
        owner = conn.execute("SELECT * FROM admins WHERE telegram_id=?", (str(ADMIN_TELEGRAM_ID),)).fetchone()
        if not owner:
            conn.execute(
                "INSERT INTO admins(telegram_id, role, added_at, added_by, active) VALUES(?,?,?,?,?)",
                (str(ADMIN_TELEGRAM_ID), "owner", int(time.time()), "system", 1)
            )
            conn.commit()

def upsert_user(telegram_id, username="", first_name="", increment_login=False):
    now = int(time.time())
    telegram_id = str(telegram_id)
    with db() as conn:
        row = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if row:
            if increment_login:
                conn.execute(
                    "UPDATE users SET username=?, first_name=?, login_count=login_count+1, last_verified_at=? WHERE telegram_id=?",
                    (username, first_name, now, telegram_id)
                )
            else:
                conn.execute("UPDATE users SET username=?, first_name=? WHERE telegram_id=?", (username, first_name, telegram_id))
        else:
            conn.execute(
                "INSERT INTO users(telegram_id, username, first_name, plan, registered_at, active) VALUES(?,?,?,?,?,?)",
                (telegram_id, username, first_name, "free", now, 1)
            )
        conn.commit()

def get_user(telegram_id):
    telegram_id = str(telegram_id)
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()

def is_premium_active(user):
    if not user:
        return False
    now = int(time.time())
    return user["premium_until"] and user["premium_until"] > now

def get_admin(telegram_id):
    telegram_id = str(telegram_id)
    with db() as conn:
        return conn.execute("SELECT * FROM admins WHERE telegram_id=?", (telegram_id,)).fetchone()

def is_owner(telegram_id):
    admin = get_admin(telegram_id)
    return admin and admin["role"] == "owner"

def is_admin(telegram_id):
    admin = get_admin(telegram_id)
    return admin and admin["active"]

def format_date(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

def generate_license_key():
    return secrets.token_urlsafe(16)

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
    if length > 32000:
        raise ValueError("Request too large")
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode("utf-8"))

def send_bot_message(chat_id, text, **kwargs):
    payload = {"chat_id": chat_id, "text": text}
    payload.update(kwargs)
    return telegram_call("sendMessage", payload)

def bot_stats_text(user):
    is_prem = is_premium_active(user)
    status = "Active" if is_prem else "Inactive"
    plan_display = "Premium" if is_prem else "Free"
    
    text = (
        "📊 *Your Stats — AR Hitter V2*\n\n"
        f"🆔 ID: `{user['telegram_id']}`\n"
        f"👤 Username: @{user['username'] if user['username'] else 'not_set'}\n"
        f"✅ Logins: **{user['login_count']}**\n"
        "📅 Registered: " + format_date(user["registered_at"]) + "\n"
        f"💳 Plan: **{plan_display}**\n"
        f"Status: {status}"
    )
    
    if is_prem:
        expires = format_date(user["premium_until"])
        text += f"\n⏰ Expires: {expires}"
    
    return text

def handle_bot_message(msg):
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = str(chat.get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    
    username = sender.get("username") or ""
    first_name = sender.get("first_name") or ""

    parts = text.split(None, 1) if text else [""]
    command = parts[0].split("@")[0].lower() if parts[0].startswith("/") else ""
    args = parts[1] if len(parts) > 1 else ""

    if command == "/start":
        user = get_user(chat_id)
        if user:
            send_bot_message(
                chat_id,
                "✅ Welcome! Use /stats to view your account, /redeem to activate Premium, or /register to refresh.",
                parse_mode="Markdown"
            )
        else:
            send_bot_message(
                chat_id,
                "👋 *AR HITTER V2*\n\nWelcome! Send /register to create your Free account.",
                parse_mode="Markdown"
            )
        return

    if command == "/register":
        upsert_user(chat_id, username, first_name, increment_login=False)
        send_bot_message(
            chat_id,
            "✅ Registration complete!\n\nPlan: *Free*\n\nUse /stats to see your account. Premium is required for OTP.",
            parse_mode="Markdown"
        )
        return

    if command == "/stats":
        user = get_user(chat_id)
        if not user:
            send_bot_message(chat_id, "❌ You are not registered. Send /register first.")
        else:
            send_bot_message(chat_id, bot_stats_text(user), parse_mode="Markdown")
        return

    if command == "/redeem":
        key = args.strip()
        if not key:
            send_bot_message(chat_id, "Usage: /redeem <KEY>")
            return
        
        user = get_user(chat_id)
        if not user:
            send_bot_message(chat_id, "❌ You are not registered. Send /register first.")
            return
        
        with db() as conn:
            k = conn.execute("SELECT * FROM license_keys WHERE key=?", (key,)).fetchone()
            
            if not k:
                send_bot_message(chat_id, "❌ Invalid license key.")
                return
            
            if k["status"] == "revoked":
                send_bot_message(chat_id, "❌ This license key has been revoked.")
                return
            
            if k["status"] == "expired":
                send_bot_message(chat_id, "❌ This license key has expired.")
                return
            
            if k["uses"] >= k["max_uses"]:
                conn.execute("UPDATE license_keys SET status='exhausted' WHERE key=?", (key,))
                conn.commit()
                send_bot_message(chat_id, "❌ This license key has no remaining uses.")
                return
            
            existing_redemption = conn.execute(
                "SELECT * FROM key_redemptions WHERE key=? AND telegram_id=?",
                (key, chat_id)
            ).fetchone()
            
            if existing_redemption:
                send_bot_message(chat_id, "❌ You have already redeemed this key.")
                return
            
            now = int(time.time())
            current_premium = user["premium_until"] if user["premium_until"] else now
            new_premium_until = max(current_premium, now) + (k["duration_days"] * 86400)
            
            conn.execute(
                "INSERT INTO key_redemptions(key, telegram_id, redeemed_at, premium_until) VALUES(?,?,?,?)",
                (key, chat_id, now, new_premium_until)
            )
            
            conn.execute("UPDATE license_keys SET uses=uses+1 WHERE key=?", (key,))
            
            conn.execute(
                "UPDATE users SET plan='premium', premium_until=? WHERE telegram_id=?",
                (new_premium_until, chat_id)
            )
            
            conn.commit()
        
        expires = format_date(new_premium_until)
        send_bot_message(
            chat_id,
            f"✅ License redeemed successfully.\n\n"
            f"Plan: Premium\n"
            f"Duration: {k['duration_days']} days\n"
            f"Premium until: {expires}",
            parse_mode="Markdown"
        )
        return

    if not is_admin(chat_id):
        send_bot_message(chat_id, "❌ Unauthorized command.")
        return

    if not is_owner(chat_id) and command in ["/addadmin", "/removeadmin", "/admins", "/key", "/keys", "/revokekey"]:
        send_bot_message(chat_id, "❌ Only the owner can use this command.")
        return

    if command == "/key":
        parts_args = args.split()
        if len(parts_args) < 2:
            send_bot_message(chat_id, "Usage: /key <days> <uses>")
            return
        try:
            days = int(parts_args[0])
            uses = int(parts_args[1])
        except ValueError:
            send_bot_message(chat_id, "❌ Days and uses must be integers.")
            return
        
        key = generate_license_key()
        now = int(time.time())
        with db() as conn:
            conn.execute(
                "INSERT INTO license_keys(key, created_at, duration_days, max_uses, uses, status, created_by) VALUES(?,?,?,?,?,?,?)",
                (key, now, days, uses, 0, "active", chat_id)
            )
            conn.commit()
        
        send_bot_message(chat_id, f"✅ Key: `{key}`", parse_mode="Markdown")
        return

    if command == "/keys":
        with db() as conn:
            keys = conn.execute("SELECT * FROM license_keys ORDER BY created_at DESC LIMIT 20").fetchall()
        
        if not keys:
            send_bot_message(chat_id, "No keys found.")
            return
        
        text = "🗝️ *License Keys*\n\n"
        for k in keys:
            text += f"`{k['key'][:16]}...` | {k['duration_days']}d | {k['uses']}/{k['max_uses']} | {k['status']}\n"
        
        send_bot_message(chat_id, text, parse_mode="Markdown")
        return

    if command == "/revokekey":
        key = args.strip()
        if not key:
            send_bot_message(chat_id, "Usage: /revokekey <key>")
            return
        
        with db() as conn:
            k = conn.execute("SELECT * FROM license_keys WHERE key=?", (key,)).fetchone()
            if not k:
                send_bot_message(chat_id, "❌ Key not found.")
                return
            conn.execute("UPDATE license_keys SET status='revoked' WHERE key=?", (key,))
            conn.commit()
        
        send_bot_message(chat_id, f"✅ Key revoked: `{key[:16]}...`", parse_mode="Markdown")
        return

    if command == "/users":
        with db() as conn:
            users = conn.execute("SELECT * FROM users ORDER BY registered_at DESC LIMIT 20").fetchall()
        
        if not users:
            send_bot_message(chat_id, "No users.")
            return
        
        text = "👥 *Users*\n\n"
        for u in users:
            is_prem = is_premium_active(u)
            plan = "Premium" if is_prem else "Free"
            text += f"`{u['telegram_id']}` | @{u['username'] or 'n/a'} | {plan}\n"
        
        send_bot_message(chat_id, text, parse_mode="Markdown")
        return

    if command == "/givepremium":
        parts_args = args.split()
        if len(parts_args) < 2:
            send_bot_message(chat_id, "Usage: /givepremium <id> <days>")
            return
        
        user_id = str(parts_args[0])
        try:
            days = int(parts_args[1])
        except ValueError:
            send_bot_message(chat_id, "❌ Days must be integer.")
            return
        
        user = get_user(user_id)
        if not user:
            send_bot_message(chat_id, "❌ User not found.")
            return
        
        now = int(time.time())
        premium_until = now + (days * 86400)
        
        with db() as conn:
            conn.execute(
                "UPDATE users SET plan='premium', premium_until=? WHERE telegram_id=?",
                (premium_until, user_id)
            )
            conn.commit()
        
        send_bot_message(chat_id, f"✅ Premium given: {user_id} for {days} days")
        return

    if command == "/removepremium":
        user_id = str(args.strip())
        if not user_id:
            send_bot_message(chat_id, "Usage: /removepremium <id>")
            return
        
        user = get_user(user_id)
        if not user:
            send_bot_message(chat_id, "❌ User not found.")
            return
        
        with db() as conn:
            conn.execute("UPDATE users SET plan='free', premium_until=0 WHERE telegram_id=?", (user_id,))
            conn.commit()
        
        send_bot_message(chat_id, f"✅ Premium removed: {user_id}")
        return

    if command == "/revoke":
        user_id = str(args.strip())
        if not user_id:
            send_bot_message(chat_id, "Usage: /revoke <id>")
            return
        
        user = get_user(user_id)
        if not user:
            send_bot_message(chat_id, "❌ User not found.")
            return
        
        with db() as conn:
            conn.execute("UPDATE users SET revoked=1, active=0 WHERE telegram_id=?", (user_id,))
            conn.commit()
        
        for session, record in list(SESSIONS.items()):
            if record["user_id"] == user_id:
                SESSIONS.pop(session, None)
        
        send_bot_message(chat_id, f"✅ User revoked: {user_id}")
        return

    if command == "/addadmin":
        admin_id = str(args.strip())
        if not admin_id:
            send_bot_message(chat_id, "Usage: /addadmin <id>")
            return
        
        existing_admin = get_admin(admin_id)
        if existing_admin:
            send_bot_message(chat_id, "❌ Already admin.")
            return
        
        user = get_user(admin_id)
        user_username = user["username"] if user else ""
        
        now = int(time.time())
        with db() as conn:
            conn.execute(
                "INSERT INTO admins(telegram_id, username, role, added_at, added_by, active) VALUES(?,?,?,?,?,?)",
                (admin_id, user_username, "admin", now, chat_id, 1)
            )
            conn.commit()
        
        send_bot_message(chat_id, f"✅ Admin added: {admin_id}")
        return

    if command == "/removeadmin":
        admin_id = str(args.strip())
        if not admin_id:
            send_bot_message(chat_id, "Usage: /removeadmin <id>")
            return
        
        if str(admin_id) == str(ADMIN_TELEGRAM_ID):
            send_bot_message(chat_id, "❌ Cannot remove owner.")
            return
        
        existing_admin = get_admin(admin_id)
        if not existing_admin:
            send_bot_message(chat_id, "❌ Not admin.")
            return
        
        with db() as conn:
            conn.execute("DELETE FROM admins WHERE telegram_id=?", (admin_id,))
            conn.commit()
        
        send_bot_message(chat_id, f"✅ Admin removed: {admin_id}")
        return

    if command == "/admins":
        with db() as conn:
            admins = conn.execute("SELECT * FROM admins ORDER BY added_at DESC").fetchall()
        
        if not admins:
            send_bot_message(chat_id, "No admins.")
            return
        
        text = "👮 *Admins*\n\n"
        for admin in admins:
            role_icon = "👑" if admin["role"] == "owner" else "🛡"
            username_display = f"@{admin['username']}" if admin['username'] else "No username"
            text += f"{role_icon} {admin['role'].upper()} | {username_display} | ID: {admin['telegram_id']}\n"
        
        send_bot_message(chat_id, text, parse_mode="Markdown")
        return

    send_bot_message(chat_id, "❌ Unknown command.")

def telegram_poll_loop():
    global OFFSET
    print("Telegram bot polling enabled.")
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
    server_version = "ARHitterOTP/2.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}")

    def do_OPTIONS(self):
        json_response(self, 204, {})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            json_response(self, 200, {"success": True, "service": "ar-hitter-otp", "port": PORT})
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
        user = get_user(user_id)
        if not user:
            json_response(self, 403, {
                "success": False,
                "registered": False,
                "error": "Not registered. Open bot and send /register."
            })
            return
        json_response(self, 200, {
            "success": True,
            "registered": True,
            "plan": user["plan"],
            "username": user["username"],
            "first_name": user["first_name"]
        })

    def request_otp(self, body):
        user_id = str(body.get("userId", "")).strip()
        if not user_id or not user_id.lstrip("-").isdigit():
            json_response(self, 400, {"success": False, "error": "userId must be numeric"})
            return
        
        user = get_user(user_id)
        if not user:
            json_response(self, 403, {"success": False, "error": "Not registered. Open bot and send /register."})
            return
        
        if not user["active"] or user["revoked"]:
            json_response(self, 403, {"success": False, "error": "Account revoked."})
            return
        
        if not is_premium_active(user):
            json_response(self, 403, {"success": False, "error": "Premium is required to request an OTP."})
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
        
        user = get_user(user_id)
        if not is_premium_active(user):
            json_response(self, 403, {"success": False, "error": "Premium expired."})
            return
        
        session = secrets.token_urlsafe(18)
        SESSIONS[session] = {"user_id": user_id, "expires": now + SESSION_TTL_SECONDS}
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
        if not user or not user["active"] or user["revoked"]:
            json_response(self, 401, {"success": False, "error": "User not valid"})
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
        text = str(body.get("text", "AR Hitter test")).strip()
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
    print(f"AR Hitter OTP server listening on http://{HOST}:{PORT}")
    print("Premium + License + Admin system active")
    threading.Thread(target=telegram_poll_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


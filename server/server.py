import json, os, secrets, sqlite3, threading, time, urllib.request, urllib.error, random
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DB_FILE = ROOT / "users.sqlite3"
HOST = os.environ.get("RAILWAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
OTP_TTL = 300
SESSION_TTL = 86400
OWNER_ID = 5765501461

WELCOME_IMAGES = [
    "https://img.freepik.com/premium-vector/welcome-banner-design_605505-20.jpg",
    "https://img.freepik.com/free-vector/welcome-concept-illustration_114360-2356.jpg",
    "https://images.unsplash.com/photo-1557821552-17105176677c?w=400",
]

def load_env(path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
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
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram error"))
    return data.get("result")

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def migrate_columns(conn, table, columns):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            telegram_id TEXT PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '',
            plan TEXT DEFAULT 'free', premium_until INTEGER DEFAULT 0, registered_at INTEGER NOT NULL,
            login_count INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0, active INTEGER DEFAULT 1)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS admins (
            telegram_id TEXT PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '',
            role TEXT DEFAULT 'admin', added_at INTEGER NOT NULL, added_by TEXT, active INTEGER DEFAULT 1)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS license_keys (
            key TEXT PRIMARY KEY, created_at INTEGER NOT NULL, duration_days INTEGER NOT NULL,
            max_uses INTEGER NOT NULL, uses INTEGER DEFAULT 0, status TEXT DEFAULT 'active', created_by TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS key_redemptions (
            id INTEGER PRIMARY KEY, key TEXT, telegram_id TEXT, redeemed_at INTEGER, premium_until INTEGER)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            name TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')""")
        migrate_columns(conn, "users", {
            "username": "TEXT DEFAULT ''", "first_name": "TEXT DEFAULT ''",
            "plan": "TEXT DEFAULT 'free'", "premium_until": "INTEGER DEFAULT 0",
            "registered_at": "INTEGER DEFAULT 0", "login_count": "INTEGER DEFAULT 0",
            "revoked": "INTEGER DEFAULT 0", "active": "INTEGER DEFAULT 1"})
        migrate_columns(conn, "admins", {
            "username": "TEXT DEFAULT ''", "first_name": "TEXT DEFAULT ''",
            "role": "TEXT DEFAULT 'admin'", "added_at": "INTEGER DEFAULT 0",
            "added_by": "TEXT DEFAULT ''", "active": "INTEGER DEFAULT 1"})
        migrate_columns(conn, "license_keys", {
            "created_at": "INTEGER DEFAULT 0", "duration_days": "INTEGER DEFAULT 0",
            "max_uses": "INTEGER DEFAULT 0", "uses": "INTEGER DEFAULT 0",
            "status": "TEXT DEFAULT 'active'", "created_by": "TEXT DEFAULT ''"})
        migrate_columns(conn, "key_redemptions", {
            "key": "TEXT DEFAULT ''", "telegram_id": "TEXT DEFAULT ''",
            "redeemed_at": "INTEGER DEFAULT 0", "premium_until": "INTEGER DEFAULT 0"})
        migrate_columns(conn, "users", {
            "telegram_id": "TEXT DEFAULT ''",
            "username": "TEXT DEFAULT ''", "first_name": "TEXT DEFAULT ''",
            "plan": "TEXT DEFAULT 'free'", "premium_until": "INTEGER DEFAULT 0",
            "registered_at": "INTEGER DEFAULT 0", "login_count": "INTEGER DEFAULT 0",
            "revoked": "INTEGER DEFAULT 0", "active": "INTEGER DEFAULT 1",
        })
        migrate_columns(conn, "admins", {
            "telegram_id": "TEXT DEFAULT ''",
            "username": "TEXT DEFAULT ''", "first_name": "TEXT DEFAULT ''",
            "role": "TEXT DEFAULT 'admin'", "added_at": "INTEGER DEFAULT 0",
            "added_by": "TEXT DEFAULT ''", "active": "INTEGER DEFAULT 1",
        })
        migrate_columns(conn, "license_keys", {
            "key": "TEXT DEFAULT ''",
            "created_at": "INTEGER DEFAULT 0", "duration_days": "INTEGER DEFAULT 0",
            "max_uses": "INTEGER DEFAULT 0", "uses": "INTEGER DEFAULT 0",
            "status": "TEXT DEFAULT 'active'", "created_by": "TEXT DEFAULT ''",
        })
        migrate_columns(conn, "key_redemptions", {
            "id": "INTEGER DEFAULT 0",
            "key": "TEXT DEFAULT ''", "telegram_id": "TEXT DEFAULT ''",
            "redeemed_at": "INTEGER DEFAULT 0", "premium_until": "INTEGER DEFAULT 0",
        })
        conn.commit()
        owner = conn.execute("SELECT telegram_id FROM admins WHERE telegram_id=?", (str(OWNER_ID),)).fetchone()
        if not owner:
            conn.execute("INSERT INTO admins(telegram_id, role, added_at, added_by, active) VALUES(?,?,?,?,1)",
                         (str(OWNER_ID), "owner", int(time.time()), "system"))
        else:
            conn.execute("UPDATE admins SET role='owner', active=1 WHERE telegram_id=?", (str(OWNER_ID),))
        conn.commit()

def get_setting(name, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
    return row["value"] if row else default

def set_setting(name, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)", (name, value))
        conn.commit()

def upsert_user(telegram_id, username="", first_name="", increment_login=False):
    telegram_id = str(telegram_id)
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        if row:
            if increment_login:
                conn.execute("UPDATE users SET username=?, first_name=?, login_count=login_count+1 WHERE telegram_id=?",
                            (username, first_name, telegram_id))
            else:
                conn.execute("UPDATE users SET username=?, first_name=? WHERE telegram_id=?", (username, first_name, telegram_id))
        else:
            conn.execute("INSERT INTO users(telegram_id, username, first_name, plan, registered_at, active) VALUES(?,?,?,?,?,?)",
                        (telegram_id, username, first_name, "free", now, 1))
        conn.commit()

def get_user(telegram_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE telegram_id=?", (str(telegram_id),)).fetchone()

def is_premium(user):
    return bool(user and user["active"] and not user["revoked"] and
                user["premium_until"] and user["premium_until"] > int(time.time()))

def premium_error(user):
    if not user:
        return "You must register before requesting an OTP."
    if user["revoked"] or not user["active"]:
        return "Your account is inactive. Contact an administrator."
    if user["premium_until"] and user["premium_until"] <= int(time.time()):
        return "Premium access is required for OTP. Contact @ZackZ10 or @kiora_AR to purchase/activate Premium."
    return "Premium access is required for OTP. Contact @ZackZ10 or @kiora_AR to purchase/activate Premium."

def expire_premium(user):
    if user and user["premium_until"] and user["premium_until"] <= int(time.time()) and user["plan"] != "free":
        with db() as conn:
            conn.execute("UPDATE users SET plan='free' WHERE telegram_id=?", (user["telegram_id"],))
            conn.commit()

def premium_remaining(until):
    remaining = max(0, int(until) - int(time.time()))
    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m"

def get_admin(telegram_id):
    with db() as conn:
        return conn.execute("SELECT * FROM admins WHERE telegram_id=?", (str(telegram_id),)).fetchone()

def is_admin(telegram_id):
    if str(telegram_id) == str(OWNER_ID):
        return True
    admin = get_admin(telegram_id)
    return admin and admin["active"]

def is_owner(telegram_id):
    if str(telegram_id) == str(OWNER_ID):
        return True
    admin = get_admin(telegram_id)
    return admin and admin["role"] == "owner"

def account_role(telegram_id):
    if str(telegram_id) == str(OWNER_ID):
        return "Owner"
    admin = get_admin(telegram_id)
    return "Admin" if admin and admin["active"] else "User"

def format_user_record(user):
    expire_premium(user)
    user = get_user(user["telegram_id"])
    plan = "Premium" if is_premium(user) else "Free"
    status = "Active" if user["active"] and not user["revoked"] else "Revoked"
    line = f"`{user['telegram_id']}` | @{user['username'] or 'n/a'}\n"
    line += f"Role: {account_role(user['telegram_id'])} | Plan: {plan} | Status: {status}\n"
    if is_premium(user):
        expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(user["premium_until"]))
        line += f"Premium until: {expires} ({premium_remaining(user['premium_until'])})\n"
    return line

def send_msg(chat_id, text, keyboard=None, photo=None, **kw):
    payload = {"chat_id": chat_id}
    if photo:
        payload["photo"] = photo
        payload["caption"] = text[:1024]
    else:
        payload["text"] = text[:4096]
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    payload.update(kw)
    if photo:
        return telegram_call("sendPhoto", payload)
    return telegram_call("sendMessage", payload)

def edit_msg(chat_id, msg_id, text, keyboard=None, **kw):
    payload = {"chat_id": chat_id, "message_id": msg_id, "text": text[:4096]}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    payload.update(kw)
    try:
        return telegram_call("editMessageText", payload)
    except RuntimeError as error:
        if "text" not in str(error).lower() and "caption" not in str(error).lower():
            raise
        payload["caption"] = payload.pop("text")[:1024]
        return telegram_call("editMessageCaption", payload)

def answer_callback(callback_id, text="", alert=False):
    telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": alert})

def handle_command(chat_id, username, first_name, command, args):
    if command == "/settings":
        if not is_owner(chat_id):
            send_msg(chat_id, "❌ Owner only")
            return True
        if len(args) != 2 or args[0] not in ("community_url", "required_chats", "welcome_text", "welcome_image", "join_gate"):
            send_msg(chat_id, "Usage: /settings <community_url|required_chats|welcome_text|welcome_image|join_gate> <value>")
            return True
        name, value = args
        if name == "join_gate" and value.lower() not in ("on", "off"):
            send_msg(chat_id, "❌ join_gate must be on or off")
            return True
        if name in ("community_url", "welcome_image"):
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                send_msg(chat_id, "❌ Use a valid http(s) URL")
                return True
        if name == "required_chats":
            if value.lower() in ("off", "clear", "none"):
                value = ""
            else:
                for item in value.split(","):
                    parts = item.split("|", 1)
                    if not parts[0] or len(parts) != 2:
                        send_msg(chat_id, "❌ Format: chat_id|https://join-url[,chat_id|https://join-url]")
                        return True
                    parsed = urlparse(parts[1])
                    if parsed.scheme not in ("http", "https") or not parsed.netloc:
                        send_msg(chat_id, "❌ Each required chat needs a valid join URL")
                        return True
        if name == "welcome_text" and len(value) > 500:
            send_msg(chat_id, "❌ Welcome text is limited to 500 characters")
            return True
        set_setting(name, value)
        send_msg(chat_id, f"✅ Setting updated: {name}")
        return True

    if command == "/stats":
        user = get_user(chat_id)
        if not user:
            send_msg(chat_id, "❌ Not registered. Use /register")
            return True
        text = f"📊 *Stats*\n\n🆔 `{user['telegram_id']}`\n👤 @{user['username'] or 'n/a'}\n✅ Logins: {user['login_count']}\n📅 Registered: {time.strftime('%Y-%m-%d', time.localtime(user['registered_at']))}"
        send_msg(chat_id, text, parse_mode="Markdown")
        return True

    if command == "/otp":
        user = get_user(chat_id)
        expire_premium(user)
        if not is_premium(user):
            send_msg(chat_id, f"❌ {premium_error(user)}")
        else:
            otp = f"{secrets.randbelow(1_000_000):06d}"
            OTPS[chat_id] = {"otp": otp, "expires": int(time.time()) + OTP_TTL, "attempts": 0}
            send_msg(chat_id, f"🔐 *OTP Code*\n\n`{otp}`\n\n⏱ Valid for 5 minutes", parse_mode="Markdown")
        return True

    if command == "/redeem":
        if args:
            set_user_state(chat_id, action="redeem_key")
            handle_message({"chat": {"id": chat_id}, "from": {"username": username, "first_name": first_name},
                            "text": args[0]})
        else:
            send_msg(chat_id, "🎟️ Send me the license key to redeem (or /cancel)")
            set_user_state(chat_id, action="redeem_key")
        return True

    if command in ("/key", "/keys", "/revokekey", "/users", "/givepremium", "/removepremium",
                   "/revoke", "/addadmin", "/removeadmin", "/admins"):
        if not is_admin(chat_id):
            send_msg(chat_id, "❌ Unauthorized")
            return True

    if command == "/key":
        if len(args) not in (1, 2):
            send_msg(chat_id, "Usage: /key <days> [uses]")
            return True
        try:
            days = int(args[0])
            uses = int(args[1]) if len(args) == 2 else 1
            if days <= 0 or uses <= 0:
                raise ValueError
        except ValueError:
            send_msg(chat_id, "❌ Days and uses must be positive integers")
            return True
        key = secrets.token_urlsafe(16)
        with db() as conn:
            conn.execute("INSERT INTO license_keys(key, created_at, duration_days, max_uses, uses, status, created_by) VALUES(?,?,?,?,?,?,?)",
                         (key, int(time.time()), days, uses, 0, "active", chat_id))
            conn.commit()
        send_msg(chat_id, f"🔑 Key Generated:\n\n`{key}`", parse_mode="Markdown")
        return True

    if command == "/keys":
        with db() as conn:
            keys = conn.execute("SELECT * FROM license_keys ORDER BY created_at DESC LIMIT 50").fetchall()
        text = "🗝️ *License Keys*\n\n" + "".join(
            f"`{key['key'][:16]}...` | {key['duration_days']}d | {key['uses']}/{key['max_uses']} | {key['status']}\n"
            for key in keys
        )
        send_msg(chat_id, text if keys else "🗝️ No license keys", parse_mode="Markdown")
        return True

    if command == "/revokekey":
        if len(args) != 1:
            send_msg(chat_id, "Usage: /revokekey <key>")
            return True
        with db() as conn:
            result = conn.execute("UPDATE license_keys SET status='revoked' WHERE key=? AND status='active'", (args[0],))
            conn.commit()
        send_msg(chat_id, "✅ Key revoked" if result.rowcount else "❌ Active key not found")
        return True

    if command == "/users":
        with db() as conn:
            users = conn.execute("SELECT * FROM users ORDER BY registered_at DESC LIMIT 50").fetchall()
        text = "👥 *Users*\n\n" + "".join(format_user_record(user) for user in users)
        send_msg(chat_id, text if users else "👥 No users", parse_mode="Markdown")
        return True

    if command == "/givepremium":
        if len(args) != 2:
            send_msg(chat_id, "Usage: /givepremium <telegram_id> <days>")
            return True
        try:
            days = int(args[1])
            if days <= 0:
                raise ValueError
        except ValueError:
            send_msg(chat_id, "❌ Days must be a positive integer")
            return True
        user = get_user(args[0])
        if not user:
            send_msg(chat_id, "❌ User not found")
            return True
        premium_until = int(time.time()) + days * 86400
        with db() as conn:
            conn.execute("UPDATE users SET plan='premium', premium_until=? WHERE telegram_id=?", (premium_until, args[0]))
            conn.commit()
        send_msg(chat_id, f"✅ Premium given to {args[0]} for {days} days")
        return True

    if command == "/removepremium":
        if len(args) != 1:
            send_msg(chat_id, "Usage: /removepremium <telegram_id>")
            return True
        with db() as conn:
            result = conn.execute("UPDATE users SET plan='free', premium_until=0 WHERE telegram_id=?", (args[0],))
            conn.commit()
        send_msg(chat_id, "✅ Premium removed" if result.rowcount else "❌ User not found")
        return True

    if command == "/revoke":
        if len(args) != 1:
            send_msg(chat_id, "Usage: /revoke <telegram_id>")
            return True
        if str(args[0]) == str(OWNER_ID):
            send_msg(chat_id, "❌ Cannot revoke owner")
            return True
        with db() as conn:
            result = conn.execute("UPDATE users SET revoked=1, active=0 WHERE telegram_id=?", (args[0],))
            conn.commit()
        send_msg(chat_id, "✅ User revoked" if result.rowcount else "❌ User not found")
        return True

    if command == "/addadmin":
        if not is_owner(chat_id) or len(args) != 1:
            send_msg(chat_id, "❌ Owner only. Usage: /addadmin <telegram_id>")
            return True
        target = get_user(args[0])
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO admins(telegram_id, username, first_name, role, added_at, added_by, active) VALUES(?,?,?,?,?,?,1)",
                         (args[0], target["username"] if target else "", target["first_name"] if target else "",
                          "admin", int(time.time()), chat_id))
            conn.commit()
        send_msg(chat_id, "✅ Admin added")
        return True

    if command == "/removeadmin":
        if not is_owner(chat_id) or len(args) != 1:
            send_msg(chat_id, "❌ Owner only. Usage: /removeadmin <telegram_id>")
            return True
        if str(args[0]) == str(OWNER_ID):
            send_msg(chat_id, "❌ Cannot remove owner")
            return True
        with db() as conn:
            result = conn.execute("UPDATE admins SET active=0 WHERE telegram_id=?", (args[0],))
            conn.commit()
        send_msg(chat_id, "✅ Admin removed" if result.rowcount else "❌ Admin not found")
        return True

    if command == "/admins":
        if not is_owner(chat_id):
            send_msg(chat_id, "❌ Owner only")
            return True
        with db() as conn:
            admins = conn.execute("SELECT * FROM admins WHERE active=1 ORDER BY added_at DESC").fetchall()
        text = "👮 *Admins*\n\n" + "".join(
            f"{'👑' if admin['role'] == 'owner' else '🛡'} {admin['role']} | ID: {admin['telegram_id']}\n"
            for admin in admins
        )
        send_msg(chat_id, text, parse_mode="Markdown")
        return True

    return False

OTPS = {}
SESSIONS = {}
OFFSET = None
USER_STATE = {}

def get_user_state(user_id):
    return USER_STATE.get(str(user_id), {})

def set_user_state(user_id, **kw):
    USER_STATE[str(user_id)] = {**get_user_state(user_id), **kw}

def required_chats():
    result = []
    for item in get_setting("required_chats").split(","):
        item = item.strip()
        if item:
            parts = item.split("|", 1)
            result.append((parts[0], parts[1] if len(parts) == 2 else ""))
    return result

def has_required_membership(chat_id):
    if str(chat_id) == str(OWNER_ID):
        return True
    for required_id, _ in required_chats():
        member = telegram_call("getChatMember", {"chat_id": required_id, "user_id": chat_id})
        if member.get("status") not in ("creator", "administrator", "member"):
            return False
    return True

def membership_panel(chat_id):
    keyboard = []
    for _, url in required_chats():
        if url:
            keyboard.append([{"text": "Join required channel", "url": url}])
    keyboard.append([{"text": "✅ Verify membership", "callback_data": "verify_membership"}])
    send_msg(chat_id, "Please join the required channels, then tap Verify membership.", keyboard)

def show_user_panel(chat_id, msg_id=None):
    user = get_user(chat_id)
    if not user:
        text = "❌ Not registered. Use /register"
        if msg_id:
            edit_msg(chat_id, msg_id, text)
        else:
            send_msg(chat_id, text)
        return
    
    expire_premium(user)
    user = get_user(chat_id)
    is_prem = is_premium(user)
    display_name = user["first_name"] or user["username"] or "User"
    text = (f"👋 *Welcome, {display_name}!*\n\n"
            f"💎 Plan: *{'Premium' if is_prem else 'Free'}*\n"
            f"🛡️ Role: *{account_role(chat_id)}*")
    custom_welcome = get_setting("welcome_text")
    if custom_welcome:
        text = f"{custom_welcome}\n\n{text}"
    if is_prem:
        expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(user["premium_until"]))
        text += f"\n✅ Status: Active\n⏰ Expires: {expires}\n⏳ Remaining: {premium_remaining(user['premium_until'])}"
    else:
        if user["premium_until"]:
            text += "\n⚠️ Premium expired. OTP is blocked.\nContact @ZackZ10 or @kiora_AR to activate Premium."
    
    keyboard = [
        [{"text": "💎 Premium Status", "callback_data": "premium_status"}],
        [{"text": "ℹ️ OTP Info", "callback_data": "otp_info"},
         {"text": "📊 My Stats", "callback_data": "user_stats"}],
        [{"text": "🎟️ Redeem Key", "callback_data": "redeem_key"},
         {"text": "🔐 Request OTP", "callback_data": "request_otp"}],
    ]
    if is_admin(chat_id):
        keyboard.append([{"text": "🛠️ Admin Panel", "callback_data": "admin_panel"}])
    
    try:
        if msg_id:
            edit_msg(chat_id, msg_id, text, keyboard, parse_mode="Markdown")
            return
        image = get_setting("welcome_image") or WELCOME_IMAGES[0]
        send_msg(chat_id, text, keyboard, photo=image, parse_mode="Markdown")
    except Exception as error:
        print(f"Welcome photo failed: {error}")
        send_msg(chat_id, text, keyboard, parse_mode="Markdown")

def show_admin_panel(chat_id, msg_id=None):
    if not is_admin(chat_id):
        send_msg(chat_id, "❌ Unauthorized", parse_mode="Markdown")
        return
    
    text = "🛠️ *Admin Panel*"
    keyboard = [
        [{"text": "👥 List Users", "callback_data": "admin_users_0"}],
        [{"text": "🔑 Generate Key", "callback_data": "admin_genkey"}],
        [{"text": "🗝️ List Keys", "callback_data": "admin_keys_0"}],
    ]
    
    if is_owner(chat_id):
        keyboard.extend([
            [{"text": "👮 Manage Admins", "callback_data": "admin_admins_0"}],
            [{"text": "⭐ Give Premium", "callback_data": "admin_giveprem"}],
        ])
    
    if msg_id:
        edit_msg(chat_id, msg_id, text, keyboard, parse_mode="Markdown")
    else:
        send_msg(chat_id, text, keyboard, parse_mode="Markdown")

def handle_callback(callback_id, from_id, msg_id, chat_id, data):
    user = get_user(chat_id)

    if data == "verify_membership":
        if not has_required_membership(chat_id):
            answer_callback(callback_id, "Join all required channels first.", alert=True)
            return
        if not user:
            upsert_user(chat_id)
        show_user_panel(chat_id, msg_id)
        answer_callback(callback_id, "Membership verified")
        return

    if data == "settings":
        if not is_owner(from_id):
            answer_callback(callback_id, "❌ Owner only", alert=True)
            return
        gate = "ON" if get_setting("join_gate") == "on" else "OFF"
        edit_msg(chat_id, msg_id, "⚙️ *Owner Settings*\n\n"
                 "Use `/settings name value` to update persistent settings.\n"
                 f"Required-channel gate: *{gate}*\n"
                 "Supported: community_url, required_chats, welcome_text, welcome_image, join_gate",
                 [[{"text": "Enable Join Gate", "callback_data": "settings_gate_on"},
                   {"text": "Disable Join Gate", "callback_data": "settings_gate_off"}],
                  [{"text": "← Back", "callback_data": "back_main"}]], parse_mode="Markdown")
        answer_callback(callback_id)
        return

    if data in ("settings_gate_on", "settings_gate_off"):
        if not is_owner(from_id):
            answer_callback(callback_id, "❌ Owner only", alert=True)
            return
        set_setting("join_gate", "on" if data.endswith("_on") else "off")
        edit_msg(chat_id, msg_id, "✅ Required-channel gate updated.",
                 [[{"text": "← Back", "callback_data": "back_main"}]], parse_mode="Markdown")
        answer_callback(callback_id)
        return

    if data == "premium_status":
        if not user:
            answer_callback(callback_id, "Not registered", alert=True)
            return
        expire_premium(user)
        user = get_user(chat_id)
        if is_premium(user):
            expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(user["premium_until"]))
            text = (f"💎 *Premium Status*\n\n✅ Premium / Active\n"
                    f"⏰ Expires: {expires}\n⏳ Remaining: {premium_remaining(user['premium_until'])}\n"
                    f"🛡️ Role: {account_role(chat_id)}")
        else:
            text = "💎 *Premium Status*\n\n"
            if user["premium_until"]:
                text += "⚠️ Premium expired.\nOTP is blocked.\n"
            else:
                text += "ℹ️ Free / Inactive.\n"
            text += "Premium access is required for OTP. Contact @ZackZ10 or @kiora_AR to purchase/activate Premium."
            text += f"\n🛡️ Role: {account_role(chat_id)}"
        edit_msg(chat_id, msg_id, text, [[{"text": "← Back", "callback_data": "back_main"}]], parse_mode="Markdown")
        answer_callback(callback_id)
        return

    if data == "otp_info":
        edit_msg(chat_id, msg_id, "ℹ️ *OTP Info*\n\nPremium is required. Each OTP is valid for 5 minutes "
                 "and can be used once. A successful verification creates a 24-hour session.",
                 [[{"text": "← Back", "callback_data": "back_main"}]], parse_mode="Markdown")
        answer_callback(callback_id)
        return
    
    if data == "user_stats":
        if not user:
            answer_callback(callback_id, "Not registered", alert=True)
            return
        text = f"📊 *Stats*\n\n🆔 `{user['telegram_id']}`\n👤 @{user['username'] or 'n/a'}\n✅ Logins: {user['login_count']}\n📅 Registered: {time.strftime('%Y-%m-%d', time.localtime(user['registered_at']))}"
        edit_msg(chat_id, msg_id, text, [[{"text": "← Back", "callback_data": "back_main"}]], parse_mode="Markdown")
        answer_callback(callback_id)
        return
    
    if data == "redeem_key":
        send_msg(chat_id, "🎟️ Send me the license key to redeem (or /cancel)")
        set_user_state(chat_id, action="redeem_key")
        answer_callback(callback_id)
        return
    
    if data == "request_otp":
        expire_premium(user)
        if not is_premium(user):
            answer_callback(callback_id, premium_error(user), alert=True)
            return
        otp = f"{secrets.randbelow(1_000_000):06d}"
        now = int(time.time())
        OTPS[str(chat_id)] = {"otp": otp, "expires": now + OTP_TTL, "attempts": 0}
        edit_msg(chat_id, msg_id, "🔐 *OTP Code*\n\n"
                 f"`{otp}`\n\n⏱ Valid for 5 minutes • Single-use\n🔑 Session: 24 hours",
                 [[{"text": "← Back", "callback_data": "back_main"}]], parse_mode="Markdown")
        answer_callback(callback_id)
        return
    
    if data == "admin_panel":
        if not is_admin(chat_id):
            answer_callback(callback_id, "❌ Unauthorized", alert=True)
            return
        show_admin_panel(chat_id, msg_id)
        answer_callback(callback_id)
        return
    
    if data.startswith("admin_users_"):
        if not is_admin(chat_id):
            answer_callback(callback_id, "❌ Unauthorized", alert=True)
            return
        page = int(data.split("_")[2])
        with db() as conn:
            all_users = conn.execute("SELECT * FROM users ORDER BY registered_at DESC").fetchall()
        total = len(all_users)
        per_page = 5
        pages = (total + per_page - 1) // per_page
        start = page * per_page
        users_page = all_users[start:start + per_page]
        text = f"👥 *Users* (Page {page + 1}/{pages})\n\n"
        for u in users_page:
            text += format_user_record(u) + "\n"
        keyboard = []
        if page > 0:
            keyboard.append({"text": "← Back", "callback_data": f"admin_users_{page-1}"})
        if page < pages - 1:
            keyboard.append({"text": "Next →", "callback_data": f"admin_users_{page+1}"})
        keyboard.append({"text": "🔙 Main", "callback_data": "back_main"})
        edit_msg(chat_id, msg_id, text, [keyboard], parse_mode="Markdown")
        answer_callback(callback_id)
        return
    
    if data.startswith("admin_keys_"):
        if not is_admin(chat_id):
            answer_callback(callback_id, "❌ Unauthorized", alert=True)
            return
        page = int(data.split("_")[2])
        with db() as conn:
            all_keys = conn.execute("SELECT * FROM license_keys ORDER BY created_at DESC").fetchall()
        total = len(all_keys)
        per_page = 5
        pages = (total + per_page - 1) // per_page
        start = page * per_page
        keys_page = all_keys[start:start + per_page]
        text = f"🗝️ *License Keys* (Page {page + 1}/{pages})\n\n"
        for k in keys_page:
            text += f"`{k['key'][:16]}...` | {k['duration_days']}d | {k['uses']}/{k['max_uses']} | {k['status']}\n"
        keyboard = []
        if page > 0:
            keyboard.append({"text": "← Back", "callback_data": f"admin_keys_{page-1}"})
        if page < pages - 1:
            keyboard.append({"text": "Next →", "callback_data": f"admin_keys_{page+1}"})
        keyboard.append({"text": "🔙 Main", "callback_data": "back_main"})
        edit_msg(chat_id, msg_id, text, [keyboard], parse_mode="Markdown")
        answer_callback(callback_id)
        return
    
    if data.startswith("admin_admins_"):
        if not is_owner(chat_id):
            answer_callback(callback_id, "❌ Owner only", alert=True)
            return
        page = int(data.split("_")[2])
        with db() as conn:
            all_admins = conn.execute("SELECT * FROM admins ORDER BY added_at DESC").fetchall()
        total = len(all_admins)
        per_page = 5
        pages = (total + per_page - 1) // per_page
        start = page * per_page
        admins_page = all_admins[start:start + per_page]
        text = f"👮 *Admins* (Page {page + 1}/{pages})\n\n"
        for a in admins_page:
            icon = "👑" if a["role"] == "owner" else "🛡"
            text += f"{icon} {a['role']} | @{a['username'] or 'n/a'} | ID: {a['telegram_id']}\n"
        keyboard = []
        if page > 0:
            keyboard.append({"text": "← Back", "callback_data": f"admin_admins_{page-1}"})
        if page < pages - 1:
            keyboard.append({"text": "Next →", "callback_data": f"admin_admins_{page+1}"})
        keyboard.append({"text": "🔙 Main", "callback_data": "back_main"})
        edit_msg(chat_id, msg_id, text, [keyboard], parse_mode="Markdown")
        answer_callback(callback_id)
        return
    
    if data == "admin_genkey":
        if not is_admin(chat_id):
            answer_callback(callback_id, "❌ Unauthorized", alert=True)
            return
        send_msg(chat_id, "🔑 Send key format: <days> <uses>\nExample: 30 1")
        set_user_state(chat_id, action="genkey")
        answer_callback(callback_id)
        return
    
    if data == "admin_giveprem":
        if not is_owner(chat_id):
            answer_callback(callback_id, "❌ Owner only", alert=True)
            return
        send_msg(chat_id, "⭐ Send format: <telegram_id> <days>\nExample: 123456789 30")
        set_user_state(chat_id, action="giveprem")
        answer_callback(callback_id)
        return
    
    if data == "back_main":
        show_user_panel(chat_id, msg_id)
        answer_callback(callback_id)
        return
    
    answer_callback(callback_id, "Unknown action")

def handle_message(msg):
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_id = str(chat.get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    
    username = sender.get("username") or ""
    first_name = sender.get("first_name") or ""

    if text.startswith("/"):
        command_parts = text.split()
        command = command_parts[0].split("@", 1)[0].lower()
        if command == "/start":
            if get_setting("join_gate") == "on" and required_chats() and not has_required_membership(chat_id):
                membership_panel(chat_id)
                return
            upsert_user(chat_id, username, first_name)
            show_user_panel(chat_id)
            return
        if command == "/register":
            if get_setting("join_gate") == "on" and required_chats() and not has_required_membership(chat_id):
                membership_panel(chat_id)
                return
            upsert_user(chat_id, username, first_name)
            show_user_panel(chat_id)
            return
        command_args = command_parts[1:]
        if command == "/settings":
            setting_parts = text.split(" ", 2)
            command_args = setting_parts[1:] if len(setting_parts) > 1 else []
        if handle_command(chat_id, username, first_name, command, command_args):
            return
    
    state = get_user_state(chat_id)
    
    if state.get("action") == "redeem_key":
        key = text.strip()
        if key.lower() == "/cancel":
            set_user_state(chat_id, action=None)
            show_user_panel(chat_id)
            return
        
        user = get_user(chat_id)
        if not user:
            send_msg(chat_id, "❌ Not registered")
            return
        
        with db() as conn:
            k = conn.execute("SELECT * FROM license_keys WHERE key=?", (key,)).fetchone()
            if not k:
                send_msg(chat_id, "❌ Invalid key")
                return
            existing = conn.execute("SELECT * FROM key_redemptions WHERE key=? AND telegram_id=?", (key, chat_id)).fetchone()
            if existing:
                send_msg(chat_id, "❌ Already redeemed this key")
                return
            
            now = int(time.time())
            current_prem = user["premium_until"] if user["premium_until"] else now
            new_prem = max(current_prem, now) + (k["duration_days"] * 86400)
            
            updated = conn.execute(
                "UPDATE license_keys SET uses=uses+1, status=CASE WHEN uses+1 >= max_uses THEN 'exhausted' ELSE status END "
                "WHERE key=? AND status='active' AND uses < max_uses", (key,))
            if not updated.rowcount:
                send_msg(chat_id, "❌ Key is invalid, revoked, or exhausted")
                return
            conn.execute("INSERT INTO key_redemptions(key, telegram_id, redeemed_at, premium_until) VALUES(?,?,?,?)",
                         (key, chat_id, now, new_prem))
            conn.execute("UPDATE users SET plan='premium', premium_until=? WHERE telegram_id=?", (new_prem, chat_id))
            conn.commit()
        
        expires = time.strftime("%Y-%m-%d", time.localtime(new_prem))
        send_msg(chat_id, f"✅ Premium activated!\n\n"
                          f"⏳ Duration: {k['duration_days']} days\n"
                          f"⏰ Expires: {expires}\n🎉 Enjoy!", parse_mode="Markdown")
        set_user_state(chat_id, action=None)
        show_user_panel(chat_id)
        return
    
    if state.get("action") == "genkey":
        parts = text.split()
        if len(parts) < 2:
            send_msg(chat_id, "❌ Format: <days> <uses>")
            return
        try:
            days, uses = int(parts[0]), int(parts[1])
        except:
            send_msg(chat_id, "❌ Days and uses must be integers")
            return
        
        key = secrets.token_urlsafe(16)
        now = int(time.time())
        with db() as conn:
            conn.execute("INSERT INTO license_keys(key, created_at, duration_days, max_uses, uses, status, created_by) VALUES(?,?,?,?,?,?,?)",
                        (key, now, days, uses, 0, "active", chat_id))
            conn.commit()
        
        send_msg(chat_id, f"🔑 Key Generated:\n\n`{key}`", parse_mode="Markdown")
        set_user_state(chat_id, action=None)
        show_admin_panel(chat_id)
        return
    
    if state.get("action") == "giveprem":
        parts = text.split()
        if len(parts) < 2:
            send_msg(chat_id, "❌ Format: <telegram_id> <days>")
            return
        try:
            user_id, days = str(parts[0]), int(parts[1])
        except:
            send_msg(chat_id, "❌ Invalid format")
            return
        
        user = get_user(user_id)
        if not user:
            send_msg(chat_id, "❌ User not found")
            return
        
        now = int(time.time())
        prem_until = now + (days * 86400)
        with db() as conn:
            conn.execute("UPDATE users SET plan='premium', premium_until=? WHERE telegram_id=?", (prem_until, user_id))
            conn.commit()
        
        send_msg(chat_id, f"✅ Premium given to {user_id} for {days} days")
        set_user_state(chat_id, action=None)
        show_admin_panel(chat_id)
        return
    
def telegram_poll():
    global OFFSET
    print("Telegram bot active with interactive menus")
    while True:
        try:
            updates = telegram_call("getUpdates", {"limit": 100, "timeout": 10, "offset": OFFSET}) or []
            for update in updates:
                OFFSET = update.get("update_id", 0) + 1
                try:
                    msg = update.get("message")
                    if msg:
                        handle_message(msg)
                    callback = update.get("callback_query")
                    if callback:
                        handle_callback(callback["id"], callback["from"]["id"], callback["message"]["message_id"],
                                      callback["message"]["chat"]["id"], callback["data"])
                except Exception as e:
                    print(f"Update {update.get('update_id', '?')} failed: {e}")
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(3)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass
    
    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()
    
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success":true,"service":"ar-hitter"}')
            return
        self.send_response(404)
        self.end_headers()
    
    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
        except:
            self.send_response(400)
            self.end_headers()
            return
        
        if path == "/request-otp":
            user_id = str(data.get("userId", "")).strip()
            user = get_user(user_id)
            expire_premium(user)
            if not is_premium(user):
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": premium_error(user)}).encode())
                return
            otp = f"{secrets.randbelow(1_000_000):06d}"
            now = int(time.time())
            OTPS[user_id] = {"otp": otp, "expires": now + OTP_TTL, "attempts": 0}
            try:
                send_msg(user_id, f"🔐 OTP: `{otp}`\n⏱ 5 minutes", parse_mode="Markdown")
            except:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success":true,"message":"OTP sent"}')
            return
        
        if path == "/verify-otp":
            user_id = str(data.get("userId", "")).strip()
            otp = str(data.get("otp", "")).strip()
            record = OTPS.get(user_id)
            now = int(time.time())
            if not record or record["expires"] <= now or otp != record["otp"]:
                if record and record["expires"] <= now:
                    OTPS.pop(user_id, None)
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success":false,"error":"Invalid OTP"}')
                return
            OTPS.pop(user_id, None)
            user = get_user(user_id)
            expire_premium(user)
            if not is_premium(user):
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": premium_error(user)}).encode())
                return
            session = secrets.token_urlsafe(18)
            SESSIONS[session] = {"user_id": user_id, "expires_at": now + SESSION_TTL}
            upsert_user(user_id, user["username"], user["first_name"], increment_login=True)
            user = get_user(user_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {"success": True, "token": session, "user_id": user_id, "username": user["username"],
                       "first_name": user["first_name"], "plan": user["plan"], "hits": 0}
            self.wfile.write(json.dumps(response).encode())
            return
        
        if path == "/validate-token":
            token = str(data.get("token", "")).strip()
            record = SESSIONS.get(token)
            now = int(time.time())
            token_user = get_user(record["user_id"]) if record else None
            if not record or record["expires_at"] <= now or not token_user or not token_user["active"] or token_user["revoked"]:
                if record and record["expires_at"] <= now:
                    SESSIONS.pop(token, None)
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success":false,"error":"Invalid token"}')
                return
            user = get_user(record["user_id"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {"success": True, "token": token, "user_id": user["telegram_id"], 
                       "username": user["username"], "first_name": user["first_name"], "plan": user["plan"], "hits": 0}
            self.wfile.write(json.dumps(response).encode())
            return
        
        self.send_response(404)
        self.end_headers()

if __name__ == "__main__":
    init_db()
    print(f"AR Hitter OTP server on http://{HOST}:{PORT}")
    threading.Thread(target=telegram_poll, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

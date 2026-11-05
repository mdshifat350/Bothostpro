import os
import re
import sys
import ast
import json
import time
import html
import signal
import logging
import subprocess
import threading
import importlib.metadata as md
from collections import deque

import psutil
import telebot
from telebot import types

import deploy

# ============================ CONFIG ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
if not BOT_TOKEN:
    sys.exit("[FATAL] BOT_TOKEN environment variable missing")
if not OWNER_ID_RAW.isdigit():
    sys.exit("[FATAL] OWNER_ID must be a numeric Telegram user ID")
OWNER_ID = int(OWNER_ID_RAW)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
ENGINE_LOG_DIR = os.path.join(DATA_DIR, "engines")
os.makedirs(ENGINE_LOG_DIR, exist_ok=True)

MAX_CHANNELS = 10
PROJECT_FILES = ["bot.py", "requirements.txt", "Procfile", "nixpacks.toml"]
START_TIME = time.time()

# ============================ LOGGING ============================
recent_logs = deque(maxlen=300)

class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        recent_logs.append(f"{ts} {record.levelname:<7} {record.getMessage()}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logging.getLogger().addHandler(MemoryLogHandler())
log = logging.getLogger("devx")

# ============================ STATE ============================
DEFAULT_STATE = {"bot_enabled": True, "channels": [], "allowed_users": [], "requests": 0}

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {**DEFAULT_STATE, **data}

def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

STATE = load_state()
engines = {}  # pid -> {"proc", "cmd", "started", "log"}

# ============================ BOT ============================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ============================ HELPERS ============================
def esc(x):
    return html.escape(str(x))

def is_owner(uid):
    return uid == OWNER_ID

def is_allowed(uid):
    return is_owner(uid) or uid in STATE["allowed_users"]

def btn(text, data):
    return types.InlineKeyboardButton(text, callback_data=data)

def fmt_uptime():
    s = int(time.time() - START_TIME)
    return f"{s // 86400}d {s % 86400 // 3600}h {s % 3600 // 60}m {s % 60}s"

def gb(n):
    return f"{n / 1024 ** 3:.2f} GB"

def live_engines():
    for pid in list(engines):
        if engines[pid]["proc"].poll() is not None:
            del engines[pid]
    return engines

def kill_engine(pid):
    info = engines.pop(pid, None)
    if not info:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)  # started with start_new_session, pgid == pid
    except ProcessLookupError:
        pass
    return True

def missing_channels(uid):
    missing = []
    for ch in STATE["channels"]:
        try:
            member = bot.get_chat_member(ch["id"], uid)
            if member.status not in ("member", "administrator", "creator"):
                missing.append(ch)
        except Exception as e:
            log.warning("channel check failed for %s: %s", ch["id"], e)
            missing.append(ch)
    return missing

def gate(uid):
    """Returns None if the user may proceed, else (text, keyboard) to show."""
    if is_owner(uid):
        return None
    if not STATE["bot_enabled"]:
        return ("⏸ <b>Bot is OFFLINE right now.</b>\nPlease try again later.", None)
    missing = missing_channels(uid)
    if missing:
        kb = types.InlineKeyboardMarkup(row_width=1)
        for ch in missing:
            if ch.get("link"):
                kb.add(types.InlineKeyboardButton(f"📢 Join {ch['title'][:30]}", url=ch["link"]))
        kb.add(btn("🔄 Verify Membership", "verify"))
        return (f"⚠️ <b>Join {len(missing)} required channel(s) first.</b>", kb)
    if not is_allowed(uid):
        kb = types.InlineKeyboardMarkup().add(btn("📩 Request Access", "auth:req"))
        return ("🔒 <b>Authorization required.</b>\nSend a request to the admin.", kb)
    STATE["requests"] += 1
    save_state()
    return None

def guard(m):
    """Use in message handlers. Returns True if the message was blocked."""
    blocked = gate(m.from_user.id)
    if blocked:
        bot.reply_to(m, blocked[0], reply_markup=blocked[1])
        return True
    return False

def deny_owner_only(m):
    bot.reply_to(m, "⛔ <b>Owner only.</b>")

# ============================ TEXT BUILDERS ============================
def main_text():
    state = "ONLINE ✅" if STATE["bot_enabled"] else "OFFLINE ⛔"
    return (
        "⚡️ <b>QUNTA X HOST | PREMIUM</b> ⚡️\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Bot: <b>{state}</b>\n"
        f"Uptime: <code>{fmt_uptime()}</code>\n"
        f"Channels: <code>{len(STATE['channels'])}/{MAX_CHANNELS}</code>\n"
        f"Authorized users: <code>{len(STATE['allowed_users'])}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Choose an action below."
    )

def main_kb(uid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(btn("📊 Status", "m:status"), btn("🧬 Vitals", "m:vitals"))
    kb.add(btn("🩺 File Check", "m:check"), btn("⚙️ Engines", "m:engines"))
    kb.add(btn("📜 Logs", "m:logs"), btn("📈 Stats", "m:stats"))
    if is_owner(uid):
        kb.add(btn("🎛 Bot Control", "m:control"), btn("👥 Users", "m:users"))
        kb.add(btn("📢 Channels", "m:channels"))
    kb.add(btn("🚀 Deploy Center", "m:deploy"))
    kb.add(btn("🪪 My ID", "m:myid"), btn("📖 Help", "m:help"))
    return kb

def back_kb(extra=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if extra:
        for row in extra:
            kb.add(*row)
    kb.add(btn("🔙 Main Menu", "m:main"))
    return kb

def status_text():
    return (
        f"🟢 <b>SERVER STATUS</b>\n"
        f"Bot: <b>{'ONLINE ✅' if STATE['bot_enabled'] else 'OFFLINE ⛔'}</b>\n"
        f"Runtime: <code>{fmt_uptime()}</code>\n"
        f"Active engines: <code>{len(live_engines())}</code>\n"
        f"Requests served: <code>{STATE['requests']}</code>"
    )

def vitals_text():
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(BASE_DIR)
    return (
        "🧬 <b>SYSTEM VITALS</b>\n"
        f"CPU: <code>{psutil.cpu_percent(interval=0.5)}%</code>\n"
        f"RAM: <code>{vm.percent}%</code> ({gb(vm.used)} / {gb(vm.total)})\n"
        f"Disk: <code>{du.percent}%</code> ({gb(du.used)} / {gb(du.total)})"
    )

def stats_text():
    return (
        "📈 <b>STATS</b>\n"
        f"Requests served: <code>{STATE['requests']}</code>\n"
        f"Authorized users: <code>{len(STATE['allowed_users'])}</code>\n"
        f"Channels: <code>{len(STATE['channels'])}/{MAX_CHANNELS}</code>\n"
        f"Live engines: <code>{len(live_engines())}</code>\n"
        f"Uptime: <code>{fmt_uptime()}</code>"
    )

def logs_text(n=30):
    raw = "\n".join(list(recent_logs)[-n:]) or "no logs yet"
    return f"📜 <b>LAST {n} LOG LINES</b>\n<pre>{esc(raw[-2500:])}</pre>"

def engines_text():
    live = live_engines()
    if not live:
        return "💤 <b>No active engines.</b>\nStart one with <code>/run &lt;command&gt;</code>"
    lines = ["⚙️ <b>ACTIVE ENGINES</b>"]
    for pid, info in live.items():
        age = int(time.time() - info["started"])
        lines.append(f"• PID <code>{pid}</code> · {age}s · <code>{esc(info['cmd'])}</code>")
    return "\n".join(lines)

def engines_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for pid, info in live_engines().items():
        kb.add(btn(f"🛑 Kill {pid}", f"kill:{pid}"))
    kb.add(btn("🔄 Refresh", "m:engines"), btn("🔙 Main Menu", "m:main"))
    return kb

def control_text():
    state = "ONLINE ✅" if STATE["bot_enabled"] else "OFFLINE ⛔"
    return (
        f"🎛 <b>BOT CONTROL</b>\nCurrent state: <b>{state}</b>\n\n"
        "When OFFLINE, regular users are blocked. The owner keeps full access."
    )

def control_kb():
    return back_kb([
        [btn("🟢 Turn ON", "ctl:on"), btn("⛔ Turn OFF", "ctl:off")],
        [btn("♻️ Restart Process", "ctl:restart")],
    ])

def users_kb_and_text():
    users = STATE["allowed_users"]
    text = "👥 <b>AUTHORIZED USERS</b>\n" + ("\n".join(f"▪️ <code>{u}</code>" for u in users) or "None yet.")
    text += "\n\nUse <code>/add &lt;id&gt;</code> or <code>/remove &lt;id&gt;</code>."
    rows = [[btn(f"❌ Remove {u}", f"rm:{u}")] for u in users[:20]]
    return text, back_kb(rows)

def channels_text_kb():
    chans = STATE["channels"]
    if not chans:
        text = "📢 <b>CHANNEL MANAGER</b>\nNo required channels yet (0/10).\nAdd one with <code>/addchannel &lt;id|@user&gt;</code> or forward a channel post with <code>/addchannel</code>."
        return text, back_kb()
    lines = [f"📢 <b>CHANNEL MANAGER</b> ({len(chans)}/{MAX_CHANNELS})"]
    rows = []
    for i, ch in enumerate(chans, 1):
        lines.append(f"{i}. <b>{esc(ch['title'])}</b>\n   <code>{ch['id']}</code>")
        rows.append([btn(f"🗑 Remove {ch['title'][:25]}", f"chdel:{ch['id']}")])
    return "\n".join(lines), back_kb(rows)

HELP_TEXT = (
    "📖 <b>HELP GUIDE</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Everyone:</b>\n"
    "/menu · /status · /vitals · /check · /myid · /deploy\n\n"
    "<b>Owner:</b>\n"
    "/run &lt;cmd&gt; — start background engine\n"
    "/stop &lt;pid&gt; — kill engine\n"
    "/elog &lt;pid&gt; — engine output\n"
    "/sh &lt;cmd&gt; — run shell command\n"
    "/logs — live log viewer\n"
    "/on · /off — bot online/offline\n"
    "/addchannel &lt;id|@user&gt; — add required channel\n"
    "/add &lt;id&gt; · /remove &lt;id&gt; — whitelist\n"
    "━━━━━━━━━━━━━━━━━━━━"
)

# ============================ FILE CHECK ============================
def check_one(name):
    """Returns (status, message). status is 'pass', 'warn' or 'fail'."""
    path = os.path.join(BASE_DIR, name)
    if not os.path.isfile(path):
        return "fail", "file not found"
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return "fail", f"cannot read: {e}"

    if name.endswith(".py"):
        try:
            ast.parse(text, filename=name)
        except SyntaxError as e:
            return "fail", f"syntax error line {e.lineno}: {e.msg}"
        return "pass", "syntax OK"

    if name == "requirements.txt":
        pkgs = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        if not pkgs:
            return "fail", "file is empty"
        names = {re.split(r"[<>=!~\[; ]", p)[0].lower() for p in pkgs}
        if "pytelegrambotapi" not in names:
            return "fail", "pyTelegramBotAPI is missing"
        if "psutil" not in names:
            return "warn", "psutil missing (vitals will not work)"
        return "pass", f"{len(pkgs)} package(s) listed"

    if name == "Procfile":
        if re.search(r"^(worker|web):\s*python3?\s+\S+", text, re.M):
            return "pass", "valid process line"
        return "fail", "expected a line like: worker: python bot.py"

    if name == "nixpacks.toml":
        try:
            import tomllib
        except ModuleNotFoundError:
            return "warn", "Python 3.11+ needed to validate TOML"
        try:
            tomllib.loads(text)
            return "pass", "valid TOML"
        except Exception as e:
            return "fail", f"invalid TOML: {e}"

    return "warn", "no rule defined"

def run_checks():
    icons = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    points = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    results = [(name, *check_one(name)) for name in PROJECT_FILES]
    accuracy = round(100 * sum(points[s] for _, s, _ in results) / len(results))

    lines = [f"🩺 <b>FILE CHECK</b> · accuracy <b>{accuracy}%</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for name, status, msg in results:
        lines.append(f"{icons[status]} <code>{esc(name)}</code> — {esc(msg)}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    for pkg in ("pyTelegramBotAPI", "psutil"):
        try:
            lines.append(f"📦 {pkg}: <code>{md.version(pkg)}</code>")
        except md.PackageNotFoundError:
            lines.append(f"📦 {pkg}: <b>not installed</b>")
    return "\n".join(lines), accuracy

# ============================ SHELL ============================
def run_shell(cmd):
    try:
        out = subprocess.run(cmd, shell=True, cwd=BASE_DIR, capture_output=True, text=True, timeout=60)
        text = (out.stdout + out.stderr).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        text = "timed out after 60s"
    return text[-3500:]

def read_tail(path, n=40):
    if not os.path.exists(path):
        return "no output yet"
    with open(path, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-n:]) or "no output yet"

# ============================ COMMANDS ============================
@bot.message_handler(commands=["start", "menu", "help", "commands"])
def cmd_menu(m):
    if guard(m):
        return
    text = HELP_TEXT if m.text.startswith(("/help", "/commands")) else main_text()
    bot.reply_to(m, text, reply_markup=main_kb(m.from_user.id))

@bot.message_handler(commands=["myid"])
def cmd_myid(m):
    bot.reply_to(m, f"🪪 <b>YOUR ID:</b> <code>{m.from_user.id}</code>")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    if guard(m):
        return
    bot.reply_to(m, status_text(), reply_markup=back_kb())

@bot.message_handler(commands=["vitals"])
def cmd_vitals(m):
    if guard(m):
        return
    bot.reply_to(m, vitals_text(), reply_markup=back_kb())

@bot.message_handler(commands=["check"])
def cmd_check(m):
    if guard(m):
        return
    text, _ = run_checks()
    bot.reply_to(m, text, reply_markup=back_kb([[btn("🔁 Re-run", "m:check")]]))

@bot.message_handler(commands=["logs"])
def cmd_logs(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    bot.reply_to(m, logs_text(), reply_markup=back_kb([[btn("🔁 Refresh", "m:logs")]]))

@bot.message_handler(commands=["on", "off"])
def cmd_toggle(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    STATE["bot_enabled"] = m.text.startswith("/on")
    save_state()
    bot.reply_to(m, f"Bot is now <b>{'ONLINE ✅' if STATE['bot_enabled'] else 'OFFLINE ⛔'}</b>")

@bot.message_handler(commands=["add"])
def cmd_add(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return bot.reply_to(m, "Usage: <code>/add &lt;userid&gt;</code>")
    uid = int(parts[1])
    if uid not in STATE["allowed_users"]:
        STATE["allowed_users"].append(uid)
        save_state()
    bot.reply_to(m, f"✅ User <code>{uid}</code> authorized.")
    try:
        bot.send_message(uid, "🎉 <b>ACCESS APPROVED</b>\nType /menu to open the dashboard.")
    except Exception:
        pass

@bot.message_handler(commands=["remove"])
def cmd_remove(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return bot.reply_to(m, "Usage: <code>/remove &lt;userid&gt;</code>")
    uid = int(parts[1])
    if uid in STATE["allowed_users"]:
        STATE["allowed_users"].remove(uid)
        save_state()
        bot.reply_to(m, f"🗑 User <code>{uid}</code> removed.")
    else:
        bot.reply_to(m, "⚠️ User not in the whitelist.")

@bot.message_handler(commands=["addchannel"])
def cmd_addchannel(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    parts = m.text.split(maxsplit=1)
    if m.forward_from_chat:
        ref = m.forward_from_chat.id
    elif len(parts) == 2:
        ref = parts[1].strip()
        if ref.lstrip("-").isdigit():
            ref = int(ref)
    else:
        return bot.reply_to(m, "Usage: <code>/addchannel &lt;id|@username&gt;</code> or forward a channel post with <code>/addchannel</code>.")
    ok, text = add_channel(ref)
    bot.reply_to(m, text)

def add_channel(ref):
    if len(STATE["channels"]) >= MAX_CHANNELS:
        return False, f"⚠️ Limit reached ({MAX_CHANNELS} channels)."
    try:
        chat = bot.get_chat(ref)
    except Exception as e:
        return False, f"❌ Could not read channel: {esc(e)}"
    if chat.type != "channel":
        return False, "❌ That is not a channel."
    if any(c["id"] == chat.id for c in STATE["channels"]):
        return False, "⚠️ Channel already added."
    link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else None)
    if not link:
        try:
            link = bot.create_chat_invite_link(chat.id).invite_link
        except Exception:
            link = None
    STATE["channels"].append({"id": chat.id, "title": chat.title or "Channel", "link": link})
    save_state()
    return True, f"✅ Added <b>{esc(chat.title)}</b> (<code>{chat.id}</code>)\nTotal: {len(STATE['channels'])}/{MAX_CHANNELS}"

@bot.message_handler(commands=["run"])
def cmd_run(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    cmd = m.text.partition(" ")[2].strip()
    if not cmd:
        return bot.reply_to(m, "Usage: <code>/run &lt;command&gt;</code>")
    logpath = os.path.join(ENGINE_LOG_DIR, f"engine_{int(time.time())}.log")
    logf = open(logpath, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, shell=True, cwd=BASE_DIR, stdout=logf,
                            stderr=subprocess.STDOUT, start_new_session=True)
    engines[proc.pid] = {"proc": proc, "cmd": cmd, "started": time.time(), "log": logpath}
    log.info("Engine started pid=%s cmd=%s", proc.pid, cmd)
    bot.reply_to(m, f"🟢 Engine started · PID <code>{proc.pid}</code>\nView output: <code>/elog {proc.pid}</code>")

@bot.message_handler(commands=["stop"])
def cmd_stop(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return bot.reply_to(m, "Usage: <code>/stop &lt;pid&gt;</code>")
    pid = int(parts[1])
    ok = kill_engine(pid)
    bot.reply_to(m, f"🛑 Engine <code>{pid}</code> stopped." if ok else "⚠️ PID not found.")

@bot.message_handler(commands=["elog"])
def cmd_elog(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    parts = m.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return bot.reply_to(m, "Usage: <code>/elog &lt;pid&gt;</code>")
    info = engines.get(int(parts[1]))
    if not info:
        return bot.reply_to(m, "⚠️ PID not found.")
    bot.reply_to(m, f"<pre>{esc(read_tail(info['log'])[-3000:])}</pre>")

@bot.message_handler(commands=["sh"])
def cmd_shell(m):
    if not is_owner(m.from_user.id):
        return deny_owner_only(m)
    cmd = m.text.partition(" ")[2].strip()
    if not cmd:
        return bot.reply_to(m, "Usage: <code>/sh &lt;command&gt;</code>")
    bot.reply_to(m, f"<pre>{esc(run_shell(cmd))}</pre>")

# ============================ CALLBACKS ============================
def edit(c, text, kb=None):
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            raise

def restart_process():
    log.info("Restart requested by owner")
    os.execv(sys.executable, [sys.executable] + sys.argv)

@bot.callback_query_handler(func=lambda c: True)
def on_callback(c):
    uid = c.from_user.id
    data = c.data or ""

    # Open paths that don't need the gate
    if data == "verify":
        blocked = gate(uid)
        if blocked is None:
            bot.answer_callback_query(c.id, "✅ Verified")
            edit(c, main_text(), main_kb(uid))
        else:
            bot.answer_callback_query(c.id, "Still not verified", show_alert=True)
            edit(c, *blocked)
        return

    if data == "auth:req":
        return handle_auth_request(c)

    if data.startswith("auth:"):
        return handle_auth_decision(c)

    blocked = gate(uid)
    if blocked:
        bot.answer_callback_query(c.id)
        return edit(c, *blocked)

    owner_only = data in {"m:control", "m:users", "m:channels", "m:logs", "ctl:on", "ctl:off",
                          "ctl:restart"} or data.startswith(("rm:", "chdel:", "kill:"))
    if owner_only and not is_owner(uid):
        return bot.answer_callback_query(c.id, "⛔ Owner only.", show_alert=True)

    if deploy.handle_callback(c):
        return

    if data == "m:main":
        edit(c, main_text(), main_kb(uid))
    elif data == "m:help":
        edit(c, HELP_TEXT, back_kb())
    elif data == "m:myid":
        edit(c, f"🪪 <b>YOUR ID:</b> <code>{uid}</code>", back_kb())
    elif data == "m:status":
        edit(c, status_text(), back_kb())
    elif data == "m:vitals":
        edit(c, vitals_text(), back_kb())
    elif data == "m:stats":
        edit(c, stats_text(), back_kb())
    elif data == "m:check":
        text, _ = run_checks()
        edit(c, text, back_kb([[btn("🔁 Re-run", "m:check")]]))
    elif data == "m:logs":
        edit(c, logs_text(), back_kb([[btn("🔁 Refresh", "m:logs")]]))
    elif data == "m:engines":
        edit(c, engines_text(), engines_kb())
    elif data.startswith("kill:"):
        pid = int(data.split(":", 1)[1])
        kill_engine(pid)
        edit(c, engines_text(), engines_kb())
    elif data == "m:control":
        edit(c, control_text(), control_kb())
    elif data in ("ctl:on", "ctl:off"):
        STATE["bot_enabled"] = data == "ctl:on"
        save_state()
        edit(c, control_text(), control_kb())
    elif data == "ctl:restart":
        bot.answer_callback_query(c.id, "Restarting…")
        edit(c, "♻️ <b>Restarting process…</b> back in a few seconds.")
        threading.Timer(2, restart_process).start()
        return
    elif data == "m:users":
        text, kb = users_kb_and_text()
        edit(c, text, kb)
    elif data.startswith("rm:"):
        target = int(data.split(":", 1)[1])
        if target in STATE["allowed_users"]:
            STATE["allowed_users"].remove(target)
            save_state()
        text, kb = users_kb_and_text()
        edit(c, text, kb)
    elif data == "m:channels":
        text, kb = channels_text_kb()
        edit(c, text, kb)
    elif data.startswith("chdel:"):
        target = int(data.split(":", 1)[1])
        STATE["channels"] = [ch for ch in STATE["channels"] if ch["id"] != target]
        save_state()
        text, kb = channels_text_kb()
        edit(c, text, kb)
    else:
        return bot.answer_callback_query(c.id, "Unknown action")

    bot.answer_callback_query(c.id)

# ============================ AUTHORIZATION FLOW ============================
def handle_auth_request(c):
    user = c.from_user
    if is_allowed(user.id):
        bot.answer_callback_query(c.id, "You are already authorized ✅")
        return edit(c, main_text(), main_kb(user.id))
    name = esc(f"{user.first_name or ''} {user.last_name or ''}".strip())
    uname = f"@{esc(user.username)}" if user.username else "none"
    kb = types.InlineKeyboardMarkup(row_width=2).add(
        btn("✅ Allow", f"auth:allow:{user.id}"),
        btn("❌ Deny", f"auth:deny:{user.id}"),
    )
    bot.send_message(
        OWNER_ID,
        f"🔔 <b>NEW ACCESS REQUEST</b>\n👤 {name}\n🔗 {uname}\n🆔 <code>{user.id}</code>",
        reply_markup=kb,
    )
    bot.answer_callback_query(c.id, "Request sent to admin 📨")
    edit(c, "⏳ <b>Request sent.</b>\nYou will be notified once the admin decides.")

def handle_auth_decision(c):
    if not is_owner(c.from_user.id):
        return bot.answer_callback_query(c.id, "⛔ Owner only.", show_alert=True)
    _, action, raw = c.data.split(":", 2)
    target = int(raw)
    decided = "✅ ALLOWED" if action == "allow" else "❌ DENIED"
    if action == "allow" and target not in STATE["allowed_users"]:
        STATE["allowed_users"].append(target)
        save_state()
    base = esc(c.message.text or "")
    edit(c, f"{base}\n\nDecision: {decided} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    bot.answer_callback_query(c.id, f"User {target} {decided.lower()}")
    try:
        if action == "allow":
            bot.send_message(target, "🎉 <b>ACCESS APPROVED</b>\nType /menu to open the dashboard.")
        else:
            bot.send_message(target, "🚫 <b>ACCESS DENIED</b>\nThe admin rejected your request.")
    except Exception as e:
        log.warning("could not notify %s: %s", target, e)

# ============================ MAIN ============================
deploy.register(bot)
deploy.init(bot, OWNER_ID, DATA_DIR, is_owner, is_allowed, gate)

def verify_token():
    try:
        me = bot.get_me()
        log.info("Logged in as @%s (id %s)", me.username, me.id)
        return True
    except Exception as e:
        log.error("BOT_TOKEN check failed: %s. Check the BOT_TOKEN variable in Railway.", e)
        return False


if __name__ == "__main__":
    log.info("QUNTA X HOST premium starting")
    if not verify_token():
        sys.exit(1)
    try:
        bot.remove_webhook()
    except Exception as e:
        log.warning("remove_webhook failed: %s", e)
    log.info("QUNTA X HOST premium online")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            log.error("Polling stopped: %s. Retrying in 5 seconds.", e)
            time.sleep(5)

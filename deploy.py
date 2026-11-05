"""
Deploy Center: users submit their own bot (token + .zip/.py).
Owner approves. Each bot runs as an isolated process with its own logs.
"""
import os
import re
import ast
import sys
import json
import html
import time
import uuid
import shutil
import signal
import zipfile
import threading
import subprocess

import psutil
import telebot
from telebot import types

MAX_BOTS_PER_USER = 1   # one bot per user
MAX_UPLOAD = 10 * 1024 * 1024
MEM_LIMIT = 384 * 1024 * 1024  # real RAM (RSS) limit per bot
TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
STATUS_ICON = {"pending": "⏳", "approved": "🔧", "running": "🟢", "stopped": "⏹", "denied": "🚫"}

BOTS = {}     # bid -> info dict
procs = {}    # bid -> subprocess.Popen
waiting = {}  # user_id -> {"stage": "token"|"file", "bid": bid}

_bot = None
_owner = None
_root = None
_store = None
_is_owner = None
_is_allowed = None
_gate = None


def esc(x):
    return html.escape(str(x))


def btn(text, data):
    return types.InlineKeyboardButton(text, callback_data=data)


# ============================ SETUP ============================
def init(bot, owner_id, data_dir, is_owner, is_allowed, gate):
    global _bot, _owner, _root, _store, _is_owner, _is_allowed, _gate
    _bot, _owner = bot, owner_id
    _root = os.path.join(data_dir, "user_bots")
    os.makedirs(_root, exist_ok=True)
    _store = os.path.join(_root, "bots.json")
    _is_owner, _is_allowed, _gate = is_owner, is_allowed, gate
    try:
        with open(_store, encoding="utf-8") as f:
            BOTS.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    for bid, b in BOTS.items():
        if b.get("status") == "running":
            ok, msg = start_bot(bid)
            if not ok:
                b["status"], b["last_error"] = "stopped", msg
    save()
    threading.Thread(target=_monitor, daemon=True).start()


def save():
    tmp = _store + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(BOTS, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _store)


def bot_dir(bid):
    return os.path.join(_root, bid)


def token_path(bid):
    return os.path.join(bot_dir(bid), "token.txt")


def read_token(bid):
    with open(token_path(bid), encoding="utf-8") as f:
        return f.read().strip()


def write_token(bid, token):
    p = token_path(bid)
    with open(p, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(p, 0o600)


def can_manage(uid, bid):
    return bid in BOTS and (BOTS[bid]["owner"] == uid or _is_owner(uid))


# ============================ CHECKS ============================
def check_dir(run_dir):
    checks = []
    entry = os.path.join(run_dir, "bot.py")
    if not os.path.isfile(entry):
        checks.append(("bot.py", "fail", "not found"))
    else:
        with open(entry, encoding="utf-8", errors="replace") as f:
            src = f.read()
        try:
            ast.parse(src)
            checks.append(("syntax", "pass", "bot.py is valid"))
        except SyntaxError as e:
            checks.append(("syntax", "fail", f"line {e.lineno}: {e.msg}"))
        checks.append(("telebot", "pass" if "telebot" in src else "warn",
                       "telebot used" if "telebot" in src else "telebot import not found"))
        polling = re.search(r"infinity_polling|\.polling\(", src)
        checks.append(("polling", "pass" if polling else "warn",
                       "polling loop found" if polling else "no polling loop found"))
        risky = [p for p in ("rmtree", "os.system", "/etc/") if p in src]
        checks.append(("safety", "warn" if risky else "pass",
                       "review: " + ", ".join(risky) if risky else "no risky calls"))
    req = os.path.join(run_dir, "requirements.txt")
    checks.append(("requirements", "pass" if os.path.isfile(req) else "warn",
                   "requirements.txt found" if os.path.isfile(req) else "missing (no extra packages)"))
    pts = {"pass": 1, "warn": 0.5, "fail": 0}
    acc = round(100 * sum(pts[s] for _, s, _ in checks) / len(checks))
    return checks, acc


def format_checks(checks, acc):
    icons = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    lines = [f"🩺 <b>ACCURACY: {acc}%</b>"]
    for name, status, msg in checks:
        lines.append(f"{icons[status]} {esc(name)} — {esc(msg)}")
    return "\n".join(lines)


# ============================ UPLOAD HELPERS ============================
def _extract_zip(zpath, dest):
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            target = os.path.realpath(os.path.join(dest, n))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError("unsafe path inside zip")
        z.extractall(dest)


def _find_entry(d):
    for cur, _dirs, files in os.walk(d):
        if "bot.py" in files and "libs" not in cur:
            return cur
    return None


# ============================ PROCESS CONTROL ============================
def start_bot(bid):
    b = BOTS[bid]
    p = procs.get(bid)
    if p and p.poll() is None:
        return True, "already running"
    run_dir = b.get("run_dir")
    if not run_dir or not os.path.isfile(os.path.join(run_dir, "bot.py")):
        return False, "bot.py missing"
    libs = os.path.join(bot_dir(bid), "libs")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": run_dir,
        "BOT_TOKEN": read_token(bid),
        "OWNER_ID": str(b["owner"]),
        "DATA_DIR": os.path.join(run_dir, "data"),
        "PYTHONPATH": libs,
        "PYTHONUNBUFFERED": "1",
    }
    logf = open(os.path.join(bot_dir(bid), "run.log"), "a", encoding="utf-8")
    logf.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    logf.flush()
    try:
        procs[bid] = subprocess.Popen(
            [sys.executable, "bot.py"], cwd=run_dir, env=env,
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        return False, str(e)
    b["status"], b["last_error"] = "running", None
    save()
    return True, "started"


def stop_bot(bid):
    p = procs.pop(bid, None)
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass
    if bid in BOTS:
        BOTS[bid]["status"] = "stopped"
        save()


def _notify_owner(bid, text):
    try:
        _bot.send_message(BOTS[bid]["owner"], text)
    except Exception:
        pass


def _tree_rss(pid):
    """Total RAM of the bot process and all its children."""
    proc = psutil.Process(pid)
    total = proc.memory_info().rss
    for child in proc.children(recursive=True):
        try:
            total += child.memory_info().rss
        except psutil.Error:
            pass
    return total


def _monitor():
    while True:
        time.sleep(15)
        for bid, b in list(BOTS.items()):
            if b.get("status") != "running":
                continue
            p = procs.get(bid)
            name = esc(b.get("username") or "bot")

            # 1) crashed or exited on its own
            if p is None or p.poll() is not None:
                code = p.poll() if p else "?"
                procs.pop(bid, None)
                b["status"], b["last_error"] = "stopped", f"exited (code {code})"
                save()
                _notify_owner(bid, f"⚠️ Your bot @{name} stopped (code {code}).\nOpen 🤖 My Bots and tap ⏻ Start to run it again.")
                continue

            # 2) RAM over the limit -> auto OFF
            try:
                used = _tree_rss(p.pid)
            except psutil.Error:
                continue
            if used > MEM_LIMIT:
                stop_bot(bid)
                b["last_error"] = f"RAM limit {MEM_LIMIT // (1024 * 1024)} MB exceeded, auto-stopped"
                save()
                _notify_owner(bid, f"🔴 Your bot @{name} was turned OFF automatically.\nReason: RAM went over {MEM_LIMIT // (1024 * 1024)} MB.\nTap ⏻ Start to turn it on again.")


def tail(bid, n=40):
    path = os.path.join(bot_dir(bid), "run.log")
    if not os.path.exists(path):
        return "no logs yet"
    with open(path, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-n:]) or "no logs yet"


# ============================ VIEWS ============================
def deploy_text():
    return (
        "🚀 <b>DEPLOY CENTER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Host your own Telegram bot on QUNTA X HOST.\n"
        "1. Send your bot token from @BotFather\n"
        "2. Upload your bot as <b>.zip</b> or <b>.py</b>\n"
        "3. Admin approves, then your bot runs 24/7\n"
        f"Limit: {MAX_BOTS_PER_USER} bot per user, {MEM_LIMIT // (1024 * 1024)} MB RAM\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


def deploy_kb(uid):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(btn("🆕 Deploy My Bot", "dep:new"))
    kb.add(btn("🤖 My Bots", "dep:list"))
    if _is_owner(uid):
        kb.add(btn("🛠 All Bots (admin)", "dep:all"))
    kb.add(btn("🔙 Main Menu", "m:main"))
    return kb


def bots_list_view(uid, everyone=False):
    items = [(bid, b) for bid, b in BOTS.items()
             if (everyone or b["owner"] == uid) and b.get("status") not in ("awaiting_token", "denied")]
    kb = types.InlineKeyboardMarkup(row_width=1)
    for bid, b in items:
        icon = STATUS_ICON.get(b["status"], "•")
        kb.add(btn(f"{icon} @{b.get('username') or 'pending'} · {b['status']}", f"bot:{bid}"))
    kb.add(btn("🔙 Deploy Center", "m:deploy"))
    title = "🛠 <b>ALL BOTS</b>" if everyone else "🤖 <b>MY BOTS</b>"
    text = f"{title}\n{len(items)} bot(s)" if items else f"{title}\nNo bots yet. Tap 🆕 Deploy My Bot."
    return text, kb


def bot_detail_view(bid):
    b = BOTS[bid]
    text = (
        f"🤖 <b>@{esc(b.get('username') or 'pending')}</b>\n"
        f"Status: <b>{esc(b['status'])}</b>\n"
        f"Bot ID: <code>{bid}</code>\n"
        f"Note: {esc(b.get('last_error') or '-')}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(btn("⏻ Start", f"bact:start:{bid}"), btn("⏹ Stop", f"bact:stop:{bid}"))
    kb.add(btn("♻️ Restart", f"bact:restart:{bid}"), btn("📜 Logs", f"bact:logs:{bid}"))
    kb.add(btn("📊 Accuracy", f"bact:acc:{bid}"), btn("🗑 Delete", f"bact:del:{bid}"))
    kb.add(btn("🔙 Back", "dep:list"))
    return text, kb


# ============================ FLOW ============================
def start_deploy(uid, chat_id):
    mine = [b for b in BOTS.values() if b["owner"] == uid and b["status"] != "denied"]
    if not _is_owner(uid) and len(mine) >= MAX_BOTS_PER_USER:
        return _bot.send_message(chat_id, f"⚠️ Limit reached: {MAX_BOTS_PER_USER} bots per user.")
    bid = uuid.uuid4().hex[:8]
    BOTS[bid] = {"owner": uid, "status": "awaiting_token", "created": time.time(),
                 "username": None, "bot_id": None, "run_dir": None, "last_error": None}
    os.makedirs(bot_dir(bid), exist_ok=True)
    save()
    waiting[uid] = {"stage": "token", "bid": bid}
    _bot.send_message(chat_id, "🔑 Send your bot <b>token</b> from @BotFather.\nThe message is deleted right after reading.\nSend /cancel to stop.")


def on_token(m):
    uid = m.from_user.id
    st = waiting[uid]
    try:
        _bot.delete_message(m.chat.id, m.message_id)
    except Exception:
        pass
    token = (m.text or "").strip()
    if not TOKEN_RE.fullmatch(token):
        return _bot.send_message(m.chat.id, "❌ Token format is wrong. Send it again or /cancel.")
    try:
        me = telebot.TeleBot(token).get_me()
    except Exception:
        return _bot.send_message(m.chat.id, "❌ Token invalid or Telegram unreachable. Try again or /cancel.")
    dup = any(b.get("bot_id") == me.id and b["status"] in ("pending", "approved", "running")
              for b in BOTS.values())
    if dup:
        return _bot.send_message(m.chat.id, "⚠️ This bot is already submitted or running.")
    bid = st["bid"]
    write_token(bid, token)
    BOTS[bid].update(username=me.username, bot_id=me.id)
    st["stage"] = "file"
    save()
    _bot.send_message(m.chat.id, f"✅ Verified <b>@{esc(me.username)}</b>\nNow send your bot as a <b>.zip</b> (full project) or <b>.py</b> file.\n/cancel to stop.")


def on_document(m):
    uid = m.from_user.id
    st = waiting[uid]
    doc = m.document
    name = (doc.file_name or "").lower()
    if not name.endswith((".zip", ".py")):
        return _bot.reply_to(m, "❌ Only .zip or .py files are allowed.")
    if (doc.file_size or 0) > MAX_UPLOAD:
        return _bot.reply_to(m, "❌ File too big (max 10 MB).")
    bid = st["bid"]
    d = bot_dir(bid)
    try:
        data = _bot.download_file(_bot.get_file(doc.file_id).file_path)
        if name.endswith(".zip"):
            zp = os.path.join(d, "upload.zip")
            with open(zp, "wb") as f:
                f.write(data)
            _extract_zip(zp, d)
            os.remove(zp)
        else:
            with open(os.path.join(d, "bot.py"), "wb") as f:
                f.write(data)
    except Exception as e:
        return _bot.reply_to(m, f"❌ Upload failed: {esc(e)}")

    run_dir = _find_entry(d)
    if not run_dir:
        return _bot.reply_to(m, "❌ bot.py not found inside the upload.")

    waiting.pop(uid, None)
    b = BOTS[bid]
    b.update(run_dir=run_dir, status="pending")
    save()
    checks, acc = check_dir(run_dir)
    report = format_checks(checks, acc)
    _bot.reply_to(m, f"📨 Sent to admin for approval.\n\n{report}")

    kb = types.InlineKeyboardMarkup().add(btn("✅ Allow", f"dapp:{bid}"), btn("❌ Deny", f"dden:{bid}"))
    who = esc(f"{m.from_user.first_name or ''} {m.from_user.last_name or ''}".strip())
    _bot.send_message(
        _owner,
        f"🚀 <b>NEW BOT DEPLOY REQUEST</b>\n👤 {who} (<code>{uid}</code>)\n🤖 @{esc(b.get('username'))}\nID: <code>{bid}</code>\n\n{report}",
        reply_markup=kb,
    )


def approve(bid):
    b = BOTS[bid]
    d = bot_dir(bid)
    req = os.path.join(b["run_dir"], "requirements.txt")
    if os.path.isfile(req):
        b["status"] = "approved"
        save()
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--target", os.path.join(d, "libs"), "-r", req],
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0:
            b["status"], b["last_error"] = "stopped", "pip install failed"
            save()
            return False, "pip install failed"
    return start_bot(bid)


def remove_bot(bid):
    stop_bot(bid)
    shutil.rmtree(bot_dir(bid), ignore_errors=True)
    BOTS.pop(bid, None)
    save()


# ============================ REGISTER ============================
def register(b):
    @b.message_handler(commands=["deploy"])
    def cmd_deploy(m):
        blocked = _gate(m.from_user.id)
        if blocked:
            return b.reply_to(m, blocked[0], reply_markup=blocked[1])
        b.reply_to(m, deploy_text(), reply_markup=deploy_kb(m.from_user.id))

    @b.message_handler(commands=["cancel"])
    def cmd_cancel(m):
        st = waiting.pop(m.from_user.id, None)
        if st:
            bid = st["bid"]
            if BOTS.get(bid, {}).get("status") == "awaiting_token":
                BOTS.pop(bid, None)
                shutil.rmtree(bot_dir(bid), ignore_errors=True)
                save()
            b.reply_to(m, "❌ Deploy cancelled.")

    @b.message_handler(content_types=["text"],
                       func=lambda m: waiting.get(m.from_user.id, {}).get("stage") == "token"
                       and not (m.text or "").startswith("/"))
    def _token_msg(m):
        on_token(m)

    @b.message_handler(content_types=["document"],
                       func=lambda m: waiting.get(m.from_user.id, {}).get("stage") == "file")
    def _file_msg(m):
        on_document(m)


def handle_callback(c):
    """Returns True if this callback belongs to the Deploy Center."""
    data = c.data or ""
    uid = c.from_user.id
    chat_id = c.message.chat.id
    mid = c.message.message_id

    def edit(text, kb=None):
        try:
            _bot.edit_message_text(text, chat_id, mid, reply_markup=kb)
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise

    if data == "m:deploy":
        edit(deploy_text(), deploy_kb(uid))
    elif data == "dep:new":
        _bot.answer_callback_query(c.id)
        start_deploy(uid, chat_id)
    elif data == "dep:list":
        edit(*bots_list_view(uid))
    elif data == "dep:all":
        if not _is_owner(uid):
            _bot.answer_callback_query(c.id, "⛔ Owner only.", show_alert=True)
            return True
        edit(*bots_list_view(uid, everyone=True))
    elif data.startswith("bot:"):
        bid = data.split(":", 1)[1]
        if not can_manage(uid, bid):
            _bot.answer_callback_query(c.id, "⛔ Not your bot.", show_alert=True)
            return True
        edit(*bot_detail_view(bid))
    elif data.startswith("bact:"):
        _, action, bid = data.split(":", 2)
        if not can_manage(uid, bid):
            _bot.answer_callback_query(c.id, "⛔ Not your bot.", show_alert=True)
            return True
        if action == "start":
            ok, msg = start_bot(bid)
            _bot.answer_callback_query(c.id, "Started ▶️" if ok else f"Failed: {msg}"[:190], show_alert=not ok)
            if not ok:
                BOTS[bid]["last_error"] = msg
                save()
            edit(*bot_detail_view(bid))
        elif action == "stop":
            stop_bot(bid)
            _bot.answer_callback_query(c.id, "Stopped ⏹")
            edit(*bot_detail_view(bid))
        elif action == "restart":
            stop_bot(bid)
            ok, msg = start_bot(bid)
            _bot.answer_callback_query(c.id, "Restarted ♻️" if ok else f"Failed: {msg}"[:190], show_alert=not ok)
            edit(*bot_detail_view(bid))
        elif action == "logs":
            _bot.answer_callback_query(c.id)
            _bot.send_message(chat_id, f"📜 <b>LOGS</b> <code>{bid}</code>\n<pre>{esc(tail(bid)[-3000:])}</pre>")
        elif action == "acc":
            _bot.answer_callback_query(c.id)
            run_dir = BOTS[bid].get("run_dir")
            if not run_dir:
                text = "⚠️ No code uploaded yet."
            else:
                checks, acc = check_dir(run_dir)
                text = format_checks(checks, acc)
            kb = types.InlineKeyboardMarkup().add(btn("🔙 Back", f"bot:{bid}"))
            edit(text, kb)
        elif action == "del":
            remove_bot(bid)
            _bot.answer_callback_query(c.id, "Deleted 🗑")
            edit(*bots_list_view(uid, everyone=_is_owner(uid)))
    elif data.startswith(("dapp:", "dden:")):
        if not _is_owner(uid):
            _bot.answer_callback_query(c.id, "⛔ Owner only.", show_alert=True)
            return True
        bid = data.split(":", 1)[1]
        if bid not in BOTS or BOTS[bid]["status"] != "pending":
            _bot.answer_callback_query(c.id, "Already handled.")
            return True
        owner_uid = BOTS[bid]["owner"]
        if data.startswith("dapp:"):
            ok, msg = approve(bid)
            decision = "✅ APPROVED" if ok else f"⚠️ APPROVED but failed ({msg})"
            try:
                _bot.send_message(owner_uid, "🎉 Your bot was approved and is running!" if ok
                                  else f"⚠️ Approved but could not start: {esc(msg)}")
            except Exception:
                pass
        else:
            BOTS[bid]["status"] = "denied"
            save()
            decision = "❌ DENIED"
            try:
                _bot.send_message(owner_uid, "🚫 Your bot deploy request was rejected by the admin.")
            except Exception:
                pass
        base = esc(c.message.text or "")
        edit(f"{base}\n\nDecision: {decision}")
        _bot.answer_callback_query(c.id, "Done")
    else:
        return False
    if not c.data.startswith(("bact:", "dapp:", "dden:")):
        try:
            _bot.answer_callback_query(c.id)
        except Exception:
            pass
    return True

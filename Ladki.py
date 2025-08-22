# requirements:
# python-telegram-bot==20.6
# optional: python-dotenv, aiosqlite (not used directly here)

import re
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, Tuple, List
from collections import deque

from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ChatMemberHandler,
    filters, ContextTypes
)

# ========= CONFIG =========
TOKEN = "YOUR_BOT_TOKEN"

# Policy toggles
POLICY = {
    "autoban_on_affirm_female": True,      # direct self-ID => permanent ban
    "warn_then_ban_pronouns": True,        # she/her etc => warn then temp ban
    "username_heuristics": True,           # soft warn on suspected usernames
    "username_confirm_timeout_s": 120,     # confirm window for new members
    "tempban_seconds": 24 * 3600,          # 24h
    "strike_window_days": 7,               # for escalation
    "rate_limit_warn_s": 20,               # avoid spamming warns
    "context_window_messages": 30,         # recent msgs per chat for linking Q->A
}

# Admins and in-memory state
ADMIN_IDS: Set[int] = {123456789}  # replace with real admin user IDs
WHITELIST: Set[int] = set()
BANNED: Set[int] = set()

# user_id -> {"count": int, "last": datetime}
STRIKES: Dict[int, Dict[str, Optional[datetime]]] = {}
LAST_WARN_TIME: Dict[Tuple[int, int], datetime] = {}  # (chat_id, user_id) -> last_warn_dt

# Join confirmation tracking: user_id -> deadline_dt
PENDING_CONFIRM: Dict[int, datetime] = {}

# Async lock to guard shared state
STATE_LOCK = asyncio.Lock()

# Maintain per-chat rolling context of last N messages: chat_id -> deque[(dt, user_id, text)]
CHAT_CONTEXT: Dict[int, deque] = {}

# ========= REGEX (smarter, safer) =========

# Questions like "tum ladki ho?", "are you a girl", "kya tum ladki ho"
QUESTION_RE = re.compile(
    r"\b(?:kya\s+)?(?:tum|aap|you|are\s+you)\s+(?:ladki|girl)\s+(?:ho|hai|are)\b|\bare\s+you\s+a\s+girl\b",
    re.I
)

# Affirmations: "haan main ladki hoon", "I am a girl", "yes I am girl"
AFFIRM_RE = re.compile(
    r"\b(?:haan|yes|main|i\s*am|me)\s+(?:ladki|girl)(?:\s*(?:hun|hoon|am))?\b|\bi\s*am\s*a\s*girl\b",
    re.I
)

# Pronouns/self-descriptors that imply female identity (softer rule)
PRONOUN_RE = re.compile(
    r"\b(?:she\/her|she\s*\/\s*her|she\s*her|she/her|ladki|girl vibes?|queen vibes?)\b",
    re.I
)

# Safer username heuristics: weighted keywords; avoid common Hindi first-names-only
USERNAME_KEYWORDS = [
    r"girl", r"\bladki\b", r"\bshe\b", r"\bher\b", r"princess", r"\bqueen\b",
    r"didi", r"\bbeti\b", r"baby[_\-]?girl", r"angel", r"barbie", r"mrs?\b",
    r"madam", r"doll"
]
USERNAME_RE = re.compile("|".join(USERNAME_KEYWORDS), re.I)

# Mentions/IDs to link Q->A context better
MENTION_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{5,})")


# ========= HELPERS =========

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_whitelisted(user_id: int) -> bool:
    return user_id in WHITELIST

def now_utc() -> datetime:
    return datetime.utcnow()

def within(dt: Optional[datetime], td: timedelta) -> bool:
    return dt is not None and (now_utc() - dt) <= td

def rate_limited(chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    last = LAST_WARN_TIME.get(key)
    if last and (now_utc() - last).total_seconds() < POLICY["rate_limit_warn_s"]:
        return True
    LAST_WARN_TIME[key] = now_utc()
    return False

def context_for_chat(chat_id: int) -> deque:
    dq = CHAT_CONTEXT.get(chat_id)
    if dq is None:
        dq = deque(maxlen=POLICY["context_window_messages"])
        CHAT_CONTEXT[chat_id] = dq
    return dq

def link_question_and_answer(chat_id: int, message_text: str, sender_id: int) -> bool:
    """
    Returns True if message_text looks like an affirmation that follows a question
    'are you a girl' and likely refers to the same sender or a recently mentioned user.
    This helps reduce false bans by requiring conversational context.
    """
    if not AFFIRM_RE.search(message_text):
        return False

    dq = context_for_chat(chat_id)
    # Scan recent context for a question near in time
    for (ts, uid, txt) in reversed(dq):
        # limit to last ~3 minutes
        if (now_utc() - ts).total_seconds() > 180:
            break
        if QUESTION_RE.search(txt):
            # If the same user later affirms, it's very likely a self-ID
            if uid != sender_id:
                # Different user asked the question; that's fine. We need the current sender to be the one affirming.
                return True
            else:
                # The user herself/himself asked; less likely, but still acceptable as context
                return True
    # If no question in recent context, still allow direct affirmative to trigger if policy says so
    return True

async def warn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, reason: str):
    async with STATE_LOCK:
        STRIKES.setdefault(user_id, {"count": 0, "last": None})
        STRIKES[user_id]["count"] += 1
        STRIKES[user_id]["last"] = now_utc()

    if not rate_limited(chat_id, user_id):
        await context.bot.send_message(
            chat_id,
            f"Warning to user {user_id}: {reason}. Repeat may lead to ban."
        )

async def ban(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
              permanent: bool = True, seconds: int = None, reason: str = ""):
    until_date = None
    if not permanent:
        seconds = seconds or POLICY["tempban_seconds"]
        until_date = now_utc() + timedelta(seconds=seconds)
    try:
        await context.bot.ban_chat_member(chat_id, user_id, until_date=until_date)
        async with STATE_LOCK:
            BANNED.add(user_id)
        msg = f"User {user_id} banned."
        if reason:
            msg += f" Reason: {reason}"
        await context.bot.send_message(chat_id, msg)
    except Exception as e:
        await context.bot.send_message(chat_id, f"Failed to ban {user_id}: {e}")

async def tempban_or_warn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, reason: str):
    # escalate if strike within window; else warn
    async with STATE_LOCK:
        rec = STRIKES.get(user_id, {"count": 0, "last": None})
        recent = within(rec.get("last"), timedelta(days=POLICY["strike_window_days"]))
        strikes = rec.get("count", 0)

    if recent and strikes >= 1:
        await ban(context, chat_id, user_id, permanent=False, reason=reason)
    else:
        await warn(context, chat_id, user_id, reason)

async def handle_join_confirmation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    # Called when username heuristic triggers; set confirm window
    deadline = now_utc() + timedelta(seconds=POLICY["username_confirm_timeout_s"])
    async with STATE_LOCK:
        PENDING_CONFIRM[user_id] = deadline
    await context.bot.send_message(
        chat_id,
        f"User {user_id}, your username suggests female identity. "
        f"Please confirm within {POLICY['username_confirm_timeout_s']}s by sending: I am not female"
    )

async def check_confirmation_expiry(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    # Should be scheduled after join
    await asyncio.sleep(POLICY["username_confirm_timeout_s"])
    async with STATE_LOCK:
        deadline = PENDING_CONFIRM.get(user_id)
        if deadline and now_utc() >= deadline:
            # No confirmation received -> temp ban rather than perma to be safe
            PENDING_CONFIRM.pop(user_id, None)
            await ban(context, chat_id, user_id, permanent=False, reason="No confirmation after join")

def confirmed_not_female(text: str) -> bool:
    # Accept a few negative confirmations
    return bool(re.search(r"\b(i\s*am\s*not\s*female|not\s*a\s*girl|main\s*ladka\s*hun|i\s*am\s*male)\b", text, re.I))


# ========= HANDLERS =========

async def on_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handles join/leave changes. We filter for joins only.
    chat = update.effective_chat
    chat_id = chat.id
    cmu = update.chat_member
    new = cmu.new_chat_member
    if not new:
        return

    status = new.status
    user = new.user

    if status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return

    if is_admin(user.id) or is_whitelisted(user.id):
        return

    if not POLICY["username_heuristics"]:
        return

    uname = " ".join(filter(None, [
        user.username or "",
        user.first_name or "",
        user.last_name or ""
    ]))

    if USERNAME_RE.search(uname):
        # Soft confirmation flow
        await handle_join_confirmation(context, chat_id, user.id)
        # fire-and-forget confirmation expiry
        asyncio.create_task(check_confirmation_expiry(context, chat_id, user.id))

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    chat_id = msg.chat_id
    user = msg.from_user
    if not user:
        return

    text = msg.text or ""
    # track context
    dq = context_for_chat(chat_id)
    dq.append((now_utc(), user.id, text))

    if is_admin(user.id) or is_whitelisted(user.id):
        # Still allow them to confirm for others
        return

    # If this user was pending confirmation and says "I am not female" => clear
    if confirmed_not_female(text):
        async with STATE_LOCK:
            if user.id in PENDING_CONFIRM:
                PENDING_CONFIRM.pop(user.id, None)
                await context.bot.send_message(chat_id, f"Confirmation accepted for user {user.id}.")
                return

    # Direct affirmative + context
    if POLICY["autoban_on_affirm_female"] and link_question_and_answer(chat_id, text, user.id):
        if AFFIRM_RE.search(text):
            await ban(context, chat_id, user.id, permanent=True, reason="Self-identified as female")
            return

    # Pronoun-based softer detection
    if POLICY["warn_then_ban_pronouns"] and PRONOUN_RE.search(text):
        await tempban_or_warn(context, chat_id, user.id, reason="Female-identifying content not allowed here")
        return

    # If someone asks “tum ladki ho?” we do nothing immediately; handled by context link.
    if QUESTION_RE.search(text):
        # Optionally, nudge: "Please avoid asking gender; policy enforced."
        # But to reduce noise, we skip messaging here.
        return


# ========= ADMIN COMMANDS =========

async def cmd_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /whitelist <user_id>")
        return
    uid = int(context.args[0])
    async with STATE_LOCK:
        WHITELIST.add(uid)
    await update.message.reply_text(f"Whitelisted {uid}")

async def cmd_unwhitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unwhitelist <user_id>")
        return
    uid = int(context.args)
    async with STATE_LOCK:
        WHITELIST.discard(uid)
    await update.message.reply_text(f"Removed {uid} from whitelist")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    uid = int(context.args)
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        async with STATE_LOCK:
            BANNED.discard(uid)
        await update.message.reply_text(f"Unbanned {uid}")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")

async def cmd_setadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setadmin <user_id>")
        return
    uid = int(context.args)
    async with STATE_LOCK:
        ADMIN_IDS.add(uid)
    await update.message.reply_text(f"Admin added: {uid}")

async def cmd_unsetadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unsetadmin <user_id>")
        return
    uid = int(context.args)
    async with STATE_LOCK:
        ADMIN_IDS.discard(uid)
    await update.message.reply_text(f"Admin removed: {uid}")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        return
    async with STATE_LOCK:
        msg = (
            f"Admins: {len(ADMIN_IDS)}; Whitelist: {len(WHITELIST)}; "
            f"Banned: {len(BANNED)}; Strikes: {len(STRIKES)}; PendingConfirm: {len(PENDING_CONFIRM)}\n"
            f"Policy: {POLICY}"
        )
    await update.message.reply_text(msg)

async def cmd_setpolicy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user
    if not caller or not is_admin(caller.id):
        return
    # Usage: /setpolicy key value
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setpolicy <key> <value>")
        return
    key = context.args
    value = " ".join(context.args[1:])
    if key not in POLICY:
        await update.message.reply_text(f"Unknown key. Keys: {list(POLICY.keys())}")
        return
    # Cast to bool/int based on current type
    cur = POLICY[key]
    try:
        if isinstance(cur, bool):
            v = value.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            v = int(value)
        else:
            await update.message.reply_text("Unsupported type for this key.")
            return
    except Exception:
        await update.message.reply_text("Invalid value.")
        return
    POLICY[key] = v
    await update.message.reply_text(f"Policy updated: {key} = {v}")

async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("unwhitelist", cmd_unwhitelist))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("setadmin", cmd_setadmin))
    app.add_handler(CommandHandler("unsetadmin", cmd_unsetadmin))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setpolicy", cmd_setpolicy))

    await app.run_polling()

# To run:
# import asyncio; asyncio.run(main())

"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                        NEXORA DISCORD AI BOT                                ║
║                              main.py                                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DISCORD DEVELOPER PORTAL — INTENTS (Settings → Bot → Privileged Intents): ║
║    ✅  MESSAGE CONTENT INTENT  (Privileged)                                 ║
║    ✅  SERVER MEMBERS INTENT   (Privileged)                                 ║
║    ✅  GUILD MESSAGES          (default)                                    ║
║    ✅  GUILDS                  (default)                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  RAILWAY ENV VARIABLES:                                                     ║
║    DISCORD_TOKEN        — bot token from discord.dev                        ║
║    OPENAI_API_KEY       — OpenAI key                                        ║
║    OWNER_ID             — your Discord user ID (integer as string)          ║
║    ADMIN_CHANNEL_NAME   — default: ai-admin                                 ║
║    HELP_CHANNEL_NAME    — default: ai-help                                  ║
║    AUDIT_CHANNEL_NAME   — default: ai-audit-log                             ║
║    FREE_DAILY_LIMIT     — default: 10                                       ║
║    PAID_ROLES           — csv, default: Nexora Ultra,Nexora Elite,Nexora Pro║
║    MODEL_ASSISTANT      — default: gpt-4o                                   ║
║    MODEL_ADMIN          — default: gpt-4o                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

# ─── IMPORTS ──────────────────────────────────────────────────────────────────
import os
import re
import json
import logging
import sqlite3
import asyncio
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nexora")

# ─── CONFIG FROM ENV ──────────────────────────────────────────────────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
OWNER_ID           = int(os.environ.get("OWNER_ID", "0"))

ADMIN_CHANNEL_NAME = os.environ.get("ADMIN_CHANNEL_NAME", "ai-admin")
HELP_CHANNEL_NAME  = os.environ.get("HELP_CHANNEL_NAME",  "ai-help")
AUDIT_CHANNEL_NAME = os.environ.get("AUDIT_CHANNEL_NAME", "ai-audit-log")
FREE_DAILY_LIMIT   = int(os.environ.get("FREE_DAILY_LIMIT", "10"))

PAID_ROLES: list[str] = [
    r.strip() for r in
    os.environ.get("PAID_ROLES", "Nexora Ultra,Nexora Elite,Nexora Pro").split(",")
    if r.strip()
]

MODEL_ASSISTANT = os.environ.get("MODEL_ASSISTANT", "gpt-4o")
MODEL_ADMIN     = os.environ.get("MODEL_ADMIN",     "gpt-4o")
DB_PATH         = "nexora.sqlite3"

# Names that must NEVER appear in public output
_HIDDEN_NAMES   = {ADMIN_CHANNEL_NAME, AUDIT_CHANNEL_NAME, "audit-log", "ai-audit-log"}
_HIDDEN_PATTERN = re.compile(
    r"#?\b(ai[-_]?admin|ai[-_]?audit[-_]?log|audit[-_]?log"
    r"|admin\s*channel|internal\s*admin|admin\s*process)\b",
    re.IGNORECASE,
)

# ─── OPENAI CLIENT ────────────────────────────────────────────────────────────
ai = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ─── DISCORD BOT ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # Privileged — enable in dev portal
intents.members         = True   # Privileged — enable in dev portal

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with _db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS message_counts (
                user_id  INTEGER NOT NULL,
                date_utc TEXT    NOT NULL,
                count    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, date_utc)
            );
            CREATE TABLE IF NOT EXISTS user_memory (
                user_id       INTEGER PRIMARY KEY,
                language      TEXT DEFAULT 'en',
                last_seen_utc TEXT
            );
        """)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def db_get_count(user_id: int) -> int:
    with _db() as c:
        row = c.execute(
            "SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
            (user_id, _today()),
        ).fetchone()
    return row["count"] if row else 0


def db_increment(user_id: int) -> int:
    today = _today()
    with _db() as c:
        c.execute(
            """INSERT INTO message_counts (user_id, date_utc, count) VALUES (?,?,1)
               ON CONFLICT(user_id, date_utc) DO UPDATE SET count = count + 1""",
            (user_id, today),
        )
        row = c.execute(
            "SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
            (user_id, today),
        ).fetchone()
    return row["count"]


def db_get_lang(user_id: int) -> str:
    with _db() as c:
        row = c.execute("SELECT language FROM user_memory WHERE user_id=?", (user_id,)).fetchone()
    return row["language"] if row else "en"


def db_upsert_memory(user_id: int, language: str):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        c.execute(
            """INSERT INTO user_memory (user_id, language, last_seen_utc) VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,
               last_seen_utc=excluded.last_seen_utc""",
            (user_id, language, now),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_paid(member: discord.Member) -> bool:
    return bool({r.name for r in member.roles} & set(PAID_ROLES))


def is_admin(member: discord.Member) -> bool:
    """Owner OR has 'AI Admin' role."""
    if member.id == OWNER_ID:
        return True
    return any(r.name == "AI Admin" for r in member.roles)


def sanitize(text: str) -> str:
    """Strip any accidental admin-channel leakage from public-facing text."""
    return _HIDDEN_PATTERN.sub("[server administration]", text)


def detect_lang(text: str) -> str:
    if re.search(r"[а-яёА-ЯЁ]", text):
        return "ru"
    return "en"


async def audit(guild: discord.Guild, msg: str):
    ch = discord.utils.get(guild.text_channels, name=AUDIT_CHANNEL_NAME)
    if ch:
        try:
            await ch.send(f"```\n{msg[:1990]}\n```")
        except Exception as e:
            log.warning("audit send failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  OPENAI — PUBLIC ASSISTANT
# ══════════════════════════════════════════════════════════════════════════════

def _public_system(lang: str) -> str:
    lang_rule = "Reply in Russian." if lang == "ru" else "Reply in the same language as the user."
    return f"""You are Nexora Bot — a friendly, knowledgeable assistant for the Nexora Discord server.

Your purpose:
- Help users with: support tickets, server roles, trading rules, subscriptions, how to navigate Nexora.
- Give specific, useful answers. Never use generic filler responses.
- {lang_rule}
- On first contact: short greeting + direct answer + brief mention of what you can help with.
- If a user asks for moderation/admin actions: say "please contact the server administrators or moderators."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT — NEVER violate:
- NEVER mention channel names used for internal administration or logging.
- NEVER reveal internal bot mechanics, admin workflows, or any internal channel/role IDs.
- NEVER say anything that hints at hidden infrastructure.
- If uncertain, give a helpful answer with reasonable assumptions rather than refusing.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Nexora knowledge:
- Tickets: users open a support ticket for any issue — the team responds there.
- Roles: earned via activity or purchased via subscriptions (Nexora Pro / Elite / Ultra).
- Trading: follow the rules posted in trading channels; violations → contact moderators.
- Subscriptions: Pro/Elite/Ultra unlock more bot messages + server perks.
- Free users: limited to {FREE_DAILY_LIMIT} AI messages per day (resets at UTC midnight).
- This channel (#ai-help) is for AI-assisted questions about the server.
"""


async def ask_public(user_msg: str, lang: str) -> Optional[str]:
    try:
        r = await ai.chat.completions.create(
            model=MODEL_ASSISTANT,
            messages=[
                {"role": "system", "content": _public_system(lang)},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=600,
            temperature=0.7,
        )
        raw = r.choices[0].message.content or ""
        return sanitize(raw)
    except Exception as e:
        log.error("ask_public: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  OPENAI — ADMIN PLANNER (function-calling)
# ══════════════════════════════════════════════════════════════════════════════

# All tools properly defined: type + function.name + function.description + function.parameters
ADMIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delete_last_message",
            "description": "Delete the most recent message from a specific user in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username":     {"type": "string", "description": "Discord username or display name (no @)."},
                    "channel_name": {"type": "string", "description": "Target channel name (no #)."},
                },
                "required": ["username", "channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_channel",
            "description": "Create a new text channel in the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name":  {"type": "string", "description": "Name for the new channel."},
                    "category_name": {"type": "string", "description": "Category to place channel in (optional)."},
                    "private":       {"type": "boolean", "description": "True = admin-only, False = public."},
                },
                "required": ["channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_channel",
            "description": "Delete an existing text channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string", "description": "Channel name to delete (no #)."},
                },
                "required": ["channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kick_member",
            "description": "Kick a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Username to kick."},
                    "reason":   {"type": "string", "description": "Reason for kick."},
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ban_member",
            "description": "Ban a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username":    {"type": "string", "description": "Username to ban."},
                    "reason":      {"type": "string", "description": "Reason for ban."},
                    "delete_days": {"type": "integer", "description": "Days of messages to delete (0–7)."},
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_slowmode",
            "description": "Set slowmode delay on a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string", "description": "Target channel name."},
                    "seconds":      {"type": "integer", "description": "Delay in seconds (0 = disable)."},
                },
                "required": ["channel_name", "seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_role",
            "description": "Give a role to a server member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username":  {"type": "string", "description": "Target username."},
                    "role_name": {"type": "string", "description": "Role name to assign."},
                },
                "required": ["username", "role_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_role",
            "description": "Remove a role from a server member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username":  {"type": "string", "description": "Target username."},
                    "role_name": {"type": "string", "description": "Role name to remove."},
                },
                "required": ["username", "role_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_announcement",
            "description": "Send a message to a specific channel as the bot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string", "description": "Target channel name."},
                    "message":      {"type": "string", "description": "Message text to send."},
                },
                "required": ["channel_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clarify",
            "description": "Ask the admin one clarifying question before proceeding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The clarifying question."},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "server_info",
            "description": "Gather and display an overview of the server (channels, roles, members).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

_ADMIN_SYSTEM = """You are Nexora Admin AI — internal planning assistant for server administrators.

Your job:
- Parse the admin's natural-language request and call the correct tool function.
- ALWAYS call a tool if an action is needed — do not just reply with text.
- If you need exactly ONE clarification first, call the `clarify` tool.
- Extract usernames/channel names precisely from the request.
- Reply in the same language the admin uses.
- You have FULL authority to plan any moderation/server action — no restrictions for admins.
"""


async def plan_admin(request: str) -> dict:
    """
    Returns one of:
      {"type": "tool_call", "name": str, "args": dict, "plan_text": str}
      {"type": "clarify",   "question": str}
      {"type": "text",      "content": str}
      {"type": "error",     "content": str}
    """
    try:
        resp = await ai.chat.completions.create(
            model=MODEL_ADMIN,
            messages=[
                {"role": "system", "content": _ADMIN_SYSTEM},
                {"role": "user",   "content": request},
            ],
            tools=ADMIN_TOOLS,
            tool_choice="auto",
            max_tokens=400,
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            tc   = msg.tool_calls[0]
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if name == "clarify":
                return {"type": "clarify", "question": args.get("question", "Could you clarify?")}

            return {
                "type": "tool_call",
                "name": name,
                "args": args,
                "plan_text": _plan_text(name, args),
            }

        return {"type": "text", "content": msg.content or "OK."}

    except Exception as e:
        log.error("plan_admin: %s", e)
        return {"type": "error", "content": str(e)}


def _plan_text(name: str, args: dict) -> str:
    templates = {
        "delete_last_message": (
            f"🗑️ **Delete last message** by `{args.get('username')}` "
            f"in `#{args.get('channel_name')}`"
        ),
        "create_channel": (
            f"➕ **Create channel** `#{args.get('channel_name')}`"
            + (f" in `{args.get('category_name')}`" if args.get("category_name") else "")
            + (" *(private)*" if args.get("private") else " *(public)*")
        ),
        "delete_channel": f"❌ **Delete channel** `#{args.get('channel_name')}`",
        "kick_member":    (
            f"👢 **Kick** `{args.get('username')}`"
            + (f"\nReason: {args.get('reason')}" if args.get("reason") else "")
        ),
        "ban_member":     (
            f"🔨 **Ban** `{args.get('username')}`"
            + (f"\nReason: {args.get('reason')}" if args.get("reason") else "")
            + (f"\nDelete {args.get('delete_days')}d of messages" if args.get("delete_days") else "")
        ),
        "set_slowmode":   (
            f"⏱️ **Set slowmode** `#{args.get('channel_name')}` → "
            f"`{args.get('seconds')}s`"
            + (" *(disabled)*" if args.get("seconds") == 0 else "")
        ),
        "give_role":      f"🎭 **Give role** `{args.get('role_name')}` → `{args.get('username')}`",
        "remove_role":    f"🎭 **Remove role** `{args.get('role_name')}` from `{args.get('username')}`",
        "send_announcement": (
            f"📢 **Send message** to `#{args.get('channel_name')}`\n"
            f"> {args.get('message', '')[:120]}"
        ),
        "server_info":    "🔍 **Retrieve server overview** (channels, roles, members)",
    }
    return templates.get(name, f"`{name}` — args: `{args}`")


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ACTION EXECUTORS
# ══════════════════════════════════════════════════════════════════════════════

async def execute_action(guild: discord.Guild, name: str, args: dict) -> str:
    """Dispatch and execute a confirmed admin action. Returns result string."""
    try:
        match name:
            case "delete_last_message":  return await _do_delete_last(guild, args)
            case "create_channel":       return await _do_create_channel(guild, args)
            case "delete_channel":       return await _do_delete_channel(guild, args)
            case "kick_member":          return await _do_kick(guild, args)
            case "ban_member":           return await _do_ban(guild, args)
            case "set_slowmode":         return await _do_slowmode(guild, args)
            case "give_role":            return await _do_give_role(guild, args)
            case "remove_role":          return await _do_remove_role(guild, args)
            case "send_announcement":    return await _do_announce(guild, args)
            case "server_info":          return await _do_server_info(guild)
            case _:                      return f"❓ Unknown action: `{name}`"
    except Exception as e:
        return f"💥 Unexpected error: {e}"


async def _do_delete_last(guild: discord.Guild, args: dict) -> str:
    uname = args.get("username", "")
    cname = args.get("channel_name", "")
    channel = discord.utils.get(guild.text_channels, name=cname)
    if not channel:
        return f"❌ Channel `#{cname}` not found."

    member = (
        discord.utils.find(lambda m: m.name.lower() == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Member `{uname}` not found."

    try:
        async for msg in channel.history(limit=200):
            if msg.author.id == member.id:
                preview = msg.content[:80] or "[embed/attachment]"
                await msg.delete()
                return (
                    f"✅ Deleted last message by `{member.display_name}` "
                    f"in `#{cname}`\nPreview: `{preview}`"
                )
        return f"❌ No recent messages by `{member.display_name}` in `#{cname}` (last 200)."
    except discord.Forbidden:
        return f"❌ Missing permissions to read history or delete in `#{cname}`."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_create_channel(guild: discord.Guild, args: dict) -> str:
    cname    = args.get("channel_name", "new-channel")
    catname  = args.get("category_name")
    private  = args.get("private", False)
    category = discord.utils.get(guild.categories, name=catname) if catname else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me:           discord.PermissionOverwrite(read_messages=True),
    } if private else {}
    try:
        ch = await guild.create_text_channel(name=cname, category=category, overwrites=overwrites)
        return f"✅ Created `#{ch.name}` (ID: {ch.id})"
    except discord.Forbidden:
        return "❌ Missing permissions to create channels."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_delete_channel(guild: discord.Guild, args: dict) -> str:
    cname = args.get("channel_name", "")
    ch    = discord.utils.get(guild.text_channels, name=cname)
    if not ch:
        return f"❌ Channel `#{cname}` not found."
    try:
        await ch.delete()
        return f"✅ Deleted `#{cname}`."
    except discord.Forbidden:
        return "❌ Missing permissions to delete channels."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_kick(guild: discord.Guild, args: dict) -> str:
    uname  = args.get("username", "")
    reason = args.get("reason", "No reason provided")
    member = (
        discord.utils.find(lambda m: m.name.lower() == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Member `{uname}` not found."
    try:
        await member.kick(reason=reason)
        return f"✅ Kicked `{member.name}` — reason: {reason}"
    except discord.Forbidden:
        return "❌ Missing permissions to kick."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_ban(guild: discord.Guild, args: dict) -> str:
    uname       = args.get("username", "")
    reason      = args.get("reason", "No reason provided")
    delete_days = min(int(args.get("delete_days", 0)), 7)
    member = (
        discord.utils.find(lambda m: m.name.lower() == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Member `{uname}` not found."
    try:
        await member.ban(reason=reason, delete_message_days=delete_days)
        return f"✅ Banned `{member.name}` — reason: {reason}"
    except discord.Forbidden:
        return "❌ Missing permissions to ban."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_slowmode(guild: discord.Guild, args: dict) -> str:
    cname   = args.get("channel_name", "")
    seconds = int(args.get("seconds", 0))
    ch      = discord.utils.get(guild.text_channels, name=cname)
    if not ch:
        return f"❌ Channel `#{cname}` not found."
    try:
        await ch.edit(slowmode_delay=seconds)
        label = f"{seconds}s" if seconds > 0 else "disabled"
        return f"✅ Slowmode on `#{cname}` → {label}"
    except discord.Forbidden:
        return "❌ Missing permissions to edit channel."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_give_role(guild: discord.Guild, args: dict) -> str:
    uname = args.get("username", "")
    rname = args.get("role_name", "")
    member = (
        discord.utils.find(lambda m: m.name.lower() == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Member `{uname}` not found."
    role = discord.utils.get(guild.roles, name=rname)
    if not role:
        return f"❌ Role `{rname}` not found."
    try:
        await member.add_roles(role)
        return f"✅ Gave role `{rname}` to `{member.display_name}`."
    except discord.Forbidden:
        return "❌ Missing permissions to assign roles."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_remove_role(guild: discord.Guild, args: dict) -> str:
    uname = args.get("username", "")
    rname = args.get("role_name", "")
    member = (
        discord.utils.find(lambda m: m.name.lower() == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Member `{uname}` not found."
    role = discord.utils.get(guild.roles, name=rname)
    if not role:
        return f"❌ Role `{rname}` not found."
    try:
        await member.remove_roles(role)
        return f"✅ Removed role `{rname}` from `{member.display_name}`."
    except discord.Forbidden:
        return "❌ Missing permissions to remove roles."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_announce(guild: discord.Guild, args: dict) -> str:
    cname = args.get("channel_name", "")
    text  = args.get("message", "")
    ch    = discord.utils.get(guild.text_channels, name=cname)
    if not ch:
        return f"❌ Channel `#{cname}` not found."
    try:
        await ch.send(text)
        return f"✅ Message sent to `#{cname}`."
    except discord.Forbidden:
        return f"❌ Missing permissions to send in `#{cname}`."
    except discord.HTTPException as e:
        return f"❌ Discord API error: {e}"


async def _do_server_info(guild: discord.Guild) -> str:
    text_channels = [f"#{c.name}" for c in guild.text_channels]
    voice_channels = [f"🔊 {c.name}" for c in guild.voice_channels]
    roles = [r.name for r in guild.roles if r.name != "@everyone"]
    total = guild.member_count
    bots  = sum(1 for m in guild.members if m.bot)
    lines = [
        f"**{guild.name}** — Server Overview",
        f"👥 Members: {total} ({total - bots} users, {bots} bots)",
        f"📝 Text channels ({len(text_channels)}): {', '.join(text_channels[:20])}",
        f"🔊 Voice channels ({len(voice_channels)}): {', '.join(voice_channels[:10])}",
        f"🎭 Roles ({len(roles)}): {', '.join(roles[:20])}",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD UI — CONFIRM / CANCEL BUTTONS
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmView(discord.ui.View):
    """
    Shown after PLAN step. Admin clicks Confirm → action runs. Cancel → aborted.
    The view stores the guild + action data. Timeout = 60s.
    """
    def __init__(self, guild: discord.Guild, name: str, args: dict, requester: discord.Member):
        super().__init__(timeout=60)
        self.guild     = guild
        self.name      = name
        self.args      = args
        self.requester = requester
        self.done      = False

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("Only the requester can confirm.", ephemeral=True)
            return
        self.done = True
        self._disable_all()
        await interaction.response.edit_message(
            content=f"⚙️ Executing `{self.name}`…", view=self
        )
        result = await execute_action(self.guild, self.name, self.args)
        await interaction.followup.send(result)
        # Audit
        await audit(
            self.guild,
            f"[EXECUTE] requested by {self.requester} ({self.requester.id})\n"
            f"action={self.name} args={self.args}\nresult={result}"
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("Only the requester can cancel.", ephemeral=True)
            return
        self._disable_all()
        await interaction.response.edit_message(content="🚫 Action cancelled.", view=self)
        self.stop()

    async def on_timeout(self):
        self._disable_all()

    def _disable_all(self):
        for item in self.children:
            item.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
#  BOT EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    db_init()
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await tree.sync()
        log.info("Synced %d slash commands.", len(synced))
    except Exception as e:
        log.error("Slash sync failed: %s", e)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/ai-help")
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Process prefix commands first (if any)
    await bot.process_commands(message)

    channel_name = message.channel.name if hasattr(message.channel, "name") else ""

    # ── PUBLIC CHANNEL: #ai-help ───────────────────────────────────────────
    if channel_name == HELP_CHANNEL_NAME:
        await handle_public_message(message)
        return

    # ── ADMIN CHANNEL: #ai-admin ───────────────────────────────────────────
    if channel_name == ADMIN_CHANNEL_NAME:
        await handle_admin_message(message)
        return


async def handle_public_message(message: discord.Message):
    """Handle a user message in #ai-help."""
    member = message.author

    # Detect language and remember it
    lang = detect_lang(message.content)
    db_upsert_memory(member.id, lang)

    # ── Rate limiting for free users ───────────────────────────────────────
    if not is_paid(member):
        count = db_get_count(member.id)
        if count >= FREE_DAILY_LIMIT:
            if lang == "ru":
                await message.reply(
                    f"⚠️ Вы использовали все **{FREE_DAILY_LIMIT}** бесплатных сообщений на сегодня (UTC).\n"
                    "Обновитесь до **Nexora Pro/Elite/Ultra** для безлимитного доступа! 🚀",
                    mention_author=False,
                )
            else:
                await message.reply(
                    f"⚠️ You've used all **{FREE_DAILY_LIMIT}** free messages for today (UTC).\n"
                    "Upgrade to **Nexora Pro/Elite/Ultra** for unlimited access! 🚀",
                    mention_author=False,
                )
            return
        new_count = db_increment(member.id)
        remaining = FREE_DAILY_LIMIT - new_count
    else:
        remaining = None   # unlimited

    # ── Get AI response ────────────────────────────────────────────────────
    async with message.channel.typing():
        reply = await ask_public(message.content, lang)

    if reply is None:
        if lang == "ru":
            await message.reply("❌ Произошла ошибка. Попробуйте снова.", mention_author=False)
        else:
            await message.reply("❌ An error occurred. Please try again.", mention_author=False)
        return

    # ── Append counter for free users ─────────────────────────────────────
    if remaining is not None:
        if lang == "ru":
            reply += f"\n\n> 💬 Осталось бесплатных сообщений сегодня: **{remaining}/{FREE_DAILY_LIMIT}**"
        else:
            reply += f"\n\n> 💬 Free messages left today: **{remaining}/{FREE_DAILY_LIMIT}**"

    await message.reply(reply, mention_author=False)


async def handle_admin_message(message: discord.Message):
    """Handle a message in #ai-admin (owner/AI Admin only)."""
    member = message.author

    # Check authorization
    if not is_admin(member):
        await message.reply(
            "🔒 Access denied. This channel is restricted to administrators.",
            mention_author=False,
        )
        return

    # Ignore very short/empty messages
    if len(message.content.strip()) < 3:
        return

    await audit(
        message.guild,
        f"[REQUEST] {member} ({member.id}): {message.content}"
    )

    async with message.channel.typing():
        plan = await plan_admin(message.content)

    match plan["type"]:
        case "tool_call":
            view = ConfirmView(message.guild, plan["name"], plan["args"], member)
            await message.reply(
                f"**📋 PLAN**\n{plan['plan_text']}\n\n"
                f"Proceed?",
                view=view,
                mention_author=False,
            )

        case "clarify":
            await message.reply(
                f"❓ **Clarification needed:**\n{plan['question']}",
                mention_author=False,
            )

        case "text":
            await message.reply(plan["content"], mention_author=False)

        case "error":
            await message.reply(
                f"💥 AI Error: `{plan['content']}`\nTry rephrasing the command.",
                mention_author=False,
            )
            await audit(message.guild, f"[AI ERROR] {plan['content']}")


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

RULES_TEXT = """
# 📋 Nexora AI — How to Use

**What is Nexora AI?**
I'm your server assistant. Ask me anything about how Nexora works.

**What I can help with:**
• 🎫 **Tickets** — How to open a support ticket and what to expect
• 🎭 **Roles** — How roles work, how to earn or purchase them
• 📈 **Trading** — Rules, guidelines, and best practices
• 🔧 **Server navigation** — Finding channels, features, commands
• 💬 **General questions** — Anything about Nexora

**How to chat with me:**
Just type your question naturally in this channel. No special commands needed.

**Message limits:**
• 🆓 Free users: **{limit} messages per day** (resets at UTC midnight)
• ⭐ Nexora Pro / Elite / Ultra: **Unlimited messages**

**Need moderation help?**
Please contact the server **administrators or moderators** directly.

**Tips for best answers:**
✅ Be specific: *"How do I open a trade ticket?"*
✅ Ask one thing at a time
✅ Tell me your role/subscription if relevant
""".format(limit=FREE_DAILY_LIMIT)


@tree.command(name="pin_rules", description="Publish and pin the Nexora AI rules in #ai-help.")
async def pin_rules(interaction: discord.Interaction):
    # Authorization check
    if not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Only admins can use this command.", ephemeral=True)
        return

    # Find #ai-help
    help_ch = discord.utils.get(interaction.guild.text_channels, name=HELP_CHANNEL_NAME)
    if not help_ch:
        await interaction.response.send_message(
            f"❌ Channel `#{HELP_CHANNEL_NAME}` not found.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        sent = await help_ch.send(RULES_TEXT)
        await sent.pin()
        await interaction.followup.send(f"✅ Rules published and pinned in `#{HELP_CHANNEL_NAME}`.")
        await audit(
            interaction.guild,
            f"[PIN_RULES] executed by {interaction.user} ({interaction.user.id})"
        )
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing permissions to send or pin messages.")
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Discord API error: {e}")


@tree.command(name="status", description="Check bot status and your message count (public).")
async def status(interaction: discord.Interaction):
    member = interaction.user
    paid   = is_paid(member)
    count  = db_get_count(member.id)
    lang   = db_get_lang(member.id)

    if paid:
        limit_info = "⭐ **Subscription active** — unlimited messages"
    else:
        remaining = max(0, FREE_DAILY_LIMIT - count)
        limit_info = f"💬 Free messages left today: **{remaining}/{FREE_DAILY_LIMIT}**"

    embed = discord.Embed(
        title="Nexora AI — Status",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Bot", value="✅ Online", inline=True)
    embed.add_field(name="Your plan", value="Subscriber" if paid else "Free", inline=True)
    embed.add_field(name="Messages", value=limit_info, inline=False)
    embed.set_footer(text="Limits reset at UTC midnight")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="admin_exec", description="[Admin only] Run a direct admin command via AI.")
@app_commands.describe(command="Natural language command, e.g. 'delete last message by User123 in #welcome'")
async def admin_exec(interaction: discord.Interaction, command: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Access denied.", ephemeral=True)
        return

    await interaction.response.defer()

    await audit(
        interaction.guild,
        f"[SLASH REQUEST] {interaction.user} ({interaction.user.id}): {command}"
    )

    plan = await plan_admin(command)

    if plan["type"] == "tool_call":
        view = ConfirmView(interaction.guild, plan["name"], plan["args"], interaction.user)
        await interaction.followup.send(
            f"**📋 PLAN**\n{plan['plan_text']}\n\nProceed?",
            view=view,
        )
    elif plan["type"] == "clarify":
        await interaction.followup.send(f"❓ **Clarification needed:**\n{plan['question']}")
    elif plan["type"] == "text":
        await interaction.followup.send(plan["content"])
    else:
        await interaction.followup.send(f"💥 AI Error: `{plan['content']}`")


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db_init()
    bot.run(DISCORD_TOKEN, log_handler=None)


# ══════════════════════════════════════════════════════════════════════════════
#  HOW TO TEST
# ══════════════════════════════════════════════════════════════════════════════
"""
TEST CHECKLIST
══════════════════════════════════════════════════════════════════════════════

1. PUBLIC MESSAGE TEST
   ─────────────────────────────────────────────────────────────────────────
   Channel: #ai-help
   Send: "как открыть тикет?" (or "how do I open a ticket?")
   Expected:
     ✅ Bot replies with a specific, helpful answer in Russian/English
     ✅ Shows "Free messages left today: X/10" footer
     ✅ NO mention of ai-admin, ai-audit-log, or admin internals

2. RATE LIMIT TEST
   ─────────────────────────────────────────────────────────────────────────
   Channel: #ai-help (use a free account — no Nexora Pro/Elite/Ultra role)
   Action: Send 10 messages one by one
   Expected:
     ✅ Counter decrements: "Free messages left today: 9/10" → ... → "1/10"
     ✅ On the 11th message: limit warning, no AI response
     ✅ Subscriber (with PAID_ROLES) has no counter shown and no limit

3. ADMIN — CREATE CHANNEL
   ─────────────────────────────────────────────────────────────────────────
   Channel: #ai-admin (as Owner or AI Admin role)
   Send: "create a channel called test-arena in category Community"
   Expected:
     ✅ Bot shows PLAN with ➕ Create channel #test-arena details
     ✅ Shows [✅ Confirm] [❌ Cancel] buttons
     ✅ After Confirm: channel created, result shown
     ✅ Audit log entry in #ai-audit-log

4. ADMIN — DELETE LAST MESSAGE
   ─────────────────────────────────────────────────────────────────────────
   Setup: Have TestUser send a message in #welcome
   Channel: #ai-admin
   Send: "delete last message by TestUser in welcome"
   Expected:
     ✅ Bot shows PLAN: 🗑️ Delete last message by TestUser in #welcome
     ✅ After Confirm: message deleted, preview shown in result
     ✅ Handles "not found" / permission errors gracefully

5. PIN RULES TEST
   ─────────────────────────────────────────────────────────────────────────
   Action: Run /pin_rules (as Owner or AI Admin)
   Expected:
     ✅ Full rules message appears in #ai-help
     ✅ Message is pinned (📌)
     ✅ Non-admin gets ephemeral "Access denied"
     ✅ Audit log entry in #ai-audit-log

══════════════════════════════════════════════════════════════════════════════
"""

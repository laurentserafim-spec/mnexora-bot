# main.py
# Nexora AI Discord bot (Public AI + Admin AI Planner/Executor)
# Upgrades:
# - Admin can issue commands in #ai-admin via @mention (no / needed)
# - Added admin actions: send_message (optionally pin), pin_last_bot_message
# - Keeps safety guardrails + audit log + modes (preview/confirm/execute/lock)

import os
import re
import json
import uuid
import sqlite3
import datetime
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from openai import OpenAI


# =========================
# ENV
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DB_PATH = os.getenv("NEXORA_DB_PATH", "nexora.db")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")

ai = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# DEFAULT SETTINGS
# =========================
DEFAULTS = {
    "ai_help_channel": "ai-help",
    "ai_admin_channel": "ai-admin",
    "ai_audit_channel": "ai-audit-log",

    "free_daily_limit": "10",
    "show_remaining": "1",
    "require_mention_outside_help": "1",

    # admin execution modes:
    # preview = plan only
    # confirm = plan + buttons (recommended)
    # execute = execute immediately (dangerous)
    # lock = no execution at all
    "admin_mode": "confirm",

    # who is admin (role name OR Discord admin permission)
    "admin_role_name": "Administrator",

    # allowlist users (ids) and roles for admin planner (in #ai-admin)
    "ai_admin_allow_users": "[]",
    "ai_admin_allow_roles": "[\"AI Admin\",\"Administrator\"]",

    # safety
    "protected_role_names": "[\"Administrator\"]",
    "protected_category_names": "[\"AI ADMIN\"]",
    "protected_channel_names": "[\"ai-admin\",\"ai-audit-log\"]",
    "max_actions_per_request": "5",

    # model selection
    "model_free": "gpt-4o-mini",
    "model_pro": "gpt-4o",
    "model_admin": "gpt-4o",

    # premium tiers (optional): role -> {model, daily_limit}
    "premium_tiers": "{\"Nexora Pro\":{\"model\":\"gpt-4o-mini\",\"daily_limit\":200},"
                     "\"Nexora Elite\":{\"model\":\"gpt-4o\",\"daily_limit\":500},"
                     "\"Nexora Ultra\":{\"model\":\"gpt-4o\",\"daily_limit\":null}}",

    # enabled actions (can toggle on/off without code)
    "enabled_actions": "{}",
}

ALL_ACTIONS = [
    "create_text_channel",
    "create_voice_channel",
    "delete_channel",
    "rename_channel",
    "move_channel",
    "create_category",
    "delete_category",
    "create_role",
    "delete_role",
    "add_role_to_user",
    "remove_role_from_user",
    "set_channel_permissions",
    "set_slowmode",
    "lock_channel",
    "unlock_channel",
    "timeout_user",
    "kick_user",
    "ban_user",
    "unban_user",

    # UPGRADE:
    "send_message",            # {channel, content, pin?:true|false}
    "pin_last_bot_message",    # {channel}
]

DEFAULT_ENABLED_ACTIONS = {
    "create_text_channel": True,
    "create_voice_channel": True,
    "create_category": True,
    "create_role": True,
    "add_role_to_user": True,
    "remove_role_from_user": True,
    "set_channel_permissions": True,
    "set_slowmode": True,
    "rename_channel": True,
    "move_channel": True,
    "lock_channel": True,
    "unlock_channel": True,

    # destructive / higher risk - start off disabled, you can enable later:
    "delete_channel": False,
    "delete_category": False,
    "delete_role": False,
    "timeout_user": True,
    "kick_user": False,
    "ban_user": False,
    "unban_user": False,

    # UPGRADE:
    "send_message": True,
    "pin_last_bot_message": True,
}


# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings(
        guild_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY(guild_id, key)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_daily(
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        day TEXT NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY(guild_id, user_id, day)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_admin(
        guild_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        requester_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(guild_id, request_id)
    )
    """)
    con.commit()
    con.close()

def parse_json(raw: str, fallback):
    try:
        return json.loads(raw)
    except Exception:
        return fallback

def get_setting(guild_id: int, key: str) -> str:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (str(guild_id), key))
    row = cur.fetchone()
    if row:
        con.close()
        return row["value"]

    # seed missing
    if key == "enabled_actions":
        value = json.dumps(DEFAULT_ENABLED_ACTIONS)
    else:
        value = DEFAULTS.get(key, "")
    cur.execute("INSERT OR REPLACE INTO settings(guild_id, key, value) VALUES(?,?,?)",
                (str(guild_id), key, str(value)))
    con.commit()
    con.close()
    return str(value)

def set_setting(guild_id: int, key: str, value: str):
    con = db()
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO settings(guild_id, key, value) VALUES(?,?,?)",
                (str(guild_id), key, str(value)))
    con.commit()
    con.close()

def today_key() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

def usage_get(guild_id: int, user_id: int) -> int:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT count FROM usage_daily WHERE guild_id=? AND user_id=? AND day=?",
                (str(guild_id), str(user_id), today_key()))
    row = cur.fetchone()
    con.close()
    return int(row["count"]) if row else 0

def usage_inc(guild_id: int, user_id: int, delta: int = 1):
    con = db()
    cur = con.cursor()
    day = today_key()
    cur.execute("SELECT count FROM usage_daily WHERE guild_id=? AND user_id=? AND day=?",
                (str(guild_id), str(user_id), day))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE usage_daily SET count=? WHERE guild_id=? AND user_id=? AND day=?",
                    (int(row["count"]) + delta, str(guild_id), str(user_id), day))
    else:
        cur.execute("INSERT INTO usage_daily(guild_id, user_id, day, count) VALUES(?,?,?,?)",
                    (str(guild_id), str(user_id), day, delta))
    con.commit()
    con.close()

def pending_put(guild_id: int, request_id: str, requester_id: int, channel_id: int, payload: Dict[str, Any]):
    con = db()
    cur = con.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO pending_admin(guild_id, request_id, requester_id, channel_id, payload_json, created_at)
    VALUES(?,?,?,?,?,?)
    """, (str(guild_id), request_id, str(requester_id), str(channel_id),
          json.dumps(payload, ensure_ascii=False), datetime.datetime.utcnow().isoformat()))
    con.commit()
    con.close()

def pending_get(guild_id: int, request_id: str) -> Optional[sqlite3.Row]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM pending_admin WHERE guild_id=? AND request_id=?",
                (str(guild_id), request_id))
    row = cur.fetchone()
    con.close()
    return row

def pending_del(guild_id: int, request_id: str):
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM pending_admin WHERE guild_id=? AND request_id=?",
                (str(guild_id), request_id))
    con.commit()
    con.close()


# =========================
# DISCORD BOT
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# UTIL / SAFETY
# =========================
def is_admin(member: discord.Member, admin_role_name: str) -> bool:
    return member.guild_permissions.administrator or any(r.name == admin_role_name for r in member.roles)

def get_enabled_actions(guild_id: int) -> Dict[str, bool]:
    raw = get_setting(guild_id, "enabled_actions")
    data = parse_json(raw, {})
    out = dict(DEFAULT_ENABLED_ACTIONS)
    out.update({k: bool(v) for k, v in data.items()})
    return out

def set_enabled_action(guild_id: int, action: str, enabled: bool):
    cur = get_enabled_actions(guild_id)
    cur[action] = bool(enabled)
    set_setting(guild_id, "enabled_actions", json.dumps(cur))

def is_ai_admin_allowed(guild: discord.Guild, member: discord.Member) -> bool:
    allow_users = parse_json(get_setting(guild.id, "ai_admin_allow_users"), [])
    allow_roles = parse_json(get_setting(guild.id, "ai_admin_allow_roles"), [])
    allow_users = {int(x) for x in allow_users if str(x).isdigit()}
    allow_roles = {str(x) for x in allow_roles}

    if member.id in allow_users:
        return True
    if is_admin(member, get_setting(guild.id, "admin_role_name")):
        return True
    if any(r.name in allow_roles for r in member.roles):
        return True
    return False

def is_protected_role(guild: discord.Guild, role_name: str) -> bool:
    protected = set(parse_json(get_setting(guild.id, "protected_role_names"), []))
    if role_name == "@everyone":
        return True
    return role_name in protected

def is_protected_category(guild: discord.Guild, category_name: str) -> bool:
    protected = set(parse_json(get_setting(guild.id, "protected_category_names"), []))
    return category_name in protected

def is_protected_channel_name(guild: discord.Guild, channel_name: str) -> bool:
    protected = set(parse_json(get_setting(guild.id, "protected_channel_names"), []))
    return channel_name in protected

async def find_text_channel_by_name(guild: discord.Guild, name: str) -> Optional[discord.TextChannel]:
    for ch in guild.text_channels:
        if ch.name == name:
            return ch
    return None

async def audit_log(guild: discord.Guild, text: str):
    ch_name = get_setting(guild.id, "ai_audit_channel")
    ch = await find_text_channel_by_name(guild, ch_name)
    if ch:
        await ch.send(text[:1900])

def strip_bot_mention(text: str) -> str:
    if bot.user:
        text = text.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    return text.strip()

def detect_lang_hint(text: str) -> str:
    cyr = len(re.findall(r"[А-Яа-яЁёІіЇїЄєҐґ]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    if cyr > lat:
        if re.search(r"[їєіґ]", text.lower()):
            return "Ukrainian"
        return "Russian"
    if lat > cyr:
        return "English"
    return "User language"


# =========================
# SERVER MAP
# =========================
def build_server_map(guild: discord.Guild) -> str:
    lines = []
    for cat in sorted(guild.categories, key=lambda c: c.position):
        lines.append(f"[CATEGORY] {cat.name}")
        for ch in sorted(cat.channels, key=lambda c: c.position):
            if isinstance(ch, discord.TextChannel):
                lines.append(f"  # {ch.name}")
            elif isinstance(ch, discord.VoiceChannel):
                lines.append(f"  🔊 {ch.name}")
            else:
                lines.append(f"  - {ch.name}")
    unc = [c for c in guild.channels if c.category is None]
    if unc:
        lines.append("[UNCATEGORIZED]")
        for ch in sorted(unc, key=lambda c: c.position):
            if isinstance(ch, discord.TextChannel):
                lines.append(f"  # {ch.name}")
            elif isinstance(ch, discord.VoiceChannel):
                lines.append(f"  🔊 {ch.name}")
            else:
                lines.append(f"  - {ch.name}")
    return "\n".join(lines)


# =========================
# AI PROMPTS
# =========================
PUBLIC_SYSTEM = """You are Nexora AI (Discord server assistant).
Rules:
- Always respond in the user's language.
- Free users: ONLY help about Nexora server (channels, rules, features, tickets, how to trade safely).
- If user asks unrelated topics in free mode: politely refuse and say full AI access is available via subscription.
- Be concise and give next-step instructions.
"""

PREMIUM_SYSTEM = """You are Nexora AI (premium assistant).
Rules:
- Always respond in the user's language.
- You can discuss any topics, but if asked about Nexora server - provide practical steps.
- Be concise and helpful.
"""

ADMIN_SYSTEM = """You are Nexora AI Admin Planner.
Return ONLY valid JSON (no markdown).

Goal: Convert the admin request into a safe executable plan.

Output schema:
{
  "summary": "short",
  "risk": "low|medium|high",
  "actions": [
    {"type":"...", "params":{...}}
  ],
  "notes":"optional"
}

Supported action types:
- create_text_channel {name, category|null}
- create_voice_channel {name, category|null}
- delete_channel {channel}
- rename_channel {channel, new_name}
- move_channel {channel, category|null}
- create_category {name, private:false|true}
- delete_category {category}
- create_role {name}
- delete_role {name}
- add_role_to_user {user, role}
- remove_role_from_user {user, role}
- set_channel_permissions {channel, role, view:true|false|null, send:true|false|null}
- set_slowmode {channel, seconds:0..21600}
- lock_channel {channel}
- unlock_channel {channel}
- timeout_user {user, minutes:1..10080, reason|null}
- kick_user {user, reason|null}
- ban_user {user, reason|null}
- unban_user {user_id}

UPGRADED:
- send_message {channel, content, pin:true|false|null}
- pin_last_bot_message {channel}

Hard safety rules:
- Never grant Administrator permission.
- Never edit protected roles/categories/channels.
- Only include actions that are enabled in enabled_actions.
- Keep actions minimal (max few actions).
"""


# =========================
# AI CALLS
# =========================
def ai_text(model: str, system: str, user: str) -> str:
    resp = ai.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (resp.choices[0].message.content or "").strip()

def ai_json(model: str, system: str, user: str) -> Dict[str, Any]:
    try:
        resp = ai.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        text = ai_text(model, system, user)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


# =========================
# RESOLVERS
# =========================
def parse_user_id(ref: str) -> Optional[int]:
    if not ref:
        return None
    ref = str(ref).strip()
    if ref.isdigit():
        return int(ref)
    if ref.startswith("<@") and ref.endswith(">"):
        digits = "".join(ch for ch in ref if ch.isdigit())
        if digits:
            return int(digits)
    m = re.search(r"(\d{15,20})", ref)
    if m:
        return int(m.group(1))
    return None

def resolve_channel(guild: discord.Guild, ref: Any) -> Optional[discord.abc.GuildChannel]:
    if ref is None:
        return None
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        return guild.get_channel(int(ref))
    name = str(ref).strip().lstrip("#").lower()
    for ch in guild.channels:
        if getattr(ch, "name", "").lower() == name:
            return ch
    return None

def resolve_category(guild: discord.Guild, ref: Any) -> Optional[discord.CategoryChannel]:
    if ref is None:
        return None
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        ch = guild.get_channel(int(ref))
        return ch if isinstance(ch, discord.CategoryChannel) else None
    name = str(ref).strip().lower()
    for c in guild.categories:
        if c.name.lower() == name:
            return c
    return None

def resolve_role(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    if not name:
        return None
    for r in guild.roles:
        if r.name.lower() == name.lower():
            return r
    return None


# =========================
# ACTION EXECUTION
# =========================
async def execute_action(guild: discord.Guild, action: Dict[str, Any]) -> Tuple[bool, str]:
    enabled = get_enabled_actions(guild.id)
    a_type = (action.get("type") or "").strip()
    params = action.get("params") or {}

    if a_type not in ALL_ACTIONS:
        return False, f"Unknown action: {a_type}"
    if not enabled.get(a_type, False):
        return False, f"Action disabled: {a_type}"

    prot_ch = set(parse_json(get_setting(guild.id, "protected_channel_names"), []))
    prot_cat = set(parse_json(get_setting(guild.id, "protected_category_names"), []))

    def channel_is_protected(ch: discord.abc.GuildChannel) -> bool:
        return hasattr(ch, "name") and (ch.name in prot_ch)

    def category_is_protected(cat: discord.CategoryChannel) -> bool:
        return cat.name in prot_cat

    async def ensure_bot_role_higher_than(role: discord.Role) -> Optional[str]:
        me = guild.me
        if me and role >= me.top_role:
            return f"Bot role must be higher than target role: {role.name}"
        return None

    # ---- create_category
    if a_type == "create_category":
        name = str(params.get("name") or "").strip()
        private = bool(params.get("private", False))
        if not name:
            return False, "Missing category name"
        if is_protected_category(guild, name):
            return False, f"Protected category: {name}"
        overwrites = None
        if private:
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        cat = await guild.create_category(name=name, overwrites=overwrites)
        return True, f"Created category: {cat.name}"

    # ---- delete_category
    if a_type == "delete_category":
        cat_ref = params.get("category")
        cat = resolve_category(guild, cat_ref)
        if not cat:
            return False, f"Category not found: {cat_ref}"
        if category_is_protected(cat) or is_protected_category(guild, cat.name):
            return False, f"Protected category: {cat.name}"
        await cat.delete()
        return True, f"Deleted category: {cat.name}"

    # ---- create_text_channel / voice
    if a_type in ("create_text_channel", "create_voice_channel"):
        name = str(params.get("name") or "").strip().lstrip("#")
        cat_ref = params.get("category", None)
        if not name:
            return False, "Missing channel name"
        category = resolve_category(guild, cat_ref) if cat_ref else None
        if category and category_is_protected(category):
            return False, f"Target category protected: {category.name}"

        if a_type == "create_text_channel":
            ch = await guild.create_text_channel(name=name, category=category)
            return True, f"Created text channel: #{ch.name}"
        else:
            ch = await guild.create_voice_channel(name=name, category=category)
            return True, f"Created voice channel: {ch.name}"

    # ---- delete_channel
    if a_type == "delete_channel":
        ch = resolve_channel(guild, params.get("channel"))
        if not ch:
            return False, "Channel not found"
        if channel_is_protected(ch) or is_protected_channel_name(guild, getattr(ch, "name", "")):
            return False, f"Protected channel: {getattr(ch, 'name', ch.id)}"
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)) and ch.category:
            if category_is_protected(ch.category):
                return False, f"Channel is in protected category: {ch.category.name}"
        await ch.delete()
        return True, f"Deleted channel: {getattr(ch, 'name', ch.id)}"

    # ---- rename_channel
    if a_type == "rename_channel":
        ch = resolve_channel(guild, params.get("channel"))
        new_name = str(params.get("new_name") or "").strip().lstrip("#")
        if not ch or not new_name:
            return False, "Missing channel or new_name"
        if channel_is_protected(ch):
            return False, f"Protected channel: {getattr(ch, 'name', ch.id)}"
        await ch.edit(name=new_name)
        return True, f"Renamed channel to: {new_name}"

    # ---- move_channel
    if a_type == "move_channel":
        ch = resolve_channel(guild, params.get("channel"))
        cat_ref = params.get("category", None)
        if not ch:
            return False, "Missing channel"
        if channel_is_protected(ch):
            return False, f"Protected channel: {getattr(ch, 'name', ch.id)}"
        category = resolve_category(guild, cat_ref) if cat_ref else None
        if category and category_is_protected(category):
            return False, f"Target category protected: {category.name}"
        await ch.edit(category=category)
        return True, f"Moved channel: {getattr(ch, 'name', ch.id)}"

    # ---- create_role / delete_role
    if a_type == "create_role":
        name = str(params.get("name") or "").strip()
        if not name:
            return False, "Missing role name"
        if is_protected_role(guild, name):
            return False, f"Protected role name: {name}"
        if resolve_role(guild, name):
            return True, f"Role already exists: {name}"
        role = await guild.create_role(name=name, permissions=discord.Permissions.none())
        return True, f"Created role: {role.name}"

    if a_type == "delete_role":
        name = str(params.get("name") or "").strip()
        if not name:
            return False, "Missing role name"
        if is_protected_role(guild, name):
            return False, f"Protected role: {name}"
        role = resolve_role(guild, name)
        if not role:
            return False, f"Role not found: {name}"
        if role.managed:
            return False, f"Managed role can't be deleted: {name}"
        err = await ensure_bot_role_higher_than(role)
        if err:
            return False, err
        await role.delete()
        return True, f"Deleted role: {name}"

    # ---- add/remove role to user
    if a_type in ("add_role_to_user", "remove_role_from_user"):
        uid = parse_user_id(str(params.get("user") or ""))
        role_name = str(params.get("role") or "").strip()
        if not uid or not role_name:
            return False, "Missing user or role"
        if is_protected_role(guild, role_name):
            return False, f"Protected role: {role_name}"
        member = guild.get_member(uid)
        if not member:
            return False, f"User not found in guild: {uid}"
        role = resolve_role(guild, role_name)
        if not role:
            return False, f"Role not found: {role_name}"
        err = await ensure_bot_role_higher_than(role)
        if err:
            return False, err

        if a_type == "add_role_to_user":
            await member.add_roles(role, reason="Nexora AI Admin")
            return True, f"Added role {role.name} to {member.display_name}"
        else:
            await member.remove_roles(role, reason="Nexora AI Admin")
            return True, f"Removed role {role.name} from {member.display_name}"

    # ---- permissions
    if a_type == "set_channel_permissions":
        ch = resolve_channel(guild, params.get("channel"))
        role_name = str(params.get("role") or "").strip()
        view = params.get("view", None)
        send = params.get("send", None)

        if not ch or not role_name:
            return False, "Missing channel or role"
        if is_protected_role(guild, role_name):
            return False, f"Protected role: {role_name}"
        if channel_is_protected(ch):
            return False, f"Protected channel: {getattr(ch, 'name', ch.id)}"

        role = resolve_role(guild, role_name)
        if not role:
            return False, f"Role not found: {role_name}"

        overwrite = ch.overwrites_for(role)  # type: ignore
        if view is not None:
            overwrite.view_channel = bool(view)
        if send is not None:
            overwrite.send_messages = bool(send)

        await ch.set_permissions(role, overwrite=overwrite)  # type: ignore
        return True, f"Updated permissions in {getattr(ch, 'name', ch.id)} for role {role.name}"

    # ---- slowmode
    if a_type == "set_slowmode":
        ch = resolve_channel(guild, params.get("channel"))
        seconds = params.get("seconds")
        if not isinstance(ch, discord.TextChannel):
            return False, "Slowmode applies to text channels only"
        if channel_is_protected(ch):
            return False, f"Protected channel: {ch.name}"
        try:
            sec = int(seconds)
            if sec < 0 or sec > 21600:
                return False, "seconds out of range (0..21600)"
        except Exception:
            return False, "Invalid seconds"
        await ch.edit(slowmode_delay=sec)
        return True, f"Set slowmode #{ch.name} to {sec}s"

    # ---- lock/unlock channel
    if a_type in ("lock_channel", "unlock_channel"):
        ch = resolve_channel(guild, params.get("channel"))
        if not isinstance(ch, discord.TextChannel):
            return False, "Lock/unlock applies to text channels only"
        if channel_is_protected(ch):
            return False, f"Protected channel: {ch.name}"

        everyone = guild.default_role
        overwrite = ch.overwrites_for(everyone)
        overwrite.send_messages = False if a_type == "lock_channel" else None
        await ch.set_permissions(everyone, overwrite=overwrite)
        return True, f"{'Locked' if a_type=='lock_channel' else 'Unlocked'} #{ch.name} for @everyone"

    # ---- moderation
    if a_type == "timeout_user":
        uid = parse_user_id(str(params.get("user") or ""))
        minutes = params.get("minutes")
        reason = params.get("reason", None)
        if not uid:
            return False, "Missing user"
        member = guild.get_member(uid)
        if not member:
            return False, f"User not found: {uid}"
        try:
            m = int(minutes)
            if m < 1 or m > 10080:
                return False, "minutes out of range (1..10080)"
        except Exception:
            return False, "Invalid minutes"
        until = datetime.datetime.utcnow() + datetime.timedelta(minutes=m)
        await member.timeout(until, reason=str(reason) if reason else "Nexora AI timeout")
        return True, f"Timed out {member.display_name} for {m} minutes"

    if a_type == "kick_user":
        uid = parse_user_id(str(params.get("user") or ""))
        reason = params.get("reason", None)
        if not uid:
            return False, "Missing user"
        member = guild.get_member(uid)
        if not member:
            return False, f"User not found: {uid}"
        await member.kick(reason=str(reason) if reason else "Nexora AI kick")
        return True, f"Kicked {member.display_name}"

    if a_type == "ban_user":
        uid = parse_user_id(str(params.get("user") or ""))
        reason = params.get("reason", None)
        if not uid:
            return False, "Missing user"
        member = guild.get_member(uid)
        if not member:
            return False, f"User not found: {uid}"
        await member.ban(reason=str(reason) if reason else "Nexora AI ban", delete_message_days=0)
        return True, f"Banned {member.display_name}"

    if a_type == "unban_user":
        uid = params.get("user_id")
        if not uid or not str(uid).isdigit():
            return False, "Invalid user_id"
        user = discord.Object(id=int(uid))
        await guild.unban(user, reason="Nexora AI unban")
        return True, f"Unbanned user id {uid}"

    # =========================
    # UPGRADE: send_message + pin
    # =========================
    if a_type == "send_message":
        ch = resolve_channel(guild, params.get("channel"))
        content = str(params.get("content") or "").strip()
        pin = params.get("pin", None)

        if not isinstance(ch, discord.TextChannel):
            return False, "send_message: channel must be a text channel"
        if not content:
            return False, "send_message: empty content"
        if channel_is_protected(ch) or is_protected_channel_name(guild, ch.name):
            return False, f"Protected channel: #{ch.name}"

        msg = await ch.send(content[:1900])
        if pin is True:
            try:
                await msg.pin(reason="Nexora AI pin")
                return True, f"Sent + pinned message in #{ch.name} (id={msg.id})"
            except Exception as e:
                return False, f"Sent message but failed to pin in #{ch.name}: {e}"
        return True, f"Sent message to #{ch.name} (id={msg.id})"

    if a_type == "pin_last_bot_message":
        ch = resolve_channel(guild, params.get("channel"))
        if not isinstance(ch, discord.TextChannel):
            return False, "pin_last_bot_message: channel must be a text channel"
        if channel_is_protected(ch) or is_protected_channel_name(guild, ch.name):
            return False, f"Protected channel: #{ch.name}"

        if not bot.user:
            return False, "Bot user not ready"

        async for m in ch.history(limit=50):
            if m.author and m.author.id == bot.user.id:
                try:
                    await m.pin(reason="Nexora AI pin")
                    return True, f"Pinned bot message in #{ch.name} (id={m.id})"
                except Exception as e:
                    return False, f"Failed to pin in #{ch.name}: {e}"
        return False, f"No recent bot message found in #{ch.name} to pin"

    return False, f"Unhandled action: {a_type}"


# =========================
# CONFIRMATION VIEW
# =========================
class ConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, request_id: str, requester_id: int):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.request_id = request_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ Только автор может подтверждать.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = pending_get(self.guild_id, self.request_id)
        if not row:
            await interaction.response.send_message("❌ Запрос не найден/истёк.", ephemeral=True)
            return

        guild = interaction.guild
        payload = json.loads(row["payload_json"])
        actions = payload.get("actions", [])

        mode = get_setting(guild.id, "admin_mode").lower().strip()
        if mode == "lock":
            await interaction.response.send_message("🔒 LOCK MODE: выполнение запрещено.", ephemeral=True)
            return
        if mode == "preview":
            await interaction.response.send_message("🧪 PREVIEW MODE: выполнение отключено. Поставь /ai-admin-mode confirm.", ephemeral=True)
            return

        await interaction.response.send_message("⏳ Выполняю...", ephemeral=True)

        results = []
        for a in actions:
            ok, msg = await execute_action(guild, a)
            results.append(("✅ " if ok else "❌ ") + msg)

        pending_del(self.guild_id, self.request_id)

        await audit_log(guild, f"✅ EXECUTED by <@{interaction.user.id}> | {payload.get('summary','(no summary)')}\n- " +
                       "\n- ".join(results))

        await interaction.followup.send("Готово:\n- " + "\n- ".join(results), ephemeral=True)

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_del(self.guild_id, self.request_id)
        await audit_log(interaction.guild, f"🛑 CANCELED by <@{interaction.user.id}> | request {self.request_id}")
        await interaction.response.send_message("🛑 Отменено.", ephemeral=True)


# =========================
# ADMIN PLANNING CORE (shared)
# =========================
async def run_admin_planner(
    guild: discord.Guild,
    member: discord.Member,
    channel: discord.TextChannel,
    task: str,
    respond_fn,   # async function(text, view=None)
):
    mode = get_setting(guild.id, "admin_mode").lower().strip()
    if mode == "lock":
        await respond_fn("🔒 LOCK MODE: выполнение запрещено. Можно только preview.", view=None)
        return

    enabled_actions = get_enabled_actions(guild.id)
    enabled_list = [k for k, v in enabled_actions.items() if v]

    server_map = build_server_map(guild)

    max_actions = int(get_setting(guild.id, "max_actions_per_request") or "5")
    protected_roles = parse_json(get_setting(guild.id, "protected_role_names"), [])
    protected_cats = parse_json(get_setting(guild.id, "protected_category_names"), [])
    protected_channels = parse_json(get_setting(guild.id, "protected_channel_names"), [])

    prompt = (
        ADMIN_SYSTEM +
        "\n\nContext:\n"
        f"- enabled_actions: {enabled_list}\n"
        f"- protected_roles: {protected_roles}\n"
        f"- protected_categories: {protected_cats}\n"
        f"- protected_channels: {protected_channels}\n"
        f"- max_actions: {max_actions}\n"
        f"\nServer map:\n{server_map}\n"
        f"\nAdmin request:\n{task}\n"
    )

    try:
        model_admin = get_setting(guild.id, "model_admin")
        plan = ai_json(model_admin, prompt, task)

        actions = plan.get("actions", [])
        if not isinstance(actions, list):
            actions = []

        safe_actions = []
        for a in actions[:max_actions]:
            if not isinstance(a, dict):
                continue
            t = (a.get("type") or "").strip()
            if t not in ALL_ACTIONS:
                continue
            if not enabled_actions.get(t, False):
                continue

            params = a.get("params") or {}

            # protect role names
            if t in ("create_role", "delete_role", "add_role_to_user", "remove_role_from_user", "set_channel_permissions"):
                role_name = str(params.get("role") or params.get("name") or "")
                if role_name and is_protected_role(guild, role_name):
                    continue

            # protect category names
            if t in ("create_category", "delete_category"):
                cname = str(params.get("name") or params.get("category") or "")
                if cname and is_protected_category(guild, cname):
                    continue

            # protect channels by name
            if t in (
                "delete_channel", "rename_channel", "lock_channel", "unlock_channel",
                "set_slowmode", "move_channel", "set_channel_permissions",
                "send_message", "pin_last_bot_message"
            ):
                chref = str(params.get("channel") or "")
                if chref:
                    nm = chref.strip().lstrip("#")
                    if nm in protected_channels:
                        continue

            safe_actions.append({"type": t, "params": params})

        plan["actions"] = safe_actions

        request_id = str(uuid.uuid4())[:8]
        pending_put(guild.id, request_id, member.id, channel.id, plan)

        summary = plan.get("summary", "(no summary)")
        risk = plan.get("risk", "medium")
        notes = plan.get("notes", "")

        pretty = (
            f"🧩 PLAN `{request_id}`\n"
            f"Summary: {summary}\n"
            f"Risk: {risk}\n"
            f"Actions:\n" +
            ("\n".join([f"- `{a['type']}` {a.get('params', {})}" for a in safe_actions]) if safe_actions else "- (no actions)") +
            (f"\nNotes: {notes}" if notes else "")
        )

        await audit_log(guild, f"📝 ADMIN PLAN by <@{member.id}> | {summary} | risk={risk} | id={request_id}")

        if mode == "preview":
            await respond_fn(pretty[:1900], view=None)
            return

        if mode == "execute":
            results = []
            for a in safe_actions:
                ok, msg = await execute_action(guild, a)
                results.append(("✅ " if ok else "❌ ") + msg)
            pending_del(guild.id, request_id)
            await audit_log(guild, f"✅ EXECUTED (no-confirm) by <@{member.id}> | {summary}\n- " + "\n- ".join(results))
            await respond_fn(pretty[:1200] + "\n\n✅ Done:\n- " + "\n- ".join(results), view=None)
            return

        # confirm mode
        view = ConfirmView(guild.id, request_id, member.id)
        await respond_fn(pretty[:1900], view=view)

    except Exception as e:
        await respond_fn(f"⚠️ AI-admin error: {e}", view=None)


# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="ai", description="Ask Nexora AI")
@app_commands.describe(question="Your question")
async def ai_cmd(interaction: discord.Interaction, question: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return
    guild = interaction.guild
    member = interaction.user

    premium_tiers = parse_json(get_setting(guild.id, "premium_tiers"), {})
    admin_role_name = get_setting(guild.id, "admin_role_name")

    model = get_setting(guild.id, "model_free")
    system = PUBLIC_SYSTEM

    if is_admin(member, admin_role_name):
        model = get_setting(guild.id, "model_pro")
        system = PREMIUM_SYSTEM
        limit = None
    else:
        tier = None
        for r in member.roles:
            if r.name in premium_tiers:
                tier = premium_tiers[r.name]
                break
        if tier:
            model = tier.get("model") or get_setting(guild.id, "model_pro")
            system = PREMIUM_SYSTEM
            limit = tier.get("daily_limit", None)
        else:
            limit = int(get_setting(guild.id, "free_daily_limit"))

    if limit is not None:
        used = usage_get(guild.id, member.id)
        if used >= int(limit):
            await interaction.response.send_message("🚫 Лимит на сегодня исчерпан. Подними подписку/роль.", ephemeral=True)
            return
        usage_inc(guild.id, member.id, 1)
        remaining = int(limit) - (used + 1)
    else:
        remaining = None

    server_map = build_server_map(guild)
    lang_hint = detect_lang_hint(question)

    try:
        reply = ai_text(model, system + f"\nLanguage hint: {lang_hint}\n\nServer map:\n{server_map}\n", question)
        if remaining is not None and get_setting(guild.id, "show_remaining") == "1":
            reply += f"\n\n🧠 Осталось бесплатных сообщений: {remaining}/{limit}"
        await interaction.response.send_message(reply[:1900], ephemeral=False)
    except Exception:
        await interaction.response.send_message("⚠️ AI временно недоступен.", ephemeral=True)


@bot.tree.command(name="ai-admin-mode", description="Admin: set ai-admin mode (preview/confirm/execute/lock)")
@app_commands.describe(mode="preview | confirm | execute | lock")
async def ai_admin_mode(interaction: discord.Interaction, mode: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return
    guild = interaction.guild
    member = interaction.user

    if not is_admin(member, get_setting(guild.id, "admin_role_name")):
        await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
        return

    mode = mode.lower().strip()
    if mode not in ("preview", "confirm", "execute", "lock"):
        await interaction.response.send_message("❌ Используй: preview/confirm/execute/lock", ephemeral=True)
        return

    set_setting(guild.id, "admin_mode", mode)
    await audit_log(guild, f"⚙️ admin_mode set to {mode} by <@{member.id}>")
    await interaction.response.send_message(f"✅ admin_mode = {mode}", ephemeral=True)


@bot.tree.command(name="ai-config", description="Admin: set config key/value")
@app_commands.describe(key="setting key", value="setting value")
async def ai_config(interaction: discord.Interaction, key: str, value: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return
    guild = interaction.guild
    member = interaction.user

    if not is_admin(member, get_setting(guild.id, "admin_role_name")):
        await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
        return

    if key not in DEFAULTS:
        await interaction.response.send_message("❌ Неизвестный ключ настройки.", ephemeral=True)
        return

    set_setting(guild.id, key, value)
    await audit_log(guild, f"⚙️ setting {key}={value} by <@{member.id}>")
    await interaction.response.send_message(f"✅ {key} = {value}", ephemeral=True)


@bot.tree.command(name="ai-enable-action", description="Admin: enable/disable an admin action")
@app_commands.describe(action="action name", enabled="true/false")
async def ai_enable_action(interaction: discord.Interaction, action: str, enabled: bool):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return
    guild = interaction.guild
    member = interaction.user

    if not is_admin(member, get_setting(guild.id, "admin_role_name")):
        await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
        return

    action = action.strip()
    if action not in ALL_ACTIONS:
        await interaction.response.send_message("❌ Неизвестное действие.", ephemeral=True)
        return

    set_enabled_action(guild.id, action, enabled)
    await audit_log(guild, f"⚙️ action {action} enabled={enabled} by <@{member.id}>")
    await interaction.response.send_message(f"✅ {action} enabled={enabled}", ephemeral=True)


# =========================
# MESSAGE-BASED AI
# - Free chat in #ai-help
# - Elsewhere: requires @mention (if enabled)
# - UPGRADE: In #ai-admin, admins can issue admin tasks via @mention (no /)
# =========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if not message.guild or not isinstance(message.author, discord.Member):
        return
    guild = message.guild
    member = message.author

    # ignore slash-like lines to avoid accidental triggers
    if message.content.strip().startswith("/"):
        return

    ai_help = get_setting(guild.id, "ai_help_channel")
    ai_admin = get_setting(guild.id, "ai_admin_channel")
    require_mention = get_setting(guild.id, "require_mention_outside_help") == "1"

    in_help = isinstance(message.channel, discord.TextChannel) and message.channel.name == ai_help
    in_admin = isinstance(message.channel, discord.TextChannel) and message.channel.name == ai_admin

    mentioned = bot.user and (bot.user in message.mentions)

    # =========================
    # ADMIN CHAT MODE (in #ai-admin): @mention triggers admin planner
    # =========================
    if in_admin and isinstance(message.channel, discord.TextChannel):
        # must be allowed admin
        if not is_ai_admin_allowed(guild, member):
            return

        # Require @mention (per your request) OR allow "ai:" prefix (optional)
        if not mentioned and not message.content.lower().strip().startswith("ai:"):
            return

        task = message.content
        if mentioned:
            task = strip_bot_mention(task)
        if task.lower().strip().startswith("ai:"):
            task = task.split(":", 1)[1].strip()

        if not task:
            return

        async def responder(text: str, view=None):
            await message.channel.send(text[:1900], view=view)

        await run_admin_planner(guild, member, message.channel, task, responder)
        return

    # =========================
    # PUBLIC/PREMIUM CHAT MODE
    # =========================
    if not in_help:
        if require_mention and not mentioned:
            return

    text = message.content
    if mentioned:
        text = strip_bot_mention(text)
    if not text.strip():
        return

    # tier
    admin_role_name = get_setting(guild.id, "admin_role_name")
    premium_tiers = parse_json(get_setting(guild.id, "premium_tiers"), {})
    model_free = get_setting(guild.id, "model_free")
    model_pro = get_setting(guild.id, "model_pro")

    is_admin_user = is_admin(member, admin_role_name)
    tier = None
    for r in member.roles:
        if r.name in premium_tiers:
            tier = premium_tiers[r.name]
            break

    if is_admin_user:
        model = model_pro
        system = PREMIUM_SYSTEM
        limit = None
    elif tier:
        model = tier.get("model") or model_pro
        system = PREMIUM_SYSTEM
        limit = tier.get("daily_limit", None)
    else:
        model = model_free
        system = PUBLIC_SYSTEM
        limit = int(get_setting(guild.id, "free_daily_limit"))

    remaining = None
    if limit is not None:
        used = usage_get(guild.id, member.id)
        if used >= int(limit):
            await message.channel.send("🚫 Лимит AI на сегодня исчерпан. Подними уровень роли/подписки.")
            return
        usage_inc(guild.id, member.id, 1)
        remaining = int(limit) - (used + 1)

    server_map = build_server_map(guild)
    lang_hint = detect_lang_hint(text)

    try:
        reply = ai_text(model, system + f"\nLanguage hint: {lang_hint}\n\nServer map:\n{server_map}\n", text)
        if remaining is not None and get_setting(guild.id, "show_remaining") == "1":
            reply += f"\n\n🧠 Осталось бесплатных сообщений: {remaining}/{limit}"
        await message.channel.send(reply[:1900])
    except Exception:
        await message.channel.send("⚠️ AI временно недоступен.")


@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print("⚠️ Sync error:", e)
    print(f"✅ Nexora AI online as {bot.user}")


bot.run(DISCORD_TOKEN)

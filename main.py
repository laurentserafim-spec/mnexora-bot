import os
import re
import json
import asyncio
import datetime as dt
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import aiosqlite
from dotenv import load_dotenv

from openai import OpenAI

# =========================
# Config
# =========================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

AI_ADMIN_ALLOWLIST = set()
_allow = os.getenv("AI_ADMIN_ALLOWLIST", "").strip()
if _allow:
    for x in _allow.split(","):
        x = x.strip()
        if x.isdigit():
            AI_ADMIN_ALLOWLIST.add(int(x))

AI_ADMIN_CHANNEL_NAME = os.getenv("AI_ADMIN_CHANNEL", "ai-admin").strip()
AI_AUDIT_CHANNEL_NAME = os.getenv("AI_AUDIT_CHANNEL", "ai-audit-log").strip()
AI_FREE_CHAT_CHANNELS = [x.strip() for x in os.getenv("AI_FREE_CHAT_CHANNELS", "ai-support,ai-guide").split(",") if x.strip()]

FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "10"))
SHOW_REMAINING = os.getenv("SHOW_REMAINING", "1").strip() == "1"
REQUIRE_MENTION_OUTSIDE_FREE = os.getenv("REQUIRE_MENTION_OUTSIDE_FREE", "1").strip() == "1"

# confirm | preview | lock
ADMIN_MODE = os.getenv("ADMIN_MODE", "confirm").strip().lower()

DB_PATH = "nexora.sqlite"

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing")

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# Helpers: language
# =========================
UA_CHARS = set("іїєґІЇЄҐ")

def detect_lang(text: str) -> str:
    # very simple heuristic: UA chars -> uk, Cyrillic -> ru, else en
    if any(ch in UA_CHARS for ch in text):
        return "uk"
    if re.search(r"[А-Яа-яЁё]", text):
        return "ru"
    return "en"

def t(lang: str, ru: str, en: str, uk: Optional[str] = None) -> str:
    if lang == "ru":
        return ru
    if lang == "uk":
        return uk or ru
    return en

# =========================
# SQLite: usage tracking + settings
# =========================
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS user_usage (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY(user_id, day)
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        """)
        await db.commit()

async def db_get_kv(key: str, default: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT v FROM kv WHERE k=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

async def db_set_kv(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
        await db.commit()

async def db_get_usage(user_id: int, day: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT count FROM user_usage WHERE user_id=? AND day=?", (user_id, day)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def db_inc_usage(user_id: int, day: str, inc: int = 1) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT count FROM user_usage WHERE user_id=? AND day=?", (user_id, day))
        row = await cur.fetchone()
        current = int(row[0]) if row else 0
        new_val = current + inc
        await db.execute(
            "INSERT INTO user_usage(user_id, day, count) VALUES(?,?,?) ON CONFLICT(user_id, day) DO UPDATE SET count=?",
            (user_id, day, new_val, new_val)
        )
        await db.commit()
        return new_val

def today_key() -> str:
    return dt.datetime.utcnow().date().isoformat()

# =========================
# Discord bot setup
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# Permissions / identity checks
# =========================
def is_admin_member(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    # AI Admin role grants admin access to admin channel logic
    if discord.utils.get(member.roles, name="AI Admin") is not None:
        return True
    if AI_ADMIN_ALLOWLIST and member.id in AI_ADMIN_ALLOWLIST:
        return True
    return False

def is_free_limited(member: discord.Member) -> bool:
    # Admins unlimited
    if is_admin_member(member):
        return False
    # If user has paid roles, also unlimited (you can adjust)
    paid = {"Nexora Pro", "Nexora Elite", "Nexora Ultra"}
    if any(r.name in paid for r in member.roles):
        return False
    # everyone else limited (Visitor/Member/etc.)
    return True

def channel_name(ch: discord.abc.GuildChannel) -> str:
    return getattr(ch, "name", "")

def bot_is_mentioned(msg: discord.Message) -> bool:
    return bot.user is not None and bot.user in msg.mentions

# =========================
# Audit logging
# =========================
async def audit_log(guild: discord.Guild, content: str):
    ch = discord.utils.get(guild.text_channels, name=AI_AUDIT_CHANNEL_NAME)
    if ch:
        await ch.send(content)

def fmt_plan_header(actor: discord.Member, plan_id: str, summary: str, risk: str) -> str:
    return f"📝 **ADMIN PLAN** by {actor.mention} | {summary} | risk={risk} | id={plan_id}"

def fmt_exec_header(actor: discord.Member, summary: str) -> str:
    return f"✅ **EXECUTED** by {actor.mention} | {summary}"

# =========================
# Tool actions (Discord operations)
# =========================
class ActionError(Exception):
    pass

async def resolve_channel(guild: discord.Guild, channel: str) -> discord.TextChannel:
    # channel can be name or ID
    if channel.isdigit():
        ch = guild.get_channel(int(channel))
        if isinstance(ch, discord.TextChannel):
            return ch
    # strip possible leading #
    name = channel.lstrip("#")
    ch = discord.utils.get(guild.text_channels, name=name)
    if not ch:
        raise ActionError(f"Channel not found: {channel}")
    return ch

async def resolve_role(guild: discord.Guild, role: str) -> discord.Role:
    # special aliases for @everyone
    if role.lower() in {"everyone", "@everyone", "default"}:
        return guild.default_role
    if role.isdigit():
        r = guild.get_role(int(role))
        if r:
            return r
    r = discord.utils.get(guild.roles, name=role)
    if not r:
        raise ActionError(f"Role not found: {role}")
    return r

async def resolve_member(guild: discord.Guild, user: str) -> discord.Member:
    # user can be mention, ID, or username
    m = re.search(r"(\d{15,20})", user)
    if m:
        uid = int(m.group(1))
        mem = guild.get_member(uid)
        if mem:
            return mem
        try:
            mem = await guild.fetch_member(uid)
            return mem
        except Exception:
            pass
    # try exact name / display name
    for mem in guild.members:
        if mem.name == user or mem.display_name == user:
            return mem
    raise ActionError(f"User not found: {user}")

async def action_create_text_channel(guild: discord.Guild, name: str, category: Optional[str] = None) -> str:
    cat_obj = None
    if category:
        cat_obj = discord.utils.get(guild.categories, name=category)
        if not cat_obj:
            raise ActionError(f"Category not found: {category}")
    ch = await guild.create_text_channel(name=name, category=cat_obj)
    return f"Created text channel: {ch.mention}"

async def action_send_message(guild: discord.Guild, channel: str, content: str, pin: bool = False) -> str:
    ch = await resolve_channel(guild, channel)
    msg = await ch.send(content)
    if pin:
        try:
            await msg.pin(reason="Nexora AI pin")
        except discord.Forbidden:
            raise ActionError(f"Sent message but failed to pin in {ch.mention}: Missing Permissions")
    return f"Sent message to {ch.mention} (id={msg.id})" + (f" and pinned" if pin else "")

async def action_pin_message(guild: discord.Guild, channel: str, message_id: str) -> str:
    ch = await resolve_channel(guild, channel)
    mid = int(message_id)
    try:
        msg = await ch.fetch_message(mid)
    except Exception:
        raise ActionError(f"Message not found in {ch.mention}: {message_id}")
    try:
        await msg.pin(reason="Nexora AI pin")
    except discord.Forbidden:
        raise ActionError("Missing Permissions to pin")
    return f"Pinned message in {ch.mention} (id={msg.id})"

async def action_delete_message(guild: discord.Guild, channel: str, message_id: str) -> str:
    ch = await resolve_channel(guild, channel)
    mid = int(message_id)
    try:
        msg = await ch.fetch_message(mid)
    except Exception:
        raise ActionError(f"Message not found in {ch.mention}: {message_id}")
    try:
        await msg.delete(reason="Nexora AI delete")
    except discord.Forbidden:
        raise ActionError("Missing Permissions to delete messages (Manage Messages)")
    return f"Deleted message in {ch.mention} (id={mid})"

async def action_add_role_to_user(guild: discord.Guild, user: str, role: str) -> str:
    mem = await resolve_member(guild, user)
    r = await resolve_role(guild, role)
    try:
        await mem.add_roles(r, reason="Nexora AI role grant")
    except discord.Forbidden:
        raise ActionError("Missing Permissions to manage roles or role hierarchy too low")
    return f"Added role {r.name} to {mem.display_name}"

async def action_remove_role_from_user(guild: discord.Guild, user: str, role: str) -> str:
    mem = await resolve_member(guild, user)
    r = await resolve_role(guild, role)
    try:
        await mem.remove_roles(r, reason="Nexora AI role remove")
    except discord.Forbidden:
        raise ActionError("Missing Permissions to manage roles or role hierarchy too low")
    return f"Removed role {r.name} from {mem.display_name}"

async def action_create_role(guild: discord.Guild, name: str) -> str:
    existing = discord.utils.get(guild.roles, name=name)
    if existing:
        return f"Role already exists: {name}"
    try:
        r = await guild.create_role(name=name, reason="Nexora AI role create")
    except discord.Forbidden:
        raise ActionError("Missing Permissions to create roles")
    return f"Created role: {r.name}"

async def action_set_channel_permissions(
    guild: discord.Guild,
    channel: str,
    role: str,
    view: Optional[bool] = None,
    send: Optional[bool] = None,
    read_history: Optional[bool] = None,
    manage_messages: Optional[bool] = None,
) -> str:
    ch = await resolve_channel(guild, channel)
    r = await resolve_role(guild, role)

    overwrite = ch.overwrites_for(r)
    if view is not None:
        overwrite.view_channel = view
    if send is not None:
        overwrite.send_messages = send
    if read_history is not None:
        overwrite.read_message_history = read_history
    if manage_messages is not None:
        overwrite.manage_messages = manage_messages

    try:
        await ch.set_permissions(r, overwrite=overwrite, reason="Nexora AI set perms")
    except discord.Forbidden:
        raise ActionError("Missing Permissions to edit channel permissions")
    return f"Set perms in #{ch.name} for role {r.name}"

# Registry for tool calls
TOOL_FUNCS = {
    "create_text_channel": action_create_text_channel,
    "send_message": action_send_message,
    "pin_message": action_pin_message,
    "delete_message": action_delete_message,
    "add_role_to_user": action_add_role_to_user,
    "remove_role_from_user": action_remove_role_from_user,
    "create_role": action_create_role,
    "set_channel_permissions": action_set_channel_permissions,
}

# =========================
# OpenAI function schema
# =========================
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "create_text_channel",
            "description": "Create a new text channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": ["string", "null"]},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to a channel (optionally pin it).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel name like 'welcome' or '#welcome' or channel ID."},
                    "content": {"type": "string"},
                    "pin": {"type": "boolean"},
                },
                "required": ["channel", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pin_message",
            "description": "Pin a message by ID in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "message_id": {"type": "string"},
                },
                "required": ["channel", "message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_message",
            "description": "Delete a message by ID in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "message_id": {"type": "string"},
                },
                "required": ["channel", "message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_role_to_user",
            "description": "Give a role to a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user": {"type": "string", "description": "User mention or user ID or name."},
                    "role": {"type": "string", "description": "Role name or role ID. Use 'everyone' for @everyone."},
                },
                "required": ["user", "role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_role_from_user",
            "description": "Remove a role from a user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["user", "role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_role",
            "description": "Create a new role.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_channel_permissions",
            "description": "Set channel permission overrides for a role.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "role": {"type": "string"},
                    "view": {"type": ["boolean", "null"]},
                    "send": {"type": ["boolean", "null"]},
                    "read_history": {"type": ["boolean", "null"]},
                    "manage_messages": {"type": ["boolean", "null"]},
                },
                "required": ["channel", "role"],
            },
        },
    },
]

SYSTEM_ADMIN = """You are Nexora AI Admin assistant for a Discord server.
You MUST:
- Produce a short PLAN summary, risk level (low/medium/high), and tool actions.
- Only propose actions that are specific and executable.
- If request is unclear, ask for clarification without tool calls.
- Respect that some actions may fail if the bot lacks Discord permissions or role hierarchy.
- Use role name 'everyone' to refer to @everyone default role.
- Keep responses concise.
"""

SYSTEM_CHAT = """You are Nexora AI assistant for a Discord server. Be helpful and concise.
If the user asks for admin actions, tell them to use #ai-admin and mention the bot.
"""

# =========================
# UI: Confirm / Cancel
# =========================
class PlanView(discord.ui.View):
    def __init__(self, *, actor_id: int, plan: Dict[str, Any], timeout: int = 120):
        super().__init__(timeout=timeout)
        self.actor_id = actor_id
        self.plan = plan
        self.result_message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Only the requesting admin can confirm/cancel this plan.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        if ADMIN_MODE == "lock":
            await interaction.followup.send("ADMIN_MODE=lock. Execution disabled.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("No guild context.", ephemeral=True)
            return

        actions: List[Dict[str, Any]] = self.plan.get("actions", [])
        summary = self.plan.get("summary", "Execute plan")

        results = []
        for a in actions:
            name = a.get("name")
            args = a.get("arguments", {})
            fn = TOOL_FUNCS.get(name)
            if not fn:
                results.append(f"❌ Unknown action: {name}")
                continue
            try:
                out = await fn(guild, **args)
                results.append(f"✅ {out}")
            except ActionError as e:
                results.append(f"❌ {name}: {e}")
            except Exception as e:
                results.append(f"❌ {name}: {type(e).__name__}: {e}")

        # audit
        actor = guild.get_member(self.actor_id)
        if actor:
            await audit_log(guild, fmt_exec_header(actor, summary) + "\n" + "\n".join(f"• {r}" for r in results))

        await interaction.followup.send("Done:\n" + "\n".join(results), ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()

# =========================
# OpenAI: build plan with tool calls
# =========================
async def build_admin_plan(user_text: str) -> Dict[str, Any]:
    # Use function calling; we ask the model to propose tool calls
    # We request JSON in normal message + tool calls in assistant
    resp = client_ai.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM_ADMIN},
            {"role": "user", "content": user_text},
        ],
        tools=TOOLS_SCHEMA,
        tool_choice="auto",
    )

    # Parse tool calls from response output
    actions = []
    summary = "Admin request"
    risk = "low"
    notes = ""

    # The Responses API returns items in resp.output; we will extract:
    # - assistant text (for summary/risk/notes)
    # - tool calls (function calls)
    for item in resp.output:
        if item.type == "message":
            # plain text
            for c in item.content:
                if c.type == "output_text":
                    text = c.text.strip()
                    # Attempt to parse summary/risk/notes from text
                    # Accept formats like:
                    # Summary: ...
                    # Risk: low
                    # Notes: ...
                    m_sum = re.search(r"Summary:\s*(.*)", text, re.IGNORECASE)
                    m_risk = re.search(r"Risk:\s*(low|medium|high)", text, re.IGNORECASE)
                    m_notes = re.search(r"Notes:\s*(.*)", text, re.IGNORECASE)
                    if m_sum:
                        summary = m_sum.group(1).strip()
                    if m_risk:
                        risk = m_risk.group(1).lower()
                    if m_notes:
                        notes = m_notes.group(1).strip()
        if item.type == "function_call":
            try:
                args = json.loads(item.arguments or "{}")
            except Exception:
                args = {}
            actions.append({"name": item.name, "arguments": args})

    if not actions:
        # If the model didn't produce tool calls, treat as unclear informational.
        if not notes:
            notes = "No actionable request detected. Provide more specific instructions."
        # keep empty actions; UI will show plan with no actions
    return {"summary": summary, "risk": risk, "notes": notes, "actions": actions}

# =========================
# Chat response (non-admin)
# =========================
async def ai_chat_response(user_text: str, lang: str) -> str:
    resp = client_ai.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM_CHAT},
            {"role": "user", "content": user_text},
        ],
    )
    # extract text
    out = ""
    for item in resp.output:
        if item.type == "message":
            for c in item.content:
                if c.type == "output_text":
                    out += c.text
    out = out.strip() or t(lang, "Я не понял запрос.", "I didn't understand the request.", "Я не зрозуміла запит.")
    return out

# =========================
# Ensure channels exist (optional)
# =========================
async def ensure_core_channels(guild: discord.Guild):
    # Create audit/admin/support channels if missing
    need = [AI_ADMIN_CHANNEL_NAME, AI_AUDIT_CHANNEL_NAME] + AI_FREE_CHAT_CHANNELS
    existing = {c.name for c in guild.text_channels}
    for name in need:
        if name not in existing:
            try:
                await guild.create_text_channel(name=name, reason="Nexora AI bootstrap")
            except discord.Forbidden:
                # ignore if can't
                pass

# =========================
# Events
# =========================
@bot.event
async def on_ready():
    await db_init()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print("Sync failed:", e)
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # Ensure channels exist in all guilds
    for g in bot.guilds:
        await ensure_core_channels(g)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    # Let slash commands work
    await bot.process_commands(message)

    member = message.author
    if not isinstance(member, discord.Member):
        return

    ch_name = channel_name(message.channel)
    lang = detect_lang(message.content)

    # Admin channel logic: require mention + admin permission
    if ch_name == AI_ADMIN_CHANNEL_NAME:
        if not bot_is_mentioned(message):
            return
        if not is_admin_member(member):
            await message.reply(t(lang,
                                 "Нет доступа: только AI Admin/Администратор.",
                                 "Access denied: AI Admin/Administrator only.",
                                 "Нема доступу: лише AI Admin/Адміністратор."))
            return

        # remove bot mention from text
        content = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()
        if not content:
            await message.reply(t(lang, "Напиши задачу после упоминания.", "Write a task after mentioning me.", "Напиши задачу після згадки."))
            return

        plan_id = os.urandom(4).hex()
        # Build plan
        plan = await build_admin_plan(content)

        # Always audit the plan
        await audit_log(message.guild, fmt_plan_header(member, plan_id, plan.get("summary",""), plan.get("risk","low")))

        # Preview-only mode
        if ADMIN_MODE == "preview":
            txt = f"🧩 **PLAN {plan_id}**\nSummary: {plan.get('summary')}\nRisk: {plan.get('risk')}\nActions:\n"
            if plan["actions"]:
                for a in plan["actions"]:
                    txt += f"• `{a['name']}` {a['arguments']}\n"
            else:
                txt += "• (no actions)\n"
            if plan.get("notes"):
                txt += f"Notes: {plan['notes']}"
            await message.reply(txt)
            return

        # confirm/lock mode -> show buttons
        txt = f"🧩 **PLAN {plan_id}**\nSummary: {plan.get('summary')}\nRisk: {plan.get('risk')}\nActions:\n"
        if plan["actions"]:
            for a in plan["actions"]:
                txt += f"• `{a['name']}` {a['arguments']}\n"
        else:
            txt += "• (no actions)\n"
        if plan.get("notes"):
            txt += f"\nNotes: {plan['notes']}"

        view = PlanView(actor_id=member.id, plan=plan)
        await message.reply(txt, view=view)
        return

    # Non-admin channels: AI chat
    free_chat = ch_name in AI_FREE_CHAT_CHANNELS

    if REQUIRE_MENTION_OUTSIDE_FREE and (not free_chat):
        # only respond if mentioned or /ai used
        if not bot_is_mentioned(message) and not message.content.strip().lower().startswith("/ai"):
            return

    # check usage limit
    if is_free_limited(member):
        day = today_key()
        used = await db_get_usage(member.id, day)
        if used >= FREE_DAILY_LIMIT:
            await message.reply(t(lang,
                                 f"Лимит на сегодня исчерпан ({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT}).",
                                 f"Daily limit reached ({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT}).",
                                 f"Ліміт на сьогодні вичерпано ({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT})."))
            return

    # Strip /ai and mention
    user_text = message.content
    user_text = re.sub(rf"<@!?{bot.user.id}>", "", user_text).strip()
    if user_text.lower().startswith("/ai"):
        user_text = user_text[3:].strip()

    if not user_text:
        return

    # respond
    try:
        answer = await ai_chat_response(user_text, lang)
    except Exception as e:
        await message.reply(t(lang,
                             f"Ошибка AI: {type(e).__name__}",
                             f"AI error: {type(e).__name__}",
                             f"Помилка AI: {type(e).__name__}"))
        return

    # inc usage after successful answer
    if is_free_limited(member):
        day = today_key()
        new_used = await db_inc_usage(member.id, day, 1)
        remaining = max(0, FREE_DAILY_LIMIT - new_used)
        if SHOW_REMAINING:
            suffix = t(lang,
                       f"\n\n🧠 Осталось бесплатных сообщений: {remaining}/{FREE_DAILY_LIMIT}",
                       f"\n\n🧠 Free messages left: {remaining}/{FREE_DAILY_LIMIT}",
                       f"\n\n🧠 Залишилось безкоштовних повідомлень: {remaining}/{FREE_DAILY_LIMIT}")
            answer += suffix

    await message.reply(answer)

# =========================
# Slash command: /ai
# =========================
@bot.tree.command(name="ai", description="Ask Nexora AI (works in any channel).")
@app_commands.describe(prompt="Your message to Nexora AI")
async def ai_slash(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True, ephemeral=False)
    lang = detect_lang(prompt)

    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("Guild-only command.")
        return

    member: discord.Member = interaction.user

    # limit check
    if is_free_limited(member):
        day = today_key()
        used = await db_get_usage(member.id, day)
        if used >= FREE_DAILY_LIMIT:
            await interaction.followup.send(t(lang,
                                             f"Лимит на сегодня исчерпан ({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT}).",
                                             f"Daily limit reached ({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT}).",
                                             f"Ліміт на сьогодні вичерпано ({FREE_DAILY_LIMIT}/{FREE_DAILY_LIMIT})."))
            return

    try:
        answer = await ai_chat_response(prompt, lang)
    except Exception as e:
        await interaction.followup.send(t(lang,
                                         f"Ошибка AI: {type(e).__name__}",
                                         f"AI error: {type(e).__name__}",
                                         f"Помилка AI: {type(e).__name__}"))
        return

    if is_free_limited(member):
        day = today_key()
        new_used = await db_inc_usage(member.id, day, 1)
        remaining = max(0, FREE_DAILY_LIMIT - new_used)
        if SHOW_REMAINING:
            answer += t(lang,
                        f"\n\n🧠 Осталось бесплатных сообщений: {remaining}/{FREE_DAILY_LIMIT}",
                        f"\n\n🧠 Free messages left: {remaining}/{FREE_DAILY_LIMIT}",
                        f"\n\n🧠 Залишилось безкоштовних повідомлень: {remaining}/{FREE_DAILY_LIMIT}")

    await interaction.followup.send(answer)

# =========================
# Run
# =========================
bot.run(DISCORD_TOKEN)

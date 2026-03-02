import os
import re
import json
import time
import sqlite3
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# OpenAI (python package: openai>=1.0.0)
from openai import OpenAI

# =========================
# CONFIG (ENV)
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# You can configure by name OR by ID (recommended).
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")

AI_ADMIN_CHANNEL_NAME = os.getenv("AI_ADMIN_CHANNEL_NAME", "ai-admin").strip()   # hidden channel
AI_AUDIT_CHANNEL_NAME = os.getenv("AI_AUDIT_CHANNEL_NAME", "ai-audit-log").strip()

# Public support/help channels where bot answers users
PUBLIC_AI_CHANNELS = [c.strip() for c in os.getenv("PUBLIC_AI_CHANNELS", "ai-help,ai-support,ai-guide").split(",") if c.strip()]

# Roles
ROLE_AI_ADMIN = os.getenv("ROLE_AI_ADMIN", "AI Admin").strip()
ROLE_ADMINISTRATOR = os.getenv("ROLE_ADMINISTRATOR", "Administrator").strip()

PAID_ROLES = [r.strip() for r in os.getenv("PAID_ROLES", "Nexora Pro,Nexora Elite,Nexora Ultra").split(",") if r.strip()]

# Limits
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "10") or "10")

# Models
MODEL_ASSIST = os.getenv("MODEL_ASSIST", "gpt-4.1-mini")
MODEL_ADMIN = os.getenv("MODEL_ADMIN", "gpt-4.1-mini")
MODEL_MODERATION = os.getenv("MODEL_MODERATION", "gpt-4.1-mini")

# Misc
DB_PATH = os.getenv("DB_PATH", "nexora.db")

# =========================
# SAFETY: NEVER MENTION AI-ADMIN TO USERS
# =========================
USER_FACING_ADMIN_REDIRECT = (
    "Для админ-действий напиши администрации сервера. "
    "Я здесь как помощник по использованию Nexora и его функций."
)

WELCOME_BLURB = (
    "Я Nexora AI — дружелюбный помощник по серверу Nexora.\n"
    "Могу подсказать, как пользоваться функциями сервера, торговыми каналами, тикетами и правилами.\n"
    "Напиши вопрос прямо сюда и укажи, что именно хочешь сделать."
)

# =========================
# DATABASE
# =========================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def db_init():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_daily (
        user_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (user_id, day)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_state (
        user_id INTEGER PRIMARY KEY,
        greeted INTEGER NOT NULL DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_plans (
        plan_id TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL,
        requester_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        plan_json TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def today_key_utc() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d")

def usage_get(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    day = today_key_utc()
    cur.execute("SELECT count FROM usage_daily WHERE user_id=? AND day=?", (user_id, day))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0

def usage_inc(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    day = today_key_utc()
    cur.execute("SELECT count FROM usage_daily WHERE user_id=? AND day=?", (user_id, day))
    row = cur.fetchone()
    if row:
        new_count = int(row[0]) + 1
        cur.execute("UPDATE usage_daily SET count=? WHERE user_id=? AND day=?", (new_count, user_id, day))
    else:
        new_count = 1
        cur.execute("INSERT INTO usage_daily(user_id, day, count) VALUES(?,?,?)", (user_id, day, new_count))
    conn.commit()
    conn.close()
    return new_count

def greeted_get(user_id: int) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT greeted FROM user_state WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and int(row[0]) == 1)

def greeted_set(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO user_state(user_id, greeted) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET greeted=1", (user_id,))
    conn.commit()
    conn.close()

def pending_save(plan_id: str, requester_id: int, channel_id: int, plan: Dict[str, Any]):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO pending_plans(plan_id, created_at, requester_id, channel_id, plan_json) VALUES(?,?,?,?,?)",
        (plan_id, int(time.time()), requester_id, channel_id, json.dumps(plan, ensure_ascii=False))
    )
    conn.commit()
    conn.close()

def pending_load(plan_id: str) -> Optional[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT plan_json FROM pending_plans WHERE plan_id=?", (plan_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0])

def pending_delete(plan_id: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_plans WHERE plan_id=?", (plan_id,))
    conn.commit()
    conn.close()

# =========================
# OPENAI CLIENT
# =========================
client_ai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# =========================
# DISCORD BOT SETUP
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # REQUIRED for reading messages / moderation / delete-last-message

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# HELPERS
# =========================
def is_paid_member(member: discord.Member) -> bool:
    role_names = {r.name for r in member.roles}
    if role_names.intersection(set(PAID_ROLES)):
        return True
    # admins unlimited
    if member.guild_permissions.administrator:
        return True
    if ROLE_ADMINISTRATOR in role_names or ROLE_AI_ADMIN in role_names:
        return True
    return False

def is_admin_operator(member: discord.Member) -> bool:
    # Owner OR has Admin perms OR has AI Admin role OR has "Administrator" role (your naming)
    if member.guild_permissions.administrator:
        return True
    role_names = {r.name for r in member.roles}
    if ROLE_AI_ADMIN in role_names or ROLE_ADMINISTRATOR in role_names:
        return True
    return False

def normalize_channel_name(ch: discord.abc.GuildChannel) -> str:
    return getattr(ch, "name", "").lower()

def is_public_ai_channel(channel: discord.abc.GuildChannel) -> bool:
    name = normalize_channel_name(channel)
    return name in {c.lower() for c in PUBLIC_AI_CHANNELS}

def is_ai_admin_channel(channel: discord.abc.GuildChannel) -> bool:
    return normalize_channel_name(channel) == AI_ADMIN_CHANNEL_NAME.lower()

def find_channel_by_name(guild: discord.Guild, name: str) -> Optional[discord.TextChannel]:
    name = name.lower()
    for ch in guild.text_channels:
        if ch.name.lower() == name:
            return ch
    return None

def strip_bot_mention(content: str) -> str:
    # Remove <@id> / <@!id> mentions + the word "Nexora AI"
    content = re.sub(r"<@!?(\d+)>", "", content).strip()
    content = re.sub(r"\bNexora\s*AI\b", "", content, flags=re.IGNORECASE).strip()
    return content

def safe_short(s: str, n: int = 1500) -> str:
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."

# =========================
# MODERATION (soft)
# =========================
async def moderation_check(text: str) -> Tuple[bool, str]:
    """
    Returns (is_problematic, reason_short)
    We do NOT auto-punish. We only warn + log.
    """
    # Simple fast heuristic to avoid calling API on every message
    bad_words = ["сука", "бляд", "еб", "fuck", "bitch", "cunt"]
    if any(w in text.lower() for w in bad_words):
        # still confirm with model if available
        pass
    if not client_ai:
        return (False, "")

    prompt = (
        "You are a moderation classifier for a Discord community.\n"
        "Classify if the message contains harassment, hate, threats, explicit sexual content, doxxing, or severe profanity.\n"
        "Return JSON only: {\"problem\": true/false, \"reason\": \"short\"}.\n\n"
        f"Message:\n{text}"
    )
    try:
        resp = client_ai.responses.create(
            model=MODEL_MODERATION,
            input=prompt,
        )
        out = resp.output_text.strip()
        data = json.loads(out)
        return (bool(data.get("problem", False)), str(data.get("reason", ""))[:120])
    except Exception:
        return (False, "")

async def log_audit(guild: discord.Guild, text: str):
    ch = find_channel_by_name(guild, AI_AUDIT_CHANNEL_NAME)
    if ch:
        await ch.send(safe_short(text, 1800))

# =========================
# SERVER HELP ASSISTANT (public)
# =========================
async def generate_public_reply(user_text: str, member: discord.Member) -> str:
    """
    Friendly helper about server usage.
    Must NOT mention ai-admin or internal admin channels.
    """
    if not client_ai:
        # fallback minimal
        return "Я онлайн. Задай вопрос по серверу Nexora (тикеты, роли, торговые правила) — и я помогу."

    role_names = [r.name for r in member.roles if r.name != "@everyone"]
    context = (
        "You are Nexora AI — a friendly Discord server helper.\n"
        "You help users understand how to use the Nexora server, its channels, tickets, trade flow, reputation, and rules.\n"
        "CRITICAL RULES:\n"
        "- NEVER mention any admin-only channels or internal admin process.\n"
        "- If user asks for admin actions, say they should contact the server staff.\n"
        "- Reply in the user's language if possible.\n"
        "- Be concise, practical, and specific.\n"
        "- If the question is unclear, ask ONE short clarifying question.\n"
        "\n"
        f"User roles: {', '.join(role_names) if role_names else 'none'}\n"
        f"User message: {user_text}\n"
    )

    resp = client_ai.responses.create(
        model=MODEL_ASSIST,
        input=context,
    )
    return resp.output_text.strip() or "Ок. Скажи, что именно ты хочешь сделать на сервере — и я подскажу шаги."

# =========================
# ADMIN ACTIONS (execute)
# =========================
async def action_create_text_channel(guild: discord.Guild, name: str, category: Optional[str] = None) -> str:
    cat_obj = None
    if category:
        for c in guild.categories:
            if c.name.lower() == category.lower():
                cat_obj = c
                break
    ch = await guild.create_text_channel(name=name, category=cat_obj)
    return f"✅ Created text channel: #{ch.name} (id={ch.id})"

async def action_send_message(guild: discord.Guild, channel: str, content: str, pin: bool = False) -> str:
    ch = None
    # allow #name or name
    channel_name = channel.replace("#", "").strip().lower()
    ch = find_channel_by_name(guild, channel_name)
    if not ch:
        return f"❌ Channel not found: {channel}"
    msg = await ch.send(content)
    if pin:
        try:
            await msg.pin(reason="Nexora AI pin")
        except discord.Forbidden:
            return f"⚠️ Sent message but cannot pin (missing permissions). msg_id={msg.id}"
    return f"✅ Sent message to #{ch.name} (msg_id={msg.id})" + (" and pinned" if pin else "")

async def action_delete_last_message_by_user(guild: discord.Guild, channel: str, user: str, limit_scan: int = 50) -> str:
    """
    Deletes the most recent message in a channel authored by a user (by @mention or username).
    """
    channel_name = channel.replace("#", "").strip().lower()
    ch = find_channel_by_name(guild, channel_name)
    if not ch:
        return f"❌ Channel not found: {channel}"

    # resolve user
    target_id = None
    m = re.search(r"<@!?(\d+)>", user)
    if m:
        target_id = int(m.group(1))
    target_member = None
    if target_id:
        target_member = guild.get_member(target_id)
    if not target_member:
        # try by name/nick
        uname = user.replace("@", "").strip().lower()
        for mem in guild.members:
            if mem.name.lower() == uname or (mem.display_name and mem.display_name.lower() == uname):
                target_member = mem
                break

    if not target_member:
        return f"❌ User not found: {user}"

    try:
        async for msg in ch.history(limit=limit_scan, oldest_first=False):
            if msg.author.id == target_member.id:
                await msg.delete(reason="Nexora AI admin request: delete last message by user")
                return f"✅ Deleted last message by {target_member} in #{ch.name} (msg_id={msg.id})"
        return f"⚠️ No recent messages by {target_member} found in last {limit_scan} messages."
    except discord.Forbidden:
        return "❌ Missing permissions: need Read Message History + Manage Messages."
    except Exception as e:
        return f"❌ Error deleting message: {type(e).__name__}: {e}"

async def action_assign_role(guild: discord.Guild, user_id: int, role_name: str) -> str:
    mem = guild.get_member(user_id)
    if not mem:
        return f"❌ User not found in guild: {user_id}"
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        return f"❌ Role not found: {role_name}"
    try:
        await mem.add_roles(role, reason="Nexora AI admin request: assign role")
        return f"✅ Assigned role '{role.name}' to {mem}."
    except discord.Forbidden:
        return "❌ Cannot assign role (permission/role hierarchy issue)."
    except Exception as e:
        return f"❌ Error: {type(e).__name__}: {e}"

# Registry of executable actions
ACTION_FUNCS = {
    "create_text_channel": action_create_text_channel,
    "send_message": action_send_message,
    "delete_last_message_by_user": action_delete_last_message_by_user,
    "assign_role": action_assign_role,
}

# =========================
# ADMIN PLANNER (LLM -> plan JSON with actions)
# =========================
ADMIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_text_channel",
            "description": "Create a new text channel",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string", "nullable": True},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to a channel (optionally pin it)",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "content": {"type": "string"},
                    "pin": {"type": "boolean"}
                },
                "required": ["channel", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_last_message_by_user",
            "description": "Delete the most recent message by a user in a channel (no message ID needed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "user": {"type": "string"},
                    "limit_scan": {"type": "integer"}
                },
                "required": ["channel", "user"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "assign_role",
            "description": "Assign a role to a user by user_id",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "role_name": {"type": "string"}
                },
                "required": ["user_id", "role_name"]
            }
        }
    },
]

def new_plan_id() -> str:
    return hex(int(time.time() * 1000))[2:]

async def build_admin_plan(request_text: str) -> Dict[str, Any]:
    """
    Returns: {summary, risk, actions:[{name,args}], notes}
    """
    if not client_ai:
        return {
            "summary": "OpenAI not configured. No actions planned.",
            "risk": "low",
            "actions": [],
            "notes": "Set OPENAI_API_KEY."
        }

    sys = (
        "You are Nexora AI operating in ADMIN MODE.\n"
        "You will create an execution plan for Discord server administration.\n"
        "RULES:\n"
        "- Only plan actions that are available as tools.\n"
        "- Prefer minimal actions.\n"
        "- Output must be valid JSON with keys: summary, risk, actions, notes.\n"
        "- actions is an array of {name, args}.\n"
        "- risk is one of: low, medium, high.\n"
        "- Never ask for #ai-admin or mention it (this is internal).\n"
    )

    prompt = f"{sys}\nAdmin request: {request_text}"

    resp = client_ai.responses.create(
        model=MODEL_ADMIN,
        input=prompt,
        tools=ADMIN_TOOLS,
        tool_choice="auto",
    )

    # Build actions from tool calls if present; fallback: parse JSON from text
    actions = []
    try:
        for item in resp.output:
            if item.type == "tool_call":
                name = item.name
                args = item.arguments if isinstance(item.arguments, dict) else json.loads(item.arguments)
                actions.append({"name": name, "args": args})
    except Exception:
        actions = []

    text = resp.output_text.strip() if hasattr(resp, "output_text") else ""
    if text:
        # try parse JSON with summary/risk/notes
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if not actions and isinstance(data.get("actions"), list):
                    actions = data["actions"]
                return {
                    "summary": data.get("summary", "Admin plan"),
                    "risk": data.get("risk", "low"),
                    "actions": actions,
                    "notes": data.get("notes", "")
                }
        except Exception:
            pass

    return {
        "summary": "Admin plan generated.",
        "risk": "low",
        "actions": actions,
        "notes": "Confirm to execute."
    }

# =========================
# UI: Confirm / Cancel
# =========================
class PlanView(discord.ui.View):
    def __init__(self, plan_id: str, requester_id: int, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.plan_id = plan_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only requester or admin operators can confirm
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.id == self.requester_id:
            return True
        return is_admin_operator(interaction.user)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        plan = pending_load(self.plan_id)
        if not plan:
            await interaction.followup.send("❌ Plan not found or expired.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ No guild context.", ephemeral=True)
            return

        results = []
        for act in plan.get("actions", []):
            name = act.get("name")
            args = act.get("args", {}) or {}
            fn = ACTION_FUNCS.get(name)
            if not fn:
                results.append(f"⚠️ Unknown action: {name}")
                continue
            try:
                res = await fn(guild, **args)
                results.append(res)
            except TypeError as e:
                results.append(f"❌ Bad args for {name}: {e}")
            except Exception as e:
                results.append(f"❌ {name} failed: {type(e).__name__}: {e}")

        pending_delete(self.plan_id)

        out = "\n".join(results) if results else "No actions executed."
        await interaction.followup.send(safe_short(out, 1900), ephemeral=True)
        await log_audit(guild, f"✅ EXECUTED plan {self.plan_id} by {interaction.user}:\n{out}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        pending_delete(self.plan_id)
        await interaction.followup.send("🛑 Cancelled.", ephemeral=True)
        if interaction.guild:
            await log_audit(interaction.guild, f"🛑 Cancelled plan {self.plan_id} by {interaction.user}")

# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    db_init()
    if GUILD_ID:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        try:
            await bot.tree.sync(guild=guild_obj)
        except Exception:
            pass
    print(f"Logged in as {bot.user} (id={bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.guild:
        return

    # Soft moderation check (public channels only)
    if is_public_ai_channel(message.channel):
        problem, reason = await moderation_check(message.content)
        if problem:
            # warn user (soft) + audit
            try:
                await message.reply(
                    "Пожалуйста, без токсичности/непристойностей 🙏 "
                    "Если есть вопрос — напиши спокойно, и я помогу.",
                    mention_author=False
                )
            except Exception:
                pass
            await log_audit(message.guild, f"⚠️ Moderation flag in #{message.channel.name} by {message.author}: {reason}\nContent: {safe_short(message.content, 700)}")

    # ADMIN CHANNEL: plan/confirm/execute
    if is_ai_admin_channel(message.channel):
        if not isinstance(message.author, discord.Member) or not is_admin_operator(message.author):
            # ignore silently (channel should be hidden anyway)
            return

        req = message.content.strip()
        if not req:
            return

        plan = await build_admin_plan(req)
        plan_id = new_plan_id()
        pending_save(plan_id, message.author.id, message.channel.id, plan)

        summary = plan.get("summary", "Plan")
        risk = plan.get("risk", "low")
        actions = plan.get("actions", [])
        notes = plan.get("notes", "")

        embed = discord.Embed(title=f"🧩 PLAN {plan_id}", description=summary, color=0x2B90D9)
        embed.add_field(name="Risk", value=str(risk), inline=True)
        if actions:
            formatted = "\n".join([f"• `{a.get('name')}` {a.get('args', {})}" for a in actions])
        else:
            formatted = "• (no actions)"
        embed.add_field(name="Actions", value=safe_short(formatted, 1000), inline=False)
        if notes:
            embed.add_field(name="Notes", value=safe_short(notes, 900), inline=False)

        view = PlanView(plan_id=plan_id, requester_id=message.author.id)
        await message.reply(embed=embed, view=view, mention_author=False)
        await log_audit(message.guild, f"📝 PLAN {plan_id} by {message.author}:\n{summary}\nActions: {actions}")
        return

    # PUBLIC: bot should answer only in PUBLIC_AI_CHANNELS
    if not is_public_ai_channel(message.channel):
        return

    # Trigger: mention bot OR reply to bot OR message starts with "ai" / "бот" / "help"
    mentioned = bot.user and bot.user.mentioned_in(message)
    is_reply_to_bot = bool(message.reference and isinstance(message.reference.resolved, discord.Message) and message.reference.resolved.author.id == bot.user.id) if bot.user else False
    trigger_prefix = re.match(r"^(ai|бот|help|помоги|вопрос)\b", message.content.strip(), flags=re.IGNORECASE)

    if not (mentioned or is_reply_to_bot or trigger_prefix):
        return

    member = message.author if isinstance(message.author, discord.Member) else None
    if not member:
        return

    # Never tell about admin channel to users (even if they ask)
    user_text = strip_bot_mention(message.content)
    if not user_text:
        user_text = "Привет!"

    # Free limit logic
    if not is_paid_member(member):
        current = usage_get(member.id)
        if current >= FREE_DAILY_LIMIT:
            await message.reply(
                f"Лимит бесплатных сообщений на сегодня исчерпан: {current}/{FREE_DAILY_LIMIT}.\n"
                f"Я могу помогать с базовыми вопросами о сервере, но полный доступ доступен по подписке.",
                mention_author=False
            )
            return
        new_count = usage_inc(member.id)
    else:
        new_count = -1  # unlimited

    # First friendly greeting (once)
    if not greeted_get(member.id):
        greeted_set(member.id)
        greet = WELCOME_BLURB
        # Include the user's question right away afterwards:
        # We'll still answer the question below (not just greeting).
        try:
            await message.reply(greet, mention_author=False)
        except Exception:
            pass

    # If user asks to do admin actions in public, redirect WITHOUT naming channels
    admin_keywords = ["delete", "remove", "ban", "kick", "создай канал", "удали", "бан", "кик", "роль", "permissions", "perms"]
    if any(k in user_text.lower() for k in admin_keywords):
        # Still can give guidance, but not execute and not reveal internal admin channel
        await message.reply(USER_FACING_ADMIN_REDIRECT, mention_author=False)
        return

    # Generate helpful reply about server usage
    answer = await generate_public_reply(user_text, member)

    # Append free counter (only for free users)
    if new_count >= 0 and not is_paid_member(member):
        answer = f"{answer}\n\n🧠 Бесплатные сообщения сегодня: {new_count}/{FREE_DAILY_LIMIT}"

    await message.reply(safe_short(answer, 1800), mention_author=False)

# =========================
# SLASH COMMANDS (optional)
# =========================
@bot.tree.command(name="ping", description="Check if Nexora AI is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("PONG ✅", ephemeral=True)

@bot.tree.command(name="help_nexora", description="How to use Nexora server features")
async def help_nexora(interaction: discord.Interaction):
    await interaction.response.send_message(WELCOME_BLURB, ephemeral=True)

# =========================
# RUN
# =========================
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")
db_init()
bot.run(DISCORD_TOKEN)

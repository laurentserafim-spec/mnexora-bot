import os
import json
import re
import asyncio
import sqlite3
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from openai import OpenAI


# =========================
# CONFIG (ENV VARS)
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Channel names (you can change if needed)
AI_ADMIN_CHANNEL_NAME = os.getenv("AI_ADMIN_CHANNEL_NAME", "ai-admin").strip()
AI_AUDIT_CHANNEL_NAME = os.getenv("AI_AUDIT_CHANNEL_NAME", "ai-audit-log").strip()

# Roles (names)
ROLE_ADMINISTRATOR = os.getenv("ROLE_ADMINISTRATOR", "Administrator").strip()
ROLE_AI_ADMIN = os.getenv("ROLE_AI_ADMIN", "AI Admin").strip()
ROLE_BOT_ROLE = os.getenv("ROLE_BOT_ROLE", "Nexora AI").strip()

ROLE_VISITOR = os.getenv("ROLE_VISITOR", "Visitor").strip()
ROLE_MEMBER = os.getenv("ROLE_MEMBER", "Member").strip()

# Daily limits
VISITOR_DAILY_LIMIT = int(os.getenv("VISITOR_DAILY_LIMIT", "15"))
MEMBER_DAILY_LIMIT = int(os.getenv("MEMBER_DAILY_LIMIT", "50"))

# Behavior
ALWAYS_ENGLISH = os.getenv("ALWAYS_ENGLISH", "1").strip() == "1"  # 1 => only English replies
MODEL_ADMIN = os.getenv("MODEL_ADMIN", "gpt-4.1-mini").strip()

# SQLite file
DB_PATH = os.getenv("DB_PATH", "nexora.sqlite3").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing")


client_ai = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# DATABASE
# =========================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, day)
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            guild_id TEXT,
            actor_user_id TEXT,
            actor_tag TEXT,
            action_type TEXT,
            payload_json TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def get_today_key() -> str:
    # Use UTC day for stable daily limits
    return date.today().isoformat()


def get_usage(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    day = get_today_key()
    cur.execute("SELECT count FROM daily_usage WHERE user_id=? AND day=?", (str(user_id), day))
    row = cur.fetchone()
    conn.close()
    return int(row["count"]) if row else 0


def inc_usage(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    day = get_today_key()
    cur.execute("SELECT count FROM daily_usage WHERE user_id=? AND day=?", (str(user_id), day))
    row = cur.fetchone()
    if row:
        new_count = int(row["count"]) + 1
        cur.execute("UPDATE daily_usage SET count=? WHERE user_id=? AND day=?", (new_count, str(user_id), day))
    else:
        new_count = 1
        cur.execute("INSERT INTO daily_usage (user_id, day, count) VALUES (?, ?, ?)", (str(user_id), day, new_count))
    conn.commit()
    conn.close()
    return new_count


def audit_write(guild_id: Optional[int], actor: discord.abc.User, action_type: str, payload: Dict[str, Any]) -> None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO audit_log (ts, guild_id, actor_user_id, actor_tag, action_type, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            str(guild_id) if guild_id else None,
            str(actor.id),
            str(actor),
            action_type,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


# =========================
# DISCORD HELPERS
# =========================
def is_admin_member(member: discord.Member) -> bool:
    # If they have Discord Administrator permission OR roles Admin/AI Admin
    if member.guild_permissions.administrator:
        return True
    role_names = {r.name for r in member.roles}
    return (ROLE_ADMINISTRATOR in role_names) or (ROLE_AI_ADMIN in role_names)


def get_daily_limit_for(member: discord.Member) -> Optional[int]:
    # None => unlimited
    if is_admin_member(member):
        return None
    role_names = {r.name for r in member.roles}
    if ROLE_MEMBER in role_names:
        return MEMBER_DAILY_LIMIT
    return VISITOR_DAILY_LIMIT  # default visitor


def find_text_channel_by_name(guild: discord.Guild, name: str) -> Optional[discord.TextChannel]:
    name = name.lstrip("#").strip().lower()
    for ch in guild.text_channels:
        if ch.name.lower() == name:
            return ch
    return None


def find_role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    name = name.strip()
    for r in guild.roles:
        if r.name == name:
            return r
    return None


def normalize_channel_ref(value: str) -> str:
    # Accept "#general", "general", channel id
    v = str(value).strip()
    return v


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except Exception:
        return None


# =========================
# OPENAI: SYSTEM + TOOLS
# =========================
SYSTEM_ADMIN = f"""
You are Nexora AI, a Discord admin assistant bot.
You must produce a safe admin PLAN before execution, with tool calls for actions.
The bot runs inside a Discord server. You ONLY act on explicit admin requests coming from the #ai-admin channel.

Language:
- {"Always respond in English." if ALWAYS_ENGLISH else "Respond in the language used by the requester."}

Rules:
- Never claim you can do something outside Discord permissions. If missing permissions/role hierarchy blocks an action, note it.
- Do NOT invent channel/role/user IDs. Use provided names/ids; if ambiguous, choose the safest option (no action) and ask for clarification.
- Prefer minimal changes. Do not modify Administrator permissions.
- Always output tool calls (if any) for concrete actions. If nothing actionable, output a short message explaining what is missing.

Output style:
- Provide short summary and risk level (low/medium/high) inside the message response.
- Then perform tool calls as needed.
""".strip()


def tool_schema() -> List[Dict[str, Any]]:
    # IMPORTANT: new Responses API tool format requires: {"type":"function","function": {"name":..., "description":..., "parameters":...}}
    return [
        {
            "type": "function",
            "function": {
                "name": "create_text_channel",
                "description": "Create a new text channel in the guild.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Channel name, without #"},
                        "category": {"type": ["string", "null"], "description": "Optional category name"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_message",
                "description": "Send a message to a channel.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel name (#name) or channel id"},
                        "content": {"type": "string", "description": "Message content"},
                        "pin": {"type": "boolean", "description": "Whether to pin the sent message"},
                    },
                    "required": ["channel", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "pin_message",
                "description": "Pin a message by id in a channel.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel name (#name) or channel id"},
                        "message_id": {"type": "string", "description": "Message id"},
                    },
                    "required": ["channel", "message_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_message",
                "description": "Delete a message by id in a channel (requires Manage Messages and proper permissions).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel name (#name) or channel id"},
                        "message_id": {"type": "string", "description": "Message id"},
                        "reason": {"type": ["string", "null"], "description": "Optional reason"},
                    },
                    "required": ["channel", "message_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_role",
                "description": "Create a new role in the guild.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Role name"},
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_role_to_user",
                "description": "Add a role to a user by user id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user": {"type": "string", "description": "User id"},
                        "role": {"type": "string", "description": "Role name"},
                    },
                    "required": ["user", "role"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remove_role_from_user",
                "description": "Remove a role from a user by user id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user": {"type": "string", "description": "User id"},
                        "role": {"type": "string", "description": "Role name"},
                    },
                    "required": ["user", "role"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_channel_permissions",
                "description": "Set channel permissions for a role (view/send).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string", "description": "Channel name (#name) or channel id"},
                        "role": {"type": "string", "description": "Role name"},
                        "view": {"type": ["boolean", "null"], "description": "Allow view channel (None = don't change)"},
                        "send": {"type": ["boolean", "null"], "description": "Allow send messages (None = don't change)"},
                    },
                    "required": ["channel", "role"],
                },
            },
        },
    ]


TOOLS_SCHEMA = tool_schema()


async def build_admin_plan(user_text: str) -> Dict[str, Any]:
    """
    Builds a plan via OpenAI Responses API.
    FIXED: tools schema format is correct and includes required function.name.
    """
    resp = client_ai.responses.create(
        model=MODEL_ADMIN,
        input=[
            {"role": "system", "content": SYSTEM_ADMIN},
            {"role": "user", "content": user_text},
        ],
        tools=TOOLS_SCHEMA,
        tool_choice="auto",
    )

    summary = "Admin request"
    risk = "low"
    notes = ""
    actions: List[Dict[str, Any]] = []

    # Parse output items
    for item in resp.output:
        # Message text
        if getattr(item, "type", None) == "message":
            for c in item.content:
                if c.type == "output_text":
                    # Use as summary if present
                    txt = (c.text or "").strip()
                    if txt:
                        summary = txt

        # Tool calls
        if getattr(item, "type", None) == "tool_call":
            # item.name = function name in Responses API
            # item.arguments may be a dict or a JSON string depending on SDK versions
            args = item.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"raw": args}
            if args is None:
                args = {}
            actions.append({"name": item.name, "arguments": args})

    # Try to detect risk keywords in summary if model includes them
    # (optional; safe default "low")
    m = re.search(r"risk\s*:\s*(low|medium|high)", summary, re.IGNORECASE)
    if m:
        risk = m.group(1).lower()

    return {"summary": summary, "risk": risk, "notes": notes, "actions": actions}


# =========================
# EXECUTION: TOOL IMPLEMENTATIONS
# =========================
async def tool_create_text_channel(guild: discord.Guild, args: Dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip().lstrip("#")
    category_name = args.get("category", None)
    if not name:
        return "❌ Missing channel name."

    category = None
    if category_name:
        cn = str(category_name).strip()
        for cat in guild.categories:
            if cat.name.lower() == cn.lower():
                category = cat
                break

    # Create channel
    ch = await guild.create_text_channel(name=name, category=category, reason="Nexora AI admin action")
    return f"✅ Created text channel: #{ch.name}"


async def resolve_channel(guild: discord.Guild, channel_ref: str) -> Optional[discord.TextChannel]:
    channel_ref = normalize_channel_ref(channel_ref)

    # By ID
    cid = parse_int(channel_ref)
    if cid:
        ch = guild.get_channel(cid)
        if isinstance(ch, discord.TextChannel):
            return ch

    # By name
    ch = find_text_channel_by_name(guild, channel_ref)
    return ch


async def tool_send_message(guild: discord.Guild, args: Dict[str, Any]) -> str:
    ch_ref = str(args.get("channel", "")).strip()
    content = str(args.get("content", "")).strip()
    pin = bool(args.get("pin", False))
    if not ch_ref or not content:
        return "❌ Missing channel/content."

    ch = await resolve_channel(guild, ch_ref)
    if not ch:
        return f"❌ Channel not found: {ch_ref}"

    msg = await ch.send(content)
    if pin:
        try:
            await msg.pin(reason="Nexora AI admin action")
        except discord.Forbidden:
            return f"✅ Sent message to #{ch.name} but ❌ failed to pin (Missing Permissions)."
    return f"✅ Sent message to #{ch.name} (id={msg.id})" + (" and pinned." if pin else ".")


async def tool_pin_message(guild: discord.Guild, args: Dict[str, Any]) -> str:
    ch_ref = str(args.get("channel", "")).strip()
    mid = str(args.get("message_id", "")).strip()
    if not ch_ref or not mid:
        return "❌ Missing channel/message_id."
    ch = await resolve_channel(guild, ch_ref)
    if not ch:
        return f"❌ Channel not found: {ch_ref}"

    mid_int = parse_int(mid)
    if not mid_int:
        return "❌ Invalid message_id."

    try:
        msg = await ch.fetch_message(mid_int)
        await msg.pin(reason="Nexora AI admin action")
        return f"✅ Pinned message {mid} in #{ch.name}"
    except discord.NotFound:
        return "❌ Message not found."
    except discord.Forbidden:
        return "❌ Missing Permissions to pin messages."
    except discord.HTTPException as e:
        return f"❌ Failed to pin: {e}"


async def tool_delete_message(guild: discord.Guild, args: Dict[str, Any]) -> str:
    ch_ref = str(args.get("channel", "")).strip()
    mid = str(args.get("message_id", "")).strip()
    reason = args.get("reason", None)
    if not ch_ref or not mid:
        return "❌ Missing channel/message_id."
    ch = await resolve_channel(guild, ch_ref)
    if not ch:
        return f"❌ Channel not found: {ch_ref}"

    mid_int = parse_int(mid)
    if not mid_int:
        return "❌ Invalid message_id."

    try:
        msg = await ch.fetch_message(mid_int)
        await msg.delete(reason=str(reason) if reason else "Nexora AI admin action")
        return f"✅ Deleted message {mid} in #{ch.name}"
    except discord.NotFound:
        return "❌ Message not found."
    except discord.Forbidden:
        return "❌ Missing Permissions to delete messages (Manage Messages)."
    except discord.HTTPException as e:
        return f"❌ Failed to delete: {e}"


async def tool_create_role(guild: discord.Guild, args: Dict[str, Any]) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "❌ Missing role name."
    existing = find_role_by_name(guild, name)
    if existing:
        return f"✅ Role already exists: {name}"
    await guild.create_role(name=name, reason="Nexora AI admin action")
    return f"✅ Created role: {name}"


async def tool_add_role_to_user(guild: discord.Guild, args: Dict[str, Any]) -> str:
    user_id = parse_int(args.get("user"))
    role_name = str(args.get("role", "")).strip()
    if not user_id or not role_name:
        return "❌ Missing user/role."

    role = find_role_by_name(guild, role_name)
    if not role:
        return f"❌ Role not found: {role_name}"

    member = guild.get_member(user_id)
    if not member:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return f"❌ User not found in guild: {user_id}"

    try:
        await member.add_roles(role, reason="Nexora AI admin action")
        return f"✅ Added role {role.name} to {member.display_name}"
    except discord.Forbidden:
        return "❌ Missing Permissions / Role hierarchy prevents adding this role."
    except discord.HTTPException as e:
        return f"❌ Failed to add role: {e}"


async def tool_remove_role_from_user(guild: discord.Guild, args: Dict[str, Any]) -> str:
    user_id = parse_int(args.get("user"))
    role_name = str(args.get("role", "")).strip()
    if not user_id or not role_name:
        return "❌ Missing user/role."

    role = find_role_by_name(guild, role_name)
    if not role:
        return f"❌ Role not found: {role_name}"

    member = guild.get_member(user_id)
    if not member:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return f"❌ User not found in guild: {user_id}"

    try:
        await member.remove_roles(role, reason="Nexora AI admin action")
        return f"✅ Removed role {role.name} from {member.display_name}"
    except discord.Forbidden:
        return "❌ Missing Permissions / Role hierarchy prevents removing this role."
    except discord.HTTPException as e:
        return f"❌ Failed to remove role: {e}"


async def tool_set_channel_permissions(guild: discord.Guild, args: Dict[str, Any]) -> str:
    ch_ref = str(args.get("channel", "")).strip()
    role_name = str(args.get("role", "")).strip()
    view = args.get("view", None)
    send = args.get("send", None)

    if not ch_ref or not role_name:
        return "❌ Missing channel/role."

    ch = await resolve_channel(guild, ch_ref)
    if not ch:
        return f"❌ Channel not found: {ch_ref}"

    role = None
    if role_name.lower() in ["@everyone", "everyone"]:
        role = guild.default_role
    else:
        role = find_role_by_name(guild, role_name)

    if not role:
        return f"❌ Role not found: {role_name}"

    # Prepare overwrite
    overwrite = ch.overwrites_for(role)
    if isinstance(view, bool):
        overwrite.view_channel = view
    if isinstance(send, bool):
        overwrite.send_messages = send

    try:
        await ch.set_permissions(role, overwrite=overwrite, reason="Nexora AI admin action")
        return f"✅ Set permissions in #{ch.name} for role {role.name}: view={overwrite.view_channel} send={overwrite.send_messages}"
    except discord.Forbidden:
        return "❌ Missing Permissions to set channel permissions."
    except discord.HTTPException as e:
        return f"❌ Failed to set permissions: {e}"


TOOL_EXECUTORS = {
    "create_text_channel": tool_create_text_channel,
    "send_message": tool_send_message,
    "pin_message": tool_pin_message,
    "delete_message": tool_delete_message,
    "create_role": tool_create_role,
    "add_role_to_user": tool_add_role_to_user,
    "remove_role_from_user": tool_remove_role_from_user,
    "set_channel_permissions": tool_set_channel_permissions,
}


# =========================
# UI: Confirm/Cancel Buttons
# =========================
class PlanView(discord.ui.View):
    def __init__(self, bot: commands.Bot, plan_id: str, actor_id: int):
        super().__init__(timeout=600)  # 10 minutes
        self.bot = bot
        self.plan_id = plan_id
        self.actor_id = actor_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("❌ Only the admin who created this plan can confirm it.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        result = await self.bot.execute_plan(self.plan_id, interaction)
        await interaction.followup.send(result, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("❌ Only the admin who created this plan can cancel it.", ephemeral=True)
            return
        await interaction.response.send_message("🛑 Cancelled.", ephemeral=True)
        # Remove stored plan
        self.bot.pending_plans.pop(self.plan_id, None)
        self.stop()


# =========================
# BOT
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

class NexoraBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.pending_plans: Dict[str, Dict[str, Any]] = {}  # plan_id -> plan data

    async def setup_hook(self):
        # Optional slash command sync
        try:
            await self.tree.sync()
        except Exception:
            pass

    async def on_ready(self):
        print(f"Logged in as {self.user} (id={self.user.id})")

    async def execute_plan(self, plan_id: str, interaction: discord.Interaction) -> str:
        plan = self.pending_plans.get(plan_id)
        if not plan:
            return "❌ Plan not found or expired."

        guild: discord.Guild = plan["guild"]
        actor: discord.Member = plan["actor"]
        actions: List[Dict[str, Any]] = plan["actions"]
        summary: str = plan["summary"]
        risk: str = plan["risk"]

        # Execute tools
        results: List[str] = []
        for a in actions:
            name = a.get("name")
            args = a.get("arguments", {}) or {}
            fn = TOOL_EXECUTORS.get(name)
            if not fn:
                results.append(f"❌ Unknown action: {name}")
                continue
            try:
                out = await fn(guild, args)
                results.append(out)
            except Exception as e:
                results.append(f"❌ Failed action {name}: {e}")

        # Audit
        audit_write(guild.id, actor, "EXECUTE_PLAN", {"plan_id": plan_id, "summary": summary, "risk": risk, "actions": actions, "results": results})

        # Send to audit channel
        audit_ch = find_text_channel_by_name(guild, AI_AUDIT_CHANNEL_NAME)
        if audit_ch:
            text = f"✅ **EXECUTED** by {actor.mention} | {summary}\n" + "\n".join([f"• {r}" for r in results]) if results else f"✅ **EXECUTED** by {actor.mention} | {summary}\n-"
            await audit_ch.send(text)

        # Remove plan
        self.pending_plans.pop(plan_id, None)

        if results:
            return "✅ Done:\n" + "\n".join([f"- {r}" for r in results])
        return "✅ Done."

bot = NexoraBot()


# =========================
# ADMIN COMMAND ENTRY (MENTION)
# =========================
MENTION_RE = re.compile(r"^<@!?\d+>\s*(.*)$", re.DOTALL)

async def handle_admin_request(message: discord.Message, request_text: str) -> None:
    if not message.guild or not isinstance(message.author, discord.Member):
        return

    guild = message.guild
    actor: discord.Member = message.author

    # Only allow from ai-admin channel
    if not isinstance(message.channel, discord.TextChannel):
        return
    if message.channel.name.lower() != AI_ADMIN_CHANNEL_NAME.lower():
        return

    # Must be admin
    if not is_admin_member(actor):
        await message.reply("❌ You do not have permission to use admin mode here.", mention_author=False)
        return

    # Build plan via OpenAI
    try:
        plan = await build_admin_plan(request_text)
    except Exception as e:
        await message.reply(f"❌ OpenAI error: {e}", mention_author=False)
        return

    plan_id = os.urandom(4).hex()
    bot.pending_plans[plan_id] = {
        "guild": guild,
        "actor": actor,
        "summary": plan.get("summary", "Admin request"),
        "risk": plan.get("risk", "low"),
        "notes": plan.get("notes", ""),
        "actions": plan.get("actions", []),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Audit PLAN
    audit_write(guild.id, actor, "PLAN", {"plan_id": plan_id, "request": request_text, "plan": plan})

    # Send to audit channel too
    audit_ch = find_text_channel_by_name(guild, AI_AUDIT_CHANNEL_NAME)
    if audit_ch:
        await audit_ch.send(f"📝 **ADMIN PLAN** by {actor.mention} | {plan['summary']} | risk={plan.get('risk','low')} | id={plan_id}")

    # Show plan in ai-admin
    actions = plan.get("actions", [])
    lines = []
    lines.append(f"🧩 **PLAN {plan_id}**")
    lines.append(f"**Summary:** {plan.get('summary','')}")
    lines.append(f"**Risk:** {plan.get('risk','low')}")
    if actions:
        lines.append("**Actions:**")
        for a in actions:
            lines.append(f"• `{a.get('name')}` {a.get('arguments')}")
    else:
        lines.append("**Actions:** (no actions)")
        if plan.get("notes"):
            lines.append(f"**Notes:** {plan.get('notes')}")

    view = PlanView(bot, plan_id=plan_id, actor_id=actor.id)
    await message.reply("\n".join(lines), view=view, mention_author=False)


# =========================
# NORMAL CHAT MODE (LIMITED)
# =========================
async def handle_public_chat(message: discord.Message) -> None:
    # Optional: If you want the bot to reply in other channels with daily limits.
    # This keeps it simple: bot replies only when mentioned.
    if not message.guild or not isinstance(message.author, discord.Member):
        return
    if message.author.bot:
        return

    member: discord.Member = message.author
    limit = get_daily_limit_for(member)
    if limit is not None:
        used = get_usage(member.id)
        if used >= limit:
            await message.reply(f"🧠 Daily limit reached: {used}/{limit}.", mention_author=False)
            return
        new_used = inc_usage(member.id)
        remaining = max(0, limit - new_used)
        footer = f"\n\n🧠 Free messages left today: {remaining}/{limit}"
    else:
        footer = ""

    # Very simple response (you can later upgrade to full assistant mode)
    # For now: acknowledge + point to tickets/ai-admin
    if ALWAYS_ENGLISH:
        text = "Hi! For admin actions, please use #ai-admin. For support, open a ticket if available." + footer
    else:
        text = "Привет! Для админ-действий используй #ai-admin. Для поддержки — открой тикет." + footer

    await message.reply(text, mention_author=False)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # If bot is mentioned
    if bot.user and bot.user.mentioned_in(message):
        content = message.content.strip()
        m = MENTION_RE.match(content)
        if m:
            after = (m.group(1) or "").strip()
        else:
            # If mention isn't at start, still accept entire content
            after = content

        # If in ai-admin -> admin mode
        if isinstance(message.channel, discord.TextChannel) and message.channel.name.lower() == AI_ADMIN_CHANNEL_NAME.lower():
            if not after:
                await message.reply("Please write the admin request after mentioning me. Example: @Nexora AI create text channel test3", mention_author=False)
                return
            await handle_admin_request(message, after)
            return

        # Else: normal limited mode
        await handle_public_chat(message)
        return

    await bot.process_commands(message)


# Optional slash command: /ping
@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Pong!", ephemeral=True)


# =========================
# STARTUP
# =========================
def main():
    db_init()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

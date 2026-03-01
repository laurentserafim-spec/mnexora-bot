import os
import re
import json
import asyncio
import sqlite3
import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# OpenAI SDK (new)
from openai import OpenAI

# ============================================================
# CONFIG
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Model can be configured from env
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

ADMIN_CHANNEL_NAME = os.getenv("ADMIN_CHANNEL_NAME", "ai-admin").strip()
AUDIT_CHANNEL_NAME = os.getenv("AUDIT_CHANNEL_NAME", "ai-audit-log").strip()

DB_PATH = os.getenv("DB_PATH", "nexora.sqlite").strip()

# Safety: do not let the bot modify these roles (you can add more)
PROTECTED_ROLE_NAMES = {
    "Administrator",
    "AI Admin",
    "Nexora AI",     # the bot's own role
    "@everyone",     # implicit
}

# Auto-moderation toggles
AUTO_MOD_ENABLED = os.getenv("AUTO_MOD_ENABLED", "1").strip() == "1"
AUTO_MOD_USE_OPENAI = os.getenv("AUTO_MOD_USE_OPENAI", "1").strip() == "1"  # optional
AUTO_MOD_ACTION = os.getenv("AUTO_MOD_ACTION", "warn").strip().lower()       # warn|delete|timeout
AUTO_MOD_TIMEOUT_MIN = int(os.getenv("AUTO_MOD_TIMEOUT_MIN", "10").strip())

# Quota (daily) example for Visitor/Member
DAILY_LIMIT_VISITOR = int(os.getenv("DAILY_LIMIT_VISITOR", "15").strip())
DAILY_LIMIT_MEMBER = int(os.getenv("DAILY_LIMIT_MEMBER", "50").strip())
# Admins unlimited by default
UNLIMITED_ROLE_NAMES = {"Administrator", "AI Admin"}

# Channels excluded from automod
AUTOMOD_EXCLUDE_CHANNELS = {ADMIN_CHANNEL_NAME, AUDIT_CHANNEL_NAME}

# ============================================================
# DB
# ============================================================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_plans (
            id TEXT PRIMARY KEY,
            guild_id TEXT,
            channel_id TEXT,
            author_id TEXT,
            created_at TEXT,
            plan_json TEXT,
            status TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            guild_id TEXT,
            user_id TEXT,
            day TEXT,
            count INTEGER,
            PRIMARY KEY (guild_id, user_id, day)
        )
    """)
    conn.commit()
    conn.close()

# ============================================================
# OPENAI CLIENT
# ============================================================

client_ai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def _safe_json_loads(text: str) -> Dict[str, Any]:
    # Try to extract first JSON object from text if model adds extra text
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    # naive extraction
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        text = text[first:last+1]
    return json.loads(text)

async def build_admin_plan(request_text: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a plan using OpenAI BUT WITHOUT tools/function-calling.
    This avoids the 'tools[0].name' schema error entirely.
    """
    if client_ai is None:
        # fallback minimal plan
        return {
            "summary": "OpenAI is not configured. I can only do basic non-AI actions.",
            "risk": "low",
            "actions": [],
        }

    system = f"""
You are Nexora AI Admin Planner for a Discord server.
You MUST output ONLY valid JSON with this schema:

{{
  "summary": "short summary",
  "risk": "low|medium|high",
  "actions": [
    {{
      "tool": "<tool_name>",
      "args": {{ ... }}
    }}
  ]
}}

Rules:
- Output JSON only. No markdown. No commentary.
- If user request is unclear, set actions=[] and explain in summary what info is missing.
- Prefer minimal required actions.
- NEVER include tools that are not in the allowed tool list.
- DO NOT attempt to do anything outside Discord capabilities.
- Do not change Administrator or @everyone permissions unless explicitly requested.
- If asked to delete a message, require either message_id OR (channel + author + approximate_time + snippet).
- Keep risk:
  - low: read-only or minor changes
  - medium: role/channel/permission changes, deletions
  - high: mass changes, bans, wide permission modifications

Allowed tools:
- create_text_channel(name, category_name|null)
- create_category(name)
- set_channel_permissions(channel_name, role_name, view|null, send|null, manage_messages|null)
- send_message(channel_name, content, pin:bool)
- pin_last_bot_message(channel_name)
- add_role_to_user(user_mention_or_id, role_name)
- remove_role_from_user(user_mention_or_id, role_name)
- timeout_user(user_mention_or_id, minutes, reason)
- delete_message(channel_name, message_id)
- delete_last_message_by_user(channel_name, user_mention_or_id)
- rename_channel(old_name, new_name)
- set_channel_topic(channel_name, topic)
- noop()

Context (JSON):
{json.dumps(context, ensure_ascii=False)}
""".strip()

    user = f"Request: {request_text}".strip()

    # Use responses.create
    resp = await asyncio.to_thread(
        lambda: client_ai.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # No tools parameter here on purpose.
        )
    )

    # Extract text
    out_text = ""
    for item in getattr(resp, "output", []) or []:
        for c in getattr(item, "content", []) or []:
            if getattr(c, "type", "") in ("output_text", "text"):
                out_text += getattr(c, "text", "") or ""
            elif isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                out_text += c.get("text", "") or ""

    if not out_text.strip():
        # Some SDK variants expose resp.output_text
        out_text = getattr(resp, "output_text", "") or ""

    plan = _safe_json_loads(out_text)
    # Basic validation
    if "summary" not in plan or "risk" not in plan or "actions" not in plan:
        return {
            "summary": "Planner returned invalid JSON schema. Try again with a clearer instruction.",
            "risk": "low",
            "actions": []
        }
    if plan["risk"] not in ("low", "medium", "high"):
        plan["risk"] = "medium"
    if not isinstance(plan["actions"], list):
        plan["actions"] = []
    return plan

# ============================================================
# DISCORD HELPERS
# ============================================================

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()

def today_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")

async def get_text_channel_by_name(guild: discord.Guild, name: str) -> Optional[discord.TextChannel]:
    for ch in guild.text_channels:
        if ch.name == name.lstrip("#"):
            return ch
    return None

async def get_category_by_name(guild: discord.Guild, name: str) -> Optional[discord.CategoryChannel]:
    for cat in guild.categories:
        if cat.name == name:
            return cat
    return None

def find_role(guild: discord.Guild, role_name: str) -> Optional[discord.Role]:
    # Handle "@everyone"
    if role_name in ("@everyone", "everyone"):
        return guild.default_role
    for r in guild.roles:
        if r.name == role_name:
            return r
    return None

async def audit_log(guild: discord.Guild, content: str):
    ch = await get_text_channel_by_name(guild, AUDIT_CHANNEL_NAME)
    if ch:
        await ch.send(content)

def is_admin_user(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    # also check special roles
    for r in member.roles:
        if r.name in UNLIMITED_ROLE_NAMES:
            return True
    return False

def has_unlimited(member: discord.Member) -> bool:
    if is_admin_user(member):
        return True
    return False

def get_daily_limit(member: discord.Member) -> Optional[int]:
    if has_unlimited(member):
        return None
    # prioritize roles: Visitor/Member
    role_names = {r.name for r in member.roles}
    if "Visitor" in role_names:
        return DAILY_LIMIT_VISITOR
    if "Member" in role_names:
        return DAILY_LIMIT_MEMBER
    # default no limit
    return None

def inc_usage(guild_id: int, user_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    day = today_utc()
    cur.execute("SELECT count FROM daily_usage WHERE guild_id=? AND user_id=? AND day=?", (str(guild_id), str(user_id), day))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO daily_usage(guild_id,user_id,day,count) VALUES(?,?,?,?)", (str(guild_id), str(user_id), day, 1))
        conn.commit()
        conn.close()
        return 1
    new_count = int(row[0]) + 1
    cur.execute("UPDATE daily_usage SET count=? WHERE guild_id=? AND user_id=? AND day=?", (new_count, str(guild_id), str(user_id), day))
    conn.commit()
    conn.close()
    return new_count

def get_usage(guild_id: int, user_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    day = today_utc()
    cur.execute("SELECT count FROM daily_usage WHERE guild_id=? AND user_id=? AND day=?", (str(guild_id), str(user_id), day))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0

# ============================================================
# AUTO-MOD (непристойности / токсичность)
# ============================================================

PROFANITY_RE = re.compile(r"\b(сука|блять|бля|пидор|хуй|нахуй|ебать|ебан|пизд|хуес|говно)\b", re.IGNORECASE)
SPAM_RE = re.compile(r"(https?://|discord\.gg/|t\.me/|wa\.me/)", re.IGNORECASE)

async def openai_moderation_label(text: str) -> Dict[str, Any]:
    """
    Lightweight classification via OpenAI (optional).
    Returns: {"flagged": bool, "labels": [...], "severity": "low|medium|high", "reason": "..."}
    """
    if client_ai is None:
        return {"flagged": False, "labels": [], "severity": "low", "reason": "no_ai"}

    # Keep it simple: use JSON-only response (no tools).
    system = """
You are a moderation classifier for a Discord server.
Return ONLY JSON:
{
  "flagged": true|false,
  "labels": ["profanity","sexual","harassment","hate","self_harm","spam","scam","other"],
  "severity": "low|medium|high",
  "reason": "short reason"
}
Rules:
- Flag profanity and explicit sexual content.
- Flag harassment/hate.
- Flag scams/spam links.
- If unsure: flagged=false.
""".strip()

    resp = await asyncio.to_thread(
        lambda: client_ai.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": text[:1500]},
            ],
        )
    )

    out_text = getattr(resp, "output_text", "") or ""
    if not out_text:
        for item in getattr(resp, "output", []) or []:
            for c in getattr(item, "content", []) or []:
                if getattr(c, "type", "") in ("output_text", "text"):
                    out_text += getattr(c, "text", "") or ""
    try:
        return _safe_json_loads(out_text)
    except Exception:
        return {"flagged": False, "labels": [], "severity": "low", "reason": "parse_error"}

async def rule_based_moderation(text: str) -> Dict[str, Any]:
    labels = []
    if PROFANITY_RE.search(text):
        labels.append("profanity")
    if SPAM_RE.search(text):
        labels.append("spam")
    flagged = len(labels) > 0
    severity = "low"
    if "spam" in labels:
        severity = "medium"
    return {"flagged": flagged, "labels": labels, "severity": severity, "reason": "rule_based"}

# ============================================================
# TOOL EXECUTION (Discord actions)
# ============================================================

@dataclass
class ToolResult:
    ok: bool
    message: str

async def tool_create_category(guild: discord.Guild, name: str) -> ToolResult:
    try:
        await guild.create_category(name=name)
        return ToolResult(True, f"Created category: {name}")
    except Exception as e:
        return ToolResult(False, f"Failed create_category({name}): {e}")

async def tool_create_text_channel(guild: discord.Guild, name: str, category_name: Optional[str]) -> ToolResult:
    try:
        category = None
        if category_name:
            category = await get_category_by_name(guild, category_name)
        await guild.create_text_channel(name=name, category=category)
        return ToolResult(True, f"Created text channel: #{name}")
    except Exception as e:
        return ToolResult(False, f"Failed create_text_channel({name}): {e}")

async def tool_rename_channel(guild: discord.Guild, old_name: str, new_name: str) -> ToolResult:
    ch = await get_text_channel_by_name(guild, old_name)
    if not ch:
        return ToolResult(False, f"Channel not found: #{old_name}")
    try:
        await ch.edit(name=new_name)
        return ToolResult(True, f"Renamed channel #{old_name} -> #{new_name}")
    except Exception as e:
        return ToolResult(False, f"Failed rename_channel: {e}")

async def tool_set_channel_topic(guild: discord.Guild, channel_name: str, topic: str) -> ToolResult:
    ch = await get_text_channel_by_name(guild, channel_name)
    if not ch:
        return ToolResult(False, f"Channel not found: #{channel_name}")
    try:
        await ch.edit(topic=topic[:1024])
        return ToolResult(True, f"Set topic for #{channel_name}")
    except Exception as e:
        return ToolResult(False, f"Failed set_channel_topic: {e}")

async def tool_send_message(guild: discord.Guild, channel_name: str, content: str, pin: bool = False) -> ToolResult:
    ch = await get_text_channel_by_name(guild, channel_name)
    if not ch:
        return ToolResult(False, f"Channel not found: #{channel_name}")
    try:
        msg = await ch.send(content[:1900])
        if pin:
            try:
                await msg.pin()
            except Exception:
                pass
        return ToolResult(True, f"Sent message to #{channel_name} (pin={pin})")
    except Exception as e:
        return ToolResult(False, f"Failed send_message: {e}")

async def tool_pin_last_bot_message(guild: discord.Guild, channel_name: str, bot_user: discord.ClientUser) -> ToolResult:
    ch = await get_text_channel_by_name(guild, channel_name)
    if not ch:
        return ToolResult(False, f"Channel not found: #{channel_name}")
    try:
        async for msg in ch.history(limit=50):
            if msg.author.id == bot_user.id:
                await msg.pin()
                return ToolResult(True, f"Pinned last bot message in #{channel_name}")
        return ToolResult(False, f"No bot message found in #{channel_name}")
    except Exception as e:
        return ToolResult(False, f"Failed pin_last_bot_message: {e}")

async def tool_set_channel_permissions(
    guild: discord.Guild,
    channel_name: str,
    role_name: str,
    view: Optional[bool],
    send: Optional[bool],
    manage_messages: Optional[bool],
) -> ToolResult:
    ch = await get_text_channel_by_name(guild, channel_name)
    if not ch:
        return ToolResult(False, f"Channel not found: #{channel_name}")

    role = find_role(guild, role_name)
    if not role:
        return ToolResult(False, f"Role not found: {role_name}")

    # Don't let the bot alter protected roles unless explicitly allowed
    if role.name in PROTECTED_ROLE_NAMES:
        return ToolResult(False, f"Role is protected from edits: {role.name}")

    try:
        overwrite = ch.overwrites_for(role)
        if view is not None:
            overwrite.view_channel = view
        if send is not None:
            overwrite.send_messages = send
        if manage_messages is not None:
            overwrite.manage_messages = manage_messages
        await ch.set_permissions(role, overwrite=overwrite)
        return ToolResult(True, f"Set permissions in #{channel_name} for role {role.name}")
    except Exception as e:
        return ToolResult(False, f"Failed set_channel_permissions: {e}")

def _resolve_member(guild: discord.Guild, user_mention_or_id: str) -> Optional[discord.Member]:
    # mention: <@123> or <@!123>
    m = re.search(r"(\d{15,22})", user_mention_or_id or "")
    if not m:
        return None
    uid = int(m.group(1))
    return guild.get_member(uid)

async def tool_add_role_to_user(guild: discord.Guild, user_mention_or_id: str, role_name: str) -> ToolResult:
    member = _resolve_member(guild, user_mention_or_id)
    if not member:
        return ToolResult(False, f"User not found: {user_mention_or_id}")
    role = find_role(guild, role_name)
    if not role:
        return ToolResult(False, f"Role not found: {role_name}")
    if role.name in PROTECTED_ROLE_NAMES:
        return ToolResult(False, f"Role is protected: {role.name}")
    try:
        await member.add_roles(role, reason="Nexora AI admin action")
        return ToolResult(True, f"Added role {role.name} to {member.display_name}")
    except Exception as e:
        return ToolResult(False, f"Failed add_role_to_user: {e}")

async def tool_remove_role_from_user(guild: discord.Guild, user_mention_or_id: str, role_name: str) -> ToolResult:
    member = _resolve_member(guild, user_mention_or_id)
    if not member:
        return ToolResult(False, f"User not found: {user_mention_or_id}")
    role = find_role(guild, role_name)
    if not role:
        return ToolResult(False, f"Role not found: {role_name}")
    if role.name in PROTECTED_ROLE_NAMES:
        return ToolResult(False, f"Role is protected: {role.name}")
    try:
        await member.remove_roles(role, reason="Nexora AI admin action")
        return ToolResult(True, f"Removed role {role.name} from {member.display_name}")
    except Exception as e:
        return ToolResult(False, f"Failed remove_role_from_user: {e}")

async def tool_timeout_user(guild: discord.Guild, user_mention_or_id: str, minutes: int, reason: str) -> ToolResult:
    member = _resolve_member(guild, user_mention_or_id)
    if not member:
        return ToolResult(False, f"User not found: {user_mention_or_id}")
    try:
        until = datetime.datetime.utcnow() + datetime.timedelta(minutes=int(minutes))
        await member.timeout(until, reason=reason[:400])
        return ToolResult(True, f"Timed out {member.display_name} for {minutes} min")
    except Exception as e:
        return ToolResult(False, f"Failed timeout_user: {e}")

async def tool_delete_message(guild: discord.Guild, channel_name: str, message_id: str) -> ToolResult:
    ch = await get_text_channel_by_name(guild, channel_name)
    if not ch:
        return ToolResult(False, f"Channel not found: #{channel_name}")
    try:
        mid = int(re.sub(r"\D", "", message_id))
        msg = await ch.fetch_message(mid)
        await msg.delete()
        return ToolResult(True, f"Deleted message {mid} in #{channel_name}")
    except Exception as e:
        return ToolResult(False, f"Failed delete_message: {e}")

async def tool_delete_last_message_by_user(guild: discord.Guild, channel_name: str, user_mention_or_id: str) -> ToolResult:
    ch = await get_text_channel_by_name(guild, channel_name)
    if not ch:
        return ToolResult(False, f"Channel not found: #{channel_name}")
    member = _resolve_member(guild, user_mention_or_id)
    if not member:
        return ToolResult(False, f"User not found: {user_mention_or_id}")
    try:
        async for msg in ch.history(limit=100):
            if msg.author.id == member.id:
                await msg.delete()
                return ToolResult(True, f"Deleted last message by {member.display_name} in #{channel_name}")
        return ToolResult(False, f"No message found for {member.display_name} in last 100 messages.")
    except Exception as e:
        return ToolResult(False, f"Failed delete_last_message_by_user: {e}")

# Tool dispatch
async def execute_tool(guild: discord.Guild, bot_user: discord.ClientUser, tool: str, args: Dict[str, Any]) -> ToolResult:
    tool = (tool or "").strip()

    if tool == "noop":
        return ToolResult(True, "No-op")

    if tool == "create_category":
        return await tool_create_category(guild, args.get("name", ""))

    if tool == "create_text_channel":
        return await tool_create_text_channel(
            guild,
            args.get("name", ""),
            args.get("category_name", None),
        )

    if tool == "rename_channel":
        return await tool_rename_channel(
            guild,
            args.get("old_name", ""),
            args.get("new_name", ""),
        )

    if tool == "set_channel_topic":
        return await tool_set_channel_topic(
            guild,
            args.get("channel_name", ""),
            args.get("topic", ""),
        )

    if tool == "set_channel_permissions":
        return await tool_set_channel_permissions(
            guild,
            args.get("channel_name", ""),
            args.get("role_name", ""),
            args.get("view", None),
            args.get("send", None),
            args.get("manage_messages", None),
        )

    if tool == "send_message":
        return await tool_send_message(
            guild,
            args.get("channel_name", ""),
            args.get("content", ""),
            bool(args.get("pin", False)),
        )

    if tool == "pin_last_bot_message":
        return await tool_pin_last_bot_message(
            guild,
            args.get("channel_name", ""),
            bot_user,
        )

    if tool == "add_role_to_user":
        return await tool_add_role_to_user(
            guild,
            args.get("user_mention_or_id", ""),
            args.get("role_name", ""),
        )

    if tool == "remove_role_from_user":
        return await tool_remove_role_from_user(
            guild,
            args.get("user_mention_or_id", ""),
            args.get("role_name", ""),
        )

    if tool == "timeout_user":
        return await tool_timeout_user(
            guild,
            args.get("user_mention_or_id", ""),
            int(args.get("minutes", 10)),
            args.get("reason", "Moderation action"),
        )

    if tool == "delete_message":
        return await tool_delete_message(
            guild,
            args.get("channel_name", ""),
            str(args.get("message_id", "")),
        )

    if tool == "delete_last_message_by_user":
        return await tool_delete_last_message_by_user(
            guild,
            args.get("channel_name", ""),
            args.get("user_mention_or_id", ""),
        )

    return ToolResult(False, f"Unknown tool: {tool}")

# ============================================================
# UI: Confirm / Cancel buttons
# ============================================================

class PlanView(discord.ui.View):
    def __init__(self, bot: "NexoraBot", plan_id: str, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.plan_id = plan_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await self.bot.execute_plan(self.plan_id, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = self.bot.cancel_plan(self.plan_id, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

# ============================================================
# BOT
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

class NexoraBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        try:
            await self.tree.sync()
        except Exception:
            pass

    async def on_ready(self):
        print(f"Logged in as {self.user} (id={self.user.id})")
        await self.change_presence(activity=discord.Game(name="Nexora Admin AI"))
        # Ensure DB
        init_db()

    # -------------------------
    # PLAN STORAGE
    # -------------------------

    def save_plan(self, plan_id: str, guild_id: int, channel_id: int, author_id: int, plan: Dict[str, Any]):
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO admin_plans(id,guild_id,channel_id,author_id,created_at,plan_json,status) VALUES(?,?,?,?,?,?,?)",
            (plan_id, str(guild_id), str(channel_id), str(author_id), now_iso(), json.dumps(plan), "pending")
        )
        conn.commit()
        conn.close()

    def load_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT plan_json,status,guild_id,channel_id,author_id FROM admin_plans WHERE id=?", (plan_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        plan_json, status, guild_id, channel_id, author_id = row
        return {
            "plan": json.loads(plan_json),
            "status": status,
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "author_id": int(author_id),
        }

    def update_plan_status(self, plan_id: str, status: str):
        conn = db()
        conn.execute("UPDATE admin_plans SET status=? WHERE id=?", (status, plan_id))
        conn.commit()
        conn.close()

    def cancel_plan(self, plan_id: str, user: discord.abc.User) -> Tuple[bool, str]:
        data = self.load_plan(plan_id)
        if not data:
            return False, "Plan not found."
        if data["status"] != "pending":
            return False, f"Plan already {data['status']}."
        # Only author or admin can cancel
        if int(user.id) != int(data["author_id"]):
            return False, "Only the plan author can cancel."
        self.update_plan_status(plan_id, "cancelled")
        return True, "Cancelled."

    async def execute_plan(self, plan_id: str, user: discord.abc.User) -> Tuple[bool, str]:
        data = self.load_plan(plan_id)
        if not data:
            return False, "Plan not found."
        if data["status"] != "pending":
            return False, f"Plan already {data['status']}."

        # Only author can confirm (you can relax this if you want)
        if int(user.id) != int(data["author_id"]):
            return False, "Only the plan author can confirm."

        guild = self.get_guild(data["guild_id"])
        if not guild:
            return False, "Guild not found."

        self.update_plan_status(plan_id, "executing")

        plan = data["plan"]
        actions = plan.get("actions", [])

        results = []
        for a in actions:
            tool = a.get("tool", "")
            args = a.get("args", {}) or {}
            res = await execute_tool(guild, self.user, tool, args)
            results.append(res)
            await audit_log(guild, f"✅ EXEC {tool} args={args} -> ok={res.ok} msg={res.message}")

        self.update_plan_status(plan_id, "done")

        ok_count = sum(1 for r in results if r.ok)
        fail_count = sum(1 for r in results if not r.ok)
        summary = plan.get("summary", "Done.")
        return True, f"Done. {summary}\nOK: {ok_count}, Failed: {fail_count}"

    # -------------------------
    # MESSAGE HANDLING
    # -------------------------

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.guild:
            return

        # Daily quota tracking (optional) for ordinary chat usage
        member = message.guild.get_member(message.author.id)
        if member:
            limit = get_daily_limit(member)
            if limit is not None:
                used = get_usage(message.guild.id, message.author.id)
                if used >= limit:
                    # do not block admin channel interactions
                    if message.channel.name not in (ADMIN_CHANNEL_NAME, AUDIT_CHANNEL_NAME):
                        try:
                            await message.reply(f"Daily limit reached ({used}/{limit}). Try tomorrow.", mention_author=False)
                        except Exception:
                            pass
                        return
                inc_usage(message.guild.id, message.author.id)

        # Auto moderation
        if AUTO_MOD_ENABLED and message.channel.name not in AUTOMOD_EXCLUDE_CHANNELS:
            await self._automod_check(message)

        # Admin channel handling
        if message.channel.name == ADMIN_CHANNEL_NAME:
            # require mention of the bot
            if self.user and self.user.mentioned_in(message):
                await self._handle_admin_request(message)
                return

        await self.process_commands(message)

    async def _handle_admin_request(self, message: discord.Message):
        # Only allow server admins / owner to use admin channel commands
        member = message.guild.get_member(message.author.id)
        if not member or not is_admin_user(member):
            await message.reply("❌ You are not allowed to use admin actions.", mention_author=False)
            return

        content = message.content
        # strip mentions
        content = re.sub(r"<@!?\d+>", "", content).strip()
        if not content:
            await message.reply("Write a clear admin request after mentioning me.", mention_author=False)
            return

        context = {
            "server": {"id": message.guild.id, "name": message.guild.name},
            "channel": {"id": message.channel.id, "name": message.channel.name},
            "author": {"id": message.author.id, "name": str(message.author)},
            "known_roles": [r.name for r in message.guild.roles][-30:],
            "notes": [
                "Do not assume you can edit @everyone unless explicitly requested.",
                "Prefer minimal safe actions."
            ]
        }

        try:
            plan = await build_admin_plan(content, context)
        except Exception as e:
            await message.reply(f"OpenAI error while building plan: {e}", mention_author=False)
            return

        # Create plan_id
        plan_id = os.urandom(4).hex()
        self.save_plan(plan_id, message.guild.id, message.channel.id, message.author.id, plan)

        # Render
        risk = plan.get("risk", "low")
        summary = plan.get("summary", "")
        actions = plan.get("actions", [])
        actions_text = "\n".join([f"• {a.get('tool')} {a.get('args', {})}" for a in actions]) or "• (no actions)"

        embed = discord.Embed(
            title=f"🧩 PLAN {plan_id}",
            description=f"**Summary:** {summary}\n**Risk:** {risk}\n\n**Actions:**\n{actions_text}",
            color=discord.Color.orange() if risk != "low" else discord.Color.green()
        )
        view = PlanView(self, plan_id)
        await message.reply(embed=embed, view=view, mention_author=False)

        await audit_log(message.guild, f"📝 PLAN {plan_id} by {message.author} | risk={risk} | {summary}")

    async def _automod_check(self, message: discord.Message):
        text = message.content or ""

        # First: cheap rules
        rule_res = await rule_based_moderation(text)
        flagged = rule_res["flagged"]
        labels = set(rule_res.get("labels", []))
        severity = rule_res.get("severity", "low")
        reason = rule_res.get("reason", "rule_based")

        # Optional: OpenAI classifier
        if AUTO_MOD_USE_OPENAI and len(text) >= 3:
            ai_res = await openai_moderation_label(text)
            if ai_res.get("flagged"):
                flagged = True
                labels |= set(ai_res.get("labels", []))
                severity = ai_res.get("severity", severity)
                reason = ai_res.get("reason", reason)

        if not flagged:
            return

        member = message.guild.get_member(message.author.id)
        if not member:
            return

        # Admin bypass
        if is_admin_user(member):
            return

        # Decide action
        action = AUTO_MOD_ACTION
        # escalate if severity high
        if severity == "high":
            action = "timeout"

        try:
            if action == "warn":
                await message.reply(
                    f"⚠️ Please keep it clean. Detected: {', '.join(sorted(labels))}.",
                    mention_author=False
                )
            elif action == "delete":
                await message.delete()
                await message.channel.send(
                    f"🧹 Message removed ({', '.join(sorted(labels))}).",
                    delete_after=8
                )
            elif action == "timeout":
                await message.delete()
                until = datetime.datetime.utcnow() + datetime.timedelta(minutes=AUTO_MOD_TIMEOUT_MIN)
                await member.timeout(until, reason=f"AutoMod: {', '.join(sorted(labels))}")
                await message.channel.send(
                    f"⏳ User timed out for {AUTO_MOD_TIMEOUT_MIN} min ({', '.join(sorted(labels))}).",
                    delete_after=10
                )
        except Exception:
            pass

        await audit_log(
            message.guild,
            f"🛡️ AutoMod: user={message.author} channel=#{message.channel.name} labels={sorted(labels)} severity={severity} reason={reason}"
        )

# ============================================================
# COMMANDS
# ============================================================

bot = NexoraBot()

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.reply("pong ✅", mention_author=False)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")
    init_db()
    bot.run(DISCORD_TOKEN)

import os
import re
import json
import uuid
import sqlite3
import datetime
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands
from discord.ext import commands
from openai import OpenAI


# =============================
# ENV
# =============================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DB_PATH = os.getenv("NEXORA_DB_PATH", "nexora.db")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in env")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing in env")

ai = OpenAI(api_key=OPENAI_API_KEY)


# =============================
# DEFAULT SETTINGS
# =============================
DEFAULTS = {
    "ai_help_channel": "ai-help",
    "ai_admin_channel": "ai-admin",
    "ai_audit_channel": "ai-audit-log",

    "free_daily_limit": "10",
    "show_remaining": "1",

    "require_mention_outside_help": "1",  # outside ai-help: only mention or /ai
    "admin_role_name": "Administrator",

    # admin modes: preview | confirm | execute | lock
    # preview: always show plan only
    # confirm: create plan + buttons confirm/cancel
    # execute: execute immediately (dangerous, use only if you really want)
    # lock: block execution, preview only
    "admin_mode": "confirm",
}


# =============================
# DB helpers
# =============================
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
      guild_id TEXT NOT NULL,
      key TEXT NOT NULL,
      value TEXT NOT NULL,
      PRIMARY KEY (guild_id, key)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_daily (
      guild_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      day TEXT NOT NULL,
      count INTEGER NOT NULL,
      PRIMARY KEY (guild_id, user_id, day)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_admin (
      guild_id TEXT NOT NULL,
      request_id TEXT NOT NULL,
      requester_id TEXT NOT NULL,
      channel_id TEXT NOT NULL,
      payload TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (guild_id, request_id)
    )""")
    con.commit()
    con.close()


def get_setting(guild_id: int, key: str) -> str:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (str(guild_id), key))
    row = cur.fetchone()
    if row:
        con.close()
        return row["value"]

    value = DEFAULTS.get(key, "")
    cur.execute("INSERT OR REPLACE INTO settings (guild_id, key, value) VALUES (?,?,?)",
                (str(guild_id), key, value))
    con.commit()
    con.close()
    return value


def set_setting(guild_id: int, key: str, value: str):
    con = db()
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (guild_id, key, value) VALUES (?,?,?)",
                (str(guild_id), key, value))
    con.commit()
    con.close()


def day_key_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def usage_get(guild_id: int, user_id: int) -> int:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT count FROM usage_daily WHERE guild_id=? AND user_id=? AND day=?",
                (str(guild_id), str(user_id), day_key_utc()))
    row = cur.fetchone()
    con.close()
    return int(row["count"]) if row else 0


def usage_inc(guild_id: int, user_id: int, delta: int = 1) -> int:
    con = db()
    cur = con.cursor()
    day = day_key_utc()
    cur.execute("SELECT count FROM usage_daily WHERE guild_id=? AND user_id=? AND day=?",
                (str(guild_id), str(user_id), day))
    row = cur.fetchone()
    if row:
        new_count = int(row["count"]) + delta
        cur.execute("UPDATE usage_daily SET count=? WHERE guild_id=? AND user_id=? AND day=?",
                    (new_count, str(guild_id), str(user_id), day))
    else:
        new_count = delta
        cur.execute("INSERT INTO usage_daily (guild_id, user_id, day, count) VALUES (?,?,?,?)",
                    (str(guild_id), str(user_id), day, new_count))
    con.commit()
    con.close()
    return new_count


def pending_put(guild_id: int, request_id: str, requester_id: int, channel_id: int, payload: Dict[str, Any]):
    con = db()
    cur = con.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO pending_admin (guild_id, request_id, requester_id, channel_id, payload, created_at)
    VALUES (?,?,?,?,?,?)
    """, (str(guild_id), request_id, str(requester_id), str(channel_id),
          json.dumps(payload), datetime.datetime.utcnow().isoformat()))
    con.commit()
    con.close()


def pending_get(guild_id: int, request_id: str) -> Optional[sqlite3.Row]:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM pending_admin WHERE guild_id=? AND request_id=?", (str(guild_id), request_id))
    row = cur.fetchone()
    con.close()
    return row


def pending_del(guild_id: int, request_id: str):
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM pending_admin WHERE guild_id=? AND request_id=?", (str(guild_id), request_id))
    con.commit()
    con.close()


# =============================
# Discord bot
# =============================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =============================
# Helpers
# =============================
def find_text_channel(guild: discord.Guild, name: str) -> Optional[discord.TextChannel]:
    for c in guild.text_channels:
        if c.name == name:
            return c
    return None


async def audit(guild: discord.Guild, text: str):
    ch = find_text_channel(guild, get_setting(guild.id, "ai_audit_channel"))
    if ch:
        await ch.send(text[:1900])


def is_admin(member: discord.Member, admin_role_name: str) -> bool:
    return member.guild_permissions.administrator or any(r.name == admin_role_name for r in member.roles)


def is_free_limited_role(member: discord.Member) -> bool:
    # Visitor/Member = ограниченные
    role_names = {r.name.lower() for r in member.roles}
    return ("visitor" in role_names) or ("member" in role_names)


def strip_bot_mention(text: str) -> str:
    # remove <@id> mention
    if bot.user:
        text = text.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    return text.strip()


def looks_like_english(s: str) -> bool:
    # quick heuristic (если латиницы больше чем кириллицы)
    latin = len(re.findall(r"[A-Za-z]", s))
    cyr = len(re.findall(r"[А-Яа-яІіЇїЄєҐґ]", s))
    return latin > cyr


# =============================
# AI prompts
# =============================
FREE_SYSTEM = (
    "You are Nexora AI — assistant for Discord server 'Nexora'.\n"
    "Rules:\n"
    "- Always respond in the user's language (detect from message).\n"
    "- Free users: ONLY help about Nexora server (channels, rules, tickets, roles, marketplace, safety).\n"
    "- If user asks unrelated topics: politely refuse and say full AI is available via subscription.\n"
    "- For newcomers: give short guide to server sections when appropriate.\n"
    "- Be concise, actionable.\n"
)

PRO_SYSTEM = (
    "You are Nexora AI — helpful assistant.\n"
    "Rules:\n"
    "- Always respond in the user's language.\n"
    "- Be максимально полезным.\n"
)

ADMIN_SYSTEM = (
    "You are Nexora AI Admin. Output ONLY valid JSON (no markdown).\n"
    "Convert the admin request into a safe plan.\n"
    "Schema:\n"
    "{"
    "\"summary\":\"...\","
    "\"risk\":\"low|medium|high\","
    "\"actions\":["
    "{\"type\":\"create_text_channel\",\"params\":{\"name\":\"x\"}},"
    "{\"type\":\"create_voice_channel\",\"params\":{\"name\":\"x\"}},"
    "{\"type\":\"create_role\",\"params\":{\"name\":\"Role\"}},"
    "{\"type\":\"add_role_to_user\",\"params\":{\"user\":\"@mention or id\",\"role\":\"Role\"}},"
    "{\"type\":\"remove_role_from_user\",\"params\":{\"user\":\"@mention or id\",\"role\":\"Role\"}}"
    "],"
    "\"notes\":\"...\""
    "}\n"
    "Rules:\n"
    "- Never grant Administrator permission.\n"
    "- Keep actions minimal.\n"
)

SUPPORTED_ACTIONS = {
    "create_text_channel",
    "create_voice_channel",
    "create_role",
    "add_role_to_user",
    "remove_role_from_user",
}


# =============================
# Executor
# =============================
async def execute_actions(guild: discord.Guild, actions: List[Dict[str, Any]]) -> List[str]:
    results = []

    for a in actions:
        t = (a.get("type") or "").strip()
        p = a.get("params") or {}

        if t not in SUPPORTED_ACTIONS:
            results.append(f"❌ Unsupported action: {t}")
            continue

        try:
            if t == "create_text_channel":
                name = p.get("name")
                if not name:
                    results.append("❌ create_text_channel missing name")
                    continue
                if find_text_channel(guild, name):
                    results.append(f"ℹ️ Text channel already exists: #{name}")
                    continue
                await guild.create_text_channel(name=name)
                results.append(f"✅ Created text channel: #{name}")

            elif t == "create_voice_channel":
                name = p.get("name")
                if not name:
                    results.append("❌ create_voice_channel missing name")
                    continue
                # check existing
                if any(vc.name == name for vc in guild.voice_channels):
                    results.append(f"ℹ️ Voice channel already exists: {name}")
                    continue
                await guild.create_voice_channel(name=name)
                results.append(f"✅ Created voice channel: {name}")

            elif t == "create_role":
                name = p.get("name")
                if not name:
                    results.append("❌ create_role missing name")
                    continue
                if discord.utils.get(guild.roles, name=name):
                    results.append(f"ℹ️ Role already exists: {name}")
                    continue
                await guild.create_role(name=name)
                results.append(f"✅ Created role: {name}")

            elif t in ("add_role_to_user", "remove_role_from_user"):
                role_name = p.get("role")
                user_ref = p.get("user")
                if not role_name or not user_ref:
                    results.append(f"❌ {t} missing user/role")
                    continue

                role = discord.utils.get(guild.roles, name=role_name)
                if not role:
                    results.append(f"❌ Role not found: {role_name}")
                    continue

                # resolve user
                user_id = None
                m = re.findall(r"\d{15,20}", str(user_ref))
                if m:
                    user_id = int(m[0])

                member = guild.get_member(user_id) if user_id else None
                if not member:
                    results.append(f"❌ User not found: {user_ref}")
                    continue

                if t == "add_role_to_user":
                    await member.add_roles(role, reason="Nexora AI Admin")
                    results.append(f"✅ Added role {role_name} to {member.display_name}")
                else:
                    await member.remove_roles(role, reason="Nexora AI Admin")
                    results.append(f"✅ Removed role {role_name} from {member.display_name}")

        except Exception as e:
            results.append(f"❌ Error {t}: {e}")

    return results


# =============================
# Views (Confirm / Cancel)
# =============================
class AdminConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, request_id: str, requester_id: int):
        super().__init__(timeout=600)  # 10 min
        self.guild_id = guild_id
        self.request_id = request_id
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ Только автор запроса может подтверждать.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = pending_get(self.guild_id, self.request_id)
        if not row:
            await interaction.response.send_message("❌ Запрос не найден или уже обработан.", ephemeral=True)
            return

        payload = json.loads(row["payload"])
        actions = payload.get("actions", [])

        guild = interaction.guild
        await interaction.response.send_message("⏳ Выполняю...", ephemeral=True)

        results = await execute_actions(guild, actions)
        pending_del(self.guild_id, self.request_id)

        await audit(guild, f"✅ EXECUTED by <@{interaction.user.id}> | {payload.get('summary','(no summary)')}\n"
                           f"Results:\n- " + "\n- ".join(results))

        await interaction.followup.send("✅ Готово:\n- " + "\n- ".join(results), ephemeral=True)

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_del(self.guild_id, self.request_id)
        await audit(interaction.guild, f"🛑 CANCELED by <@{interaction.user.id}> | request {self.request_id}")
        await interaction.response.send_message("🛑 Отменено.", ephemeral=True)


# =============================
# AI calls
# =============================
def chat(model: str, system: str, user: str) -> str:
    resp = ai.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def chat_json(model: str, system: str, user: str) -> Dict[str, Any]:
    text = chat(model, system, user)
    try:
        return json.loads(text)
    except:
        # fallback if model answered with extra text
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# =============================
# Slash commands
# =============================
@bot.tree.command(name="ai", description="Ask Nexora AI")
@app_commands.describe(question="Your question to Nexora AI")
async def ai_cmd(interaction: discord.Interaction, question: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return

    guild = interaction.guild
    member = interaction.user

    admin_role_name = get_setting(guild.id, "admin_role_name")
    admin_unlimited = is_admin(member, admin_role_name)

    # лимит только для Visitor/Member (и не админ)
    limit = int(get_setting(guild.id, "free_daily_limit") or "10")
    show_remaining = get_setting(guild.id, "show_remaining") == "1"

    if (not admin_unlimited) and is_free_limited_role(member):
        used = usage_get(guild.id, member.id)
        if used >= limit:
            await interaction.response.send_message(
                f"🚫 Лимит AI исчерпан на сегодня ({limit}/{limit}).\n"
                f"⭐ Оформите подписку Nexora Pro/Elite/Ultra (если включите) — и лимит будет выше.",
                ephemeral=True
            )
            return
        usage_inc(guild.id, member.id, 1)
        remaining = limit - (used + 1)
        system = FREE_SYSTEM
        model = "gpt-4o-mini"
    else:
        # админ/прочие без лимита (как ты просил)
        remaining = None
        system = PRO_SYSTEM if not is_free_limited_role(member) else FREE_SYSTEM
        model = "gpt-4o-mini"

    try:
        reply = chat(model, system, question)

        if show_remaining and remaining is not None:
            reply += f"\n\n🧠 Осталось бесплатных сообщений: {remaining}/{limit}"

        await interaction.response.send_message(reply[:1900], ephemeral=False)

    except Exception as e:
        await interaction.response.send_message(f"⚠️ AI временно недоступен. ({e})", ephemeral=True)


@bot.tree.command(name="ai-admin", description="Admin: ask Nexora AI to manage server (confirm/cancel)")
@app_commands.describe(task="What to do (create channel, add role, etc.)")
async def ai_admin_cmd(interaction: discord.Interaction, task: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return

    guild = interaction.guild
    member = interaction.user

    # доступ только в #ai-admin (или через слэш где угодно, но я рекомендую только там)
    admin_channel_name = get_setting(guild.id, "ai_admin_channel")
    if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
        if interaction.channel.name != admin_channel_name:
            await interaction.response.send_message(
                f"❌ Используй эту команду только в #{admin_channel_name}.", ephemeral=True
            )
            return

    admin_role_name = get_setting(guild.id, "admin_role_name")
    if not is_admin(member, admin_role_name):
        await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
        return

    mode = get_setting(guild.id, "admin_mode").lower().strip()
    if mode not in ("preview", "confirm", "execute", "lock"):
        mode = "confirm"

    if mode == "lock":
        await interaction.response.send_message("🔒 LOCK MODE включен — выполнение запрещено. Используй /ai-admin-mode.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    try:
        plan = chat_json("gpt-4o-mini", ADMIN_SYSTEM, task)

        # sanitize
        actions = plan.get("actions", [])
        safe_actions = []
        for a in actions:
            if isinstance(a, dict) and (a.get("type") in SUPPORTED_ACTIONS):
                safe_actions.append(a)
        plan["actions"] = safe_actions

        summary = plan.get("summary", "(no summary)")
        risk = plan.get("risk", "medium")
        notes = plan.get("notes", "")

        pretty = (
            f"🧩 **PLAN**\n"
            f"**Summary:** {summary}\n"
            f"**Risk:** {risk}\n"
            f"**Actions:**\n" +
            "\n".join([f"- `{a['type']}` {a.get('params',{})}" for a in safe_actions]) +
            (f"\n\n**Notes:** {notes}" if notes else "")
        )

        await audit(guild, f"📝 ADMIN PLAN by <@{member.id}> | {summary} | risk={risk}")

        if mode == "preview" or mode == "lock":
            await interaction.followup.send(pretty[:1900], ephemeral=True)
            return

        if mode == "execute":
            results = await execute_actions(guild, safe_actions)
            await audit(guild, f"✅ EXECUTED (no-confirm) by <@{member.id}> | {summary}\n- " + "\n- ".join(results))
            await interaction.followup.send(pretty[:1200] + "\n\n✅ Done:\n- " + "\n- ".join(results), ephemeral=True)
            return

        # confirm mode
        request_id = str(uuid.uuid4())[:8]
        pending_put(guild.id, request_id, member.id, interaction.channel_id, plan)

        view = AdminConfirmView(guild.id, request_id, member.id)
        await interaction.followup.send(pretty[:1900] + f"\n\nRequest ID: `{request_id}`", view=view, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Ошибка AI-admin: {e}", ephemeral=True)


@bot.tree.command(name="ai-admin-mode", description="Admin: set ai-admin mode (preview/confirm/execute/lock)")
@app_commands.describe(mode="preview | confirm | execute | lock")
async def ai_admin_mode(interaction: discord.Interaction, mode: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return
    guild = interaction.guild
    member = interaction.user

    admin_role_name = get_setting(guild.id, "admin_role_name")
    if not is_admin(member, admin_role_name):
        await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
        return

    mode = mode.lower().strip()
    if mode not in ("preview", "confirm", "execute", "lock"):
        await interaction.response.send_message("❌ Неверный режим. Используй: preview/confirm/execute/lock", ephemeral=True)
        return

    set_setting(guild.id, "admin_mode", mode)
    await audit(guild, f"⚙️ admin_mode set to {mode} by <@{member.id}>")
    await interaction.response.send_message(f"✅ admin_mode = **{mode}**", ephemeral=True)


@bot.tree.command(name="ai-config", description="Admin: set config keys (channels, limits, etc.)")
@app_commands.describe(key="setting key", value="setting value")
async def ai_config(interaction: discord.Interaction, key: str, value: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return
    guild = interaction.guild
    member = interaction.user

    admin_role_name = get_setting(guild.id, "admin_role_name")
    if not is_admin(member, admin_role_name):
        await interaction.response.send_message("❌ Нет доступа.", ephemeral=True)
        return

    allowed = set(DEFAULTS.keys())
    if key not in allowed:
        await interaction.response.send_message(
            "❌ Такой настройки нет.\n"
            f"✅ Доступные ключи:\n- " + "\n- ".join(sorted(allowed)),
            ephemeral=True
        )
        return

    set_setting(guild.id, key, value)
    await audit(guild, f"⚙️ setting {key}={value} by <@{member.id}>")
    await interaction.response.send_message(f"✅ {key} = {value}", ephemeral=True)


# =============================
# Message listener
# =============================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # let slash commands work
    await bot.process_commands(message)

    guild = message.guild
    help_channel_name = get_setting(guild.id, "ai_help_channel")
    require_mention_outside = get_setting(guild.id, "require_mention_outside_help") == "1"

    # when to reply automatically?
    in_help = isinstance(message.channel, discord.TextChannel) and message.channel.name == help_channel_name
    mentioned = bot.user and (bot.user in message.mentions)

    if in_help:
        # free chat allowed here
        question = message.content.strip()
        if not question:
            return
    else:
        # outside help: only if mention AND require flag enabled
        if require_mention_outside:
            if not mentioned:
                return
        question = strip_bot_mention(message.content)
        if not question:
            return

    member = message.author
    if not isinstance(member, discord.Member):
        return

    admin_role_name = get_setting(guild.id, "admin_role_name")
    admin_unlimited = is_admin(member, admin_role_name)

    # limit only Visitor/Member & not admin
    limit = int(get_setting(guild.id, "free_daily_limit") or "10")
    show_remaining = get_setting(guild.id, "show_remaining") == "1"

    remaining = None
    if (not admin_unlimited) and is_free_limited_role(member):
        used = usage_get(guild.id, member.id)
        if used >= limit:
            await message.channel.send(
                f"🚫 Лимит AI исчерпан на сегодня ({limit}/{limit}).\n"
                f"⭐ Оформите подписку Nexora Pro/Elite/Ultra (если включите) — и лимит будет выше."
            )
            return
        usage_inc(guild.id, member.id, 1)
        remaining = limit - (used + 1)
        system = FREE_SYSTEM
        model = "gpt-4o-mini"
    else:
        system = PRO_SYSTEM if not is_free_limited_role(member) else FREE_SYSTEM
        model = "gpt-4o-mini"

    # make language more likely correct
    # (hint to system: user asked in English)
    if looks_like_english(question):
        system = system + "\nUser language hint: English."

    try:
        reply = chat(model, system, question)
        if show_remaining and remaining is not None:
            reply += f"\n\n🧠 Осталось бесплатных сообщений: {remaining}/{limit}"
        await message.channel.send(reply[:1900])
    except Exception as e:
        await message.channel.send("⚠️ AI временно недоступен.")


# =============================
# Ready
# =============================
@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands")
    except Exception as e:
        print("⚠️ Sync error:", e)
    print(f"✅ Nexora AI online as {bot.user}")


# =============================
# Run
# =============================
bot.run(DISCORD_TOKEN)

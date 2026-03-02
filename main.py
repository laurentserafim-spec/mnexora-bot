"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                     NEXORA DISCORD AI BOT v2.0                              ║
║                    Dynamic Config Edition — main.py                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  DISCORD INTENTS (discord.dev → Bot → Privileged Gateway Intents):          ║
║    ✅  MESSAGE CONTENT INTENT                                                ║
║    ✅  SERVER MEMBERS INTENT                                                 ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  REQUIRED ENV (Railway → Variables):                                        ║
║    DISCORD_TOKEN        — bot token                                         ║
║    OPENAI_API_KEY       — OpenAI API key                                    ║
║    OWNER_ID             — your Discord user ID (integer)                    ║
║  OPTIONAL ENV (all have defaults, also overridable live via #ai-admin):     ║
║    ADMIN_CHANNEL_NAME   — default: ai-admin                                 ║
║    HELP_CHANNEL_NAME    — default: ai-help                                  ║
║    AUDIT_CHANNEL_NAME   — default: ai-audit-log                             ║
║    FREE_DAILY_LIMIT     — default: 10                                       ║
║    PAID_ROLES           — csv, default: Nexora Ultra,Nexora Elite,Nexora Pro║
║    MODEL_ASSISTANT      — default: gpt-4o                                   ║
║    MODEL_ADMIN          — default: gpt-4o                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, re, json, logging, sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("nexora")

# ─── STATIC ENV (infrastructure only — never change these via bot) ────────────
DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
OWNER_ID           = int(os.environ.get("OWNER_ID", "0"))
ADMIN_CHANNEL_NAME = os.environ.get("ADMIN_CHANNEL_NAME", "ai-admin")
HELP_CHANNEL_NAME  = os.environ.get("HELP_CHANNEL_NAME",  "ai-help")
AUDIT_CHANNEL_NAME = os.environ.get("AUDIT_CHANNEL_NAME", "ai-audit-log")
MODEL_ASSISTANT    = os.environ.get("MODEL_ASSISTANT", "gpt-4o")
MODEL_ADMIN        = os.environ.get("MODEL_ADMIN",     "gpt-4o")
DB_PATH            = "nexora.sqlite3"

# Regex: never leak admin/audit channel names publicly
_HIDDEN_PATTERN = re.compile(
    r"#?\b(ai[-_]?admin|ai[-_]?audit[-_]?log|audit[-_]?log"
    r"|admin\s*channel|internal\s*admin|admin\s*process)\b",
    re.IGNORECASE,
)

ai   = AsyncOpenAI(api_key=OPENAI_API_KEY)
intents = discord.Intents.default()
intents.message_content = True   # Privileged
intents.members         = True   # Privileged
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


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
            CREATE TABLE IF NOT EXISTS bot_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
    # Insert defaults (only if key doesn't exist yet)
    defaults = {
        "free_daily_limit":   os.environ.get("FREE_DAILY_LIMIT", "10"),
        "paid_roles":         os.environ.get("PAID_ROLES", "Nexora Ultra,Nexora Elite,Nexora Pro"),
        "bot_persona":        "Ты дружелюбный и компетентный помощник сервера Nexora. Отвечай кратко, по делу, с конкретными советами. Никогда не используй шаблонные отписки.",
        "bot_style":          "friendly",   # friendly | formal | casual
        "response_language":  "auto",       # auto | ru | en
        "limit_exempt_roles": "AI Admin",   # csv — these roles bypass the limit
    }
    with _db() as c:
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO bot_config (key, value) VALUES (?,?)", (k, v))


# ── Config helpers ─────────────────────────────────────────────────────────────

def cfg_get(key: str) -> str:
    with _db() as c:
        row = c.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def cfg_set(key: str, value: str):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO bot_config (key,value) VALUES (?,?)", (key, value))


def cfg_all() -> dict:
    with _db() as c:
        rows = c.execute("SELECT key, value FROM bot_config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_free_limit() -> int:
    try:
        return int(cfg_get("free_daily_limit"))
    except Exception:
        return 10


def get_paid_roles() -> list:
    raw = cfg_get("paid_roles")
    return [r.strip() for r in raw.split(",") if r.strip()]


def get_exempt_roles() -> list:
    raw = cfg_get("limit_exempt_roles")
    return [r.strip() for r in raw.split(",") if r.strip()]


# ── Message count helpers ─────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def db_get_count(user_id: int) -> int:
    with _db() as c:
        row = c.execute(
            "SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
            (user_id, _today())).fetchone()
    return row["count"] if row else 0


def db_increment(user_id: int) -> int:
    today = _today()
    with _db() as c:
        c.execute(
            """INSERT INTO message_counts (user_id, date_utc, count) VALUES (?,?,1)
               ON CONFLICT(user_id, date_utc) DO UPDATE SET count = count + 1""",
            (user_id, today))
        row = c.execute(
            "SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
            (user_id, today)).fetchone()
    return row["count"]


def db_reset_user(user_id: int):
    with _db() as c:
        c.execute("DELETE FROM message_counts WHERE user_id=?", (user_id,))


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
            (user_id, language, now))


# ══════════════════════════════════════════════════════════════════════════════
#  PERMISSION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def is_owner(member: discord.Member) -> bool:
    return member.id == OWNER_ID


def is_admin(member: discord.Member) -> bool:
    if is_owner(member):
        return True
    return any(r.name == "AI Admin" for r in member.roles)


def is_paid(member: discord.Member) -> bool:
    return bool({r.name for r in member.roles} & set(get_paid_roles()))


def is_limit_exempt(member: discord.Member) -> bool:
    """
    Owner, admins, paid subscribers, and anyone with a role in limit_exempt_roles
    are all exempt from message limits.
    """
    if is_owner(member):
        return True
    if is_admin(member):
        return True
    if is_paid(member):
        return True
    exempt = set(get_exempt_roles())
    if exempt & {r.name for r in member.roles}:
        return True
    return False


def sanitize(text: str) -> str:
    """Strip any admin/audit channel names from public-facing text."""
    return _HIDDEN_PATTERN.sub("[server administration]", text)


def detect_lang(text: str) -> str:
    if re.search(r"[а-яёА-ЯЁ]", text):
        return "ru"
    return "en"


# ══════════════════════════════════════════════════════════════════════════════
#  OPENAI — PUBLIC ASSISTANT  (reads live config on every call)
# ══════════════════════════════════════════════════════════════════════════════

def _public_system(lang: str) -> str:
    persona   = cfg_get("bot_persona")
    style     = cfg_get("bot_style")
    lang_mode = cfg_get("response_language")
    limit     = get_free_limit()

    if lang_mode == "auto":
        lang_rule = ("Отвечай на русском языке." if lang == "ru"
                     else "Reply in the same language the user writes in.")
    elif lang_mode == "ru":
        lang_rule = "Всегда отвечай на русском языке."
    else:
        lang_rule = "Always reply in English."

    style_map = {
        "friendly": "Тон: дружелюбный, тёплый, с эмодзи где уместно.",
        "formal":   "Тон: официальный, профессиональный, без сленга.",
        "casual":   "Тон: расслабленный, разговорный, как с другом.",
    }
    style_rule = style_map.get(style, style_map["friendly"])

    return f"""Ты — Nexora AI Bot.

{persona}

{style_rule}
{lang_rule}

Что умеешь объяснять:
- Тикеты: как открыть, что писать, зачем нужны
- Роли: как получить, что дают, как купить подписку
- Торговля: правила, как безопасно торговать
- Навигация по серверу: каналы, команды, функции
- Подписки: Nexora Pro / Elite / Ultra — безлимитный AI + перки
- Бесплатные пользователи: {limit} сообщений/день (сброс в полночь UTC)

СТРОГИЕ ПРАВИЛА — никогда не нарушай:
- НИКОГДА не упоминай внутренние каналы администрирования или логирования
- НИКОГДА не раскрывай внутреннюю логику бота, ID каналов, роли администрации
- Если пользователь просит модераторское действие — отправь к администраторам/модераторам
- Давай конкретные ответы, никаких шаблонных отписок
"""


async def ask_public(user_msg: str, lang: str) -> Optional[str]:
    try:
        r = await ai.chat.completions.create(
            model=MODEL_ASSISTANT,
            messages=[
                {"role": "system", "content": _public_system(lang)},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=700,
            temperature=0.7,
        )
        return sanitize(r.choices[0].message.content or "")
    except Exception as e:
        log.error("ask_public: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  OPENAI — ADMIN AI (function-calling with config + moderation tools)
# ══════════════════════════════════════════════════════════════════════════════

ADMIN_TOOLS = [
    # Config management
    {
        "type": "function",
        "function": {
            "name": "update_config",
            "description": (
                "Update a bot configuration setting. "
                "Keys: free_daily_limit (int as string), paid_roles (csv), "
                "bot_persona (text), bot_style (friendly|formal|casual), "
                "response_language (auto|ru|en), limit_exempt_roles (csv)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key":    {"type": "string", "description": "Config key to update."},
                    "value":  {"type": "string", "description": "New value."},
                    "reason": {"type": "string", "description": "Reason for this change."},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_config",
            "description": "Display all current bot configuration settings.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_user_limit",
            "description": "Reset the daily message limit for a specific user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "Username to reset."},
                },
                "required": ["username"],
            },
        },
    },
    # Server moderation
    {
        "type": "function",
        "function": {
            "name": "delete_last_message",
            "description": "Delete the most recent message from a user in a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username":     {"type": "string"},
                    "channel_name": {"type": "string"},
                },
                "required": ["username", "channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_channel",
            "description": "Create a new text channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name":  {"type": "string"},
                    "category_name": {"type": "string"},
                    "private":       {"type": "boolean"},
                },
                "required": ["channel_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_channel",
            "description": "Delete a text channel.",
            "parameters": {
                "type": "object",
                "properties": {"channel_name": {"type": "string"}},
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
                    "username": {"type": "string"},
                    "reason":   {"type": "string"},
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
                    "username":    {"type": "string"},
                    "reason":      {"type": "string"},
                    "delete_days": {"type": "integer"},
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_slowmode",
            "description": "Set slowmode on a channel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string"},
                    "seconds":      {"type": "integer"},
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
                    "username":  {"type": "string"},
                    "role_name": {"type": "string"},
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
                    "username":  {"type": "string"},
                    "role_name": {"type": "string"},
                },
                "required": ["username", "role_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_announcement",
            "description": "Send a message to a channel as the bot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {"type": "string"},
                    "message":      {"type": "string"},
                },
                "required": ["channel_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "server_info",
            "description": "Show server overview: channels, roles, members.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clarify",
            "description": "Ask the admin one clarifying question before proceeding.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]

_ADMIN_SYSTEM = """Ты — Nexora Admin AI. Внутренний ИИ-ассистент для администраторов сервера.

Возможности:
1. УПРАВЛЕНИЕ КОНФИГУРАЦИЕЙ бота (лимиты, роли, стиль, персонаж) → tool: update_config
2. МОДЕРАЦИЯ сервера (каналы, кик/бан, роли, сообщения) → соответствующие tools
3. ПРОСМОТР настроек → tool: show_config

Правила:
- ВСЕГДА вызывай tool — не отвечай просто текстом если нужно действие
- Если нужен один уточняющий вопрос — tool: clarify
- Отвечай на том же языке что и администратор
- У администраторов и овнера ПОЛНЫЕ права, никаких ограничений

Примеры маппинга запросов → конфиг:
  "поставь лимит 5"            → update_config(key="free_daily_limit", value="5")
  "сделай бота официальным"    → update_config(key="bot_style", value="formal")
  "отвечай только по-русски"   → update_config(key="response_language", value="ru")
  "измени персонаж на строгий" → update_config(key="bot_persona", value="Ты строгий и лаконичный ассистент...")
  "добавь роль VIP в платные"  → update_config(key="paid_roles", value="Nexora Ultra,Nexora Elite,Nexora Pro,VIP")
  "покажи настройки"           → show_config()
  "сбрось лимит UserXYZ"       → reset_user_limit(username="UserXYZ")
"""


async def plan_admin(request: str) -> dict:
    try:
        resp = await ai.chat.completions.create(
            model=MODEL_ADMIN,
            messages=[
                {"role": "system", "content": _ADMIN_SYSTEM},
                {"role": "user",   "content": request},
            ],
            tools=ADMIN_TOOLS,
            tool_choice="auto",
            max_tokens=500,
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
                return {"type": "clarify", "question": args.get("question", "?")}
            return {
                "type":      "tool_call",
                "name":      name,
                "args":      args,
                "plan_text": _plan_text(name, args),
            }
        return {"type": "text", "content": msg.content or "OK."}
    except Exception as e:
        log.error("plan_admin: %s", e)
        return {"type": "error", "content": str(e)}


def _plan_text(name: str, args: dict) -> str:
    labels = {
        "free_daily_limit":   "📊 Лимит сообщений/день",
        "paid_roles":         "⭐ Платные роли",
        "bot_persona":        "🤖 Персонаж бота",
        "bot_style":          "🎨 Стиль общения",
        "response_language":  "🌐 Язык ответов",
        "limit_exempt_roles": "🔓 Роли без лимита",
    }
    match name:
        case "update_config":
            label  = labels.get(args.get("key", ""), args.get("key", ""))
            reason = f"\nПричина: {args['reason']}" if args.get("reason") else ""
            return (f"⚙️ **Изменить настройку**\n{label}\n"
                    f"Новое значение: `{args.get('value', '')[:120]}`{reason}")
        case "show_config":
            return "📋 **Показать все настройки бота**"
        case "reset_user_limit":
            return f"🔄 **Сбросить лимит** для `{args.get('username')}`"
        case "delete_last_message":
            return (f"🗑️ **Удалить последнее сообщение** от `{args.get('username')}` "
                    f"в `#{args.get('channel_name')}`")
        case "create_channel":
            return (f"➕ **Создать канал** `#{args.get('channel_name')}`"
                    + (f" в `{args.get('category_name')}`" if args.get("category_name") else "")
                    + (" *(приватный)*" if args.get("private") else " *(публичный)*"))
        case "delete_channel":
            return f"❌ **Удалить канал** `#{args.get('channel_name')}`"
        case "kick_member":
            return (f"👢 **Кик** `{args.get('username')}`"
                    + (f"\nПричина: {args.get('reason')}" if args.get("reason") else ""))
        case "ban_member":
            return (f"🔨 **Бан** `{args.get('username')}`"
                    + (f"\nПричина: {args.get('reason')}" if args.get("reason") else ""))
        case "set_slowmode":
            s = args.get("seconds", 0)
            return (f"⏱️ **Slowmode** `#{args.get('channel_name')}` → "
                    + (f"`{s}s`" if s > 0 else "`выключен`"))
        case "give_role":
            return f"🎭 **Дать роль** `{args.get('role_name')}` → `{args.get('username')}`"
        case "remove_role":
            return f"🎭 **Убрать роль** `{args.get('role_name')}` от `{args.get('username')}`"
        case "send_announcement":
            return (f"📢 **Отправить сообщение** в `#{args.get('channel_name')}`\n"
                    f"> {str(args.get('message', ''))[:120]}")
        case "server_info":
            return "🔍 **Обзор сервера** (каналы, роли, участники)"
        case _:
            return f"`{name}` — {args}"


# ══════════════════════════════════════════════════════════════════════════════
#  ACTION EXECUTORS
# ══════════════════════════════════════════════════════════════════════════════

async def execute_action(guild: discord.Guild, name: str, args: dict) -> str:
    try:
        match name:
            case "update_config":       return _exec_update_config(args)
            case "show_config":         return _exec_show_config()
            case "reset_user_limit":    return await _exec_reset_limit(guild, args)
            case "delete_last_message": return await _exec_delete_last(guild, args)
            case "create_channel":      return await _exec_create_channel(guild, args)
            case "delete_channel":      return await _exec_delete_channel(guild, args)
            case "kick_member":         return await _exec_kick(guild, args)
            case "ban_member":          return await _exec_ban(guild, args)
            case "set_slowmode":        return await _exec_slowmode(guild, args)
            case "give_role":           return await _exec_give_role(guild, args)
            case "remove_role":         return await _exec_remove_role(guild, args)
            case "send_announcement":   return await _exec_announce(guild, args)
            case "server_info":         return await _exec_server_info(guild)
            case _:                     return f"❓ Неизвестное действие: `{name}`"
    except Exception as e:
        return f"💥 Ошибка: {e}"


def _exec_update_config(args: dict) -> str:
    key   = args.get("key", "")
    value = args.get("value", "")
    valid = {"free_daily_limit", "paid_roles", "bot_persona",
             "bot_style", "response_language", "limit_exempt_roles"}
    if key not in valid:
        return f"❌ Неизвестный ключ: `{key}`\nДоступные: {', '.join(sorted(valid))}"
    old = cfg_get(key)
    cfg_set(key, value)
    return (f"✅ **Настройка обновлена!**\n"
            f"**{key}**\n"
            f"Было: `{old[:100]}`\n"
            f"Стало: `{value[:100]}`\n\n"
            f"*Изменение активно немедленно* 🔄")


def _exec_show_config() -> str:
    config = cfg_all()
    labels = {
        "free_daily_limit":   "📊 Лимит сообщений/день",
        "paid_roles":         "⭐ Платные роли (без лимита)",
        "limit_exempt_roles": "🔓 Доп. роли без лимита",
        "bot_persona":        "🤖 Персонаж бота",
        "bot_style":          "🎨 Стиль (friendly/formal/casual)",
        "response_language":  "🌐 Язык (auto/ru/en)",
    }
    lines = ["**⚙️ Текущие настройки Nexora AI**\n"]
    for key, label in labels.items():
        val = config.get(key, "—")
        if len(val) > 80:
            val = val[:80] + "..."
        lines.append(f"**{label}**\n`{val}`\n")
    lines.append("*Изменить: напиши команду в свободной форме или `/config`*")
    return "\n".join(lines)


async def _exec_reset_limit(guild: discord.Guild, args: dict) -> str:
    uname = args.get("username", "")
    member = (
        discord.utils.find(lambda m: m.name.lower()         == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Пользователь `{uname}` не найден."
    db_reset_user(member.id)
    return f"✅ Лимит сброшен для `{member.display_name}`."


async def _exec_delete_last(guild: discord.Guild, args: dict) -> str:
    uname = args.get("username", "")
    cname = args.get("channel_name", "")
    ch    = discord.utils.get(guild.text_channels, name=cname)
    if not ch:
        return f"❌ Канал `#{cname}` не найден."
    member = (
        discord.utils.find(lambda m: m.name.lower()         == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Пользователь `{uname}` не найден."
    try:
        async for msg in ch.history(limit=200):
            if msg.author.id == member.id:
                preview = msg.content[:80] or "[вложение/эмбед]"
                await msg.delete()
                return (f"✅ Удалено последнее сообщение от `{member.display_name}` "
                        f"в `#{cname}`\nПревью: `{preview}`")
        return f"❌ Последние 200 сообщений в `#{cname}` не содержат постов от `{member.display_name}`."
    except discord.Forbidden:
        return f"❌ Нет прав на чтение истории или удаление в `#{cname}`."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_create_channel(guild: discord.Guild, args: dict) -> str:
    cname   = args.get("channel_name", "new-channel")
    catname = args.get("category_name")
    private = args.get("private", False)
    cat     = discord.utils.get(guild.categories, name=catname) if catname else None
    ow      = ({guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me:           discord.PermissionOverwrite(read_messages=True)}
               if private else {})
    try:
        ch = await guild.create_text_channel(name=cname, category=cat, overwrites=ow)
        return f"✅ Канал `#{ch.name}` создан (ID: {ch.id})"
    except discord.Forbidden:
        return "❌ Нет прав на создание каналов."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_delete_channel(guild: discord.Guild, args: dict) -> str:
    cname = args.get("channel_name", "")
    ch    = discord.utils.get(guild.text_channels, name=cname)
    if not ch:
        return f"❌ Канал `#{cname}` не найден."
    try:
        await ch.delete()
        return f"✅ Канал `#{cname}` удалён."
    except discord.Forbidden:
        return "❌ Нет прав на удаление каналов."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_kick(guild: discord.Guild, args: dict) -> str:
    uname  = args.get("username", "")
    reason = args.get("reason", "Нет причины")
    member = (
        discord.utils.find(lambda m: m.name.lower()         == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Пользователь `{uname}` не найден."
    try:
        await member.kick(reason=reason)
        return f"✅ `{member.name}` кикнут. Причина: {reason}"
    except discord.Forbidden:
        return "❌ Нет прав на кик."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_ban(guild: discord.Guild, args: dict) -> str:
    uname       = args.get("username", "")
    reason      = args.get("reason", "Нет причины")
    delete_days = min(int(args.get("delete_days", 0)), 7)
    member = (
        discord.utils.find(lambda m: m.name.lower()         == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Пользователь `{uname}` не найден."
    try:
        await member.ban(reason=reason, delete_message_days=delete_days)
        return f"✅ `{member.name}` забанен. Причина: {reason}"
    except discord.Forbidden:
        return "❌ Нет прав на бан."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_slowmode(guild: discord.Guild, args: dict) -> str:
    cname   = args.get("channel_name", "")
    seconds = int(args.get("seconds", 0))
    ch      = discord.utils.get(guild.text_channels, name=cname)
    if not ch:
        return f"❌ Канал `#{cname}` не найден."
    try:
        await ch.edit(slowmode_delay=seconds)
        label = f"{seconds}s" if seconds > 0 else "выключен"
        return f"✅ Slowmode в `#{cname}` → {label}"
    except discord.Forbidden:
        return "❌ Нет прав на редактирование канала."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_give_role(guild: discord.Guild, args: dict) -> str:
    uname = args.get("username", "")
    rname = args.get("role_name", "")
    member = (
        discord.utils.find(lambda m: m.name.lower()         == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Пользователь `{uname}` не найден."
    role = discord.utils.get(guild.roles, name=rname)
    if not role:
        return f"❌ Роль `{rname}` не найдена."
    try:
        await member.add_roles(role)
        return f"✅ Роль `{rname}` выдана `{member.display_name}`."
    except discord.Forbidden:
        return "❌ Нет прав на выдачу ролей."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_remove_role(guild: discord.Guild, args: dict) -> str:
    uname = args.get("username", "")
    rname = args.get("role_name", "")
    member = (
        discord.utils.find(lambda m: m.name.lower()         == uname.lower(), guild.members)
        or discord.utils.find(lambda m: m.display_name.lower() == uname.lower(), guild.members)
    )
    if not member:
        return f"❌ Пользователь `{uname}` не найден."
    role = discord.utils.get(guild.roles, name=rname)
    if not role:
        return f"❌ Роль `{rname}` не найдена."
    try:
        await member.remove_roles(role)
        return f"✅ Роль `{rname}` убрана у `{member.display_name}`."
    except discord.Forbidden:
        return "❌ Нет прав на управление ролями."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_announce(guild: discord.Guild, args: dict) -> str:
    cname = args.get("channel_name", "")
    text  = args.get("message", "")
    ch    = discord.utils.get(guild.text_channels, name=cname)
    if not ch:
        return f"❌ Канал `#{cname}` не найден."
    try:
        await ch.send(text)
        return f"✅ Сообщение отправлено в `#{cname}`."
    except discord.Forbidden:
        return f"❌ Нет прав на отправку в `#{cname}`."
    except discord.HTTPException as e:
        return f"❌ Discord API ошибка: {e}"


async def _exec_server_info(guild: discord.Guild) -> str:
    text_ch  = [f"#{c.name}" for c in guild.text_channels]
    voice_ch = [f"🔊{c.name}" for c in guild.voice_channels]
    roles    = [r.name for r in guild.roles if r.name != "@everyone"]
    total    = guild.member_count
    bots     = sum(1 for m in guild.members if m.bot)
    return (
        f"**{guild.name}** — обзор сервера\n"
        f"👥 Участники: {total} ({total - bots} людей, {bots} ботов)\n"
        f"📝 Текстовые каналы ({len(text_ch)}): {', '.join(text_ch[:20])}\n"
        f"🔊 Голосовые ({len(voice_ch)}): {', '.join(voice_ch[:10])}\n"
        f"🎭 Роли ({len(roles)}): {', '.join(roles[:20])}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD UI — CONFIRM / CANCEL
# ══════════════════════════════════════════════════════════════════════════════

async def audit(guild: discord.Guild, msg: str):
    ch = discord.utils.get(guild.text_channels, name=AUDIT_CHANNEL_NAME)
    if ch:
        try:
            await ch.send(f"```\n{msg[:1990]}\n```")
        except Exception as e:
            log.warning("audit: %s", e)


class ConfirmView(discord.ui.View):
    def __init__(self, guild, name, args, requester):
        super().__init__(timeout=60)
        self.guild     = guild
        self.name      = name
        self.args      = args
        self.requester = requester

    @discord.ui.button(label="✅ Подтвердить", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Только запросивший может подтвердить.", ephemeral=True)
            return
        self._off()
        await interaction.response.edit_message(
            content=f"⚙️ Выполняю `{self.name}`…", view=self)
        result = await execute_action(self.guild, self.name, self.args)
        await interaction.followup.send(result)
        await audit(self.guild,
            f"[EXECUTE] {self.requester} ({self.requester.id})\n"
            f"action={self.name} args={self.args}\nresult={result}")
        self.stop()

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Только запросивший может отменить.", ephemeral=True)
            return
        self._off()
        await interaction.response.edit_message(content="🚫 Действие отменено.", view=self)
        self.stop()

    async def on_timeout(self):
        self._off()

    def _off(self):
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
        log.error("Slash sync: %s", e)
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="#ai-help"))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    ch_name = getattr(message.channel, "name", "")
    if ch_name == HELP_CHANNEL_NAME:
        await _handle_public(message)
    elif ch_name == ADMIN_CHANNEL_NAME:
        await _handle_admin(message)


async def _handle_public(message: discord.Message):
    member = message.author
    lang   = detect_lang(message.content)
    db_upsert_memory(member.id, lang)

    # Rate limit — exempt: owner, admin, paid, exempt-role
    if not is_limit_exempt(member):
        limit = get_free_limit()
        count = db_get_count(member.id)
        if count >= limit:
            if lang == "ru":
                await message.reply(
                    f"⚠️ Вы исчерпали **{limit}** бесплатных сообщений на сегодня (UTC).\n"
                    "Обновитесь до **Nexora Pro / Elite / Ultra** для безлимитного доступа! 🚀",
                    mention_author=False)
            else:
                await message.reply(
                    f"⚠️ You've used all **{limit}** free messages for today (UTC).\n"
                    "Upgrade to **Nexora Pro / Elite / Ultra** for unlimited access! 🚀",
                    mention_author=False)
            return
        new_count = db_increment(member.id)
        remaining = limit - new_count
    else:
        remaining = None  # unlimited

    async with message.channel.typing():
        reply = await ask_public(message.content, lang)

    if reply is None:
        err = ("❌ Произошла ошибка. Попробуйте снова."
               if lang == "ru" else "❌ An error occurred. Please try again.")
        await message.reply(err, mention_author=False)
        return

    # Append counter only for limited users
    if remaining is not None:
        if lang == "ru":
            reply += f"\n\n> 💬 Осталось сообщений сегодня: **{remaining}/{get_free_limit()}**"
        else:
            reply += f"\n\n> 💬 Free messages left today: **{remaining}/{get_free_limit()}**"

    await message.reply(reply, mention_author=False)


async def _handle_admin(message: discord.Message):
    member = message.author
    if not is_admin(member):
        await message.reply("🔒 Доступ запрещён. Только для администраторов.", mention_author=False)
        return
    if len(message.content.strip()) < 2:
        return

    await audit(message.guild, f"[REQUEST] {member} ({member.id}): {message.content}")

    async with message.channel.typing():
        plan = await plan_admin(message.content)

    match plan["type"]:
        case "tool_call":
            view = ConfirmView(message.guild, plan["name"], plan["args"], member)
            await message.reply(
                f"**📋 ПЛАН**\n{plan['plan_text']}\n\nПодтвердить?",
                view=view, mention_author=False)
        case "clarify":
            await message.reply(f"❓ **Уточнение:**\n{plan['question']}", mention_author=False)
        case "text":
            await message.reply(plan["content"], mention_author=False)
        case "error":
            await message.reply(f"💥 Ошибка AI: `{plan['content']}`", mention_author=False)
            await audit(message.guild, f"[AI ERROR] {plan['content']}")


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def _rules_text() -> str:
    limit = get_free_limit()
    paid  = cfg_get("paid_roles")
    return (
        f"# 📋 Nexora AI — Как пользоваться\n\n"
        f"**Что я умею:**\n"
        f"🎫 **Тикеты** — объясню как открыть и описать проблему\n"
        f"🎭 **Роли** — как получить, что дают, как купить подписку\n"
        f"📈 **Торговля** — правила, безопасные сделки\n"
        f"🔧 **Навигация** — каналы, команды, функции сервера\n\n"
        f"**Как общаться:**\n"
        f"Просто пиши вопрос обычным языком в этот канал.\n\n"
        f"**Лимиты:**\n"
        f"🆓 Бесплатно: **{limit} сообщений/день** (сброс в полночь UTC)\n"
        f"⭐ Подписчики ({paid}): **безлимитно**\n\n"
        f"**Нужна модерация?**\n"
        f"Обратись к администраторам или модераторам сервера."
    )


@tree.command(name="pin_rules", description="Опубликовать и закрепить правила в #ai-help.")
async def pin_rules(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Только для администраторов.", ephemeral=True)
        return
    help_ch = discord.utils.get(interaction.guild.text_channels, name=HELP_CHANNEL_NAME)
    if not help_ch:
        await interaction.response.send_message(
            f"❌ Канал `#{HELP_CHANNEL_NAME}` не найден.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        sent = await help_ch.send(_rules_text())
        await sent.pin()
        await interaction.followup.send(f"✅ Правила опубликованы и закреплены.")
        await audit(interaction.guild,
            f"[PIN_RULES] {interaction.user} ({interaction.user.id})")
    except discord.Forbidden:
        await interaction.followup.send("❌ Нет прав на отправку или закрепление.")
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")


@tree.command(name="config", description="[Админ] Просмотр или изменение настроек бота.")
@app_commands.describe(
    key="Ключ настройки (пусто = показать все)",
    value="Новое значение")
async def config_cmd(interaction: discord.Interaction, key: str = "", value: str = ""):
    if not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Только для администраторов.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if not key:
        await interaction.followup.send(_exec_show_config())
        return
    if not value:
        current = cfg_get(key)
        await interaction.followup.send(
            f"**{key}** = `{current or '(не задан)'}`\n"
            f"Для изменения: `/config key:{key} value:новое_значение`")
        return
    result = _exec_update_config({"key": key, "value": value})
    await interaction.followup.send(result)
    await audit(interaction.guild,
        f"[CONFIG] {interaction.user}: {key} = {value}")


@tree.command(name="status", description="Проверить статус и лимит сообщений.")
async def status_cmd(interaction: discord.Interaction):
    member = interaction.user
    exempt = is_limit_exempt(member)
    limit  = get_free_limit()
    count  = db_get_count(member.id)

    if is_owner(member):
        plan_info = "👑 **Овнер** — полный доступ, без лимитов"
    elif is_admin(member):
        plan_info = "🛡️ **Администратор** — полный доступ, без лимитов"
    elif is_paid(member):
        plan_info = "⭐ **Подписчик** — безлимитные сообщения"
    else:
        remaining = max(0, limit - count)
        plan_info = f"🆓 Бесплатный — осталось: **{remaining}/{limit}**"

    embed = discord.Embed(title="Nexora AI — Статус", color=discord.Color.blurple())
    embed.add_field(name="Бот", value="✅ Онлайн", inline=True)
    embed.add_field(name="Доступ", value=plan_info, inline=False)
    embed.set_footer(text="Лимит сбрасывается в полночь UTC")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db_init()
    bot.run(DISCORD_TOKEN, log_handler=None)

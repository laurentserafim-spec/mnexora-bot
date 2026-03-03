"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                     NEXORA DISCORD AI BOT v3.0                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  REQUIRED ENV (Railway → Variables):                                        ║
║    DISCORD_TOKEN  OPENAI_API_KEY  OWNER_ID                                  ║
║  OPTIONAL ENV:                                                              ║
║    ADMIN_CHANNEL_NAME  HELP_CHANNEL_NAME  AUDIT_CHANNEL_NAME               ║
║    FREE_DAILY_LIMIT  PAID_ROLES  MODEL_ASSISTANT  MODEL_ADMIN              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, re, json, logging, sqlite3, asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("nexora")

DISCORD_TOKEN      = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
OWNER_ID           = int(os.environ.get("OWNER_ID", "0"))
ADMIN_CHANNEL_NAME = os.environ.get("ADMIN_CHANNEL_NAME", "ai-admin")
HELP_CHANNEL_NAME  = os.environ.get("HELP_CHANNEL_NAME",  "ai-help")
AUDIT_CHANNEL_NAME = os.environ.get("AUDIT_CHANNEL_NAME", "ai-audit-log")
MODEL_ASSISTANT    = os.environ.get("MODEL_ASSISTANT", "gpt-4o")
MODEL_ADMIN        = os.environ.get("MODEL_ADMIN",     "gpt-4o")
DB_PATH            = "nexora.sqlite3"

_HIDDEN_PATTERN = re.compile(
    r"#?\b(ai[-_]?admin|ai[-_]?audit[-_]?log|audit[-_]?log"
    r"|admin\s*channel|internal\s*admin|admin\s*process)\b",
    re.IGNORECASE,
)

ai      = AsyncOpenAI(api_key=OPENAI_API_KEY)
intents = discord.Intents.default()
intents.guilds          = True
intents.message_content = True
intents.members         = True
bot     = commands.Bot(command_prefix="!", intents=intents)
tree    = bot.tree

# ── Role-based daily limits (highest role wins) ───────────────────────────────
ROLE_LIMITS: dict[str, int] = {
    "Nexora Ultra":    999999,   # unlimited
    "Nexora Elite":    300,
    "Nexora Pro":      150,
    "Verified Trader": 30,
    "Trader":          20,
    "Member":          15,
}
ROLE_LIMIT_ORDER = list(ROLE_LIMITS.keys())   # priority: first = highest
DEFAULT_LIMIT    = 10                          # everyone / Visitor

# ── Anti-spam: max 5 AI calls per 10 seconds per user ─────────────────────────
_SPAM_WINDOW   = 10.0   # seconds
_SPAM_MAX      = 5      # max calls per window
_spam_calls: dict[int, deque] = defaultdict(deque)   # user_id → deque of timestamps


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def _db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def db_init():
    with _db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS message_counts (
                user_id INTEGER NOT NULL, date_utc TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, date_utc));
            CREATE TABLE IF NOT EXISTS user_memory (
                user_id INTEGER PRIMARY KEY, language TEXT DEFAULT 'en',
                first_seen INTEGER DEFAULT 0, last_seen_utc TEXT);
            CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
    _cfg_defaults()

def _cfg_defaults():
    defaults = {
        "free_daily_limit":   os.environ.get("FREE_DAILY_LIMIT", "10"),
        "paid_roles":         os.environ.get("PAID_ROLES", "Nexora Ultra,Nexora Elite,Nexora Pro"),
        "bot_persona":        "Ты дружелюбный и компетентный помощник сервера Nexora. Отвечай кратко, конкретно, по делу. Никаких шаблонных отписок.",
        "bot_style":          "friendly",
        "response_language":  "auto",
        "limit_exempt_roles": "AI Admin",
        "server_guide": (
            "**Краткий гид по серверу Nexora:**\n"
            "• `#ticket-logs` — создай тикет для поддержки\n"
            "• `#general-trade` — торговля и сделки\n"
            "• `#verified-traders` — верифицированные трейдеры\n"
            "• `#rules` — правила сервера\n"
            "• `#vouches` — отзывы о сделках\n"
            "Хочешь больше возможностей? Оформи подписку **Nexora Pro/Elite/Ultra**!"
        ),
    }
    with _db() as c:
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO bot_config (key,value) VALUES (?,?)", (k, v))

def cfg_get(key):
    with _db() as c:
        row = c.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def cfg_set(key, value):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO bot_config (key,value) VALUES (?,?)", (key, value))

def cfg_all():
    with _db() as c:
        rows = c.execute("SELECT key,value FROM bot_config").fetchall()
    return {r["key"]: r["value"] for r in rows}

def get_free_limit():
    try: return int(cfg_get("free_daily_limit"))
    except: return 10

def get_paid_roles():
    return [r.strip() for r in cfg_get("paid_roles").split(",") if r.strip()]

def get_exempt_roles():
    return [r.strip() for r in cfg_get("limit_exempt_roles").split(",") if r.strip()]

def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def db_get_count(user_id):
    with _db() as c:
        row = c.execute("SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
                        (user_id, _today())).fetchone()
    return row["count"] if row else 0

def db_increment(user_id):
    today = _today()
    with _db() as c:
        c.execute("""INSERT INTO message_counts (user_id,date_utc,count) VALUES (?,?,1)
                     ON CONFLICT(user_id,date_utc) DO UPDATE SET count=count+1""", (user_id, today))
        row = c.execute("SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
                        (user_id, today)).fetchone()
    return row["count"]

def db_reset_user(user_id):
    with _db() as c:
        c.execute("DELETE FROM message_counts WHERE user_id=?", (user_id,))

def db_is_first(user_id):
    with _db() as c:
        row = c.execute("SELECT first_seen FROM user_memory WHERE user_id=?", (user_id,)).fetchone()
    return (row is None) or (row["first_seen"] == 0)

def db_upsert_memory(user_id, language, mark_seen=False):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        if mark_seen:
            c.execute("""INSERT INTO user_memory (user_id,language,first_seen,last_seen_utc) VALUES (?,?,1,?)
                         ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,
                         first_seen=1, last_seen_utc=excluded.last_seen_utc""", (user_id, language, now))
        else:
            c.execute("""INSERT INTO user_memory (user_id,language,first_seen,last_seen_utc) VALUES (?,?,0,?)
                         ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,
                         last_seen_utc=excluded.last_seen_utc""", (user_id, language, now))


# ══════════════════════════════════════════════════════════════════════════════
#  PERMISSIONS
# ══════════════════════════════════════════════════════════════════════════════

def is_owner(member): return member.id == OWNER_ID
def is_admin(member): return is_owner(member) or any(r.name == "AI Admin" for r in member.roles)
def is_paid(member):  return bool({r.name for r in member.roles} & set(get_paid_roles()))

def get_user_daily_limit(member: discord.Member) -> int:
    """Return the highest applicable daily message limit for this member."""
    if is_owner(member) or is_admin(member):
        return 999999
    role_names = {r.name for r in member.roles}
    for role_name in ROLE_LIMIT_ORDER:
        if role_name in role_names:
            return ROLE_LIMITS[role_name]
    try:
        configured = int(cfg_get("free_daily_limit"))
        return configured if configured > 0 else DEFAULT_LIMIT
    except Exception:
        return DEFAULT_LIMIT

def is_unlimited(member: discord.Member) -> bool:
    return get_user_daily_limit(member) >= 999999

def check_antispam(user_id: int) -> bool:
    """Returns True if allowed, False if rate-limited (5 calls / 10 sec)."""
    now = datetime.now(timezone.utc).timestamp()
    dq  = _spam_calls[user_id]
    while dq and now - dq[0] > _SPAM_WINDOW:
        dq.popleft()
    if len(dq) >= _SPAM_MAX:
        return False
    dq.append(now)
    return True

def sanitize(text): return _HIDDEN_PATTERN.sub("[server administration]", text)
def detect_lang(text): return "ru" if re.search(r"[а-яёА-ЯЁ]", text) else "en"


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC ASSISTANT
# ══════════════════════════════════════════════════════════════════════════════

def _system_paid(lang):
    persona = cfg_get("bot_persona"); style = cfg_get("bot_style")
    lang_mode = cfg_get("response_language"); limit = get_free_limit()
    if lang_mode == "auto": lang_rule = "Отвечай на русском." if lang == "ru" else "Reply in the user's language."
    elif lang_mode == "ru": lang_rule = "Всегда отвечай на русском."
    else: lang_rule = "Always reply in English."
    style_map = {"friendly": "Тон: дружелюбный, тёплый.", "formal": "Тон: официальный.", "casual": "Тон: расслабленный."}
    return f"""Ты — Nexora AI Bot. Полноценный помощник для подписчиков.
{persona}
{style_map.get(style, style_map['friendly'])}
{lang_rule}
СТРОГИЕ ПРАВИЛА:
- НИКОГДА не упоминай каналы администрирования или логирования
- Если просят модераторское действие — скажи обратиться к администраторам
- Конкретные ответы, без шаблонных отписок
- Свободные пользователи: {limit} сообщений/день"""

def _system_free(lang):
    lang_mode = cfg_get("response_language")
    if lang_mode == "auto": lang_rule = "Отвечай на русском." if lang == "ru" else "Reply in the user's language."
    elif lang_mode == "ru": lang_rule = "Всегда отвечай на русском."
    else: lang_rule = "Always reply in English."
    return f"""Ты — Nexora AI Bot. Базовый помощник для бесплатных пользователей.
{lang_rule}
Ты можешь ТОЛЬКО объяснять как устроен сервер Nexora, каналы, тикеты, правила, роли.
СТРОГИЕ ПРАВИЛА: НИКОГДА не упоминай каналы администрирования. Конкретные ответы."""

async def ask_ai(user_msg, lang, paid):
    system = _system_paid(lang) if paid else _system_free(lang)
    try:
        r = await ai.chat.completions.create(
            model=MODEL_ASSISTANT,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            max_tokens=700, temperature=0.7)
        return sanitize(r.choices[0].message.content or "")
    except Exception as e:
        log.error("ask_ai: %s", e); return None

def _welcome_suffix(lang, is_free, limit):
    guide = cfg_get("server_guide")
    if lang == "ru":
        return (f"\n\n{guide}\n\n> 💬 Бесплатный доступ: **{limit} сообщений/день**\n> ⭐ Безлимитно — **Nexora Pro/Elite/Ultra**"
                if is_free else f"\n\n{guide}")
    return (f"\n\n{guide}\n\n> 💬 Free: **{limit} messages/day**\n> ⭐ Unlimited — **Nexora Pro/Elite/Ultra**"
            if is_free else f"\n\n{guide}")

async def handle_public(message, content):
    member = message.author; lang = detect_lang(content)
    first  = db_is_first(member.id)

    # Anti-spam check
    if not check_antispam(member.id):
        warn = ('⚠️ Слишком много запросов. Подожди 10 секунд.' if lang == 'ru'
                else '⚠️ Too many requests. Please wait 10 seconds.')
        await message.reply(warn, mention_author=False)
        await _audit(message.guild,
            f'[SPAM] {member} ({member.id}) — rate limited in #{getattr(message.channel,"name","?")}')
        return

    # Role-based daily limit
    limit     = get_user_daily_limit(member)
    unlimited = limit >= 999999

    if not unlimited:
        count = db_get_count(member.id)
        if count >= limit:
            msg = (f'⚠️ Вы исчерпали **{limit}** сообщений на сегодня.\nПовысьте роль или оформите **Nexora Pro/Elite/Ultra**! 🚀'
                   if lang == 'ru' else f"⚠️ You've used all **{limit}** messages today.\nUpgrade your role or get **Nexora Pro/Elite/Ultra**! 🚀")
            await message.reply(msg, mention_author=False); return
        new_count = db_increment(member.id); remaining = limit - new_count
    else:
        remaining = None

    db_upsert_memory(member.id, lang, mark_seen=first)
    async with message.channel.typing():
        reply = await ask_ai(content, lang, paid=unlimited)
    if reply is None:
        await message.reply('❌ Произошла ошибка. Попробуйте снова.' if lang == 'ru' else '❌ An error occurred.', mention_author=False); return
    if first: reply += _welcome_suffix(lang, is_free=not unlimited, limit=limit)
    if remaining is not None:
        reply += (f'\n\n> 💬 Осталось сегодня: **{remaining}/{limit}**' if lang == 'ru'
                  else f'\n\n> 💬 Messages left today: **{remaining}/{limit}**')
    await message.reply(reply, mention_author=False)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN HANDLER
# ══════════════════════════════════════════════════════════════════════════════

ADMIN_TOOLS = [
    {"type":"function","function":{"name":"update_config","description":"Update a live bot config setting. Keys: free_daily_limit (int), paid_roles (csv), bot_persona (text), bot_style (friendly|formal|casual), response_language (auto|ru|en), limit_exempt_roles (csv), server_guide (text).","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"},"reason":{"type":"string"}},"required":["key","value"]}}},
    {"type":"function","function":{"name":"show_config","description":"Show all current bot config settings.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"reset_user_limit","description":"Reset the daily message counter for a specific user.","parameters":{"type":"object","properties":{"username":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"delete_last_message","description":"Delete the most recent message from a user in a channel.","parameters":{"type":"object","properties":{"username":{"type":"string"},"channel_name":{"type":"string"}},"required":["username","channel_name"]}}},
    {"type":"function","function":{"name":"create_channel","description":"Create a new text channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"category_name":{"type":"string"},"private":{"type":"boolean"}},"required":["channel_name"]}}},
    {"type":"function","function":{"name":"delete_channel","description":"Delete a text channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"}},"required":["channel_name"]}}},
    {"type":"function","function":{"name":"kick_member","description":"Kick a member from the server.","parameters":{"type":"object","properties":{"username":{"type":"string"},"reason":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"ban_member","description":"Ban a member from the server.","parameters":{"type":"object","properties":{"username":{"type":"string"},"reason":{"type":"string"},"delete_days":{"type":"integer"}},"required":["username"]}}},
    {"type":"function","function":{"name":"set_slowmode","description":"Set slowmode on a channel (seconds=0 to disable).","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"seconds":{"type":"integer"}},"required":["channel_name","seconds"]}}},
    {"type":"function","function":{"name":"give_role","description":"Give a Discord role to a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"role_name":{"type":"string"}},"required":["username","role_name"]}}},
    {"type":"function","function":{"name":"remove_role","description":"Remove a Discord role from a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"role_name":{"type":"string"}},"required":["username","role_name"]}}},
    {"type":"function","function":{"name":"send_announcement","description":"Send a message to a channel as the bot.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"message":{"type":"string"}},"required":["channel_name","message"]}}},
    {"type":"function","function":{"name":"server_info","description":"Show full server overview: channels, roles, members count.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"clarify","description":"Ask the admin one clarifying question before proceeding.","parameters":{"type":"object","properties":{"question":{"type":"string"}},"required":["question"]}}},
]

_ADMIN_SYSTEM = """Ты — Nexora Admin AI. Внутренний ИИ-ассистент для администраторов сервера.

Возможности:
1. УПРАВЛЕНИЕ КОНФИГУРАЦИЕЙ → update_config, show_config
2. МОДЕРАЦИЯ → kick, ban, каналы, роли, сообщения
3. СБРОС ЛИМИТОВ → reset_user_limit
4. ОБЗОР СЕРВЕРА → server_info

═══════════════════════════════════
КОГДА ОТВЕЧАТЬ ТЕКСТОМ (БЕЗ TOOLS):
═══════════════════════════════════
Если запрос ИНФОРМАЦИОННЫЙ — отвечай обычным текстом, НЕ вызывай tools.

Информационный запрос — это:
- "Расскажи о себе / своём функционале"
- "Как ты работаешь / как устроен"
- "Что ты умеешь"
- "Опиши структуру / механизм работы"
- "Какие у тебя возможности"

На информационные запросы отвечай ТЕКСТОМ, описывая свои возможности.

═══════════════════════════════════
КОГДА ВЫЗЫВАТЬ TOOLS:
═══════════════════════════════════
Tools вызываются ТОЛЬКО если запрос требует ДЕЙСТВИЯ или ИЗМЕНЕНИЯ:
- Изменить настройку → update_config
- Показать текущие настройки → show_config
- Кик/бан → kick_member / ban_member
- Создать/удалить канал → create_channel / delete_channel
- Выдать/убрать роль → give_role / remove_role
- Сбросить лимит → reset_user_limit
- Отправить объявление → send_announcement
- Посмотреть участников/каналы/роли → server_info

Правила:
- Один уточняющий вопрос → tool: clarify
- Отвечай на языке администратора
- Администраторы и овнер имеют ПОЛНЫЕ права

Примеры:
  "поставь лимит 5"           → update_config(key="free_daily_limit", value="5")
  "сделай бота официальным"   → update_config(key="bot_style", value="formal")
  "отвечай только по-русски"  → update_config(key="response_language", value="ru")
  "покажи настройки"          → show_config()
  "сбрось лимит Username"     → reset_user_limit(username="Username")
"""

async def plan_admin(request):
    try:
        resp = await ai.chat.completions.create(
            model=MODEL_ADMIN,
            messages=[{"role":"system","content":_ADMIN_SYSTEM},{"role":"user","content":request}],
            tools=ADMIN_TOOLS, tool_choice="auto", max_tokens=500)
        msg = resp.choices[0].message
        if msg.tool_calls:
            tc = msg.tool_calls[0]; name = tc.function.name
            try: args = json.loads(tc.function.arguments)
            except: args = {}
            if name == "clarify": return {"type":"clarify","question":args.get("question","?")}
            return {"type":"tool_call","name":name,"args":args,"plan_text":_plan_text(name,args)}
        return {"type":"text","content":msg.content or "OK."}
    except Exception as e:
        log.error("plan_admin: %s", e); return {"type":"error","content":str(e)}

def _plan_text(name, args):
    cfg_labels = {"free_daily_limit":"📊 Лимит сообщений/день","paid_roles":"⭐ Платные роли",
                  "bot_persona":"🤖 Персонаж бота","bot_style":"🎨 Стиль общения",
                  "response_language":"🌐 Язык ответов","limit_exempt_roles":"🔓 Роли без лимита",
                  "server_guide":"🗺️ Гид для новых пользователей"}
    match name:
        case "update_config":
            label = cfg_labels.get(args.get("key",""), args.get("key",""))
            reason = f"\nПричина: {args['reason']}" if args.get("reason") else ""
            return f"⚙️ **Изменить настройку**\n{label}\nНовое значение: `{str(args.get('value',''))[:120]}`{reason}"
        case "show_config": return "📋 **Показать все настройки**"
        case "reset_user_limit": return f"🔄 **Сбросить лимит** для `{args.get('username')}`"
        case "delete_last_message": return f"🗑️ **Удалить последнее сообщение** от `{args.get('username')}` в `#{args.get('channel_name')}`"
        case "create_channel":
            return (f"➕ **Создать канал** `#{args.get('channel_name')}`"
                    + (f" в `{args.get('category_name')}`" if args.get("category_name") else "")
                    + (" *(приватный)*" if args.get("private") else " *(публичный)*"))
        case "delete_channel": return f"❌ **Удалить канал** `#{args.get('channel_name')}`"
        case "kick_member": return f"👢 **Кик** `{args.get('username')}`" + (f"\nПричина: {args.get('reason')}" if args.get("reason") else "")
        case "ban_member": return f"🔨 **Бан** `{args.get('username')}`" + (f"\nПричина: {args.get('reason')}" if args.get("reason") else "")
        case "set_slowmode":
            s = args.get("seconds",0)
            return f"⏱️ **Slowmode** `#{args.get('channel_name')}` → " + (f"`{s}s`" if s > 0 else "`выключен`")
        case "give_role": return f"🎭 **Дать роль** `{args.get('role_name')}` → `{args.get('username')}`"
        case "remove_role": return f"🎭 **Убрать роль** `{args.get('role_name')}` от `{args.get('username')}`"
        case "send_announcement": return f"📢 **Сообщение** в `#{args.get('channel_name')}`\n> {str(args.get('message',''))[:120]}"
        case "server_info": return "🔍 **Обзор сервера**"
        case _: return f"`{name}` — {args}"

async def handle_admin(message):
    member = message.author
    if not is_admin(member):
        await message.reply("🔒 Только для администраторов.", mention_author=False); return
    if len(message.content.strip()) < 2: return
    await _audit(message.guild, f"[REQUEST] {member} ({member.id}): {message.content}")
    async with message.channel.typing():
        plan = await plan_admin(message.content)
    match plan["type"]:
        case "tool_call":
            view = ConfirmView(message.guild, plan["name"], plan["args"], member)
            await message.reply(f"**📋 ПЛАН**\n{plan['plan_text']}\n\nПодтвердить?", view=view, mention_author=False)
        case "clarify": await message.reply(f"❓ **Уточнение:**\n{plan['question']}", mention_author=False)
        case "text": await message.reply(plan["content"], mention_author=False)
        case "error":
            await message.reply(f"💥 Ошибка AI: `{plan['content']}`", mention_author=False)
            await _audit(message.guild, f"[AI ERROR] {plan['content']}")


# ══════════════════════════════════════════════════════════════════════════════
#  ACTION EXECUTORS
# ══════════════════════════════════════════════════════════════════════════════

def _find_member(guild, name):
    name_l = name.lower()
    return (discord.utils.find(lambda m: m.name.lower() == name_l, guild.members)
            or discord.utils.find(lambda m: m.display_name.lower() == name_l, guild.members))

async def execute_action(guild, name, args):
    try:
        match name:
            case "update_config":       return _do_update_config(args)
            case "show_config":         return _do_show_config()
            case "reset_user_limit":    return await _do_reset_limit(guild, args)
            case "delete_last_message": return await _do_delete_last(guild, args)
            case "create_channel":      return await _do_create_channel(guild, args)
            case "delete_channel":      return await _do_delete_channel(guild, args)
            case "kick_member":         return await _do_kick(guild, args)
            case "ban_member":          return await _do_ban(guild, args)
            case "set_slowmode":        return await _do_slowmode(guild, args)
            case "give_role":           return await _do_give_role(guild, args)
            case "remove_role":         return await _do_remove_role(guild, args)
            case "send_announcement":   return await _do_announce(guild, args)
            case "server_info":         return await _do_server_info(guild)
            case _:                     return f"❓ Неизвестное действие: `{name}`"
    except Exception as e:
        return f"💥 Ошибка: {e}"

def _do_update_config(args):
    key = args.get("key",""); value = args.get("value","")
    valid = {"free_daily_limit","paid_roles","bot_persona","bot_style","response_language","limit_exempt_roles","server_guide"}
    if key not in valid: return f"❌ Неизвестный ключ: `{key}`\nДоступные: {', '.join(sorted(valid))}"
    old = cfg_get(key); cfg_set(key, value)
    return f"✅ **Настройка обновлена!**\n**{key}**\nБыло: `{old[:100]}`\nСтало: `{value[:100]}`\n\n*Активно немедленно* 🔄"

def _do_show_config():
    config = cfg_all()
    labels = {"free_daily_limit":"📊 Лимит/день","paid_roles":"⭐ Платные роли","limit_exempt_roles":"🔓 Роли без лимита",
              "bot_style":"🎨 Стиль","response_language":"🌐 Язык","bot_persona":"🤖 Персонаж","server_guide":"🗺️ Гид"}
    lines = ["**⚙️ Настройки Nexora AI**\n"]
    for key, label in labels.items():
        val = config.get(key,"—")
        if len(val) > 80: val = val[:80] + "..."
        lines.append(f"**{label}**\n`{val}`\n")
    lines.append("*Изменить: напиши команду свободным текстом*")
    return "\n".join(lines)

async def _do_reset_limit(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    db_reset_user(m.id); return f"✅ Лимит сброшен для `{m.display_name}`."

async def _do_delete_last(guild, args):
    cname = args.get("channel_name",""); ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    try:
        async for msg in ch.history(limit=200):
            if msg.author.id == m.id:
                preview = msg.content[:80] or "[вложение]"; await msg.delete()
                return f"✅ Удалено последнее сообщение от `{m.display_name}` в `#{cname}`\nПревью: `{preview}`"
        return f"❌ Нет сообщений от `{m.display_name}` в последних 200 в `#{cname}`."
    except discord.Forbidden: return f"❌ Нет прав на чтение/удаление в `#{cname}`."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_create_channel(guild, args):
    cname = args.get("channel_name","new-channel"); catname = args.get("category_name")
    private = args.get("private", False); cat = discord.utils.get(guild.categories, name=catname) if catname else None
    ow = ({guild.default_role: discord.PermissionOverwrite(read_messages=False),
           guild.me: discord.PermissionOverwrite(read_messages=True)} if private else {})
    try:
        ch = await guild.create_text_channel(name=cname, category=cat, overwrites=ow)
        return f"✅ Канал `#{ch.name}` создан."
    except discord.Forbidden: return "❌ Нет прав на создание каналов."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_delete_channel(guild, args):
    cname = args.get("channel_name",""); ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    try: await ch.delete(); return f"✅ Канал `#{cname}` удалён."
    except discord.Forbidden: return "❌ Нет прав на удаление."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_kick(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    try: await m.kick(reason=args.get("reason","Нет причины")); return f"✅ `{m.name}` кикнут."
    except discord.Forbidden: return "❌ Нет прав на кик."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_ban(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    try:
        await m.ban(reason=args.get("reason","Нет причины"), delete_message_days=min(int(args.get("delete_days",0)),7))
        return f"✅ `{m.name}` забанен."
    except discord.Forbidden: return "❌ Нет прав на бан."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_slowmode(guild, args):
    cname = args.get("channel_name",""); secs = int(args.get("seconds",0))
    ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    try:
        await ch.edit(slowmode_delay=secs)
        return f"✅ Slowmode `#{cname}` → {'выключен' if secs==0 else f'{secs}s'}"
    except discord.Forbidden: return "❌ Нет прав на редактирование канала."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_give_role(guild, args):
    m = _find_member(guild, args.get("username","")); role = discord.utils.get(guild.roles, name=args.get("role_name",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    if not role: return f"❌ Роль `{args.get('role_name')}` не найдена."
    try: await m.add_roles(role); return f"✅ Роль `{role.name}` выдана `{m.display_name}`."
    except discord.Forbidden: return "❌ Нет прав на выдачу ролей."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_remove_role(guild, args):
    m = _find_member(guild, args.get("username","")); role = discord.utils.get(guild.roles, name=args.get("role_name",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    if not role: return f"❌ Роль `{args.get('role_name')}` не найдена."
    try: await m.remove_roles(role); return f"✅ Роль `{role.name}` убрана у `{m.display_name}`."
    except discord.Forbidden: return "❌ Нет прав на управление ролями."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_announce(guild, args):
    cname = args.get("channel_name",""); text = args.get("message","")
    ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    try: await ch.send(text); return f"✅ Сообщение отправлено в `#{cname}`."
    except discord.Forbidden: return f"❌ Нет прав на отправку в `#{cname}`."
    except discord.HTTPException as e: return f"❌ Discord API: {e}"

async def _do_server_info(guild):
    tch = [f"#{c.name}" for c in guild.text_channels]; vch = [f"🔊{c.name}" for c in guild.voice_channels]
    roles = [r.name for r in guild.roles if r.name != "@everyone"]; total = guild.member_count
    bots = sum(1 for m in guild.members if m.bot)
    return (f"**{guild.name}**\n👥 {total} участников ({total-bots} людей, {bots} ботов)\n"
            f"📝 Каналы ({len(tch)}): {', '.join(tch[:25])}\n"
            f"🔊 Голосовые ({len(vch)}): {', '.join(vch[:10])}\n"
            f"🎭 Роли ({len(roles)}): {', '.join(roles[:25])}")


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD UI
# ══════════════════════════════════════════════════════════════════════════════

async def _audit(guild, msg):
    ch = discord.utils.get(guild.text_channels, name=AUDIT_CHANNEL_NAME)
    if ch:
        try: await ch.send(f"```\n{msg[:1990]}\n```")
        except Exception as e: log.warning("audit: %s", e)

class ConfirmView(discord.ui.View):
    def __init__(self, guild, name, args, requester):
        super().__init__(timeout=60)
        self.guild = guild; self.name = name; self.args = args; self.requester = requester

    @discord.ui.button(label="✅ Подтвердить", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("Только запросивший может подтвердить.", ephemeral=True); return
        self._off(); await interaction.response.edit_message(content="⚙️ Выполняю…", view=self)
        result = await execute_action(self.guild, self.name, self.args)
        await interaction.followup.send(result)
        await _audit(self.guild, f"[EXECUTE] {self.requester} ({self.requester.id})\naction={self.name} args={self.args}\nresult={result}")
        self.stop()

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("Только запросивший может отменить.", ephemeral=True); return
        self._off(); await interaction.response.edit_message(content="🚫 Отменено.", view=self); self.stop()

    async def on_timeout(self): self._off()
    def _off(self):
        for item in self.children: item.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
#  BOT EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    db_init(); log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    try:
        synced = await tree.sync(); log.info("Synced %d slash commands.", len(synced))
    except Exception as e: log.error("Slash sync: %s", e)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Nexora | @mention me!"))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return
    await bot.process_commands(message)
    ch_name = getattr(message.channel, "name", "")
    mentioned = bot.user in (message.mentions or [])
    if ch_name == ADMIN_CHANNEL_NAME:
        await handle_admin(message); return
    if ch_name == HELP_CHANNEL_NAME:
        await handle_public(message, message.content); return
    if mentioned:
        clean = re.sub(r"<@!?\d+>", "", message.content).strip()
        if not clean:
            lang = detect_lang(message.content)
            await message.reply("Привет! Чем могу помочь? 😊" if lang == "ru" else "Hey! How can I help? 😊", mention_author=False); return
        await handle_public(message, clean)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

def _rules_text():
    limit = get_free_limit(); paid = cfg_get("paid_roles")
    return (f"# Nexora AI — Как пользоваться\n\n**Как обратиться:**\n"
            f"• В `#ai-help` — просто пиши вопрос\n• В любом канале — `@Nexora AI` + вопрос\n\n"
            f"**Что я умею:**\n🎫 Тикеты  🎭 Роли  📈 Торговля  🔧 Навигация по серверу\n\n"
            f"**Лимиты:**\n🆓 Бесплатно: **{limit} сообщений/день**\n⭐ Подписчики ({paid}): **безлимитно**\n\n"
            f"**Нужна модерация?** Обратись к администраторам.")

@tree.command(name="pin_rules", description="Опубликовать и закрепить правила в #ai-help.")
async def pin_rules(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Только для администраторов.", ephemeral=True); return
    help_ch = discord.utils.get(interaction.guild.text_channels, name=HELP_CHANNEL_NAME)
    if not help_ch:
        await interaction.response.send_message(f"❌ Канал `#{HELP_CHANNEL_NAME}` не найден.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try:
        sent = await help_ch.send(_rules_text()); await sent.pin()
        await interaction.followup.send("✅ Правила опубликованы и закреплены.")
        await _audit(interaction.guild, f"[PIN_RULES] {interaction.user} ({interaction.user.id})")
    except discord.Forbidden: await interaction.followup.send("❌ Нет прав.")
    except discord.HTTPException as e: await interaction.followup.send(f"❌ Ошибка: {e}")

@tree.command(name="config", description="[Админ] Просмотр или изменение настроек бота.")
@app_commands.describe(key="Ключ (пусто = показать все)", value="Новое значение")
async def config_cmd(interaction: discord.Interaction, key: str = "", value: str = ""):
    if not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Только для администраторов.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    if not key: await interaction.followup.send(_do_show_config()); return
    if not value:
        await interaction.followup.send(f"**{key}** = `{cfg_get(key) or '(не задан)'}`"); return
    result = _do_update_config({"key": key, "value": value})
    await interaction.followup.send(result)
    await _audit(interaction.guild, f"[CONFIG] {interaction.user}: {key} = {value}")

@tree.command(name="status", description="Мой статус и лимит сообщений.")
async def status_cmd(interaction: discord.Interaction):
    member = interaction.user; limit = get_free_limit(); count = db_get_count(member.id)
    if is_owner(member): info = "👑 **Овнер** — полный доступ, без лимитов"
    elif is_admin(member): info = "🛡️ **Администратор** — полный доступ, без лимитов"
    elif is_paid(member): info = "⭐ **Подписчик** — безлимитный AI"
    else:
        remaining = max(0, limit - count)
        info = f"🆓 **Бесплатный**\nОсталось сегодня: **{remaining}/{limit}** сообщений"
    embed = discord.Embed(title="Nexora AI — Статус", color=discord.Color.blurple())
    embed.add_field(name="Бот", value="✅ Онлайн", inline=True)
    embed.add_field(name="Доступ", value=info, inline=False)
    embed.set_footer(text="Лимит сбрасывается в полночь UTC")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db_init()
    bot.run(DISCORD_TOKEN, log_handler=None)

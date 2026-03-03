"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                     NEXORA DISCORD AI BOT v4.0                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  REQUIRED ENV:  DISCORD_TOKEN  OPENAI_API_KEY  OWNER_ID                     ║
║  OPTIONAL ENV:  ADMIN_CHANNEL_NAME  HELP_CHANNEL_NAME  AUDIT_CHANNEL_NAME   ║
║                 FREE_DAILY_LIMIT  PAID_ROLES  MODEL_ASSISTANT  MODEL_ADMIN  ║
║                                                                              ║
║  CHANGELOG v4.0:                                                             ║
║    + Role-based daily limits (Member/Trader/Verified/Pro/Elite/Ultra)       ║
║    + Anti-spam (5 calls / 10 sec)                                            ║
║    + Ticket auto-translation (2 users=auto, 3+=@mention)                   ║
║    + Dialog memory (20 turns, TTL 24h, reset context)                       ║
║    + Point system (deals, referrals, anti-fraud)                             ║
║    + Additional message quota (purchasable, admin-assigned)                 ║
║    + ai-directives governance channel (startup load + realtime)             ║
║    + No-fabrication mode (payment_enabled check)                            ║
║    + Auto role upgrade (1 referral + 20 points → Member)                   ║
║    + Ticket close point awards                                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, re, json, logging, sqlite3, asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("nexora")

# ── ENV ───────────────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY       = os.environ["OPENAI_API_KEY"]
OWNER_ID             = int(os.environ.get("OWNER_ID", "0"))
ADMIN_CHANNEL_NAME   = os.environ.get("ADMIN_CHANNEL_NAME",   "ai-admin")
HELP_CHANNEL_NAME    = os.environ.get("HELP_CHANNEL_NAME",    "ai-help")
AUDIT_CHANNEL_NAME   = os.environ.get("AUDIT_CHANNEL_NAME",   "ai-audit-log")
DIRECTIVES_CHANNEL   = os.environ.get("DIRECTIVES_CHANNEL",   "ai-directives")
MODEL_ASSISTANT      = os.environ.get("MODEL_ASSISTANT", "gpt-4o")
MODEL_ADMIN          = os.environ.get("MODEL_ADMIN",     "gpt-4o")
DB_PATH              = "nexora.sqlite3"

_HIDDEN_PATTERN = re.compile(
    r"#?\b(ai[-_]?admin|ai[-_]?audit[-_]?log|audit[-_]?log"
    r"|admin\s*channel|internal\s*admin|admin\s*process|ai[-_]?directives)\b",
    re.IGNORECASE,
)
_TICKET_PATTERN = re.compile(r'^ticket-\d+$', re.IGNORECASE)

ai      = AsyncOpenAI(api_key=OPENAI_API_KEY)
intents = discord.Intents.default()
intents.guilds          = True
intents.message_content = True
intents.members         = True
bot     = commands.Bot(command_prefix="!", intents=intents)
tree    = bot.tree

# ── Role limits ───────────────────────────────────────────────────────────────
ROLE_LIMITS: dict[str, int] = {
    "Nexora Ultra":    999999,
    "Nexora Elite":    300,
    "Nexora Pro":      150,
    "Verified Trader": 30,
    "Trader":          20,
    "Member":          15,
}
ROLE_LIMIT_ORDER = list(ROLE_LIMITS.keys())
DEFAULT_LIMIT    = 10

# ── Anti-spam ─────────────────────────────────────────────────────────────────
_SPAM_WINDOW = 10.0
_SPAM_MAX    = 5
_spam_calls: dict[int, deque] = defaultdict(deque)

# ── Directives in-memory state ────────────────────────────────────────────────
_directive_state: list[str] = []   # last N directive contents


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
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, date_utc));

            CREATE TABLE IF NOT EXISTS user_memory (
                user_id INTEGER PRIMARY KEY, language TEXT DEFAULT 'en',
                first_seen INTEGER DEFAULT 0, last_seen_utc TEXT,
                subscription_status TEXT DEFAULT 'NORMAL');

            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);

            CREATE TABLE IF NOT EXISTS user_quota (
                user_id INTEGER PRIMARY KEY,
                additional_quota INTEGER DEFAULT 0);

            CREATE TABLE IF NOT EXISTS user_points (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0,
                total_deals INTEGER DEFAULT 0,
                referrer_id INTEGER DEFAULT NULL);

            CREATE TABLE IF NOT EXISTS point_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, reason TEXT,
                delta INTEGER, created_at TEXT);

            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER, referred_id INTEGER,
                deal_rewarded INTEGER DEFAULT 0,
                created_at TEXT,
                PRIMARY KEY (referrer_id, referred_id));

            CREATE TABLE IF NOT EXISTS conversation_memory (
                guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
                updated_at TEXT, history_json TEXT,
                PRIMARY KEY (guild_id, channel_id, user_id));

            CREATE TABLE IF NOT EXISTS directive_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, message_id INTEGER, author_id INTEGER,
                created_at TEXT, content TEXT);
        """)
    _cfg_defaults()

def _cfg_defaults():
    defaults = {
        "free_daily_limit":   os.environ.get("FREE_DAILY_LIMIT", "10"),
        "paid_roles":         os.environ.get("PAID_ROLES", "Nexora Ultra,Nexora Elite,Nexora Pro"),
        "bot_persona":        "Ты дружелюбный и компетентный помощник сервера Nexora. Отвечай кратко, конкретно, по делу.",
        "bot_style":          "friendly",
        "response_language":  "auto",
        "limit_exempt_roles": "AI Admin",
        "payment_enabled":    "false",
        "payment_instructions": "",
        "auto_upgrade_role":  "Member",
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

# ── Config ────────────────────────────────────────────────────────────────────
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

# ── Message counts ────────────────────────────────────────────────────────────
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

# ── User memory ───────────────────────────────────────────────────────────────
def db_is_first(user_id):
    with _db() as c:
        row = c.execute("SELECT first_seen FROM user_memory WHERE user_id=?", (user_id,)).fetchone()
    return (row is None) or (row["first_seen"] == 0)

def db_upsert_memory(user_id, language, mark_seen=False):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        if mark_seen:
            c.execute("""INSERT INTO user_memory (user_id,language,first_seen,last_seen_utc)
                         VALUES (?,?,1,?)
                         ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,
                         first_seen=1, last_seen_utc=excluded.last_seen_utc""",
                      (user_id, language, now))
        else:
            c.execute("""INSERT INTO user_memory (user_id,language,first_seen,last_seen_utc)
                         VALUES (?,?,0,?)
                         ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,
                         last_seen_utc=excluded.last_seen_utc""",
                      (user_id, language, now))

def db_get_lang(user_id):
    with _db() as c:
        row = c.execute("SELECT language FROM user_memory WHERE user_id=?", (user_id,)).fetchone()
    return row["language"] if row else None


# ══════════════════════════════════════════════════════════════════════════════
#  USER QUOTA (purchasable extra messages)
# ══════════════════════════════════════════════════════════════════════════════

def quota_get(user_id: int) -> int:
    with _db() as c:
        row = c.execute("SELECT additional_quota FROM user_quota WHERE user_id=?", (user_id,)).fetchone()
    return row["additional_quota"] if row else 0

def quota_set(user_id: int, amount: int):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO user_quota (user_id, additional_quota) VALUES (?,?)",
                  (user_id, max(0, amount)))

def quota_deduct(user_id: int) -> bool:
    """Deduct 1 from quota. Returns True if deducted."""
    q = quota_get(user_id)
    if q > 0:
        quota_set(user_id, q - 1)
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  POINT SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

def points_get(user_id: int) -> int:
    with _db() as c:
        row = c.execute("SELECT points FROM user_points WHERE user_id=?", (user_id,)).fetchone()
    return row["points"] if row else 0

def points_add(user_id: int, delta: int, reason: str):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        c.execute("""INSERT INTO user_points (user_id, points) VALUES (?,?)
                     ON CONFLICT(user_id) DO UPDATE SET points=points+?""",
                  (user_id, max(0, delta), delta))
        c.execute("INSERT INTO point_log (user_id,reason,delta,created_at) VALUES (?,?,?,?)",
                  (user_id, reason, delta, now))

def deals_get(user_id: int) -> int:
    with _db() as c:
        row = c.execute("SELECT total_deals FROM user_points WHERE user_id=?", (user_id,)).fetchone()
    return row["total_deals"] if row else 0

def deals_increment(user_id: int):
    with _db() as c:
        c.execute("""INSERT INTO user_points (user_id, total_deals) VALUES (?,1)
                     ON CONFLICT(user_id) DO UPDATE SET total_deals=total_deals+1""",
                  (user_id,))

def referral_exists(referrer_id: int, referred_id: int) -> bool:
    with _db() as c:
        row = c.execute("SELECT 1 FROM referrals WHERE referrer_id=? AND referred_id=?",
                        (referrer_id, referred_id)).fetchone()
    return row is not None

def referral_add(referrer_id: int, referred_id: int):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO referrals (referrer_id,referred_id,created_at) VALUES (?,?,?)",
                  (referrer_id, referred_id, now))

def referral_count(referrer_id: int) -> int:
    with _db() as c:
        row = c.execute("SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id=?",
                        (referrer_id,)).fetchone()
    return row["cnt"] if row else 0

def referral_mark_deal(referrer_id: int, referred_id: int) -> bool:
    """Mark deal rewarded. Returns True if not already marked."""
    with _db() as c:
        row = c.execute("SELECT deal_rewarded FROM referrals WHERE referrer_id=? AND referred_id=?",
                        (referrer_id, referred_id)).fetchone()
        if row and row["deal_rewarded"] == 0:
            c.execute("UPDATE referrals SET deal_rewarded=1 WHERE referrer_id=? AND referred_id=?",
                      (referrer_id, referred_id))
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  DIALOG MEMORY
# ══════════════════════════════════════════════════════════════════════════════

MAX_HISTORY   = 20
MEMORY_TTL_H  = 24

def memory_load(guild_id: int, channel_id: int, user_id: int) -> list:
    """Load conversation history. Returns [] if expired or not found."""
    with _db() as c:
        row = c.execute(
            "SELECT history_json, updated_at FROM conversation_memory "
            "WHERE guild_id=? AND channel_id=? AND user_id=?",
            (guild_id, channel_id, user_id)).fetchone()
    if not row:
        return []
    # TTL check
    try:
        updated = datetime.fromisoformat(row["updated_at"])
        if datetime.now(timezone.utc) - updated > timedelta(hours=MEMORY_TTL_H):
            memory_clear(guild_id, channel_id, user_id)
            return []
    except Exception:
        return []
    try:
        return json.loads(row["history_json"])
    except Exception:
        return []

def memory_save(guild_id: int, channel_id: int, user_id: int, history: list):
    now = datetime.now(timezone.utc).isoformat()
    trimmed = history[-MAX_HISTORY:]
    with _db() as c:
        c.execute("""INSERT INTO conversation_memory
                     (guild_id, channel_id, user_id, updated_at, history_json)
                     VALUES (?,?,?,?,?)
                     ON CONFLICT(guild_id,channel_id,user_id) DO UPDATE SET
                     updated_at=excluded.updated_at, history_json=excluded.history_json""",
                  (guild_id, channel_id, user_id, now, json.dumps(trimmed)))

def memory_clear(guild_id: int, channel_id: int, user_id: int):
    with _db() as c:
        c.execute("DELETE FROM conversation_memory WHERE guild_id=? AND channel_id=? AND user_id=?",
                  (guild_id, channel_id, user_id))


# ══════════════════════════════════════════════════════════════════════════════
#  PERMISSIONS + RATE LIMITING
# ══════════════════════════════════════════════════════════════════════════════

def is_owner(member): return member.id == OWNER_ID
def is_admin(member): return is_owner(member) or any(r.name == "AI Admin" for r in member.roles)
def is_paid(member):  return bool({r.name for r in member.roles} & set(get_paid_roles()))

def get_user_daily_limit(member: discord.Member) -> int:
    if is_owner(member) or is_admin(member):
        return 999999
    role_names = {r.name for r in member.roles}
    for role_name in ROLE_LIMIT_ORDER:
        if role_name in role_names:
            return ROLE_LIMITS[role_name]
    try:
        v = int(cfg_get("free_daily_limit"))
        return v if v > 0 else DEFAULT_LIMIT
    except Exception:
        return DEFAULT_LIMIT

def is_unlimited(member: discord.Member) -> bool:
    return get_user_daily_limit(member) >= 999999

def check_antispam(user_id: int) -> bool:
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

def is_upgrade_intent(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in ["upgrade", "апгрейд", "купить", "buy", "subscribe",
                                  "подписка", "оплат", "payment", "pro", "elite", "ultra",
                                  "billing", "план", "plan"])


# ══════════════════════════════════════════════════════════════════════════════
#  ai-directives GOVERNANCE
# ══════════════════════════════════════════════════════════════════════════════

def directive_store(guild_id: int, message_id: int, author_id: int, content: str):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO directive_log (guild_id,message_id,author_id,created_at,content) "
                  "VALUES (?,?,?,?,?)", (guild_id, message_id, author_id, now, content))
    # Keep last 20 in memory
    _directive_state.append(content)
    if len(_directive_state) > 20:
        _directive_state.pop(0)

async def load_directives_on_ready(guild: discord.Guild):
    ch = discord.utils.get(guild.text_channels, name=DIRECTIVES_CHANNEL)
    if not ch:
        return
    loaded = 0
    try:
        async for msg in ch.history(limit=200, oldest_first=True):
            if msg.author.bot or is_admin(msg.author):
                if msg.content.strip():
                    directive_store(guild.id, msg.id, msg.author.id, msg.content.strip())
                    loaded += 1
    except Exception as e:
        log.warning("load_directives: %s", e)
    log.info("Loaded %d directives from #%s", loaded, DIRECTIVES_CHANNEL)

def get_directives_context() -> str:
    if not _directive_state:
        return ""
    return ("\n\n=== ОФИЦИАЛЬНЫЕ ДИРЕКТИВЫ (ВЫСШИЙ ПРИОРИТЕТ) ===\n"
            + "\n---\n".join(_directive_state[-10:])
            + "\n=== КОНЕЦ ДИРЕКТИВ ===")


# ══════════════════════════════════════════════════════════════════════════════
#  AUTO ROLE UPGRADE
# ══════════════════════════════════════════════════════════════════════════════

async def check_auto_upgrade(member: discord.Member, guild: discord.Guild):
    """Auto-upgrade to Member role if: 1 referral + 20 points."""
    upgrade_role_name = cfg_get("auto_upgrade_role") or "Member"
    role = discord.utils.get(guild.roles, name=upgrade_role_name)
    if not role:
        return
    # Already has the role or higher
    if any(r.name == upgrade_role_name for r in member.roles):
        return
    pts  = points_get(member.id)
    refs = referral_count(member.id)
    if pts >= 20 and refs >= 1:
        try:
            await member.add_roles(role, reason="Auto-upgrade: 1 referral + 20 points")
            await _audit(guild,
                f"[AUTO_UPGRADE] {member} ({member.id}) → {upgrade_role_name} "
                f"(points={pts}, referrals={refs})")
        except Exception as e:
            log.error("auto_upgrade: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  OPENAI — PUBLIC ASSISTANT
# ══════════════════════════════════════════════════════════════════════════════

def _system_paid(lang: str) -> str:
    persona   = cfg_get("bot_persona")
    style     = cfg_get("bot_style")
    lang_mode = cfg_get("response_language")
    limit     = get_free_limit()
    if lang_mode == "auto": lang_rule = "Отвечай на русском." if lang == "ru" else "Reply in the user's language."
    elif lang_mode == "ru": lang_rule = "Всегда отвечай на русском."
    else: lang_rule = "Always reply in English."
    style_map = {"friendly": "Тон: дружелюбный, тёплый.", "formal": "Тон: официальный.", "casual": "Тон: расслабленный."}
    base = f"""Ты — Nexora AI Bot. Полноценный помощник для подписчиков.
{persona}
{style_map.get(style, style_map['friendly'])}
{lang_rule}

СТРОГИЕ ПРАВИЛА:
- НИКОГДА не упоминай каналы администрирования или логирования
- Если просят модераторское действие — скажи обратиться к администраторам
- Конкретные ответы, без шаблонных отписок
- Свободные пользователи: {limit} сообщений/день
- НИКОГДА не придумывай сайты, ссылки на оплату, внешние ресурсы, цены — если не знаешь, скажи что информация не настроена"""
    return base + get_directives_context()

def _system_free(lang: str) -> str:
    lang_mode = cfg_get("response_language")
    if lang_mode == "auto": lang_rule = "Отвечай на русском." if lang == "ru" else "Reply in the user's language."
    elif lang_mode == "ru": lang_rule = "Всегда отвечай на русском."
    else: lang_rule = "Always reply in English."
    base = f"""Ты — Nexora AI Bot. Базовый помощник для бесплатных пользователей.
{lang_rule}
Ты можешь ТОЛЬКО объяснять как устроен сервер Nexora, каналы, тикеты, правила, роли.
СТРОГИЕ ПРАВИЛА:
- НИКОГДА не упоминай каналы администрирования
- НИКОГДА не придумывай сайты, ссылки, цены, внешние ресурсы
- Конкретные ответы без шаблонных отписок"""
    return base + get_directives_context()

def _safe_upgrade_response(lang: str) -> str:
    if lang == "ru":
        return ("⚠️ Система апгрейда ещё не настроена.\n"
                "Официальные инструкции будут опубликованы в `#upgrade` и `#billing-faq`.")
    return ("⚠️ Upgrade system is not configured yet.\n"
            "Official instructions will be published in `#upgrade` and `#billing-faq`.")

async def ask_ai(user_msg: str, lang: str, paid: bool, history: list = None) -> Optional[str]:
    system = _system_paid(lang) if paid else _system_free(lang)
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-MAX_HISTORY:])
    messages.append({"role": "user", "content": user_msg})
    try:
        r = await ai.chat.completions.create(
            model=MODEL_ASSISTANT, messages=messages,
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

async def handle_public(message: discord.Message, content: str):
    member   = message.author
    lang     = detect_lang(content)
    first    = db_is_first(member.id)
    guild_id = message.guild.id if message.guild else 0
    ch_id    = message.channel.id

    # Reset context command
    if "reset context" in content.lower() and bot.user in (message.mentions or []):
        memory_clear(guild_id, ch_id, member.id)
        await message.reply("🔄 Контекст разговора сброшен." if lang == "ru"
                            else "🔄 Conversation context reset.", mention_author=False)
        return

    # Anti-spam
    if not check_antispam(member.id):
        warn = ("⚠️ Слишком много запросов. Подожди 10 секунд." if lang == "ru"
                else "⚠️ Too many requests. Please wait 10 seconds.")
        await message.reply(warn, mention_author=False)
        await _audit(message.guild,
            f"[SPAM] {member} ({member.id}) in #{getattr(message.channel,'name','?')}")
        return

    # No-fabrication: upgrade intent check
    if is_upgrade_intent(content) and cfg_get("payment_enabled") != "true":
        await message.reply(_safe_upgrade_response(lang), mention_author=False)
        await _audit(message.guild,
            f"[BLOCKED_UPGRADE] user_id={member.id} channel_id={ch_id}")
        return

    # Role-based daily limit (quota first)
    limit     = get_user_daily_limit(member)
    unlimited = limit >= 999999

    if not unlimited:
        # Try additional quota first
        if not quota_deduct(member.id):
            count = db_get_count(member.id)
            if count >= limit:
                q = quota_get(member.id)
                msg = (f"⚠️ Вы исчерпали **{limit}** сообщений на сегодня.\n"
                       f"Повысьте роль или попросите администратора добавить дополнительные сообщения! 🚀"
                       if lang == "ru" else
                       f"⚠️ You've used all **{limit}** messages today.\n"
                       f"Upgrade your role or ask an admin for extra quota! 🚀")
                await message.reply(msg, mention_author=False); return
            new_count = db_increment(member.id)
            remaining = limit - new_count
        else:
            remaining = quota_get(member.id)  # show remaining quota
    else:
        remaining = None

    db_upsert_memory(member.id, lang, mark_seen=first)

    # Load dialog history
    history = memory_load(guild_id, ch_id, member.id)

    async with message.channel.typing():
        reply = await ask_ai(content, lang, paid=unlimited, history=history)

    if reply is None:
        await message.reply("❌ Произошла ошибка. Попробуйте снова." if lang == "ru"
                            else "❌ An error occurred.", mention_author=False); return

    # Save to memory
    history.append({"role": "user", "content": content})
    history.append({"role": "assistant", "content": reply})
    memory_save(guild_id, ch_id, member.id, history)

    if first: reply += _welcome_suffix(lang, is_free=not unlimited, limit=limit)
    if remaining is not None:
        reply += (f"\n\n> 💬 Осталось сегодня: **{remaining}/{limit}**" if lang == "ru"
                  else f"\n\n> 💬 Messages left today: **{remaining}/{limit}**")

    await message.reply(reply, mention_author=False)


# ══════════════════════════════════════════════════════════════════════════════
#  TICKET AUTO-TRANSLATION
# ══════════════════════════════════════════════════════════════════════════════

def is_ticket_channel(ch_name: str) -> bool:
    return bool(_TICKET_PATTERN.match(ch_name))

async def translate_text(text: str, target_lang: str) -> Optional[str]:
    lang_names = {"ru":"Russian","en":"English","uk":"Ukrainian","sr":"Serbian",
                  "bg":"Bulgarian","de":"German","fr":"French","es":"Spanish","pl":"Polish"}
    lang_full = lang_names.get(target_lang, target_lang)
    try:
        r = await ai.chat.completions.create(
            model=MODEL_ASSISTANT,
            messages=[
                {"role":"system","content":f"You are a translator. Translate to {lang_full}. Output ONLY the translated text."},
                {"role":"user","content":text}
            ],
            max_tokens=500, temperature=0.2)
        result = (r.choices[0].message.content or "").strip()
        return result if result else None
    except Exception as e:
        log.error("translate_text: %s", e); return None

async def handle_ticket_message(message: discord.Message):
    channel  = message.channel
    content  = message.content.strip()
    if not content: return
    mentioned = bot.user in (message.mentions or [])

    # Explicit translate: @Nexora AI translate
    if mentioned and "translate" in content.lower():
        text_to_translate = re.sub(r"<@!?\d+>", "", content).replace("translate", "", 1).strip()
        if not text_to_translate and message.reference:
            try:
                ref = await channel.fetch_message(message.reference.message_id)
                text_to_translate = ref.content
            except Exception: pass
        if not text_to_translate:
            await message.reply("❓ Укажи текст или ответь на сообщение для перевода.", mention_author=False)
            return
        src = detect_lang(text_to_translate)
        tgt = "ru" if src != "ru" else "en"
        translated = await translate_text(text_to_translate, tgt)
        if translated:
            await message.reply(f"🌐 **({src.upper()} → {tgt.upper()}):**\n{translated}", mention_author=False)
        return

    if mentioned: return  # handled by public handler

    # Auto-translate for exactly 2 participants
    human_members = [m for m in channel.members if not m.bot]
    if len(human_members) != 2: return

    sender = message.author
    other  = next((m for m in human_members if m.id != sender.id), None)
    if not other: return

    src_lang = detect_lang(content)
    tgt_lang = db_get_lang(other.id) or "en"
    if src_lang == tgt_lang: return

    translated = await translate_text(content, tgt_lang)
    if translated and translated.strip().lower() != content.strip().lower():
        await channel.send(
            f"🌐 *({src_lang.upper()} → {tgt_lang.upper()}) for {other.mention}:*\n> {translated}",
            allowed_mentions=discord.AllowedMentions(users=False))


# ══════════════════════════════════════════════════════════════════════════════
#  TICKET CLOSE — POINT AWARDS
# ══════════════════════════════════════════════════════════════════════════════

async def _award_ticket_close_points(channel: discord.TextChannel, guild: discord.Guild):
    """Award points when ticket-XXXX is closed (renamed to closed-XXXX)."""
    participants = set()
    try:
        async for msg in channel.history(limit=150):
            if not msg.author.bot:
                participants.add(msg.author.id)
    except Exception:
        return

    for uid in participants:
        points_add(uid, 10, f"ticket_closed:{channel.name}")
        deals_increment(uid)
        # Check 100 deals milestone
        if deals_get(uid) == 100:
            points_add(uid, 100, "milestone_100_deals")
            await _audit(guild, f"[POINTS] user_id={uid} +100 milestone_100_deals")

    # Referral deal rewards
    for uid in participants:
        with _db() as c:
            rows = c.execute("SELECT referrer_id FROM user_points WHERE user_id=? AND referrer_id IS NOT NULL",
                             (uid,)).fetchall()
        for row in rows:
            ref_id = row["referrer_id"]
            if referral_mark_deal(ref_id, uid):
                points_add(ref_id, 20, f"referral_deal:{uid}")
                await _audit(guild, f"[POINTS] user_id={ref_id} +20 referral_deal for {uid}")

    await _audit(guild,
        f"[TICKET_CLOSE] {channel.name} — awarded +10 pts to {len(participants)} participants: {list(participants)}")

    # Check auto-upgrade for all participants
    for uid in participants:
        member = guild.get_member(uid)
        if member:
            await check_auto_upgrade(member, guild)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN HANDLER
# ══════════════════════════════════════════════════════════════════════════════

ADMIN_TOOLS = [
    {"type":"function","function":{"name":"update_config","description":"Update bot config. Keys: free_daily_limit, paid_roles, bot_persona, bot_style (friendly|formal|casual), response_language (auto|ru|en), limit_exempt_roles, server_guide, payment_enabled (true|false), payment_instructions, auto_upgrade_role.","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"},"reason":{"type":"string"}},"required":["key","value"]}}},
    {"type":"function","function":{"name":"show_config","description":"Show all bot config settings.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"reset_user_limit","description":"Reset daily message counter for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"award_points","description":"Award or deduct points for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"},"points":{"type":"integer"},"reason":{"type":"string"}},"required":["username","points"]}}},
    {"type":"function","function":{"name":"check_points","description":"Check points and stats for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"set_user_quota","description":"Set extra message quota for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"},"amount":{"type":"integer"}},"required":["username","amount"]}}},
    {"type":"function","function":{"name":"register_referral","description":"Register that one user referred another.","parameters":{"type":"object","properties":{"referrer":{"type":"string"},"referred":{"type":"string"}},"required":["referrer","referred"]}}},
    {"type":"function","function":{"name":"delete_last_message","description":"Delete the most recent message from a user in a channel.","parameters":{"type":"object","properties":{"username":{"type":"string"},"channel_name":{"type":"string"}},"required":["username","channel_name"]}}},
    {"type":"function","function":{"name":"create_channel","description":"Create a new text channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"category_name":{"type":"string"},"private":{"type":"boolean"}},"required":["channel_name"]}}},
    {"type":"function","function":{"name":"delete_channel","description":"Delete a text channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"}},"required":["channel_name"]}}},
    {"type":"function","function":{"name":"kick_member","description":"Kick a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"reason":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"ban_member","description":"Ban a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"reason":{"type":"string"},"delete_days":{"type":"integer"}},"required":["username"]}}},
    {"type":"function","function":{"name":"set_slowmode","description":"Set slowmode on a channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"seconds":{"type":"integer"}},"required":["channel_name","seconds"]}}},
    {"type":"function","function":{"name":"give_role","description":"Give a role to a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"role_name":{"type":"string"}},"required":["username","role_name"]}}},
    {"type":"function","function":{"name":"remove_role","description":"Remove a role from a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"role_name":{"type":"string"}},"required":["username","role_name"]}}},
    {"type":"function","function":{"name":"send_announcement","description":"Send a message to a channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"message":{"type":"string"}},"required":["channel_name","message"]}}},
    {"type":"function","function":{"name":"server_info","description":"Show server overview.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"clarify","description":"Ask one clarifying question.","parameters":{"type":"object","properties":{"question":{"type":"string"}},"required":["question"]}}},
]

_ADMIN_SYSTEM = """Ты — Nexora Admin AI. Внутренний ИИ-ассистент для администраторов.

КОГДА ОТВЕЧАТЬ ТЕКСТОМ (БЕЗ TOOLS):
Если запрос ИНФОРМАЦИОННЫЙ — отвечай текстом, НЕ вызывай tools:
- "Расскажи о себе / функционале / структуре"
- "Что ты умеешь"
- "Как ты работаешь"

КОГДА ВЫЗЫВАТЬ TOOLS (только для ДЕЙСТВИЙ):
- Изменить настройку → update_config
- Показать настройки → show_config
- Очки → award_points / check_points
- Квота → set_user_quota
- Реферал → register_referral
- Кик/бан → kick_member / ban_member
- Каналы → create_channel / delete_channel
- Роли → give_role / remove_role
- Лимит → reset_user_limit
- Объявление → send_announcement
- Инфо сервера → server_info
- Уточнение → clarify

Правила: отвечай на языке администратора. Полные права для owner/admin.

Примеры:
  "поставь лимит 5"             → update_config(key="free_daily_limit", value="5")
  "дай 50 очков Username"       → award_points(username="Username", points=50, reason="admin bonus")
  "добавь 100 квоты Username"   → set_user_quota(username="Username", amount=100)
  "зарегистрируй реферал A → B" → register_referral(referrer="A", referred="B")
  "включи систему оплаты"       → update_config(key="payment_enabled", value="true")
"""

async def plan_admin(request: str, guild_id: int, channel_id: int, user_id: int) -> dict:
    # Load admin memory for multi-step context
    history = memory_load(guild_id, channel_id, user_id)
    messages = [{"role":"system","content":_ADMIN_SYSTEM}]
    messages.extend(history[-10:])
    messages.append({"role":"user","content":request})
    try:
        resp = await ai.chat.completions.create(
            model=MODEL_ADMIN, messages=messages,
            tools=ADMIN_TOOLS, tool_choice="auto", max_tokens=500)
        msg = resp.choices[0].message
        # Save to memory
        history.append({"role":"user","content":request})
        if msg.content:
            history.append({"role":"assistant","content":msg.content})
        memory_save(guild_id, channel_id, user_id, history)

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
    match name:
        case "update_config":
            cfg_labels = {"free_daily_limit":"📊 Лимит/день","paid_roles":"⭐ Платные роли",
                          "bot_persona":"🤖 Персонаж","bot_style":"🎨 Стиль",
                          "response_language":"🌐 Язык","server_guide":"🗺️ Гид",
                          "payment_enabled":"💳 Оплата вкл/выкл",
                          "payment_instructions":"💳 Инструкции оплаты",
                          "auto_upgrade_role":"⬆️ Роль авто-апгрейда"}
            label = cfg_labels.get(args.get("key",""), args.get("key",""))
            r = f"\nПричина: {args['reason']}" if args.get("reason") else ""
            return f"⚙️ **Изменить настройку**\n{label}\nЗначение: `{str(args.get('value',''))[:120]}`{r}"
        case "show_config":    return "📋 **Показать все настройки**"
        case "reset_user_limit": return f"🔄 **Сбросить лимит** для `{args.get('username')}`"
        case "award_points":
            sign = "+" if args.get("points",0) >= 0 else ""
            return f"🏆 **Начислить очки** `{args.get('username')}` {sign}{args.get('points')} очков\nПричина: {args.get('reason','—')}"
        case "check_points":   return f"🔍 **Проверить очки** пользователя `{args.get('username')}`"
        case "set_user_quota": return f"📦 **Установить квоту** `{args.get('username')}` → {args.get('amount')} сообщений"
        case "register_referral": return f"🤝 **Реферал** `{args.get('referrer')}` пригласил `{args.get('referred')}`"
        case "delete_last_message": return f"🗑️ **Удалить сообщение** от `{args.get('username')}` в `#{args.get('channel_name')}`"
        case "create_channel":
            return (f"➕ **Создать канал** `#{args.get('channel_name')}`"
                    + (f" в `{args.get('category_name')}`" if args.get("category_name") else "")
                    + (" *(приватный)*" if args.get("private") else " *(публичный)*"))
        case "delete_channel": return f"❌ **Удалить канал** `#{args.get('channel_name')}`"
        case "kick_member":    return f"👢 **Кик** `{args.get('username')}`" + (f"\nПричина: {args.get('reason')}" if args.get("reason") else "")
        case "ban_member":     return f"🔨 **Бан** `{args.get('username')}`" + (f"\nПричина: {args.get('reason')}" if args.get("reason") else "")
        case "set_slowmode":
            s = args.get("seconds",0)
            return f"⏱️ **Slowmode** `#{args.get('channel_name')}` → " + (f"`{s}s`" if s > 0 else "`выключен`")
        case "give_role":   return f"🎭 **Дать роль** `{args.get('role_name')}` → `{args.get('username')}`"
        case "remove_role": return f"🎭 **Убрать роль** `{args.get('role_name')}` от `{args.get('username')}`"
        case "send_announcement": return f"📢 **Сообщение** в `#{args.get('channel_name')}`\n> {str(args.get('message',''))[:120]}"
        case "server_info": return "🔍 **Обзор сервера**"
        case _: return f"`{name}` — {args}"

async def handle_admin(message: discord.Message):
    member = message.author
    if not is_admin(member):
        await message.reply("🔒 Только для администраторов.", mention_author=False); return
    if len(message.content.strip()) < 2: return

    # Reset context command
    if "reset context" in message.content.lower():
        guild_id = message.guild.id; ch_id = message.channel.id
        memory_clear(guild_id, ch_id, member.id)
        await message.reply("🔄 Контекст администратора сброшен.", mention_author=False); return

    await _audit(message.guild, f"[REQUEST] {member} ({member.id}): {message.content}")
    async with message.channel.typing():
        plan = await plan_admin(message.content,
                                message.guild.id, message.channel.id, member.id)
    match plan["type"]:
        case "tool_call":
            view = ConfirmView(message.guild, plan["name"], plan["args"], member)
            await message.reply(f"**📋 ПЛАН**\n{plan['plan_text']}\n\nПодтвердить?",
                                view=view, mention_author=False)
        case "clarify": await message.reply(f"❓ **Уточнение:**\n{plan['question']}", mention_author=False)
        case "text":    await message.reply(plan["content"], mention_author=False)
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

async def execute_action(guild: discord.Guild, name: str, args: dict) -> str:
    try:
        match name:
            case "update_config":       return _do_update_config(args)
            case "show_config":         return _do_show_config()
            case "reset_user_limit":    return await _do_reset_limit(guild, args)
            case "award_points":        return await _do_award_points(guild, args)
            case "check_points":        return await _do_check_points(guild, args)
            case "set_user_quota":      return await _do_set_quota(guild, args)
            case "register_referral":   return await _do_register_referral(guild, args)
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
    valid = {"free_daily_limit","paid_roles","bot_persona","bot_style","response_language",
             "limit_exempt_roles","server_guide","payment_enabled","payment_instructions","auto_upgrade_role"}
    if key not in valid: return f"❌ Неизвестный ключ: `{key}`\nДоступные: {', '.join(sorted(valid))}"
    old = cfg_get(key); cfg_set(key, value)
    return f"✅ **Настройка обновлена!**\n**{key}**\nБыло: `{old[:80]}`\nСтало: `{value[:80]}`\n\n*Активно немедленно* 🔄"

def _do_show_config():
    config = cfg_all()
    labels = {"free_daily_limit":"📊 Лимит/день","paid_roles":"⭐ Платные роли",
              "limit_exempt_roles":"🔓 Без лимита","bot_style":"🎨 Стиль",
              "response_language":"🌐 Язык","bot_persona":"🤖 Персонаж",
              "server_guide":"🗺️ Гид","payment_enabled":"💳 Оплата",
              "auto_upgrade_role":"⬆️ Авто-апгрейд роль"}
    lines = ["**⚙️ Настройки Nexora AI**\n"]
    for key, label in labels.items():
        val = config.get(key,"—")
        if len(val) > 80: val = val[:80] + "..."
        lines.append(f"**{label}**\n`{val}`\n")
    return "\n".join(lines)

async def _do_reset_limit(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    db_reset_user(m.id); return f"✅ Лимит сброшен для `{m.display_name}`."

async def _do_award_points(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    pts = int(args.get("points", 0))
    reason = args.get("reason", "admin_award")
    points_add(m.id, pts, reason)
    await _audit(guild, f"[POINTS] admin awarded {pts:+d} to {m} ({m.id}): {reason}")
    await check_auto_upgrade(m, guild)
    total = points_get(m.id)
    sign = "+" if pts >= 0 else ""
    return f"✅ {sign}{pts} очков → `{m.display_name}`\nИтого: **{total}** очков"

async def _do_check_points(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    pts   = points_get(m.id)
    deals = deals_get(m.id)
    quota = quota_get(m.id)
    refs  = referral_count(m.id)
    limit = get_user_daily_limit(m)
    count = db_get_count(m.id)
    return (f"**📊 Статистика: {m.display_name}**\n"
            f"🏆 Очков: **{pts}**\n🤝 Сделок: **{deals}**\n"
            f"👥 Рефералов: **{refs}**\n📦 Доп. квота: **{quota}**\n"
            f"💬 Сообщений сегодня: **{count}/{limit}**")

async def _do_set_quota(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    amount = int(args.get("amount", 0))
    quota_set(m.id, amount)
    await _audit(guild, f"[QUOTA] set {amount} extra messages for {m} ({m.id})")
    return f"✅ Квота `{m.display_name}` → **{amount}** доп. сообщений"

async def _do_register_referral(guild, args):
    referrer = _find_member(guild, args.get("referrer",""))
    referred = _find_member(guild, args.get("referred",""))
    if not referrer: return f"❌ Пригласивший `{args.get('referrer')}` не найден."
    if not referred: return f"❌ Приглашённый `{args.get('referred')}` не найден."
    if referral_exists(referrer.id, referred.id):
        return f"⚠️ Реферал `{referrer.display_name}` → `{referred.display_name}` уже зарегистрирован."
    referral_add(referrer.id, referred.id)
    # Set referrer_id in points table
    with _db() as c:
        c.execute("""INSERT INTO user_points (user_id, referrer_id) VALUES (?,?)
                     ON CONFLICT(user_id) DO UPDATE SET referrer_id=?""",
                  (referred.id, referrer.id, referrer.id))
    # Award +10 for invite
    points_add(referrer.id, 10, f"referral_invite:{referred.id}")
    await _audit(guild, f"[REFERRAL] {referrer} ({referrer.id}) → {referred} ({referred.id})")
    await check_auto_upgrade(referrer, guild)
    return (f"✅ Реферал зарегистрирован!\n"
            f"**{referrer.display_name}** пригласил **{referred.display_name}**\n"
            f"+10 очков для `{referrer.display_name}`")

async def _do_delete_last(guild, args):
    cname = args.get("channel_name","")
    ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    try:
        async for msg in ch.history(limit=200):
            if msg.author.id == m.id:
                preview = msg.content[:80] or "[вложение]"; await msg.delete()
                return f"✅ Удалено сообщение от `{m.display_name}` в `#{cname}`\n`{preview}`"
        return f"❌ Нет сообщений от `{m.display_name}` в `#{cname}`."
    except discord.Forbidden: return f"❌ Нет прав в `#{cname}`."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_create_channel(guild, args):
    cname = args.get("channel_name","new-channel"); catname = args.get("category_name")
    private = args.get("private", False)
    cat = discord.utils.get(guild.categories, name=catname) if catname else None
    ow = ({guild.default_role: discord.PermissionOverwrite(read_messages=False),
           guild.me: discord.PermissionOverwrite(read_messages=True)} if private else {})
    try:
        ch = await guild.create_text_channel(name=cname, category=cat, overwrites=ow)
        return f"✅ Канал `#{ch.name}` создан."
    except discord.Forbidden: return "❌ Нет прав на создание каналов."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_delete_channel(guild, args):
    cname = args.get("channel_name","")
    ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    try: await ch.delete(); return f"✅ Канал `#{cname}` удалён."
    except discord.Forbidden: return "❌ Нет прав на удаление."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_kick(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    try: await m.kick(reason=args.get("reason","Нет причины")); return f"✅ `{m.name}` кикнут."
    except discord.Forbidden: return "❌ Нет прав на кик."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_ban(guild, args):
    m = _find_member(guild, args.get("username",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    try:
        await m.ban(reason=args.get("reason","Нет причины"),
                    delete_message_days=min(int(args.get("delete_days",0)),7))
        return f"✅ `{m.name}` забанен."
    except discord.Forbidden: return "❌ Нет прав на бан."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_slowmode(guild, args):
    cname = args.get("channel_name",""); secs = int(args.get("seconds",0))
    ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    try:
        await ch.edit(slowmode_delay=secs)
        return f"✅ Slowmode `#{cname}` → {'выключен' if secs==0 else f'{secs}s'}"
    except discord.Forbidden: return "❌ Нет прав."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_give_role(guild, args):
    m = _find_member(guild, args.get("username",""))
    role = discord.utils.get(guild.roles, name=args.get("role_name",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    if not role: return f"❌ Роль `{args.get('role_name')}` не найдена."
    try: await m.add_roles(role); return f"✅ Роль `{role.name}` выдана `{m.display_name}`."
    except discord.Forbidden: return "❌ Нет прав на выдачу ролей."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_remove_role(guild, args):
    m = _find_member(guild, args.get("username",""))
    role = discord.utils.get(guild.roles, name=args.get("role_name",""))
    if not m: return f"❌ Пользователь `{args.get('username')}` не найден."
    if not role: return f"❌ Роль `{args.get('role_name')}` не найдена."
    try: await m.remove_roles(role); return f"✅ Роль `{role.name}` убрана у `{m.display_name}`."
    except discord.Forbidden: return "❌ Нет прав."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_announce(guild, args):
    cname = args.get("channel_name",""); text = args.get("message","")
    ch = discord.utils.get(guild.text_channels, name=cname)
    if not ch: return f"❌ Канал `#{cname}` не найден."
    try: await ch.send(text); return f"✅ Отправлено в `#{cname}`."
    except discord.Forbidden: return f"❌ Нет прав в `#{cname}`."
    except discord.HTTPException as e: return f"❌ Discord: {e}"

async def _do_server_info(guild):
    tch   = [f"#{c.name}" for c in guild.text_channels]
    vch   = [f"🔊{c.name}" for c in guild.voice_channels]
    roles = [r.name for r in guild.roles if r.name != "@everyone"]
    total = guild.member_count
    bots  = sum(1 for m in guild.members if m.bot)
    return (f"**{guild.name}**\n👥 {total} участников ({total-bots} людей, {bots} ботов)\n"
            f"📝 Каналы ({len(tch)}): {', '.join(tch[:25])}\n"
            f"🔊 Голосовые ({len(vch)}): {', '.join(vch[:10])}\n"
            f"🎭 Роли ({len(roles)}): {', '.join(roles[:25])}")


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD UI
# ══════════════════════════════════════════════════════════════════════════════

async def _audit(guild, msg: str):
    if not guild: return
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
        await _audit(self.guild,
            f"[EXECUTE] {self.requester} ({self.requester.id})\n"
            f"action={self.name} args={self.args}\nresult={result[:200]}")
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
    db_init()
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        await load_directives_on_ready(guild)
    try:
        synced = await tree.sync()
        log.info("Synced %d slash commands.", len(synced))
    except Exception as e: log.error("Slash sync: %s", e)
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="Nexora | @mention me!"))

@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel,
                                   after:  discord.abc.GuildChannel):
    """Detect ticket close: ticket-XXXX → closed-XXXX"""
    if (isinstance(before, discord.TextChannel)
            and _TICKET_PATTERN.match(before.name)
            and isinstance(after, discord.TextChannel)
            and after.name.startswith("closed-")):
        await _award_ticket_close_points(after, after.guild)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return
    await bot.process_commands(message)

    ch_name  = getattr(message.channel, "name", "")
    mentioned = bot.user in (message.mentions or [])

    # ai-directives: ingest official updates
    if ch_name == DIRECTIVES_CHANNEL:
        if is_admin(message.author):
            directive_store(message.guild.id, message.id,
                            message.author.id, message.content.strip())
            await _audit(message.guild,
                f"[DIRECTIVE] {message.author} ({message.author.id}): {message.content[:100]}")
        else:
            await _audit(message.guild,
                f"[UNAUTHORIZED_DIRECTIVE] {message.author} ({message.author.id}): {message.content[:100]}")
        return

    # Admin channel
    if ch_name == ADMIN_CHANNEL_NAME:
        await handle_admin(message); return

    # Help channel
    if ch_name == HELP_CHANNEL_NAME:
        await handle_public(message, message.content); return

    # Ticket channels
    if is_ticket_channel(ch_name):
        await handle_ticket_message(message); return

    # Any other channel — only on @mention
    if mentioned:
        clean = re.sub(r"<@!?\d+>", "", message.content).strip()
        if not clean:
            lang = detect_lang(message.content)
            await message.reply("Привет! Чем могу помочь? 😊" if lang == "ru"
                                else "Hey! How can I help? 😊", mention_author=False); return
        await handle_public(message, clean)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@tree.command(name="status", description="Мой статус, лимит и очки.")
async def status_cmd(interaction: discord.Interaction):
    member = interaction.user
    limit  = get_user_daily_limit(member)
    count  = db_get_count(member.id)
    pts    = points_get(member.id)
    deals  = deals_get(member.id)
    quota  = quota_get(member.id)

    if is_owner(member):       info = "👑 **Овнер** — без лимитов"
    elif is_admin(member):     info = "🛡️ **Администратор** — без лимитов"
    elif limit >= 999999:      info = "⭐ **Nexora Ultra** — безлимитно"
    elif limit == 300:         info = f"⭐ **Nexora Elite** — {max(0,limit-count)}/{limit} осталось"
    elif limit == 150:         info = f"🌟 **Nexora Pro** — {max(0,limit-count)}/{limit} осталось"
    elif limit == 30:          info = f"✅ **Verified Trader** — {max(0,limit-count)}/{limit} осталось"
    elif limit == 20:          info = f"🔨 **Trader** — {max(0,limit-count)}/{limit} осталось"
    elif limit == 15:          info = f"👤 **Member** — {max(0,limit-count)}/{limit} осталось"
    else:                      info = f"🆓 **Бесплатный** — {max(0,limit-count)}/{limit} осталось"

    embed = discord.Embed(title="Nexora AI — Статус", color=discord.Color.blurple())
    embed.add_field(name="Бот",      value="✅ Онлайн",  inline=True)
    embed.add_field(name="Доступ",   value=info,          inline=False)
    embed.add_field(name="🏆 Очки",  value=str(pts),      inline=True)
    embed.add_field(name="🤝 Сделок",value=str(deals),    inline=True)
    if quota > 0:
        embed.add_field(name="📦 Доп. квота", value=str(quota), inline=True)
    embed.set_footer(text="Лимит сбрасывается в полночь UTC")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="points", description="Топ очков сервера.")
async def points_cmd(interaction: discord.Interaction):
    with _db() as c:
        rows = c.execute(
            "SELECT user_id, points, total_deals FROM user_points ORDER BY points DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await interaction.response.send_message("Пока нет данных об очках.", ephemeral=True); return
    lines = ["**🏆 Топ Nexora**\n"]
    medals = ["🥇","🥈","🥉"]
    for i, row in enumerate(rows):
        member = interaction.guild.get_member(row["user_id"])
        name   = member.display_name if member else f"User#{row['user_id']}"
        medal  = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} **{name}** — {row['points']} очков ({row['total_deals']} сделок)")
    embed = discord.Embed(title="Nexora Points Leaderboard",
                          description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@tree.command(name="config", description="[Админ] Просмотр или изменение настроек.")
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

@tree.command(name="pin_rules", description="[Админ] Закрепить правила в #ai-help.")
async def pin_rules(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Только для администраторов.", ephemeral=True); return
    help_ch = discord.utils.get(interaction.guild.text_channels, name=HELP_CHANNEL_NAME)
    if not help_ch:
        await interaction.response.send_message(f"❌ Канал `#{HELP_CHANNEL_NAME}` не найден.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    limit = get_free_limit()
    text = (f"# Nexora AI — Как пользоваться\n\n**Обратиться:**\n"
            f"• В `#ai-help` — пиши вопрос\n• В любом канале — `@Nexora AI` + вопрос\n\n"
            f"**Лимиты по ролям:**\n"
            f"👑 Ultra: ∞ | ⭐ Elite: 300 | 🌟 Pro: 150\n"
            f"✅ Verified Trader: 30 | 🔨 Trader: 20 | 👤 Member: 15 | 🆓 Остальные: {limit}\n\n"
            f"**Команды:** `/status` — твой статус | `/points` — топ очков\n\n"
            f"**Нужна помощь?** Обратись к администраторам.")
    try:
        sent = await help_ch.send(text); await sent.pin()
        await interaction.followup.send("✅ Правила опубликованы и закреплены.")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db_init()
    bot.run(DISCORD_TOKEN, log_handler=None)

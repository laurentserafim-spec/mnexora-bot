"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                     NEXORA DISCORD AI BOT v4.1 (PATCH)                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  REQUIRED ENV:  DISCORD_TOKEN  OPENAI_API_KEY  OWNER_ID                     ║
║  OPTIONAL ENV:  ADMIN_CHANNEL_NAME  HELP_CHANNEL_NAME  AUDIT_CHANNEL_NAME   ║
║                 DIRECTIVES_CHANNEL  FREE_DAILY_LIMIT  PAID_ROLES            ║
║                 MODEL_ASSISTANT  MODEL_ADMIN                                  ║
║                                                                              ║
║  PATCH v4.1 (NO REWRITE):                                                    ║
║   ✅ Mention-only replies everywhere (no auto replies), except ticket translate
║   ✅ Send+Pin race fix with retries + per-channel locks                       ║
║   ✅ Dynamic channel resolution (id / mention / name + category preference)  ║
║   ✅ Guild channel cache refresh on ready/create/update/delete                ║
║   ✅ Strict error logging to ai-audit-log (no silent fails)                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, re, json, logging, sqlite3, asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

import discord
from discord import app_commands
from discord.ext import commands
from openai import AsyncOpenAI

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("nexora")

# ──────────────────────────────────────────────────────────────────────────────
# ENV
# ──────────────────────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ["DISCORD_TOKEN"]
OPENAI_API_KEY       = os.environ["OPENAI_API_KEY"]
OWNER_ID             = int(os.environ.get("OWNER_ID", "0"))

ADMIN_CHANNEL_NAME   = os.environ.get("ADMIN_CHANNEL_NAME",   "ai-admin")
HELP_CHANNEL_NAME    = os.environ.get("HELP_CHANNEL_NAME",    "ai-help")        # kept for naming only; NO auto replies
AUDIT_CHANNEL_NAME   = os.environ.get("AUDIT_CHANNEL_NAME",   "ai-audit-log")
DIRECTIVES_CHANNEL   = os.environ.get("DIRECTIVES_CHANNEL",   "ai-directives")

MODEL_ASSISTANT      = os.environ.get("MODEL_ASSISTANT", "gpt-4o")
MODEL_ADMIN          = os.environ.get("MODEL_ADMIN",     "gpt-4o")

DB_PATH              = "nexora.sqlite3"

# Minimal denylist for system/internal channels (still allow admin logic there)
DENY_PUBLIC_CHANNELS = {AUDIT_CHANNEL_NAME, DIRECTIVES_CHANNEL}

# Regex helpers
_HIDDEN_PATTERN = re.compile(
    r"#?\b(ai[-_]?admin|ai[-_]?audit[-_]?log|audit[-_]?log"
    r"|admin\s*channel|internal\s*admin|admin\s*process|ai[-_]?directives)\b",
    re.IGNORECASE,
)
_TICKET_PATTERN = re.compile(r'^ticket-\d+$', re.IGNORECASE)
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
_USER_MENTION_RE    = re.compile(r"<@!?\d+>")

# ──────────────────────────────────────────────────────────────────────────────
# DISCORD + OPENAI
# ──────────────────────────────────────────────────────────────────────────────
ai = AsyncOpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.guilds          = True
intents.message_content = True
intents.members         = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ──────────────────────────────────────────────────────────────────────────────
# ROLE LIMITS
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# ANTI-SPAM
# ──────────────────────────────────────────────────────────────────────────────
_SPAM_WINDOW = 10.0
_SPAM_MAX    = 5
_spam_calls: dict[int, deque] = defaultdict(deque)

# ──────────────────────────────────────────────────────────────────────────────
# LOCKS (multitasking protection)
# ──────────────────────────────────────────────────────────────────────────────
_channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# ──────────────────────────────────────────────────────────────────────────────
# DIRECTIVES STATE
# ──────────────────────────────────────────────────────────────────────────────
_directive_state: list[str] = []  # last N directive contents

# ──────────────────────────────────────────────────────────────────────────────
# GUILD STRUCTURE CACHE (dynamic channel resolution)
# ──────────────────────────────────────────────────────────────────────────────
# guild_id -> cache dict
_guild_cache: dict[int, dict[str, Any]] = {}  # {"by_id": {...}, "by_name": {...}}

def _build_guild_cache(guild: discord.Guild):
    by_id = {}
    by_name = defaultdict(list)
    for ch in guild.channels:
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            cat = ch.category.name if getattr(ch, "category", None) else ""
            by_id[ch.id] = {"name": ch.name, "category": cat, "type": "thread" if isinstance(ch, discord.Thread) else "text"}
            by_name[ch.name.lower()].append(ch.id)
        elif isinstance(ch, discord.VoiceChannel):
            # not used for resolution now, but harmless
            pass
    _guild_cache[guild.id] = {"by_id": by_id, "by_name": dict(by_name)}
    log.info("Guild cache built: %s (%d channels indexed)", guild.name, len(by_id))

def _refresh_guild_cache(guild: discord.Guild):
    try:
        _build_guild_cache(guild)
    except Exception as e:
        log.warning("Guild cache refresh failed: %s", e)

def resolve_channel(guild: discord.Guild, raw: str, category_name: Optional[str] = None) -> Optional[discord.abc.GuildChannel]:
    """
    Resolution priority:
      1) ID
      2) mention <#id>
      3) name (prefer category match if provided)
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # 1) ID
    if raw.isdigit():
        ch = guild.get_channel(int(raw))
        if ch:
            return ch

    # 2) mention
    m = _CHANNEL_MENTION_RE.search(raw)
    if m:
        ch = guild.get_channel(int(m.group(1)))
        if ch:
            return ch

    # 3) name (prefer category)
    name = raw.lstrip("#").lower()
    cache = _guild_cache.get(guild.id) or {}
    ids = (cache.get("by_name") or {}).get(name, [])

    candidates = []
    for cid in ids:
        ch = guild.get_channel(cid)
        if ch and isinstance(ch, discord.TextChannel):
            candidates.append(ch)

    if not candidates:
        # fallback scan
        candidates = [c for c in guild.text_channels if c.name.lower() == name]

    if not candidates:
        return None

    if category_name:
        cat_l = category_name.lower()
        for c in candidates:
            if c.category and c.category.name.lower() == cat_l:
                return c

    return candidates[0]

# ──────────────────────────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────────────────────────
def _db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

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
    _db_migrate()

def _db_migrate():
    # Optional: add last_intent if not present (safe patch)
    try:
        with _db() as c:
            cols = [r["name"] for r in c.execute("PRAGMA table_info(conversation_memory)").fetchall()]
            if "last_intent" not in cols:
                c.execute("ALTER TABLE conversation_memory ADD COLUMN last_intent TEXT DEFAULT ''")
                log.info("DB MIGRATION: added conversation_memory.last_intent")
    except Exception as e:
        # Never crash on migration; just log
        log.warning("DB migration skipped/failed: %s", e)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
def cfg_get(key: str) -> str:
    with _db() as c:
        row = c.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def cfg_set(key: str, value: str):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO bot_config (key,value) VALUES (?,?)", (key, value))

def cfg_all():
    with _db() as c:
        rows = c.execute("SELECT key,value FROM bot_config").fetchall()
    return {r["key"]: r["value"] for r in rows}

def get_free_limit():
    try:
        return int(cfg_get("free_daily_limit"))
    except:
        return 10

def get_paid_roles():
    return [r.strip() for r in cfg_get("paid_roles").split(",") if r.strip()]

def get_exempt_roles():
    return [r.strip() for r in cfg_get("limit_exempt_roles").split(",") if r.strip()]

# ──────────────────────────────────────────────────────────────────────────────
# COUNTS
# ──────────────────────────────────────────────────────────────────────────────
def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def db_get_count(user_id: int) -> int:
    with _db() as c:
        row = c.execute("SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
                        (user_id, _today())).fetchone()
    return row["count"] if row else 0

def db_increment(user_id: int) -> int:
    today = _today()
    with _db() as c:
        c.execute("""INSERT INTO message_counts (user_id,date_utc,count) VALUES (?,?,1)
                     ON CONFLICT(user_id,date_utc) DO UPDATE SET count=count+1""", (user_id, today))
        row = c.execute("SELECT count FROM message_counts WHERE user_id=? AND date_utc=?",
                        (user_id, today)).fetchone()
    return row["count"]

def db_reset_user(user_id: int):
    with _db() as c:
        c.execute("DELETE FROM message_counts WHERE user_id=?", (user_id,))

# ──────────────────────────────────────────────────────────────────────────────
# USER MEMORY
# ──────────────────────────────────────────────────────────────────────────────
def db_is_first(user_id: int) -> bool:
    with _db() as c:
        row = c.execute("SELECT first_seen FROM user_memory WHERE user_id=?", (user_id,)).fetchone()
    return (row is None) or (row["first_seen"] == 0)

def db_upsert_memory(user_id: int, language: str, mark_seen: bool = False):
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

def db_get_lang(user_id: int) -> Optional[str]:
    with _db() as c:
        row = c.execute("SELECT language FROM user_memory WHERE user_id=?", (user_id,)).fetchone()
    return row["language"] if row else None

# ──────────────────────────────────────────────────────────────────────────────
# QUOTA
# ──────────────────────────────────────────────────────────────────────────────
def quota_get(user_id: int) -> int:
    with _db() as c:
        row = c.execute("SELECT additional_quota FROM user_quota WHERE user_id=?", (user_id,)).fetchone()
    return row["additional_quota"] if row else 0

def quota_set(user_id: int, amount: int):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO user_quota (user_id, additional_quota) VALUES (?,?)",
                  (user_id, max(0, amount)))

def quota_deduct(user_id: int) -> bool:
    q = quota_get(user_id)
    if q > 0:
        quota_set(user_id, q - 1)
        return True
    return False

# ──────────────────────────────────────────────────────────────────────────────
# POINTS
# ──────────────────────────────────────────────────────────────────────────────
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
    with _db() as c:
        row = c.execute("SELECT deal_rewarded FROM referrals WHERE referrer_id=? AND referred_id=?",
                        (referrer_id, referred_id)).fetchone()
        if row and row["deal_rewarded"] == 0:
            c.execute("UPDATE referrals SET deal_rewarded=1 WHERE referrer_id=? AND referred_id=?",
                      (referrer_id, referred_id))
            return True
    return False

# ──────────────────────────────────────────────────────────────────────────────
# DIALOG MEMORY
# ──────────────────────────────────────────────────────────────────────────────
MAX_HISTORY   = 20
MEMORY_TTL_H  = 24

def memory_load(guild_id: int, channel_id: int, user_id: int) -> list:
    with _db() as c:
        row = c.execute(
            "SELECT history_json, updated_at FROM conversation_memory "
            "WHERE guild_id=? AND channel_id=? AND user_id=?",
            (guild_id, channel_id, user_id)).fetchone()
    if not row:
        return []
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

def memory_save(guild_id: int, channel_id: int, user_id: int, history: list, last_intent: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    trimmed = history[-MAX_HISTORY:]
    with _db() as c:
        # last_intent may or may not exist; keep compatibility
        try:
            c.execute("""INSERT INTO conversation_memory
                         (guild_id, channel_id, user_id, updated_at, history_json, last_intent)
                         VALUES (?,?,?,?,?,?)
                         ON CONFLICT(guild_id,channel_id,user_id) DO UPDATE SET
                         updated_at=excluded.updated_at, history_json=excluded.history_json, last_intent=excluded.last_intent""",
                      (guild_id, channel_id, user_id, now, json.dumps(trimmed), last_intent))
        except sqlite3.OperationalError:
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

# ──────────────────────────────────────────────────────────────────────────────
# PERMISSIONS + HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def is_owner(member: discord.abc.User) -> bool:
    return int(member.id) == OWNER_ID

def is_admin(member: discord.Member) -> bool:
    return is_owner(member) or any(r.name == "AI Admin" for r in getattr(member, "roles", []))

def is_paid(member: discord.Member) -> bool:
    return bool({r.name for r in member.roles} & set(get_paid_roles()))

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

def sanitize(text: str) -> str:
    return _HIDDEN_PATTERN.sub("[server administration]", text)

def detect_lang(text: str) -> str:
    # simple but stable: Cyrillic -> ru (covers uk/sr/bg too for now in prompt logic)
    return "ru" if re.search(r"[а-яёА-ЯЁІіЇїЄєЎў]", text) else "en"

def check_antispam(user_id: int) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    dq  = _spam_calls[user_id]
    while dq and now - dq[0] > _SPAM_WINDOW:
        dq.popleft()
    if len(dq) >= _SPAM_MAX:
        return False
    dq.append(now)
    return True

def is_upgrade_intent(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in [
        "upgrade","апгрейд","купить","buy","subscribe","подписка","оплат","payment",
        "pro","elite","ultra","billing","план","plan"
    ])

# ──────────────────────────────────────────────────────────────────────────────
# AUDIT
# ──────────────────────────────────────────────────────────────────────────────
async def _audit(guild: Optional[discord.Guild], msg: str):
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name=AUDIT_CHANNEL_NAME)
    if ch:
        try:
            await ch.send(f"```\n{msg[:1990]}\n```")
        except Exception as e:
            log.warning("audit: %s", e)

# ──────────────────────────────────────────────────────────────────────────────
# SEND + PIN (Race condition fix + retries + lock)
# ──────────────────────────────────────────────────────────────────────────────
async def safe_send_and_pin(channel: discord.TextChannel, content: str, *, audit_guild: discord.Guild):
    async with _channel_locks[channel.id]:
        try:
            msg = await channel.send(content)
        except Exception as e:
            await _audit(audit_guild, f"[SEND_FAIL] channel_id={channel.id} reason={e}")
            return None, f"Send failed: {e}"

        await asyncio.sleep(0.8)

        perms = channel.permissions_for(channel.guild.me)
        if not perms.manage_messages:
            await _audit(audit_guild, f"[PIN_FAIL] channel_id={channel.id} msg_id={msg.id} reason=missing_manage_messages")
            return msg, "Missing Manage Messages permission for pin."

        for attempt in range(3):
            try:
                await msg.pin(reason="Nexora auto pin")
                return msg, None
            except discord.Forbidden:
                await _audit(audit_guild, f"[PIN_FAIL] channel_id={channel.id} msg_id={msg.id} reason=forbidden")
                return msg, "Pin failed: permission denied."
            except discord.HTTPException as e:
                await _audit(audit_guild, f"[PIN_RETRY] channel_id={channel.id} msg_id={msg.id} attempt={attempt+1} err={e}")
                await asyncio.sleep(1.5 * (attempt + 1))

        await _audit(audit_guild, f"[PIN_FAIL] channel_id={channel.id} msg_id={msg.id} reason=retries_exhausted")
        return msg, "Pin failed after retries."

# ──────────────────────────────────────────────────────────────────────────────
# DIRECTIVES INGESTION
# ──────────────────────────────────────────────────────────────────────────────
def directive_store(guild_id: int, message_id: int, author_id: int, content: str):
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        c.execute(
            "INSERT OR IGNORE INTO directive_log (guild_id,message_id,author_id,created_at,content) "
            "VALUES (?,?,?,?,?)",
            (guild_id, message_id, author_id, now, content)
        )
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
            if msg.author.bot or (isinstance(msg.author, discord.Member) and is_admin(msg.author)):
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

# ──────────────────────────────────────────────────────────────────────────────
# OPENAI SYSTEM PROMPTS
# ──────────────────────────────────────────────────────────────────────────────
def _system_paid(lang: str) -> str:
    persona   = cfg_get("bot_persona")
    style     = cfg_get("bot_style")
    lang_mode = cfg_get("response_language")
    limit     = get_free_limit()
    if lang_mode == "auto":
        lang_rule = "Отвечай на русском." if lang == "ru" else "Reply in the user's language."
    elif lang_mode == "ru":
        lang_rule = "Всегда отвечай на русском."
    else:
        lang_rule = "Always reply in English."

    style_map = {
        "friendly": "Тон: дружелюбный, тёплый.",
        "formal": "Тон: официальный.",
        "casual": "Тон: расслабленный."
    }
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
    if lang_mode == "auto":
        lang_rule = "Отвечай на русском." if lang == "ru" else "Reply in the user's language."
    elif lang_mode == "ru":
        lang_rule = "Всегда отвечай на русском."
    else:
        lang_rule = "Always reply in English."

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
        return ("⚠️ Система апгрейда/оплаты ещё не настроена.\n"
                "Официальные инструкции будут опубликованы в `#upgrade` и `#billing-faq`.")
    return ("⚠️ Upgrade/payment system is not configured yet.\n"
            "Official instructions will be published in `#upgrade` and `#billing-faq`.")

async def ask_ai(user_msg: str, lang: str, paid: bool, history: Optional[list] = None) -> Optional[str]:
    system = _system_paid(lang) if paid else _system_free(lang)
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-MAX_HISTORY:])
    messages.append({"role": "user", "content": user_msg})
    try:
        r = await ai.chat.completions.create(
            model=MODEL_ASSISTANT,
            messages=messages,
            max_tokens=700,
            temperature=0.7
        )
        return sanitize(r.choices[0].message.content or "")
    except Exception as e:
        log.error("ask_ai: %s", e)
        return None

def _welcome_suffix(lang: str, is_free: bool, limit: int) -> str:
    guide = cfg_get("server_guide")
    if lang == "ru":
        return (f"\n\n{guide}\n\n> 💬 Бесплатный доступ: **{limit} сообщений/день**\n> ⭐ Безлимитно — **Nexora Pro/Elite/Ultra**"
                if is_free else f"\n\n{guide}")
    return (f"\n\n{guide}\n\n> 💬 Free: **{limit} messages/day**\n> ⭐ Unlimited — **Nexora Pro/Elite/Ultra**"
            if is_free else f"\n\n{guide}")

# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC HANDLER (mention-only entry point)
# ──────────────────────────────────────────────────────────────────────────────
async def handle_public(message: discord.Message, content: str):
    member = message.author
    lang   = detect_lang(content)
    first  = db_is_first(member.id)

    guild_id = message.guild.id if message.guild else 0
    ch_id    = message.channel.id

    # Reset context
    if "reset context" in content.lower():
        memory_clear(guild_id, ch_id, member.id)
        await message.reply("🔄 Контекст разговора сброшен." if lang == "ru" else "🔄 Conversation context reset.",
                            mention_author=False)
        return

    # Anti-spam
    if not check_antispam(member.id):
        warn = ("⚠️ Слишком много запросов. Подожди 10 секунд." if lang == "ru"
                else "⚠️ Too many requests. Please wait 10 seconds.")
        await message.reply(warn, mention_author=False)
        await _audit(message.guild, f"[SPAM] user_id={member.id} channel_id={ch_id}")
        return

    # No-fabrication for upgrade/billing when not configured
    if is_upgrade_intent(content) and cfg_get("payment_enabled") != "true":
        await message.reply(_safe_upgrade_response(lang), mention_author=False)
        await _audit(message.guild, f"[BLOCKED_UPGRADE] user_id={member.id} channel_id={ch_id}")
        return

    # Role-based daily limit (quota first)
    limit     = get_user_daily_limit(member)
    unlimited = limit >= 999999

    if not unlimited:
        if not quota_deduct(member.id):
            count = db_get_count(member.id)
            if count >= limit:
                msg = ("⚠️ Вы исчерпали лимит сообщений на сегодня.\n"
                       "Повысьте роль или попросите администратора добавить квоту. 🚀"
                       if lang == "ru" else
                       "⚠️ You've used all messages for today.\n"
                       "Upgrade your role or ask an admin for extra quota. 🚀")
                await message.reply(msg, mention_author=False)
                return
            new_count = db_increment(member.id)
            remaining = limit - new_count
        else:
            remaining = quota_get(member.id)
    else:
        remaining = None

    db_upsert_memory(member.id, lang, mark_seen=first)

    history = memory_load(guild_id, ch_id, member.id)

    async with message.channel.typing():
        reply = await ask_ai(content, lang, paid=unlimited, history=history)

    if reply is None:
        await message.reply("❌ Произошла ошибка. Попробуйте снова." if lang == "ru" else "❌ An error occurred.",
                            mention_author=False)
        await _audit(message.guild, f"[AI_ERROR] user_id={member.id} channel_id={ch_id}")
        return

    history.append({"role": "user", "content": content})
    history.append({"role": "assistant", "content": reply})
    memory_save(guild_id, ch_id, member.id, history, last_intent="chat")

    if first:
        reply += _welcome_suffix(lang, is_free=not unlimited, limit=limit)
    if remaining is not None:
        reply += (f"\n\n> 💬 Осталось сегодня: **{remaining}/{limit}**" if lang == "ru"
                  else f"\n\n> 💬 Messages left today: **{remaining}/{limit}**")

    await message.reply(reply, mention_author=False)

# ──────────────────────────────────────────────────────────────────────────────
# TICKET AUTO-TRANSLATION
# ──────────────────────────────────────────────────────────────────────────────
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
            max_tokens=500, temperature=0.2
        )
        result = (r.choices[0].message.content or "").strip()
        return result if result else None
    except Exception as e:
        log.error("translate_text: %s", e)
        return None

async def handle_ticket_message(message: discord.Message):
    channel  = message.channel
    content  = message.content.strip()
    if not content:
        return

    mentioned = bot.user in (message.mentions or [])

    # Explicit translate: @Nexora AI translate
    if mentioned and "translate" in content.lower():
        text_to_translate = _USER_MENTION_RE.sub("", content).strip()
        text_to_translate = re.sub(r"\btranslate\b", "", text_to_translate, count=1, flags=re.IGNORECASE).strip()

        if not text_to_translate and message.reference:
            try:
                ref = await channel.fetch_message(message.reference.message_id)
                text_to_translate = ref.content
            except Exception:
                pass

        if not text_to_translate:
            await message.reply("❓ Укажи текст или ответь на сообщение для перевода.", mention_author=False)
            return

        src = detect_lang(text_to_translate)
        requester_lang = db_get_lang(message.author.id) or detect_lang(content)
        tgt = requester_lang if requester_lang else ("ru" if src != "ru" else "en")

        translated = await translate_text(text_to_translate, tgt)
        if translated:
            await message.reply(f"{translated}", mention_author=False)
        return

    # If mentioned but not translate: let public handler process (mention-only behavior)
    if mentioned:
        return

    # Auto-translate only if exactly 2 human participants
    if not isinstance(channel, discord.TextChannel):
        return

    human_members = [m for m in channel.members if not m.bot]
    if len(human_members) != 2:
        return

    sender = message.author
    other  = next((m for m in human_members if m.id != sender.id), None)
    if not other:
        return

    src_lang = detect_lang(content)
    tgt_lang = db_get_lang(other.id) or "en"
    if src_lang == tgt_lang:
        return

    translated = await translate_text(content, tgt_lang)
    if translated and translated.strip().lower() != content.strip().lower():
        await channel.send(
            f"🌐 *({src_lang.upper()} → {tgt_lang.upper()}) for {other.mention}:*\n> {translated}",
            allowed_mentions=discord.AllowedMentions(users=False)
        )

# ──────────────────────────────────────────────────────────────────────────────
# TICKET CLOSE — POINT AWARDS
# ──────────────────────────────────────────────────────────────────────────────
async def _award_ticket_close_points(channel: discord.TextChannel, guild: discord.Guild):
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
        if deals_get(uid) == 100:
            points_add(uid, 100, "milestone_100_deals")
            await _audit(guild, f"[POINTS] user_id={uid} +100 milestone_100_deals")

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

# ──────────────────────────────────────────────────────────────────────────────
# ADMIN TOOLS (kept as-is, minimal changes: resolve_channel used where applicable)
# ──────────────────────────────────────────────────────────────────────────────
ADMIN_TOOLS = [
    {"type":"function","function":{"name":"update_config","description":"Update bot config. Keys: free_daily_limit, paid_roles, bot_persona, bot_style (friendly|formal|casual), response_language (auto|ru|en), limit_exempt_roles, server_guide, payment_enabled (true|false), payment_instructions, auto_upgrade_role.","parameters":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":"string"},"reason":{"type":"string"}},"required":["key","value"]}}},
    {"type":"function","function":{"name":"show_config","description":"Show all bot config settings.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"reset_user_limit","description":"Reset daily message counter for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"award_points","description":"Award or deduct points for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"},"points":{"type":"integer"},"reason":{"type":"string"}},"required":["username","points"]}}},
    {"type":"function","function":{"name":"check_points","description":"Check points and stats for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"set_user_quota","description":"Set extra message quota for a user.","parameters":{"type":"object","properties":{"username":{"type":"string"},"amount":{"type":"integer"}},"required":["username","amount"]}}},
    {"type":"function","function":{"name":"register_referral","description":"Register that one user referred another.","parameters":{"type":"object","properties":{"referrer":{"type":"string"},"referred":{"type":"string"}},"required":["referrer","referred"]}}},
    {"type":"function","function":{"name":"delete_last_message","description":"Delete the most recent message from a user in a channel.","parameters":{"type":"object","properties":{"username":{"type":"string"},"channel_name":{"type":"string"},"category_name":{"type":"string"}},"required":["username","channel_name"]}}},
    {"type":"function","function":{"name":"create_channel","description":"Create a new text channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"category_name":{"type":"string"},"private":{"type":"boolean"}},"required":["channel_name"]}}},
    {"type":"function","function":{"name":"delete_channel","description":"Delete a text channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"category_name":{"type":"string"}},"required":["channel_name"]}}},
    {"type":"function","function":{"name":"kick_member","description":"Kick a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"reason":{"type":"string"}},"required":["username"]}}},
    {"type":"function","function":{"name":"ban_member","description":"Ban a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"reason":{"type":"string"},"delete_days":{"type":"integer"}},"required":["username"]}}},
    {"type":"function","function":{"name":"set_slowmode","description":"Set slowmode on a channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"seconds":{"type":"integer"},"category_name":{"type":"string"}},"required":["channel_name","seconds"]}}},
    {"type":"function","function":{"name":"give_role","description":"Give a role to a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"role_name":{"type":"string"}},"required":["username","role_name"]}}},
    {"type":"function","function":{"name":"remove_role","description":"Remove a role from a member.","parameters":{"type":"object","properties":{"username":{"type":"string"},"role_name":{"type":"string"}},"required":["username","role_name"]}}},
    {"type":"function","function":{"name":"send_announcement","description":"Send a message to a channel.","parameters":{"type":"object","properties":{"channel_name":{"type":"string"},"message":{"type":"string"},"category_name":{"type":"string"}},"required":["channel_name","message"]}}},
    {"type":"function","function":{"name":"server_info","description":"Show server overview.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"clarify","description":"Ask one clarifying question.","parameters":{"type":"object","properties":{"question":{"type":"string"}},"required":["question"]}}},
]

_ADMIN_SYSTEM = """Ты — Nexora Admin AI. Внутренний ИИ-ассистент для администраторов.
КОГДА ОТВЕЧАТЬ ТЕКСТОМ (БЕЗ TOOLS):
Если запрос ИНФОРМАЦИОННЫЙ — отвечай текстом, НЕ вызывай tools.

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
"""

def _find_member(guild: discord.Guild, name: str) -> Optional[discord.Member]:
    name_l = (name or "").lower()
    return (discord.utils.find(lambda m: m.name.lower() == name_l, guild.members)
            or discord.utils.find(lambda m: m.display_name.lower() == name_l, guild.members))

async def plan_admin(request: str, guild_id: int, channel_id: int, user_id: int) -> dict:
    history = memory_load(guild_id, channel_id, user_id)
    messages = [{"role":"system","content":_ADMIN_SYSTEM}]
    messages.extend(history[-10:])
    messages.append({"role":"user","content":request})
    try:
        resp = await ai.chat.completions.create(
            model=MODEL_ADMIN,
            messages=messages,
            tools=ADMIN_TOOLS,
            tool_choice="auto",
            max_tokens=500
        )
        msg = resp.choices[0].message

        history.append({"role":"user","content":request})
        if msg.content:
            history.append({"role":"assistant","content":msg.content})
        memory_save(guild_id, channel_id, user_id, history, last_intent="admin")

        if msg.tool_calls:
            tc = msg.tool_calls[0]
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except:
                args = {}
            if name == "clarify":
                return {"type":"clarify","question":args.get("question","?")}
            return {"type":"tool_call","name":name,"args":args,"plan_text":f"{name} {args}"}
        return {"type":"text","content":msg.content or "OK."}
    except Exception as e:
        log.error("plan_admin: %s", e)
        return {"type":"error","content":str(e)}

async def handle_admin(message: discord.Message):
    member = message.author
    if not isinstance(member, discord.Member) or not is_admin(member):
        await message.reply("🔒 Только для администраторов.", mention_author=False)
        return

    if len(message.content.strip()) < 2:
        return

    if "reset context" in message.content.lower():
        memory_clear(message.guild.id, message.channel.id, member.id)
        await message.reply("🔄 Контекст администратора сброшен.", mention_author=False)
        return

    await _audit(message.guild, f"[REQUEST] {member} ({member.id}): {message.content[:300]}")
    async with message.channel.typing():
        plan = await plan_admin(message.content, message.guild.id, message.channel.id, member.id)

    if plan["type"] == "text":
        await message.reply(plan["content"], mention_author=False)
    elif plan["type"] == "clarify":
        await message.reply(f"❓ {plan['question']}", mention_author=False)
    elif plan["type"] == "tool_call":
        view = ConfirmView(message.guild, plan["name"], plan["args"], member)
        await message.reply(f"**📋 ПЛАН**\n{plan['plan_text']}\n\nПодтвердить?",
                            view=view, mention_author=False)
    else:
        await message.reply(f"💥 Ошибка AI: `{plan.get('content','unknown')}`", mention_author=False)
        await _audit(message.guild, f"[AI ERROR] {plan.get('content','unknown')}")

# ──────────────────────────────────────────────────────────────────────────────
# EXECUTORS (use resolve_channel where relevant)
# ──────────────────────────────────────────────────────────────────────────────
async def execute_action(guild: discord.Guild, name: str, args: dict) -> str:
    try:
        if name == "show_config":
            config = cfg_all()
            return json.dumps(config, ensure_ascii=False, indent=2)

        if name == "update_config":
            key = args.get("key","")
            value = args.get("value","")
            valid = {"free_daily_limit","paid_roles","bot_persona","bot_style","response_language",
                     "limit_exempt_roles","server_guide","payment_enabled","payment_instructions","auto_upgrade_role"}
            if key not in valid:
                return f"❌ Неизвестный ключ: `{key}`"
            old = cfg_get(key)
            cfg_set(key, value)
            await _audit(guild, f"[CONFIG] key={key} old={old[:80]} new={value[:80]}")
            return f"✅ {key} обновлён."

        if name == "reset_user_limit":
            m = _find_member(guild, args.get("username",""))
            if not m:
                return "❌ Пользователь не найден."
            db_reset_user(m.id)
            await _audit(guild, f"[RESET_LIMIT] user_id={m.id}")
            return f"✅ Лимит сброшен для {m.display_name}."

        if name == "set_user_quota":
            m = _find_member(guild, args.get("username",""))
            if not m:
                return "❌ Пользователь не найден."
            amount = int(args.get("amount", 0))
            quota_set(m.id, amount)
            await _audit(guild, f"[QUOTA] user_id={m.id} set={amount}")
            return f"✅ Квота {m.display_name} = {amount}."

        if name == "award_points":
            m = _find_member(guild, args.get("username",""))
            if not m:
                return "❌ Пользователь не найден."
            pts = int(args.get("points", 0))
            reason = args.get("reason","admin_award")
            points_add(m.id, pts, reason)
            await _audit(guild, f"[POINTS] user_id={m.id} delta={pts} reason={reason}")
            return f"✅ Очки обновлены: {m.display_name} {pts:+d}."

        if name == "check_points":
            m = _find_member(guild, args.get("username",""))
            if not m:
                return "❌ Пользователь не найден."
            return (f"{m.display_name}: points={points_get(m.id)} deals={deals_get(m.id)} "
                    f"refs={referral_count(m.id)} quota={quota_get(m.id)}")

        if name == "register_referral":
            referrer = _find_member(guild, args.get("referrer",""))
            referred = _find_member(guild, args.get("referred",""))
            if not referrer or not referred:
                return "❌ Пользователь(и) не найдены."
            if referral_exists(referrer.id, referred.id):
                return "⚠️ Уже зарегистрирован."
            referral_add(referrer.id, referred.id)
            with _db() as c:
                c.execute("""INSERT INTO user_points (user_id, referrer_id) VALUES (?,?)
                             ON CONFLICT(user_id) DO UPDATE SET referrer_id=?""",
                          (referred.id, referrer.id, referrer.id))
            points_add(referrer.id, 10, f"referral_invite:{referred.id}")
            await _audit(guild, f"[REFERRAL] {referrer.id} -> {referred.id}")
            return "✅ Реферал зарегистрирован."

        if name == "send_announcement":
            ch = resolve_channel(guild, args.get("channel_name",""), args.get("category_name"))
            if not ch or not isinstance(ch, discord.TextChannel):
                return "❌ Канал не найден."
            try:
                await ch.send(args.get("message",""))
                await _audit(guild, f"[ANNOUNCE] channel_id={ch.id}")
                return "✅ Отправлено."
            except Exception as e:
                await _audit(guild, f"[ANNOUNCE_FAIL] channel_id={getattr(ch,'id','?')} err={e}")
                return f"❌ Ошибка: {e}"

        if name == "delete_last_message":
            ch = resolve_channel(guild, args.get("channel_name",""), args.get("category_name"))
            if not ch or not isinstance(ch, discord.TextChannel):
                return "❌ Канал не найден."
            m = _find_member(guild, args.get("username",""))
            if not m:
                return "❌ Пользователь не найден."
            try:
                async for msg in ch.history(limit=200):
                    if msg.author.id == m.id:
                        await msg.delete()
                        await _audit(guild, f"[DELETE_LAST] channel_id={ch.id} user_id={m.id} msg_id={msg.id}")
                        return "✅ Удалено."
                return "❌ Сообщений не найдено."
            except Exception as e:
                await _audit(guild, f"[DELETE_LAST_FAIL] channel_id={ch.id} err={e}")
                return f"❌ Ошибка: {e}"

        if name == "set_slowmode":
            ch = resolve_channel(guild, args.get("channel_name",""), args.get("category_name"))
            if not ch or not isinstance(ch, discord.TextChannel):
                return "❌ Канал не найден."
            secs = int(args.get("seconds", 0))
            try:
                await ch.edit(slowmode_delay=secs)
                await _audit(guild, f"[SLOWMODE] channel_id={ch.id} secs={secs}")
                return "✅ Ок."
            except Exception as e:
                await _audit(guild, f"[SLOWMODE_FAIL] channel_id={ch.id} err={e}")
                return f"❌ Ошибка: {e}"

        if name == "create_channel":
            cname = args.get("channel_name","new-channel")
            catname = args.get("category_name")
            private = bool(args.get("private", False))
            cat = discord.utils.get(guild.categories, name=catname) if catname else None
            overwrites = None
            if private:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
            try:
                ch = await guild.create_text_channel(name=cname, category=cat, overwrites=overwrites)
                _refresh_guild_cache(guild)
                await _audit(guild, f"[CREATE_CHANNEL] channel_id={ch.id} name={ch.name}")
                return f"✅ Канал создан: #{ch.name}"
            except Exception as e:
                await _audit(guild, f"[CREATE_CHANNEL_FAIL] err={e}")
                return f"❌ Ошибка: {e}"

        if name == "delete_channel":
            ch = resolve_channel(guild, args.get("channel_name",""), args.get("category_name"))
            if not ch or not isinstance(ch, discord.TextChannel):
                return "❌ Канал не найден."
            try:
                cid = ch.id
                await ch.delete()
                _refresh_guild_cache(guild)
                await _audit(guild, f"[DELETE_CHANNEL] channel_id={cid}")
                return "✅ Канал удалён."
            except Exception as e:
                await _audit(guild, f"[DELETE_CHANNEL_FAIL] err={e}")
                return f"❌ Ошибка: {e}"

        if name == "give_role":
            m = _find_member(guild, args.get("username",""))
            role = discord.utils.get(guild.roles, name=args.get("role_name",""))
            if not m or not role:
                return "❌ Пользователь или роль не найдены."
            try:
                await m.add_roles(role)
                await _audit(guild, f"[GIVE_ROLE] user_id={m.id} role={role.name}")
                return "✅ Роль выдана."
            except Exception as e:
                await _audit(guild, f"[GIVE_ROLE_FAIL] err={e}")
                return f"❌ Ошибка: {e}"

        if name == "remove_role":
            m = _find_member(guild, args.get("username",""))
            role = discord.utils.get(guild.roles, name=args.get("role_name",""))
            if not m or not role:
                return "❌ Пользователь или роль не найдены."
            try:
                await m.remove_roles(role)
                await _audit(guild, f"[REMOVE_ROLE] user_id={m.id} role={role.name}")
                return "✅ Роль убрана."
            except Exception as e:
                await _audit(guild, f"[REMOVE_ROLE_FAIL] err={e}")
                return f"❌ Ошибка: {e}"

        if name == "kick_member":
            m = _find_member(guild, args.get("username",""))
            if not m:
                return "❌ Пользователь не найден."
            try:
                await m.kick(reason=args.get("reason",""))
                await _audit(guild, f"[KICK] user_id={m.id}")
                return "✅ Кик."
            except Exception as e:
                await _audit(guild, f"[KICK_FAIL] err={e}")
                return f"❌ Ошибка: {e}"

        if name == "ban_member":
            m = _find_member(guild, args.get("username",""))
            if not m:
                return "❌ Пользователь не найден."
            try:
                await m.ban(reason=args.get("reason",""), delete_message_days=min(int(args.get("delete_days",0)), 7))
                await _audit(guild, f"[BAN] user_id={m.id}")
                return "✅ Бан."
            except Exception as e:
                await _audit(guild, f"[BAN_FAIL] err={e}")
                return f"❌ Ошибка: {e}"

        if name == "server_info":
            return f"{guild.name}: members={guild.member_count} text_channels={len(guild.text_channels)} roles={len(guild.roles)}"

        return f"❓ Неизвестное действие: {name}"
    except Exception as e:
        await _audit(guild, f"[EXECUTE_FAIL] action={name} err={e}")
        return f"💥 Ошибка: {e}"

# ──────────────────────────────────────────────────────────────────────────────
# DISCORD UI
# ──────────────────────────────────────────────────────────────────────────────
class ConfirmView(discord.ui.View):
    def __init__(self, guild: discord.Guild, name: str, args: dict, requester: discord.Member):
        super().__init__(timeout=60)
        self.guild = guild
        self.name = name
        self.args = args
        self.requester = requester

    @discord.ui.button(label="✅ Подтвердить", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("Только запросивший может подтвердить.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⚙️ Выполняю…", view=self)
        result = await execute_action(self.guild, self.name, self.args)
        await interaction.followup.send(result)
        await _audit(self.guild, f"[EXECUTE] user_id={self.requester.id} action={self.name} args={self.args} result={str(result)[:200]}")
        self.stop()

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("Только запросивший может отменить.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="🚫 Отменено.", view=self)
        self.stop()

# ──────────────────────────────────────────────────────────────────────────────
# EVENTS
# ──────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    db_init()
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    for guild in bot.guilds:
        _refresh_guild_cache(guild)
        await load_directives_on_ready(guild)

    try:
        synced = await tree.sync()
        log.info("Synced %d slash commands.", len(synced))
    except Exception as e:
        log.error("Slash sync: %s", e)

    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="Nexora | @mention me!"
    ))

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    if channel.guild:
        _refresh_guild_cache(channel.guild)

@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    if after.guild:
        _refresh_guild_cache(after.guild)

    # Ticket close detection: ticket-XXXX → closed-XXXX
    if (isinstance(before, discord.TextChannel)
            and _TICKET_PATTERN.match(before.name)
            and isinstance(after, discord.TextChannel)
            and after.name.startswith("closed-")):
        await _award_ticket_close_points(after, after.guild)

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    if channel.guild:
        _refresh_guild_cache(channel.guild)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Always allow commands
    await bot.process_commands(message)

    # Ignore DMs
    if not message.guild or not isinstance(message.author, discord.Member):
        return

    ch_name = getattr(message.channel, "name", "")

    # ai-directives ingestion (official)
    if ch_name == DIRECTIVES_CHANNEL:
        if is_admin(message.author) or message.author.bot:
            content = (message.content or "").strip()
            if content:
                directive_store(message.guild.id, message.id, message.author.id, content)
                await _audit(message.guild, f"[DIRECTIVE_INGESTED] author_id={message.author.id} msg_id={message.id}")
        else:
            await _audit(message.guild, f"[UNAUTHORIZED_DIRECTIVE_ATTEMPT] author_id={message.author.id} msg_id={message.id}")
        return

    # Admin channel handler (still by configured name)
    if ch_name == ADMIN_CHANNEL_NAME:
        await handle_admin(message)
        return

    # Ticket channels: translation logic may run automatically
    if is_ticket_channel(ch_name):
        await handle_ticket_message(message)
        return

    # PUBLIC: mention-only everywhere (including #ai-help)
    mentioned = bot.user in (message.mentions or [])
    if not mentioned:
        return

    # Deny public usage inside internal channels
    if ch_name in DENY_PUBLIC_CHANNELS:
        return

    # Permission gate (operate only where bot can view + send)
    perms = message.channel.permissions_for(message.guild.me)
    if not perms.view_channel or not perms.send_messages:
        await _audit(message.guild, f"[PERM_BLOCK] channel_id={message.channel.id} view={perms.view_channel} send={perms.send_messages}")
        return

    # Strip mention
    clean = _USER_MENTION_RE.sub("", message.content).strip()
    if not clean:
        lang = detect_lang(message.content)
        await message.reply("Привет! Чем могу помочь? 😊" if lang == "ru" else "Hey! How can I help? 😊",
                            mention_author=False)
        return

    await handle_public(message, clean)

# ──────────────────────────────────────────────────────────────────────────────
# SLASH COMMANDS
# ──────────────────────────────────────────────────────────────────────────────
@tree.command(name="status", description="Мой статус, лимит и очки.")
async def status_cmd(interaction: discord.Interaction):
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("❌ Только на сервере.", ephemeral=True)
        return

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
    embed.add_field(name="Доступ",   value=info,        inline=False)
    embed.add_field(name="🏆 Очки",  value=str(pts),    inline=True)
    embed.add_field(name="🤝 Сделок",value=str(deals),  inline=True)
    if quota > 0:
        embed.add_field(name="📦 Доп. квота", value=str(quota), inline=True)
    embed.set_footer(text="Лимит сбрасывается в полночь UTC")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="pin_rules", description="[Админ] Опубликовать и закрепить правила (send+pin safe).")
async def pin_rules(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
        await interaction.response.send_message("🔒 Только для администраторов.", ephemeral=True)
        return

    help_ch = discord.utils.get(interaction.guild.text_channels, name=HELP_CHANNEL_NAME)
    if not help_ch:
        await interaction.response.send_message(f"❌ Канал `#{HELP_CHANNEL_NAME}` не найден.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    limit = get_free_limit()
    text = (f"# Nexora AI — Как пользоваться\n\n**Обратиться:**\n"
            f"• В любом канале — `@Nexora AI` + вопрос\n\n"
            f"**Лимиты по ролям:**\n"
            f"👑 Ultra: ∞ | ⭐ Elite: 300 | 🌟 Pro: 150\n"
            f"✅ Verified Trader: 30 | 🔨 Trader: 20 | 👤 Member: 15 | 🆓 Остальные: {limit}\n\n"
            f"**Команды:** `/status` — твой статус\n\n"
            f"**Нужна помощь?** Обратись к администраторам.")

    sent, err = await safe_send_and_pin(help_ch, text, audit_guild=interaction.guild)
    if err:
        await interaction.followup.send(f"⚠️ Сообщение отправлено, но закрепить не смог: {err}")
    else:
        await interaction.followup.send("✅ Правила опубликованы и закреплены.")

# ──────────────────────────────────────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    db_init()
    bot.run(DISCORD_TOKEN, log_handler=None)

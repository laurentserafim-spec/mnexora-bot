import os
import discord
from openai import OpenAI

# =============================
# CONFIG
# =============================

FREE_LIMIT = 15
PREMIUM_ROLE = "Nexora Pro"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# =============================
# DISCORD INTENTS
# =============================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = discord.Client(intents=intents)

# =============================
# MEMORY (temporary)
# =============================

user_messages = {}

# =============================
# AI PROMPTS
# =============================

FREE_PROMPT = """
Ты Nexora AI — помощник Discord сервера Nexora.

ПРАВИЛА:
- Всегда отвечай на языке пользователя.
- Помогай только по серверу Nexora.
- Объясняй каналы, правила и функции.
- Направляй пользователей.

Если вопрос вне сервера —
вежливо скажи, что полный AI доступ доступен по подписке.
"""

PRO_PROMPT = """
Ты Nexora AI — продвинутый интеллектуальный ассистент.

ПРАВИЛА:
- Отвечай на языке пользователя.
- Можно обсуждать любые темы.
- Помогай максимально полезно.
- Общайся естественно.
"""

# =============================
# ROLE CHECK
# =============================

def has_premium(member):
    for role in member.roles:
        if role.name == PREMIUM_ROLE:
            return True
    return False

# =============================
# READY EVENT
# =============================

@bot.event
async def on_ready():
    print(f"✅ Nexora AI online as {bot.user}")

# =============================
# MESSAGE HANDLER
# =============================

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    member = message.author
    user_id = str(member.id)

    premium = has_premium(member)

    if user_id not in user_messages:
        user_messages[user_id] = 0

    # =============================
    # FREE USERS
    # =============================
    if not premium:

        if user_messages[user_id] >= FREE_LIMIT:
            await message.channel.send(
                "🚫 Бесплатный лимит сообщений исчерпан.\n"
                "⭐ Оформите Nexora Pro для полного AI доступа."
            )
            return

        user_messages[user_id] += 1
        remaining = FREE_LIMIT - user_messages[user_id]

        model = "gpt-4o-mini"
        system_prompt = FREE_PROMPT

    # =============================
    # PREMIUM USERS
    # =============================
    else:
        model = "gpt-4o"
        system_prompt = PRO_PROMPT

    # =============================
    # AI RESPONSE
    # =============================

    try:
        response = client_ai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message.content}
            ]
        )

        reply = response.choices[0].message.content

        if premium:
            await message.channel.send(
                f"⭐ **Nexora Pro AI**\n{reply}"
            )
        else:
            await message.channel.send(
                f"{reply}\n\n"
                f"🧠 Осталось бесплатных сообщений: {remaining}/{FREE_LIMIT}"
            )

    except Exception as e:
        print(e)
        await message.channel.send("⚠️ AI временно недоступен.")

# =============================
# START BOT
# =============================

bot.run(DISCORD_TOKEN)

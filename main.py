import discord
import os
from openai import OpenAI

client_ai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content.startswith("!ai"):
        user_prompt = message.content.replace("!ai", "")

        response = client_ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are Nexora AI assistant."},
                {"role": "user", "content": user_prompt}
            ]
        )

        reply = response.choices[0].message.content
        await message.channel.send(reply)

client.run(os.getenv("DISCORD_TOKEN"))

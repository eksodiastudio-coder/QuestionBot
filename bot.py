import discord
from discord import app_commands
from google import genai
from dotenv import load_dotenv
import os
import sqlite3
from keep_alive import keep_alive 

load_dotenv()

# ================= CONFIGURATION =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MODEL_NAME = "gemini-flash-lite-latest"

# Setup Discord Client & Command Tree for Slash Commands
intents = discord.Intents.default()
intents.message_content = True
client_discord = discord.Client(intents=intents)
tree = app_commands.CommandTree(client_discord)

# Memory dictionary so the bot doesn't spam Discord's API fetching 500 messages per question
# Format: { guild_id: "All text from the helper channel" }
server_knowledge_memory = {}

# ================= DATABASE SETUP =================
def init_db():
    """Initializes the database to store server configurations."""
    conn = sqlite3.connect('server_configs.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS configs
                 (guild_id INTEGER PRIMARY KEY, 
                  api_key TEXT, 
                  questions_channel INTEGER, 
                  missing_channel INTEGER, 
                  add_channel INTEGER)''')
    conn.commit()
    conn.close()

def get_server_config(guild_id):
    """Retrieves the configuration for a specific server."""
    conn = sqlite3.connect('server_configs.db')
    c = conn.cursor()
    c.execute("SELECT api_key, questions_channel, missing_channel, add_channel FROM configs WHERE guild_id=?", (guild_id,))
    result = c.fetchone()
    conn.close()
    
    if result:
        return {
            "api_key": result[0],
            "questions_channel": result[1],
            "missing_channel": result[2],
            "add_channel": result[3]
        }
    return None

# ================= KNOWLEDGE BUILDER =================
async def build_knowledge_base(guild_id):
    """Reads the server's specific helper channel to build the AI's knowledge."""
    config = get_server_config(guild_id)
    if not config:
        return False

    dynamic_kb = ""
    
    # Load Info purely from the Server's Helper Channel
    helper_channel = client_discord.get_channel(config['add_channel'])
    if helper_channel:
        try:
            # Fetches the last 500 messages from the helper channel
            messages = [msg async for msg in helper_channel.history(limit=500)]
            messages.reverse() # Read from oldest to newest
            for msg in messages:
                if msg.content.strip():
                    dynamic_kb += f"- {msg.content}\n"
        except Exception as e:
            print(f"Error reading helper channel for guild {guild_id}: {e}")
    
    # Save the channel's text to the bot's memory
    server_knowledge_memory[guild_id] = f"--- SERVER RULES & ANSWERS ---\n{dynamic_kb}"
    
    print(f"Knowledge compiled for server {guild_id}! ({len(server_knowledge_memory[guild_id])} chars)")
    return True

# ================= EVENTS & COMMANDS =================
@client_discord.event
async def on_ready():
    init_db() 
    await tree.sync() 
    print(f'Logged in as {client_discord.user}')
    print("Database initialized and Slash Commands synced.")

@tree.command(name="setup", description="Admin setup for the AI Bot")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, 
                api_key: str, 
                questions_channel: discord.TextChannel, 
                missing_answers_channel: discord.TextChannel, 
                add_answers_channel: discord.TextChannel):
    """Slash command for Server Admins to set up the bot."""
    
    conn = sqlite3.connect('server_configs.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO configs 
                 (guild_id, api_key, questions_channel, missing_channel, add_channel) 
                 VALUES (?, ?, ?, ?, ?)''', 
              (interaction.guild_id, api_key, questions_channel.id, missing_answers_channel.id, add_answers_channel.id))
    conn.commit()
    conn.close()

    # Immediately read the channel for this server
    await build_knowledge_base(interaction.guild_id)

    await interaction.response.send_message(
        f"✅ **Setup Complete!**\n"
        f"- Questions Channel: {questions_channel.mention}\n"
        f"- Unanswered Alerts: {missing_answers_channel.mention}\n"
        f"- Knowledge Channel: {add_answers_channel.mention}\n"
        f"*(Your API key has been securely saved)*", 
        ephemeral=True 
    )

@tree.command(name="reload_knowledge", description="Forces the bot to re-read the knowledge channel")
@app_commands.checks.has_permissions(administrator=True)
async def reload_knowledge(interaction: discord.Interaction):
    """Slash command to reload channel knowledge."""
    await interaction.response.defer(ephemeral=True)
    success = await build_knowledge_base(interaction.guild_id)
    if success:
        await interaction.followup.send("✅ Bot has re-read the knowledge channel successfully!")
    else:
        await interaction.followup.send("❌ Error: Server not set up yet. Run `/setup` first.")

@client_discord.event
async def on_message(message):
    # Ignore DMs and bot's own messages
    if message.author == client_discord.user or not message.guild:
        return

    # Fetch configuration for this specific server
    config = get_server_config(message.guild.id)
    
    if not config:
        return

    # =======================================================
    # LOGIC 1: AUTO-LEARNING (Helper Channel)
    # =======================================================
    # If someone types in the knowledge channel, the bot re-reads the channel instantly
    if message.channel.id == config['add_channel']:
        await build_knowledge_base(message.guild.id) 
        await message.add_reaction("🧠")
        return

    # =======================================================
    # LOGIC 2: PUBLIC QUESTIONS
    # =======================================================
    if message.channel.id != config['questions_channel']:
        return

    is_question = message.content.strip().endswith("?")
    is_mentioned = client_discord.user in message.mentions

    if not (is_question or is_mentioned):
        return

    # If the bot rebooted, it needs to read the channel before answering the first question
    if message.guild.id not in server_knowledge_memory:
        await build_knowledge_base(message.guild.id)
        
    kb_text = server_knowledge_memory.get(message.guild.id, "")

    async with message.channel.typing():
        try:
            # --- CONTEXT AWARENESS ---
            history_buffer = []
            async for msg in message.channel.history(limit=5):
                clean_content = msg.clean_content 
                history_buffer.append(f"{msg.author.name}: {clean_content}")
            
            history_buffer.reverse()
            conversation_text = "\n".join(history_buffer)

            # --- PROMPT ---
            prompt = (
            f"You are a helpful assistant for a Discord server. "
            f"Use the 'Knowledge Base' below to answer the user's question.\n\n"
            
            f"INSTRUCTIONS FOR AI:\n"
            f"1. **Match Concepts:** Apply the rules directly based on the text provided.\n"
            f"2. **Be Direct:** Answer clearly without fluff.\n"
            f"3. **When to use SILENCE:** If the answer is NOT found in the Knowledge Base provided below, you MUST reply with exactly the word 'SILENCE'. Do not make up answers.\n"
            f"4. Do NOT use markdown headers like #.\n\n"

            f"{kb_text}\n\n"
            f"--- CONVERSATION HISTORY ---\n{conversation_text}\n\n"
            f"User Question: {message.content}"
            )

            # Create a specific client using this server's API Key
            server_genai_client = genai.Client(api_key=config['api_key'])

            response = server_genai_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt
            )
            
            if response.text:
                response_text = response.text.strip()
                
                # --- SILENCE CHECK (MISSING KNOWLEDGE LOGIC) ---
                if response_text == "SILENCE":
                    print(f"Answer not found in Guild {message.guild.name}. Forwarding to missing answers channel.")
                    
                    missing_channel = client_discord.get_channel(config['missing_channel'])
                    if missing_channel:
                        await missing_channel.send(
                            f"🚨 **Unanswered Question** 🚨\n"
                            f"**User:** {message.author.mention}\n"
                            f"**Question:** {message.content}\n"
                            f"*Please add the answer to the <#{config['add_channel']}> channel.*"
                        )
                    return 

                # --- SENDING THE MESSAGE ---
                if len(response_text) > 2000:
                    parts = [response_text[i:i+1900] for i in range(0, len(response_text), 1900)]
                    for index, part in enumerate(parts):
                        if index == 0:
                            await message.reply(part)
                        else:
                            await message.channel.send(part)
                else:
                    await message.reply(response_text)

        except Exception as e:
            print(f"API Error in server {message.guild.name}: {e}")
            if "API_KEY_INVALID" in str(e):
                 await message.channel.send("⚠️ The API key provided for this server is invalid or has expired. Please ask an admin to run `/setup` again.")

keep_alive()
client_discord.run(DISCORD_TOKEN)

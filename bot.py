import discord
from discord import app_commands
from google import genai
import os
from pymongo import MongoClient
from keep_alive import keep_alive 

# ================= CONFIGURATION =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI") 
DEFAULT_MODEL = "gemini-2.5-flash"
# IMPORTANT: Replace this with your actual SKU ID from the Discord Developer Portal
PREMIUM_SKU_ID = 1516180697310691392 

# Setup Discord Client & Command Tree
intents = discord.Intents.default()
intents.message_content = True
client_discord = discord.Client(intents=intents)
tree = app_commands.CommandTree(client_discord)

server_knowledge_memory = {}

# ================= DATABASE SETUP =================
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["QuestionBotDB"]
configs_collection = db["server_configs"]

def get_server_config(guild_id):
    """Retrieves the configuration for a specific server from MongoDB."""
    return configs_collection.find_one({"_id": guild_id})

def is_premium(interaction: discord.Interaction):
    """Checks if the guild or user has an active subscription entitlement."""
    # Check if any active entitlement matches your premium SKU ID
    return any(entitlement.sku_id == PREMIUM_SKU_ID for entitlement in interaction.entitlements)

# ================= KNOWLEDGE BUILDER =================
async def build_knowledge_base(guild_id):
    """Reads the server's specific helper channel to build the AI's knowledge."""
    config = get_server_config(guild_id)
    if not config:
        return False

    dynamic_kb = ""
    
    # Use the channel ID saved in the database
    channel_id = config.get('add_channel')
    if not channel_id:
        return False

    helper_channel = client_discord.get_channel(channel_id)
    if helper_channel:
        try:
            messages = [msg async for msg in helper_channel.history(limit=500)]
            messages.reverse() 
            for msg in messages:
                if msg.content.strip():
                    dynamic_kb += f"- {msg.content}\n"
        except Exception as e:
            print(f"Error reading helper channel for guild {guild_id}: {e}")
    
    server_knowledge_memory[guild_id] = f"--- SERVER RULES & ANSWERS ---\n{dynamic_kb}"
    print(f"Knowledge compiled for server {guild_id}!")
    return True

# ================= EVENTS & COMMANDS =================
@client_discord.event
async def on_ready():
    await tree.sync() 
    print(f'Logged in as {client_discord.user}')
    print("MongoDB Connected and Slash Commands synced.")

@tree.command(name="setup", description="Basic setup for the AI Bot channels")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, 
                questions_channel: discord.TextChannel, 
                missing_answers_channel: discord.TextChannel, 
                add_answers_channel: discord.TextChannel):
    """Slash command for Server Admins to set up the basic bot channels."""
    
    configs_collection.update_one(
        {"_id": interaction.guild_id},
        {"$set": {
            "questions_channel": questions_channel.id,
            "missing_channel": missing_answers_channel.id,
            "add_channel": add_answers_channel.id
        }},
        upsert=True
    )

    await build_knowledge_base(interaction.guild_id)

    await interaction.response.send_message(
        f"✅ **Basic Setup Complete!**\n"
        f"- Questions: {questions_channel.mention}\n"
        f"- Unanswered: {missing_answers_channel.mention}\n"
        f"- Knowledge: {add_answers_channel.mention}\n\n"
        f"💎 *Premium members can use `/changeapi` and `/changemodel` to unlock full AI power.*", 
        ephemeral=True 
    )

@tree.command(name="changeapi", description="💎 Premium: Change the Gemini API Key for this server")
@app_commands.checks.has_permissions(administrator=True)
async def change_api(interaction: discord.Interaction, api_key: str):
    """Premium command to set a custom API Key."""
    if not is_premium(interaction):
        await interaction.response.send_message(
            "❌ **Subscription Required**: This feature is locked. Please subscribe via the server shop to use your own API keys.", 
            ephemeral=True
        )
        return

    configs_collection.update_one(
        {"_id": interaction.guild_id},
        {"$set": {"api_key": api_key}},
        upsert=True
    )
    
    await interaction.response.send_message("💎 **Success:** The server API key has been updated.", ephemeral=True)

@tree.command(name="changemodel", description="💎 Premium: Choose a more powerful AI model")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(model=[
    app_commands.Choice(name="Gemini 2.0 Flash (Fast & Default)", value="gemini-2.0-flash"),
    app_commands.Choice(name="Gemini 2.0 Pro (Most Intelligent)", value="gemini-2.0-pro-exp-02-05"),
    app_commands.Choice(name="Gemini 1.5 Pro (Better for long text)", value="gemini-1.5-pro"),
])
async def change_model(interaction: discord.Interaction, model: app_commands.Choice[str]):
    """Premium command to change the AI model."""
    if not is_premium(interaction):
        await interaction.response.send_message(
            "❌ **Subscription Required**: You need an active subscription to upgrade the AI model.", 
            ephemeral=True
        )
        return

    configs_collection.update_one(
        {"_id": interaction.guild_id},
        {"$set": {"model_name": model.value}},
        upsert=True
    )
    
    await interaction.response.send_message(f"💎 **Model Updated:** The bot is now using `{model.name}`.", ephemeral=True)

@tree.command(name="reload_knowledge", description="Forces the bot to re-read the knowledge channel")
@app_commands.checks.has_permissions(administrator=True)
async def reload_knowledge(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    success = await build_knowledge_base(interaction.guild_id)
    if success:
        await interaction.followup.send("✅ Bot has re-read the knowledge channel successfully!")
    else:
        await interaction.followup.send("❌ Error: Server not set up yet. Run `/setup` first.")

@client_discord.event
async def on_message(message):
    if message.author == client_discord.user or not message.guild:
        return

    config = get_server_config(message.guild.id)
    if not config:
        return

    # Check if this is the knowledge channel
    if message.channel.id == config.get('add_channel'):
        await build_knowledge_base(message.guild.id) 
        await message.add_reaction("🧠")
        return

    # Check if this is the questions channel
    if message.channel.id != config.get('questions_channel'):
        return

    is_question = message.content.strip().endswith("?")
    is_mentioned = client_discord.user in message.mentions

    if not (is_question or is_mentioned):
        return

    # Check if an API key exists (Premium users must set this via /changeapi)
    api_key = config.get("api_key")
    if not api_key:
        await message.reply("⚠️ No API key configured. An admin must run `/changeapi` (Premium Feature).")
        return

    if message.guild.id not in server_knowledge_memory:
        await build_knowledge_base(message.guild.id)
        
    kb_text = server_knowledge_memory.get(message.guild.id, "")
    model_name = config.get("model_name", DEFAULT_MODEL)

    async with message.channel.typing():
        try:
            # Get conversation context
            history_buffer = []
            async for msg in message.channel.history(limit=5):
                history_buffer.append(f"{msg.author.name}: {msg.clean_content}")
            
            history_buffer.reverse()
            conversation_text = "\n".join(history_buffer)

            prompt = (
                f"You are a helpful assistant for a Discord server. "
                f"Use the 'Knowledge Base' below to answer the user's question.\n\n"
                f"INSTRUCTIONS:\n"
                f"1. If the answer is NOT in the Knowledge Base, reply exactly 'SILENCE'.\n"
                f"2. Do not use markdown headers.\n\n"
                f"KNOWLEDGE BASE:\n{kb_text}\n\n"
                f"--- CONVERSATION HISTORY ---\n{conversation_text}\n\n"
                f"User Question: {message.content}"
            )

            # Initialize client with server-specific API key and Model
            server_genai_client = genai.Client(api_key=api_key)
            response = await server_genai_client.aio.models.generate_content(
                model=model_name,
                contents=prompt
            )
            
            if response.text:
                response_text = response.text.strip()
                
                if response_text == "SILENCE":
                    missing_channel_id = config.get('missing_channel')
                    missing_channel = client_discord.get_channel(missing_channel_id)
                    if missing_channel:
                        await missing_channel.send(
                            f"🚨 **Unanswered Question**\n"
                            f"**User:** {message.author.mention}\n"
                            f"**Question:** {message.content}"
                        )
                    return 

                # Handle Discord's 2000 character limit
                if len(response_text) > 2000:
                    for i in range(0, len(response_text), 1900):
                        await message.channel.send(response_text[i:i+1900])
                else:
                    await message.reply(response_text)

        except Exception as e:
            print(f"API Error: {e}")
            if "API_KEY_INVALID" in str(e):
                 await message.channel.send("⚠️ The API key for this server is invalid.")
            else:
                 await message.channel.send("⚠️ I encountered an error while thinking.")

keep_alive()
client_discord.run(DISCORD_TOKEN)

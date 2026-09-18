import os
import json
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

# The server IDs provided by the user
SERVER_IDS = [
    "a4ecfba6-2c2d-47db-bd46-58843bafd8ed",
    "34f3a634-8db3-4725-8264-44bbc6bb39d3"
]

API_URL = "https://wardogserverlist.com/api/server"
MESSAGE_ID_FILE = "message_id.json"

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def get_saved_message_id():
    if os.path.exists(MESSAGE_ID_FILE):
        with open(MESSAGE_ID_FILE, "r") as f:
            data = json.load(f)
            return data.get("message_id")
    return None

def save_message_id(message_id):
    with open(MESSAGE_ID_FILE, "w") as f:
        json.dump({"message_id": message_id}, f)

async def fetch_server_data(session, server_id):
    # Pass as `key=id|<server_id>` according to the API spec
    url = f"{API_URL}?key=id|{server_id}"
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                return await response.json()
            else:
                logging.warning(f"Failed to fetch {server_id}: HTTP {response.status}")
                return None
    except Exception as e:
        logging.error(f"Error fetching {server_id}: {e}")
        return None

@tasks.loop(seconds=60)
async def update_status_embed():
    if not CHANNEL_ID:
        logging.error("DISCORD_CHANNEL_ID is not set.")
        return

    channel = bot.get_channel(int(CHANNEL_ID))
    if not channel:
        try:
            channel = await bot.fetch_channel(int(CHANNEL_ID))
        except Exception as e:
            logging.error(f"Could not fetch channel: {e}")
            return

    embed = discord.Embed(
        title="📡 WARDOGS Server Status", 
        color=discord.Color.green(),
        description="Live overview of our servers."
    )

    async with aiohttp.ClientSession() as session:
        for s_id in SERVER_IDS:
            data = await fetch_server_data(session, s_id)
            if data and "server" in data:
                srv = data["server"]
                name = srv.get("name", "Unknown Server")
                players = srv.get("players", 0)
                max_players = srv.get("maxPlayers", 0)
                map_name = srv.get("map", "Unknown")
                mode = srv.get("gameMode", "Unknown")
                
                # Format a nice value string
                value = (
                    f"**Status:** 🟢 Online\n"
                    f"**Players:** {players}/{max_players}\n"
                    f"**Map:** {map_name}\n"
                    f"**Mode:** {mode}\n"
                )
                embed.add_field(name=name, value=value, inline=False)
            else:
                # Offline or not found
                embed.add_field(
                    name=f"Server: {s_id}", 
                    value="**Status:** 🔴 Offline / Not Found", 
                    inline=False
                )
    
    embed.set_footer(text="Updates every 60 seconds")

    message_id = get_saved_message_id()
    message = None

    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            logging.info("Updated existing embed.")
        except discord.NotFound:
            logging.warning("Saved message not found, creating a new one.")
            message = None
        except Exception as e:
            logging.error(f"Error editing message: {e}")
            message = None

    if message is None:
        try:
            new_message = await channel.send(embed=embed)
            save_message_id(new_message.id)
            logging.info("Created new embed and saved message ID.")
        except Exception as e:
            logging.error(f"Error sending new message: {e}")

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user.name}")
    if not update_status_embed.is_running():
        update_status_embed.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Please set DISCORD_TOKEN in your .env file.")
    else:
        bot.run(DISCORD_TOKEN)

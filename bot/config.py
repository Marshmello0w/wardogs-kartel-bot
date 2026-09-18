import os
from dotenv import load_dotenv

# Lade die .env Datei
load_dotenv()

# Discord Bot Token
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Channel ID für den Server Status Embed
SERVER_STATUS_CHANNEL_ID = os.getenv("SERVER_STATUS_CHANNEL_ID")

# Konfiguration für das Server Status Feature
SERVER_IDS = [
    "a4ecfba6-2c2d-47db-bd46-58843bafd8ed",
    "34f3a634-8db3-4725-8264-44bbc6bb39d3"
]
API_URL = "https://wardogserverlist.com/api/server"
MESSAGE_ID_FILE = "message_id.json"

# Datenbank Konfiguration
DB_CONNECTION_URL = os.getenv("DB_CONNECTION_URL")

# RCON Konfiguration für Leaderboard (Server 2)
SERVER2_RCON_URL = os.getenv("SERVER2_RCON_URL", "http://84.32.176.40:20001")
SERVER2_RCON_PASS = os.getenv("SERVER2_RCON_PASS", "jWxyom4CXuFjCPsN")
LEADERBOARD_CHANNEL_ID = os.getenv("LEADERBOARD_CHANNEL_ID", SERVER_STATUS_CHANNEL_ID)
LEADERBOARD_MSG_FILE = "leaderboard_msg.json"


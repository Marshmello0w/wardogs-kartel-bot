import os
from dotenv import load_dotenv

# Lade die .env Datei
load_dotenv()

# Discord Konfiguration
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SERVER_STATUS_CHANNEL_ID = os.getenv("SERVER_STATUS_CHANNEL_ID")
DISCORD_LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID")

# Konfiguration für das Server Status Feature
SERVER_IDS = [
    "a4ecfba6-2c2d-47db-bd46-58843bafd8ed",
    "34f3a634-8db3-4725-8264-44bbc6bb39d3"
]
API_URL = "https://wardogserverlist.com/api/server"
MESSAGE_ID_FILE = "message_id.json"

# Datenbank Konfiguration
DB_CONNECTION_URL = os.getenv("DB_CONNECTION_URL")

# RCON Konfiguration für Leaderboard (Server 1 & 2)
LEADERBOARD_CHANNEL_ID = os.getenv("LEADERBOARD_CHANNEL_ID", "")

SERVER1_RCON_URL = os.getenv("SERVER1_RCON_URL", "")
SERVER1_RCON_PASS = os.getenv("SERVER1_RCON_PASS", "")

SERVER2_RCON_URL = os.getenv("SERVER2_RCON_URL", "http://84.32.176.40:20001")
SERVER2_RCON_PASS = os.getenv("SERVER2_RCON_PASS", "jWxyom4CXuFjCPsN")

# Map Voting
SERVER2_VOTE_CHANNEL_ID = os.getenv("SERVER2_VOTE_CHANNEL_ID", "")
ADMIN_ROLE_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ROLE_IDS", "").split(",") if x.strip().isdigit()]

# Format: Name im Discord -> (Map, Experience, Lighting)
MAP_VOTE_OPTIONS = {
    "Bakurani": {"Map": "Kavkazi", "Experience": "Bakurani_KOTH_01", "Lighting": "DayClear"},
    "Ozeti": {"Map": "Kavkazi", "Experience": "Ozeti_KOTH_01", "Lighting": "DayClear"},
    "Zestafona": {"Map": "Kavkazi", "Experience": "Zestafona_KOTH_01", "Lighting": "DayClear"}
}


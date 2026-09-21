import os
import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse
from dotenv import load_dotenv

# Lade die .env Datei
load_dotenv()

# Discord Konfiguration
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
SERVER_STATUS_CHANNEL_ID = os.getenv("SERVER_STATUS_CHANNEL_ID")
SERVER_RECAP_CHANNEL_ID = os.getenv("SERVER_RECAP_CHANNEL_ID")
SERVER_RECAP_MESSAGE_IDS_FILE = "server_recap_message_ids.json"
DISCORD_LOG_CHANNEL_ID = os.getenv("DISCORD_LOG_CHANNEL_ID")
VIP_CHANNEL_ID = os.getenv("VIP_CHANNEL_ID", "1550791504455532554")


def _bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        logging.warning("Invalid %s; using %s", name, default)
        return default
    if minimum <= value <= maximum:
        return value
    logging.warning("Invalid %s; using %s", name, default)
    return default

# Konfiguration für das Server Status Feature
SERVER_IDS = [id.strip() for id in os.getenv("SERVER_IDS", "a4ecfba6-2c2d-47db-bd46-58843bafd8ed,34f3a634-8db3-4725-8264-44bbc6bb39d3").split(",") if id.strip()]
API_URL = "https://wardogserverlist.com/api/server"
MESSAGE_ID_FILE = "message_id.json"

# Datenbank Konfiguration
DB_CONNECTION_URL = os.getenv("DB_CONNECTION_URL")

# RCON Konfiguration für Leaderboard (Server 1 & 2)
LEADERBOARD_CHANNEL_ID = os.getenv("LEADERBOARD_CHANNEL_ID", "")

SERVER1_RCON_URL = os.getenv("SERVER1_RCON_URL", "")
SERVER1_RCON_PASS = os.getenv("SERVER1_RCON_PASS", "")

SERVER2_RCON_URL = os.getenv("SERVER2_RCON_URL", "")
SERVER2_RCON_PASS = os.getenv("SERVER2_RCON_PASS", "")

# Map Voting
SERVER2_VOTE_CHANNEL_ID = os.getenv("SERVER2_VOTE_CHANNEL_ID", "")
SERVER3_RCON_URL = os.getenv("SERVER3_RCON_URL", "")
SERVER3_RCON_PASS = os.getenv("SERVER3_RCON_PASS", "")
RCON_TIMEOUT_SECONDS = _bounded_int("RCON_TIMEOUT_SECONDS", 10, 1, 60)
SERVER3_VOTE_CHANNEL_ID = os.getenv("SERVER3_VOTE_CHANNEL_ID", "")
ADMIN_ROLE_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ROLE_IDS", "").split(",") if x.strip().isdigit()]
ADMIN_PANEL_CHANNEL_ID = os.getenv("ADMIN_PANEL_CHANNEL_ID", "")
ADMIN_PANEL_MSG_ID_FILE = "admin_panel_msg_id.json"


# -----------------
# Ingame Broadcasts (Runden-Ende)
# -----------------
# Der Bot wählt am Ende jeder Runde zufällig eine dieser Nachrichten aus und sendet sie auf dem Server.
BROADCAST_MESSAGES = [
    "Immer die neuesten News & Events zu WarDogs mitbekommen und neue Teamkollegen kennenlernen 👉 hier geht’s zum Discord: https://discord.gg/bakuranikartell",
    "Während ihr gerade wartet: Verpasst keine News, Events und unser neues Leaderboard auf dem Discord: https://discord.gg/bakuranikartell",
    "Dir gefällt der Server? Lass uns ein Feedback auf unserem Discord da: https://discord.gg/bakuranikartell"
]

# Nach der deutschen Runden-Ende-Nachricht wird nach kurzer Einblendepause eine
# inhaltlich gleiche englische Variante zufällig ausgewählt.
BROADCAST_FOLLOWUP_DELAY_SECONDS = 10
ENGLISH_BROADCAST_MESSAGES = [
    "Stay up to date with the latest WarDogs news and events, and find new teammates 👉 join our Discord: https://discord.gg/bakuranikartell",
    "While you're waiting: don't miss news, events, and our new leaderboard on Discord: https://discord.gg/bakuranikartell",
    "Enjoying the server? Leave us some feedback on our Discord: https://discord.gg/bakuranikartell"
]

# Automatisches Rundenende bei dauerhaft zu geringer Spielerzahl.
LOW_POPULATION_THRESHOLD = 20
LOW_POPULATION_GRACE_SECONDS = 5 * 60
LOW_POPULATION_COUNTDOWN_SECONDS = 2 * 60
LOW_POPULATION_ENGLISH_DELAY_SECONDS = 10
LOW_POPULATION_WARNING_DE = (
    "Aufgrund der geringen Spielerzahl wird die Runde in 2 Minuten beendet, "
    "um unfaire Vorteile zu vermeiden. Euer Admin-Team."
)
LOW_POPULATION_WARNING_EN = (
    "Due to the low player count, this round will end in 2 minutes "
    "to prevent unfair advantages. Your Admin Team."
)
LOW_POPULATION_RECOVERED_DE = "Die Spielerzahl ist wieder ausreichend. Die Runde wird fortgesetzt. Euer Admin-Team."
LOW_POPULATION_RECOVERED_EN = "The player count is sufficient again. The round will continue. Your Admin Team."
LOW_POPULATION_ENDING_DE = "Die Runde wird jetzt aufgrund der geringen Spielerzahl beendet. Euer Admin-Team."

# Quest-System: dauerhafte Punkte sowie wöchentliche Fraktions-Meilensteine.
QUEST_PLAYTIME_SECONDS_PER_POINT = 60 * 60
QUEST_CASH_PER_POINT = 100_000
QUEST_TEAM_MILESTONES = ((2 * 60 * 60, 5), (4 * 60 * 60, 5))
# Diese drei Fraktionen stehen in WarDogs zur Auswahl. White ist bewusst nicht
# enthalten: Dort befindet sich ein Spieler noch im Auswahlmenü.
QUEST_TEAMS = ('Lonestar', 'Valkyra', 'Manticore')
QUEST_SAMPLE_DELAY_SECONDS = 15
QUEST_MAX_OBSERVATION_GAP_SECONDS = 90
QUEST_PERMANENT_SYNC_SECONDS = 5 * 60

# Belohnungsshop
REWARD_FACTION_COST = 20
VIP_WEEK_COST = 150
VIP_MONTH_COST = 550
VIP_SLOT_LIMIT = 20
VIP_EXPIRY_CHECK_SECONDS = 60

# Format: Name im Discord -> (Map, Experience, Lighting)
MAP_VOTE_OPTIONS = {
    "Bakurani": {"Map": "Kavkazi", "Experience": "Bakurani_KOTH_01", "Lighting": "DayClear"},
    "Ozeti": {"Map": "Europe", "Experience": "Madrid_KOTH_01", "Lighting": "DayStartClear"}
}


@dataclass(frozen=True)
class Server:
    id: str
    title: str
    url: str
    password: str = field(repr=False)
    uuid: str = ""
    vote_channel_id: str = ""

    @property
    def enabled(self):
        try:
            parsed = urlparse(self.url)
            port = parsed.port
            return bool(parsed.scheme in ("http", "https") and parsed.hostname and self.password
                        and not parsed.username and not parsed.password and not parsed.query
                        and not parsed.fragment and (port is None or port > 0))
        except ValueError:
            return False


def servers():
    return tuple(Server(
        f"server{i}", f"Server {i}", globals()[f"SERVER{i}_RCON_URL"].rstrip("/"),
        globals()[f"SERVER{i}_RCON_PASS"],
        SERVER_IDS[i - 1] if len(SERVER_IDS) >= i else "",
        globals().get(f"SERVER{i}_VOTE_CHANNEL_ID", ""),
    ) for i in range(1, 4))


def server(server_id):
    return next(s for s in servers() if s.id == server_id)


def validate():
    """Disable only the misconfigured surface; never print credentials."""
    for key in ("GUILD_ID", "SERVER_STATUS_CHANNEL_ID", "SERVER_RECAP_CHANNEL_ID", "DISCORD_LOG_CHANNEL_ID",
                "LEADERBOARD_CHANNEL_ID", "ADMIN_PANEL_CHANNEL_ID", "VIP_CHANNEL_ID",
                "SERVER2_VOTE_CHANNEL_ID", "SERVER3_VOTE_CHANNEL_ID"):
        value = globals()[key]
        if value and (not str(value).isascii() or not str(value).isdigit() or int(value) <= 0):
            logging.error("Invalid %s; associated surface disabled", key)
            globals()[key] = ""
    for srv in servers():
        if not srv.enabled:
            logging.warning("%s disabled: missing or invalid RCON configuration", srv.title)
    return bool(DISCORD_BOT_TOKEN)


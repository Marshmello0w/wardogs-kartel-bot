import logging
import aiomysql
import aiohttp
import discord
from datetime import datetime, timezone
from discord.ext import tasks, commands

import config
import database


class StatsTracker(commands.Cog):
    """
    Zentraler Statistik-Tracker. Sammelt alle 60 Sekunden:
    - Spielzeit pro Spieler pro Server
    - Faction-Wahl pro Spieler
    - Ping pro Spieler (Durchschnitt)
    - Spieleranzahl-Snapshots (für Peak-Zeiten)
    - Runden-Historie (Gewinner-Faction, Dauer, Map)
    """

    def __init__(self, bot):
        self.bot = bot
        self.db_pool = None

        # Spieler-Tracking State
        self.last_online = {"server1": set(), "server2": set(), "server3": set()}

        # Runden-Tracking State
        self.round_state = {}  # pro server_id: {"active": bool, "started_at": datetime, "map": str, ...}
        self.last_scores = {}  # pro server_id: highest_score vom letzten Poll

        self.track_stats.start()

    def cog_unload(self):
        self.track_stats.cancel()

    def _get_servers(self):
        return [
            {"id": "server1", "title": "Server 1", "rcon_url": config.SERVER1_RCON_URL, "rcon_pass": config.SERVER1_RCON_PASS},
            {"id": "server2", "title": "Server 2", "rcon_url": config.SERVER2_RCON_URL, "rcon_pass": config.SERVER2_RCON_PASS},
            {"id": "server3", "title": "Server 3", "rcon_url": config.SERVER3_RCON_URL, "rcon_pass": config.SERVER3_RCON_PASS},
        ]

    async def fetch_players(self, session, rcon_url, rcon_pass):
        """Holt die Spielerliste mit Name, SteamID, Faction, Kills, Deaths, Cash, Ping."""
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/players"
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("players", [])
        except Exception as e:
            logging.error(f"StatsTracker: Error fetching players from {rcon_url}: {e}")
        return None

    async def fetch_status(self, session, rcon_url, rcon_pass):
        """Holt den Server-Status mit Map, Scores, Spielerzahl."""
        headers = {"Authorization": f"Bearer {rcon_pass}"}
        url = f"{rcon_url.rstrip('/')}/v1/status"
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
        except Exception as e:
            logging.error(f"StatsTracker: Error fetching status from {rcon_url}: {e}")
        return None

    # ─────────────────────────────────────────────
    # Haupt-Loop: Alle 60 Sekunden
    # ─────────────────────────────────────────────
    @tasks.loop(seconds=60)
    async def track_stats(self):
        self.db_pool = await database.check_and_reconnect(self.db_pool)
        if not self.db_pool:
            return

        servers = self._get_servers()

        async with aiohttp.ClientSession() as session:
            for srv in servers:
                if not srv["rcon_url"] or not srv["rcon_pass"]:
                    continue

                # Beide Endpoints parallel abfragen
                players = await self.fetch_players(session, srv["rcon_url"], srv["rcon_pass"])
                status = await self.fetch_status(session, srv["rcon_url"], srv["rcon_pass"])

                # ── Spieler-Statistiken ──
                if players is not None:
                    await self._process_players(srv["id"], players)

                # ── Server-Statistiken ──
                if status is not None:
                    await self._process_server_status(srv["id"], status)

    # ─────────────────────────────────────────────
    # Spieler-Daten verarbeiten
    # ─────────────────────────────────────────────
    async def _process_players(self, server_id, players):
        current_online = set()

        for p in players:
            steam_id = p.get("steamId")
            if not steam_id:
                continue

            name = p.get("name", "Unknown")
            faction = p.get("faction", "Unknown")
            ping = p.get("pingMs", 0)

            current_online.add(steam_id)

            was_online = steam_id in self.last_online.get(server_id, set())

            try:
                async with self.db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        # 1. Spielzeit (nur wenn vorher schon online war → +60s)
                        if was_online:
                            await cur.execute('''
                                INSERT INTO player_playtime (server_id, steam_id, name, playtime_seconds)
                                VALUES (%s, %s, %s, 60)
                                ON DUPLICATE KEY UPDATE
                                    name = VALUES(name),
                                    playtime_seconds = playtime_seconds + 60,
                                    last_seen = NOW()
                            ''', (server_id, steam_id, name))

                        # 2. Faction-Tracking
                        await cur.execute('''
                            INSERT INTO player_faction_stats (server_id, steam_id, faction, times_seen)
                            VALUES (%s, %s, %s, 1)
                            ON DUPLICATE KEY UPDATE
                                times_seen = times_seen + 1
                        ''', (server_id, steam_id, faction))

                        # 3. Ping-Tracking
                        if ping and ping > 0:
                            await cur.execute('''
                                INSERT INTO player_ping_stats (server_id, steam_id, total_ping, ping_samples)
                                VALUES (%s, %s, %s, 1)
                                ON DUPLICATE KEY UPDATE
                                    total_ping = total_ping + VALUES(total_ping),
                                    ping_samples = ping_samples + 1
                            ''', (server_id, steam_id, ping))

            except Exception as e:
                logging.error(f"StatsTracker: Error processing player {steam_id} on {server_id}: {e}")

        self.last_online[server_id] = current_online

    # ─────────────────────────────────────────────
    # Server-Status verarbeiten
    # ─────────────────────────────────────────────
    async def _process_server_status(self, server_id, status):
        player_count = status.get("players", {}).get("current", 0)
        faction_scores = status.get("factionScores", [])
        current_map = status.get("map", "Unknown")
        experiences = status.get("experiences", [])
        lighting = status.get("lighting", "Unknown")

        # Höchsten Score finden
        highest_score = 0
        winner_faction = None
        for f in faction_scores:
            score = f.get("score", 0)
            if score > highest_score:
                highest_score = score
                winner_faction = f.get("name", "Unknown")

        try:
            async with self.db_pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 4. Spieleranzahl-Snapshot (für Peak-Zeiten)
                    await cur.execute('''
                        INSERT INTO server_player_snapshots (server_id, player_count)
                        VALUES (%s, %s)
                    ''', (server_id, player_count))

        except Exception as e:
            logging.error(f"StatsTracker: Error saving player snapshot for {server_id}: {e}")

        # ── Runden-Tracking ──
        prev_score = self.last_scores.get(server_id, 0)
        state = self.round_state.get(server_id)

        # Neue Runde erkannt (Score fällt unter 10 → frisch gestartet)
        if highest_score < 10 and prev_score >= 10:
            self.round_state[server_id] = {
                "active": True,
                "started_at": datetime.now(timezone.utc),
                "map": current_map,
                "experience": ", ".join(experiences) if experiences else "Unknown",
                "lighting": lighting,
            }

        # Runde beendet erkannt (Score erreicht 100)
        if highest_score >= 100 and prev_score < 100 and state and state.get("active"):
            ended_at = datetime.now(timezone.utc)
            started_at = state["started_at"]
            duration = int((ended_at - started_at).total_seconds())

            try:
                async with self.db_pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        # 5. Runden-Historie
                        await cur.execute('''
                            INSERT INTO round_history (server_id, winner_faction, map_name, experience, lighting, started_at, ended_at, duration_seconds)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (server_id, winner_faction, state["map"], state["experience"], state["lighting"], started_at, ended_at, duration))

                        # 6. Faction-Wins Counter
                        if winner_faction:
                            await cur.execute('''
                                INSERT INTO faction_wins (server_id, faction, wins)
                                VALUES (%s, %s, 1)
                                ON DUPLICATE KEY UPDATE
                                    wins = wins + 1
                            ''', (server_id, winner_faction))

                        # 7. Map-Statistiken
                        await cur.execute('''
                            INSERT INTO map_play_stats (server_id, map_name, times_played)
                            VALUES (%s, %s, 1)
                            ON DUPLICATE KEY UPDATE
                                times_played = times_played + 1
                        ''', (server_id, state["map"]))

            except Exception as e:
                logging.error(f"StatsTracker: Error saving round history for {server_id}: {e}")

            # Runde als beendet markieren
            self.round_state[server_id] = {"active": False}

        self.last_scores[server_id] = highest_score

    @track_stats.before_loop
    async def before_track_stats(self):
        await self.bot.wait_until_ready()
        self.db_pool = await database.get_db_pool()


async def setup(bot):
    await bot.add_cog(StatsTracker(bot))

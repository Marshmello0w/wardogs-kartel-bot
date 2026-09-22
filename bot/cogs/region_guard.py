"""Restart empty servers until WarDogs confirms the requested region.

The public server list is deliberately slow.  RCON decides whether a server is
empty, while the list's ``updatedAt`` timestamp decides whether a restart has
been observed by the public backend.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from core import config
from core.permissions import require_admin
from infrastructure import storage


BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


class PterodactylError(RuntimeError):
    """Safe operational error; no remote body or token is retained."""

    @property
    def safe_message(self):
        return str(self)


def utc_now():
    return datetime.now(UTC)


def as_utc(value: datetime | None = None):
    value = value or utc_now()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def berlin_day(value: datetime | None = None):
    return as_utc(value).astimezone(BERLIN).date().isoformat()


def in_restart_window(value: datetime | None = None):
    local = as_utc(value).astimezone(BERLIN)
    return config.REGION_GUARD_START_HOUR <= local.hour < config.REGION_GUARD_END_HOUR


def restart_allowed(state, value: datetime | None = None):
    """Allow the regular window or an explicit early-release before it opens.

    An early release never extends the guard past its normal 09:00 cutoff.
    """
    local = as_utc(value).astimezone(BERLIN)
    return local.hour < config.REGION_GUARD_END_HOUR and (
        local.hour >= config.REGION_GUARD_START_HOUR or bool(state.get("early_enabled")))


def parse_api_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def response_is_new_enough(updated_at, restarted_at):
    """A public response is usable only after the configured post-restart delay."""
    return bool(updated_at and restarted_at and
                updated_at >= restarted_at + timedelta(seconds=config.REGION_GUARD_API_DELAY_SECONDS))


def initial_state(day):
    return {
        "day": day,
        "paused": False,
        "panel_disabled": False,
        "completed": False,
        "early_enabled": False,
        "phase": "idle",
        "empty_since": None,
        "restart_at": None,
        "last_query_at": None,
        "last_api_updated_at": None,
        "last_region": None,
        "link_key": None,
        "last_error": None,
    }


class RegionGuardCog(commands.Cog):
    """Daily, independently persisted region recovery for all configured servers."""

    health_key = "EU-Central Region-Guard"

    def __init__(self, bot):
        self.bot = bot
        self.locks = defaultdict(asyncio.Lock)
        self.states = {}
        self.panel_ids = {}
        self.panel_checked_at = None
        self.ptero_session = None
        self.public_session = None
        self.monitor.start()

    def cog_unload(self):
        self.monitor.cancel()
        for session in (self.ptero_session, self.public_session):
            if session and not session.closed:
                asyncio.create_task(session.close())

    async def _ptero_session(self):
        if self.ptero_session is None or self.ptero_session.closed:
            timeout = aiohttp.ClientTimeout(total=config.REGION_GUARD_HTTP_TIMEOUT_SECONDS)
            self.ptero_session = aiohttp.ClientSession(timeout=timeout, headers={
                "Authorization": f"Bearer {config.PTERODACTYL_CLIENT_API_KEY}",
                "Accept": "Application/vnd.pterodactyl.v1+json",
                "User-Agent": "KartellBot/region-guard",
            })
        return self.ptero_session

    async def _public_session(self):
        if self.public_session is None or self.public_session.closed:
            timeout = aiohttp.ClientTimeout(total=config.REGION_GUARD_HTTP_TIMEOUT_SECONDS)
            self.public_session = aiohttp.ClientSession(timeout=timeout, headers={
                "User-Agent": "KartellBot/region-guard (Discord: Marshmello0w)",
            })
        return self.public_session

    async def load(self, server_id, now=None):
        day = berlin_day(now)
        if server_id not in self.states:
            state = await storage.get_state("region_guard", server_id)
            state = dict(initial_state(day), **(state or {}))
        else:
            state = dict(self.states[server_id])
        # Pause is an explicit admin setting and deliberately survives days.
        if state.get("day") != day:
            paused = bool(state.get("paused"))
            panel_disabled = bool(state.get("panel_disabled"))
            link_key = state.get("link_key")
            state = initial_state(day)
            state.update(paused=paused, panel_disabled=panel_disabled, link_key=link_key)
        self.states[server_id] = state
        return state

    async def save(self, server_id, state):
        state = dict(state)
        await storage.save_state("region_guard", server_id, state)
        self.states[server_id] = state

    def _problem(self, srv, state, message):
        if state.get("last_error") != message:
            self.bot.dispatch("bot_log", "⚠️ Regionsprüfung wartet",
                              f"**{srv.title}:** {message}", discord.Color.orange())
        state["last_error"] = message

    async def _discover_panel_servers(self):
        """Map exactly one Pterodactyl server to each configured panel name."""
        now = utc_now()
        if self.panel_checked_at and (now - self.panel_checked_at).total_seconds() < 300:
            return self.panel_ids
        if not config.PTERODACTYL_CLIENT_API_KEY:
            raise PterodactylError("Pterodactyl-API-Key fehlt")
        session = await self._ptero_session()
        try:
            async with session.get(f"{config.PTERODACTYL_API_BASE_URL}/api/client") as response:
                if response.status != 200:
                    raise PterodactylError(f"Pterodactyl HTTP {response.status}")
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError:
            raise PterodactylError("Pterodactyl Timeout") from None
        except aiohttp.ClientError:
            raise PterodactylError("Pterodactyl Verbindung fehlgeschlagen") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise PterodactylError("Pterodactyl ungültige Antwort")

        found = {}
        for server_id, expected_name in config.PTERODACTYL_SERVER_NAMES.items():
            matches = []
            for item in payload["data"]:
                attributes = item.get("attributes") if isinstance(item, dict) else None
                if not isinstance(attributes, dict):
                    continue
                if str(attributes.get("name", "")).casefold() == expected_name.casefold():
                    identifier = str(attributes.get("identifier", "")).strip()
                    if identifier:
                        matches.append(identifier)
            if len(matches) == 1:
                found[server_id] = matches[0]
        self.panel_ids = found
        self.panel_checked_at = now
        return found

    async def _restart(self, srv, state, now):
        """Ask Pterodactyl for exactly one restart after a fresh final RCON check."""
        tracker = self.bot.get_cog("RoundTracker")
        current = tracker.current(srv.id) if tracker else None
        players = ((current or {}).get("snapshot", {}).get("players", {}).get("current"))
        if not isinstance(players, int) or players != 0:
            return False
        panel_ids = await self._discover_panel_servers()
        identifier = panel_ids.get(srv.id)
        if not identifier:
            raise PterodactylError("Server im Pterodactyl-Panel nicht eindeutig gefunden")
        session = await self._ptero_session()
        try:
            async with session.post(f"{config.PTERODACTYL_API_BASE_URL}/api/client/servers/{identifier}/power",
                                    json={"signal": "restart"}) as response:
                if response.status not in (200, 204):
                    raise PterodactylError(f"Pterodactyl Restart HTTP {response.status}")
        except asyncio.TimeoutError:
            raise PterodactylError("Pterodactyl Restart Timeout") from None
        except aiohttp.ClientError:
            raise PterodactylError("Pterodactyl Restart-Verbindung fehlgeschlagen") from None
        state.update(phase="awaiting_api", restart_at=now.isoformat(), last_query_at=None,
                     last_api_updated_at=None, last_region=None, last_error=None)
        self.bot.dispatch("bot_log", "🔄 Region-Guard startet Server neu",
                          f"**{srv.title}** war leer. Warte auf einen öffentlichen Regionsnachweis.",
                          discord.Color.blue())
        return True

    async def _server_api(self, srv, state):
        """Fetch one server entry, preferring a learned stable link key."""
        params = {"key": state["link_key"]} if state.get("link_key") else {"code": srv.uuid}
        session = await self._public_session()
        async def fetch(query):
            try:
                async with session.get("https://wardogserverlist.com/api/server", params=query) as response:
                    if response.status != 200:
                        return None, response.status
                    return await response.json(content_type=None), None
            except asyncio.TimeoutError:
                return None, "timeout"
            except aiohttp.ClientError:
                return None, "connection"

        payload, failure = await fetch(params)
        # A link key can disappear after a host-side re-registration. The last
        # known join code is the safe fallback that lets us learn a new key.
        if failure == 404 and state.get("link_key") and srv.uuid:
            payload, failure = await fetch({"code": srv.uuid})
        if failure:
            messages = {"timeout": "WarDogs API Timeout", "connection": "WarDogs API Verbindung fehlgeschlagen"}
            return None, messages.get(failure, f"WarDogs API HTTP {failure}")
        if not isinstance(payload, dict) or not isinstance(payload.get("server"), dict):
            return None, "WarDogs API ungültige Antwort"
        server = payload["server"]
        updated_at = parse_api_timestamp(payload.get("updatedAt"))
        if not updated_at:
            return None, "WarDogs API ohne gültigen Zeitstempel"
        link_key = server.get("linkKey")
        if isinstance(link_key, str) and link_key.strip():
            state["link_key"] = link_key.strip()
        region = server.get("region")
        return {"updated_at": updated_at, "region": str(region or "").casefold()}, None

    async def tick_server(self, srv, now=None):
        now = as_utc(now)
        async with self.locks[srv.id]:
            state = await self.load(srv.id, now)
            if (not restart_allowed(state, now) or state.get("paused") or state.get("panel_disabled") or
                    state.get("completed")):
                await self.save(srv.id, state)
                return

            tracker = self.bot.get_cog("RoundTracker")
            current = tracker.current(srv.id) if tracker else None
            players = ((current or {}).get("snapshot", {}).get("players", {}).get("current"))
            if not isinstance(players, int):
                self._problem(srv, state, "RCON-Spielerstand ist nicht frisch genug.")
                await self.save(srv.id, state)
                return
            if players != 0:
                state["empty_since"] = None
                if state.get("restart_at"):
                    state.update(phase="waiting_empty", restart_at=None, last_query_at=None)
                    self.bot.dispatch("bot_log", "⏸️ Region-Guard pausiert",
                                      f"**{srv.title}:** Spieler sind wieder online; kein weiterer Restart.",
                                      discord.Color.orange())
                await self.save(srv.id, state)
                return

            if not state.get("restart_at"):
                empty_since = parse_api_timestamp(state.get("empty_since"))
                if not empty_since:
                    state.update(empty_since=now.isoformat(), phase="waiting_empty", last_error=None)
                    await self.save(srv.id, state)
                    return
                if now < empty_since + timedelta(seconds=config.REGION_GUARD_EMPTY_SECONDS):
                    await self.save(srv.id, state)
                    return
                if state.get("phase") == "waiting_empty":
                    state.update(phase="idle", last_error=None)
                    self.bot.dispatch("bot_log", "▶️ Region-Guard fortgesetzt",
                                      f"**{srv.title}** ist seit fünf Minuten leer.", discord.Color.blue())
                try:
                    if await self._restart(srv, state, now):
                        await self.save(srv.id, state)
                        return
                    self._problem(srv, state, "Server ist nicht mehr eindeutig leer.")
                except PterodactylError as exc:
                    state.update(panel_disabled=True, phase="disabled", restart_at=None)
                    self._problem(srv, state, exc.safe_message)
                await self.save(srv.id, state)
                return

            restarted_at = parse_api_timestamp(state.get("restart_at"))
            if not restarted_at:
                state.update(restart_at=None, phase="idle")
                self._problem(srv, state, "Ungültige gespeicherte Restart-Zeit; Vorgang wird neu aufgebaut.")
                await self.save(srv.id, state)
                return
            ready_at = restarted_at + timedelta(seconds=config.REGION_GUARD_API_DELAY_SECONDS)
            last_query_at = parse_api_timestamp(state.get("last_query_at"))
            if now < ready_at or (last_query_at and
                                  now < last_query_at + timedelta(seconds=config.REGION_GUARD_API_POLL_SECONDS)):
                await self.save(srv.id, state)
                return

            state["last_query_at"] = now.isoformat()
            result, problem = await self._server_api(srv, state)
            if problem:
                self._problem(srv, state, problem)
                await self.save(srv.id, state)
                return
            updated_at, region = result["updated_at"], result["region"]
            state.update(last_api_updated_at=updated_at.isoformat(), last_region=region or None)
            if not response_is_new_enough(updated_at, restarted_at):
                self._problem(srv, state, "Warte auf einen API-Zeitstempel mindestens vier Minuten nach dem Restart.")
                await self.save(srv.id, state)
                return
            state["last_error"] = None
            if region == config.REGION_GUARD_TARGET:
                state.update(completed=True, phase="complete")
                self.bot.dispatch("bot_log", "✅ Region-Guard erfolgreich",
                                  f"**{srv.title}** wurde mit Region **{region}** bestätigt.",
                                  discord.Color.green())
                await self.save(srv.id, state)
                return
            try:
                if not await self._restart(srv, state, now):
                    state.update(phase="waiting_empty", restart_at=None)
            except PterodactylError as exc:
                state.update(panel_disabled=True, phase="disabled", restart_at=None)
                self._problem(srv, state, exc.safe_message)
            await self.save(srv.id, state)

    async def tick(self):
        now = utc_now()
        for srv in config.servers():
            if not srv.enabled:
                continue
            await self.tick_server(srv, now)

    async def initialise_panel(self):
        """Fail closed per server before the first daily restart window opens."""
        try:
            mapped = await self._discover_panel_servers()
        except PterodactylError as exc:
            self.bot.dispatch("bot_log", "⚠️ Region-Guard Panel nicht verfügbar", exc.safe_message,
                              discord.Color.orange())
            return
        for srv in config.servers():
            if not srv.enabled or srv.id in mapped:
                continue
            async with self.locks[srv.id]:
                state = await self.load(srv.id)
                state.update(panel_disabled=True, phase="disabled",
                             last_error="Server im Pterodactyl-Panel nicht eindeutig gefunden")
                await self.save(srv.id, state)
            self.bot.dispatch("bot_log", "⚠️ Region-Guard Server deaktiviert",
                              f"**{srv.title}** wurde im Pterodactyl-Panel nicht eindeutig gefunden.",
                              discord.Color.orange())

    @tasks.loop(seconds=15)
    async def monitor(self):
        try:
            await self.tick()
            self.bot.health.ok(self.health_key)
        except Exception as exc:
            self.bot.health.error(self.health_key, exc)

    @monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()
        await self.initialise_panel()

    def status_embed(self):
        embed = discord.Embed(title="EU-Central Region-Guard", color=discord.Color.blue())
        for srv in config.servers():
            state = self.states.get(srv.id) or initial_state(berlin_day())
            description = (f"**Status:** {state.get('phase', 'idle')}\n"
                           f"**Pausiert:** {'Ja' if state.get('paused') else 'Nein'}\n"
                           f"**Frühfreigabe:** {'Ja' if state.get('early_enabled') else 'Nein'}\n"
                           f"**Panel deaktiviert:** {'Ja' if state.get('panel_disabled') else 'Nein'}\n"
                           f"**Heute abgeschlossen:** {'Ja' if state.get('completed') else 'Nein'}\n"
                           f"**Letzte Region:** {state.get('last_region') or '—'}")
            if state.get("last_error"):
                description += f"\n**Wartet auf:** {state['last_error']}"
            embed.add_field(name=srv.title, value=description[:1024], inline=False)
        return embed

    @app_commands.command(name="regionguard", description="Steuert die automatische EU-Central-Regionsprüfung.")
    @app_commands.choices(
        action=[app_commands.Choice(name="Status", value="status"),
                app_commands.Choice(name="Pausieren", value="pause"),
                app_commands.Choice(name="Fortsetzen", value="resume")],
        server=[app_commands.Choice(name=f"Server {number}", value=f"server{number}") for number in range(1, 4)],
    )
    async def region_guard(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                           server: app_commands.Choice[str] | None = None):
        if not await require_admin(interaction):
            return
        if action.value == "status":
            for srv in config.servers():
                await self.load(srv.id)
            await interaction.response.send_message(embed=self.status_embed(), ephemeral=True)
            return
        if server is None:
            await interaction.response.send_message("Bitte wähle einen Server.", ephemeral=True)
            return
        async with self.locks[server.value]:
            state = await self.load(server.value)
            state["paused"] = action.value == "pause"
            if state["paused"]:
                state.update(phase="paused", restart_at=None, last_query_at=None)
            else:
                state.update(panel_disabled=False, last_error=None)
                if state.get("phase") in ("paused", "disabled"):
                    state["phase"] = "idle"
            await self.save(server.value, state)
        wording = "pausierte" if action.value == "pause" else "aktivierte"
        self.bot.dispatch("bot_log", "🛠️ Region-Guard Einstellung",
                          f"{interaction.user.mention} {wording} **{config.server(server.value).title}**.",
                          discord.Color.orange() if action.value == "pause" else discord.Color.blue())
        await interaction.response.send_message(
            f"✅ Region-Guard für {config.server(server.value).title} "
            f"{'pausiert' if action.value == 'pause' else 'aktiviert'}.", ephemeral=True)

    @app_commands.command(name="regionguardstart", description="Gibt den Region-Guard vor 03:00 Uhr für einen leeren Server frei.")
    @app_commands.choices(server=[app_commands.Choice(name=f"Server {number}", value=f"server{number}") for number in range(1, 4)])
    async def region_guard_start(self, interaction: discord.Interaction, server: app_commands.Choice[str]):
        if not await require_admin(interaction):
            return
        now = utc_now()
        local = now.astimezone(BERLIN)
        if local.hour >= config.REGION_GUARD_END_HOUR:
            await interaction.response.send_message(
                f"Die Regionsprüfung endet um {config.REGION_GUARD_END_HOUR:02d}:00 Uhr. Eine Frühfreigabe ist heute nicht mehr möglich.",
                ephemeral=True)
            return
        async with self.locks[server.value]:
            state = await self.load(server.value, now)
            if state.get("completed"):
                await interaction.response.send_message("Dieser Server wurde heute bereits erfolgreich geprüft.", ephemeral=True)
                return
            state["early_enabled"] = True
            await self.save(server.value, state)
        self.bot.dispatch("bot_log", "🛠️ Region-Guard früh freigegeben",
                          f"{interaction.user.mention} gab **{config.server(server.value).title}** vorzeitig frei. "
                          "Ein Restart ist erst nach fünf Minuten durchgehendem Leerstand möglich.", discord.Color.blue())
        await interaction.response.send_message(
            f"✅ Frühfreigabe für {config.server(server.value).title} erteilt. Der Server startet nur, wenn er fünf Minuten am Stück leer bleibt.",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(RegionGuardCog(bot))

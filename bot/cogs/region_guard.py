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


def current_api_observation(updated_at, now):
    """Do not restart from an old or future-dated public server entry."""
    return bool(updated_at and
                timedelta(0) <= now - updated_at <= timedelta(seconds=config.REGION_GUARD_BASELINE_MAX_AGE_SECONDS))


def confirmation_matches(candidate, result):
    first = parse_api_timestamp((candidate or {}).get("updated_at"))
    return bool(first and candidate.get("instance_id") == result["instance_id"]
                and candidate.get("region") == result["region"]
                and result["updated_at"] >= first + timedelta(seconds=config.REGION_GUARD_CONFIRMATION_GAP_SECONDS))


def confirmation_sample(result):
    return {"instance_id": result["instance_id"], "region": result["region"],
            "updated_at": result["updated_at"].isoformat()}


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
        "last_instance_id": None,
        "previous_instance_id": None,
        "verified_instance_id": None,
        "pending_confirmation": None,
        "success_probe": None,
        "occupied_logged": False,
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
        self.trace_at = {}
        self.monitor.start()

    def log(self, title, description, color):
        """Route all Region-Guard events through the central logger separately."""
        self.bot.dispatch("bot_log", title, description, color, config.REGION_GUARD_LOG_CHANNEL_ID or None)

    def trace(self, srv, title, description, *, repeat=False):
        """Detailed test trace for every Region-Guard decision and external call."""
        if not config.REGION_GUARD_VERBOSE_LOGGING:
            return
        if repeat:
            trace_at = getattr(self, "trace_at", None)
            if trace_at is None:
                trace_at = self.trace_at = {}
            key, now = (srv.id, title), utc_now()
            previous = trace_at.get(key)
            if previous and (now - previous).total_seconds() < config.REGION_GUARD_TRACE_REPEAT_SECONDS:
                return
            trace_at[key] = now
        self.log(f"🔎 {title}", f"**{srv.title}:** {description}", discord.Color.blurple())

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
            self.log("⚠️ Regionsprüfung wartet", f"**{srv.title}:** {message}", discord.Color.orange())
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
        previous_id = state.get("last_instance_id")
        if not previous_id:
            self.trace(srv, "Restart abgebrochen", "Vorherige Spielinstanz-ID ist nicht belegt.")
            return False
        tracker = self.bot.get_cog("RoundTracker")
        current = tracker.current(srv.id) if tracker else None
        players = ((current or {}).get("snapshot", {}).get("players", {}).get("current"))
        if not isinstance(players, int) or players != 0:
            self.trace(srv, "Restart abgebrochen", "Finale RCON-Prüfung bestätigt keinen leeren Server.")
            return False
        self.trace(srv, "Restart-Vorprüfung", "RCON bestätigt 0 Spieler. Pterodactyl-Server wird ermittelt.")
        panel_ids = await self._discover_panel_servers()
        identifier = panel_ids.get(srv.id)
        if not identifier:
            raise PterodactylError("Server im Pterodactyl-Panel nicht eindeutig gefunden")
        self.trace(srv, "Pterodactyl-Restart", "Restart-Aufruf wird an das Panel gesendet.")
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
        self.trace(srv, "Pterodactyl-Restart", "Panel hat den Restart angenommen.")
        state.update(phase="awaiting_api", restart_at=now.isoformat(), last_query_at=None,
                     last_api_updated_at=None, last_region=None, last_error=None,
                     previous_instance_id=previous_id, verified_instance_id=None,
                     pending_confirmation=None, success_probe=None, occupied_logged=False)
        self.log("🔄 Region-Guard startet Server neu",
                 f"**{srv.title}** war leer. Vorherige Spielinstanz: `{previous_id}`. "
                 "Warte auf eine neue Spielinstanz und den öffentlichen Regionsnachweis.", discord.Color.blue())
        return True

    async def _server_api(self, srv, state):
        """Fetch one server entry, preferring a learned stable link key."""
        params = {"key": state["link_key"]} if state.get("link_key") else {"code": srv.uuid}
        session = await self._public_session()
        async def fetch(query, source):
            self.trace(srv, "WarDogs-API Anfrage", f"Öffentliche Serverliste wird über {source} abgefragt.")
            try:
                async with session.get("https://wardogserverlist.com/api/server", params=query) as response:
                    if response.status != 200:
                        self.trace(srv, "WarDogs-API Antwort", f"HTTP {response.status}.")
                        return None, response.status
                    return await response.json(content_type=None), None
            except asyncio.TimeoutError:
                self.trace(srv, "WarDogs-API Antwort", "Timeout.")
                return None, "timeout"
            except aiohttp.ClientError:
                self.trace(srv, "WarDogs-API Antwort", "Verbindung fehlgeschlagen.")
                return None, "connection"

        payload, failure = await fetch(params, "stabilen Server-Schlüssel" if state.get("link_key") else "Join-Code")
        # A link key can disappear after a host-side re-registration. The last
        # known join code is the safe fallback that lets us learn a new key.
        if failure == 404 and state.get("link_key") and srv.uuid:
            payload, failure = await fetch({"code": srv.uuid}, "Join-Code nach Schlüssel-Fehler")
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
        instance_id = server.get("id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            return None, "WarDogs API ohne Spielinstanz-ID"
        instance_id = instance_id.strip()
        normalized_region = str(region or "").strip().casefold()
        if not normalized_region:
            return None, "WarDogs API ohne Region"
        self.trace(srv, "WarDogs-API Antwort",
                   f"Instanz: `{instance_id}` · Region: **{normalized_region or '—'}** · "
                   f"Datenstand: {updated_at:%d.%m.%Y %H:%M:%S UTC}.")
        return {"updated_at": updated_at, "region": normalized_region,
                "instance_id": instance_id}, None

    async def _check_completed(self, srv, state, now):
        """Observe a confirmed server until the window closes; never restart here."""
        if not state.get("verified_instance_id"):
            # Successes saved by the previous version cannot be re-proven after
            # deployment. Preserve today's completion instead of restarting.
            return
        last_query = parse_api_timestamp(state.get("last_query_at"))
        if last_query and now < last_query + timedelta(seconds=config.REGION_GUARD_SUCCESS_POLL_SECONDS):
            return
        state["last_query_at"] = now.isoformat()
        result, problem = await self._server_api(srv, state)
        if problem:
            self._problem(srv, state, problem)
            return
        updated_at = result["updated_at"]
        last_updated = parse_api_timestamp(state.get("last_api_updated_at"))
        if not current_api_observation(updated_at, now) or (last_updated and updated_at <= last_updated):
            return
        state["last_api_updated_at"] = updated_at.isoformat()
        state["last_error"] = None
        if (result["instance_id"] == state["verified_instance_id"]
                and result["region"] == config.REGION_GUARD_TARGET):
            state["success_probe"] = None
            return
        first = state.get("success_probe")
        if confirmation_matches(first, result):
            state["success_probe"] = None
            state["last_instance_id"] = result["instance_id"]
            state["last_region"] = result["region"] or None
            if result["region"] == config.REGION_GUARD_TARGET:
                state["verified_instance_id"] = result["instance_id"]
                self.log("ℹ️ Region-Guard neue Spielinstanz",
                         f"**{srv.title}** ist weiterhin **{result['region']}** "
                         f"(Instanz `{result['instance_id']}`).", discord.Color.blue())
            else:
                state.update(completed=False, phase="waiting_empty", restart_at=None, empty_since=None,
                             previous_instance_id=None, verified_instance_id=None)
                self.log("⚠️ Region-Guard Region gewechselt",
                         f"**{srv.title}** wurde zweimal mit **{result['region'] or 'unbekannter Region'}** "
                         "beobachtet. Ein neuer Restart ist erst nach fünf Minuten Leerstand möglich.",
                         discord.Color.orange())
            return
        if not first or (first.get("instance_id"), first.get("region")) != (result["instance_id"], result["region"]):
            state["success_probe"] = confirmation_sample(result)
            self.trace(srv, "Erfolgs-Nachprüfung",
                       "Abweichung erkannt; eine zweite unabhängige API-Aktualisierung wird abgewartet.")

    async def tick_server(self, srv, now=None):
        now = as_utc(now)
        async with self.locks[srv.id]:
            state = await self.load(srv.id, now)
            if not restart_allowed(state, now) or state.get("paused") or state.get("panel_disabled"):
                await self.save(srv.id, state)
                return

            if state.get("completed"):
                await self._check_completed(srv, state, now)
                await self.save(srv.id, state)
                return

            tracker = self.bot.get_cog("RoundTracker")
            current = tracker.current(srv.id) if tracker else None
            players = ((current or {}).get("snapshot", {}).get("players", {}).get("current"))
            self.trace(srv, "Region-Guard Prüfschritt",
                       f"Phase: **{state.get('phase')}** · RCON-Spieler: **{players if isinstance(players, int) else 'nicht verfügbar'}** · "
                       f"Restart läuft: **{'Ja' if state.get('restart_at') else 'Nein'}**.", repeat=True)
            # Before the first restart, an empty fresh RCON snapshot is the
            # safety gate. Once Pterodactyl has accepted a restart, RCON is
            # expected to be briefly unavailable; the public API confirmation
            # must continue independently until a future restart is considered.
            if not isinstance(players, int) and not state.get("restart_at"):
                self._problem(srv, state, "RCON-Spielerstand ist nicht frisch genug.")
                await self.save(srv.id, state)
                return
            if not isinstance(players, int):
                self.trace(srv, "RCON während Restart", "Nicht erreichbar; API-Nachweis wird trotzdem fortgesetzt.", repeat=True)
            if isinstance(players, int) and players != 0:
                state["empty_since"] = None
                if state.get("restart_at"):
                    if not state.get("occupied_logged"):
                        self.log("⏸️ Region-Guard Restart gesperrt",
                                 f"**{srv.title}:** Spieler sind online. Der API-Nachweis läuft weiter, "
                                 "aber ein weiterer Restart bleibt gesperrt.", discord.Color.orange())
                    state["occupied_logged"] = True
                else:
                    await self.save(srv.id, state)
                    return
            elif players == 0 and state.get("restart_at") and not state.get("empty_since"):
                state["empty_since"] = now.isoformat()
                state["occupied_logged"] = False

            if not state.get("restart_at"):
                empty_since = parse_api_timestamp(state.get("empty_since"))
                if not empty_since:
                    state.update(empty_since=now.isoformat(), phase="waiting_empty", last_error=None)
                    self.trace(srv, "Leerstand-Timer gestartet", "0 Spieler erkannt. Warte fünf Minuten durchgehenden Leerstand.")
                    await self.save(srv.id, state)
                    return
                if now < empty_since + timedelta(seconds=config.REGION_GUARD_EMPTY_SECONDS):
                    remaining = int((empty_since + timedelta(seconds=config.REGION_GUARD_EMPTY_SECONDS) - now).total_seconds())
                    self.trace(srv, "Leerstand-Timer", f"Server bleibt leer; noch **{max(0, remaining)} Sekunden** bis zur Restart-Freigabe.", repeat=True)
                    await self.save(srv.id, state)
                    return
                if state.get("phase") == "waiting_empty":
                    state.update(phase="idle", last_error=None)
                    self.log("▶️ Region-Guard fortgesetzt", f"**{srv.title}** ist seit fünf Minuten leer.", discord.Color.blue())
                baseline, problem = await self._server_api(srv, state)
                if problem:
                    self._problem(srv, state, f"Vor Restart: {problem}")
                    await self.save(srv.id, state)
                    return
                if not current_api_observation(baseline["updated_at"], now):
                    self._problem(srv, state, "Vor Restart: WarDogs-Daten sind nicht frisch genug.")
                    await self.save(srv.id, state)
                    return
                state.update(last_instance_id=baseline["instance_id"],
                             last_api_updated_at=baseline["updated_at"].isoformat(),
                             last_region=baseline["region"] or None)
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
            previous_id = state.get("previous_instance_id")
            if not previous_id:
                self._problem(srv, state, "Vorherige Spielinstanz-ID fehlt; kein automatischer Folgerestart.")
                await self.save(srv.id, state)
                return
            ready_at = restarted_at + timedelta(seconds=config.REGION_GUARD_API_DELAY_SECONDS)
            last_query_at = parse_api_timestamp(state.get("last_query_at"))
            last_api_updated_at = parse_api_timestamp(state.get("last_api_updated_at"))
            # Once the API gave us a valid but still pre-restart timestamp, it
            # is actively catching up. Poll it faster without increasing calls
            # for timeouts, errors, or normally fresh responses.
            poll_seconds = (config.REGION_GUARD_API_STALE_POLL_SECONDS
                            if last_api_updated_at and last_api_updated_at < ready_at
                            else config.REGION_GUARD_API_POLL_SECONDS)
            if now < ready_at or (last_query_at and
                                  now < last_query_at + timedelta(seconds=poll_seconds)):
                if now < ready_at:
                    remaining = int((ready_at - now).total_seconds())
                    self.trace(srv, "API-Wartezeit", f"Warte noch **{max(0, remaining)} Sekunden** auf frische Serverlisten-Daten.", repeat=True)
                else:
                    remaining = int((last_query_at + timedelta(seconds=poll_seconds) - now).total_seconds())
                    self.trace(srv, "API-Poll-Wartezeit", f"Nächste Serverlisten-Abfrage in **{max(0, remaining)} Sekunden**.", repeat=True)
                await self.save(srv.id, state)
                return

            state["last_query_at"] = now.isoformat()
            result, problem = await self._server_api(srv, state)
            if problem:
                self._problem(srv, state, problem)
                await self.save(srv.id, state)
                return
            updated_at, region = result["updated_at"], result["region"]
            state["last_api_updated_at"] = updated_at.isoformat()
            if not response_is_new_enough(updated_at, restarted_at) or not current_api_observation(updated_at, now):
                self._problem(srv, state, "Warte auf einen aktuellen API-Zeitstempel mindestens vier Minuten nach dem Restart.")
                await self.save(srv.id, state)
                return
            if result["instance_id"] == previous_id:
                self._problem(srv, state, "WarDogs zeigt noch die alte Spielinstanz; kein Regionsnachweis.")
                await self.save(srv.id, state)
                return
            state["last_error"] = None
            state["last_instance_id"] = result["instance_id"]
            first = state.get("pending_confirmation")
            if not confirmation_matches(first, result):
                if not first or (first.get("instance_id"), first.get("region")) != (result["instance_id"], region):
                    state["pending_confirmation"] = confirmation_sample(result)
                    self.trace(srv, "Regionsbestätigung",
                               "Neue Spielinstanz erkannt; warte auf eine zweite unabhängige API-Aktualisierung.")
                await self.save(srv.id, state)
                return
            state["pending_confirmation"] = None
            state["last_region"] = region or None
            if region == config.REGION_GUARD_TARGET:
                state.update(completed=True, phase="complete", verified_instance_id=result["instance_id"])
                self.log("✅ Region-Guard erfolgreich",
                         f"**{srv.title}** wurde mit neuer Spielinstanz `{result['instance_id']}` "
                         f"zweimal in Region **{region}** bestätigt.", discord.Color.green())
                await self.save(srv.id, state)
                return
            state.update(phase="waiting_empty", restart_at=None, empty_since=None,
                         previous_instance_id=None)
            self.log("🔁 Region-Guard falsche Region",
                     f"**{srv.title}** meldet auf neuer Spielinstanz `{result['instance_id']}` "
                     f"zweimal **{region or 'unbekannt'}**. Nächster Restart frühestens "
                     "nach fünf Minuten durchgehendem Leerstand.", discord.Color.orange())
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
            self.log("⚠️ Region-Guard Panel nicht verfügbar", exc.safe_message, discord.Color.orange())
            return
        for srv in config.servers():
            if not srv.enabled:
                continue
            async with self.locks[srv.id]:
                state = await self.load(srv.id)
                if srv.id in mapped:
                    recovered = bool(state.get("panel_disabled"))
                    if recovered:
                        state.update(panel_disabled=False, last_error=None)
                        if state.get("phase") == "disabled":
                            state["phase"] = "idle"
                        await self.save(srv.id, state)
                else:
                    recovered = False
                    state.update(panel_disabled=True, phase="disabled",
                                 last_error="Server im Pterodactyl-Panel nicht eindeutig gefunden")
                    await self.save(srv.id, state)
            if srv.id not in mapped:
                self.log("⚠️ Region-Guard Server deaktiviert",
                         f"**{srv.title}** wurde im Pterodactyl-Panel nicht eindeutig gefunden.", discord.Color.orange())
            elif recovered:
                self.log("✅ Region-Guard Panel wieder verfügbar",
                         f"**{srv.title}** wurde automatisch wieder freigegeben.", discord.Color.green())

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
                           f"**Letzte bestätigte Region:** {state.get('last_region') or '—'}\n"
                           f"**Bestätigte Spielinstanz:** {state.get('verified_instance_id') or '—'}\n"
                           f"**Regionsnachweis:** {'1/2' if state.get('pending_confirmation') else '—'}")
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
        self.log("🛠️ Region-Guard Einstellung", f"{interaction.user.mention} {wording} **{config.server(server.value).title}**.",
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
        self.log("🛠️ Region-Guard früh freigegeben",
                 f"{interaction.user.mention} gab **{config.server(server.value).title}** vorzeitig frei. "
                 "Ein Restart ist erst nach fünf Minuten durchgehendem Leerstand möglich.", discord.Color.blue())
        await interaction.response.send_message(
            f"✅ Frühfreigabe für {config.server(server.value).title} erteilt. Der Server startet nur, wenn er fünf Minuten am Stück leer bleibt.",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(RegionGuardCog(bot))

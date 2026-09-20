"""End active matches fairly when a server remains underpopulated."""
import asyncio
from dataclasses import dataclass
import time

import discord
from discord.ext import commands, tasks

from core import config
from services.rcon import RconError


@dataclass
class LowPopulationState:
    round_id: str
    low_since: float
    warning_sent: bool = False
    deadline: float | None = None
    english_task: asyncio.Task | None = None
    force_attempted: bool = False


class LowPopulationGuardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.states = {}
        self.monitor.start()

    def cog_unload(self):
        self.monitor.cancel()
        for state in self.states.values():
            if state.english_task:
                state.english_task.cancel()

    def clear(self, server_id):
        state = self.states.pop(server_id, None)
        if state and state.english_task:
            state.english_task.cancel()

    async def broadcast(self, server_id, message):
        reply = await self.bot.rcon.request(server_id, 'POST', '/v1/broadcast', payload={'message': message})
        # A 202 response cannot prove whether the game server displayed the text.
        if reply.status == 202:
            raise RconError(202, uncertain=True)

    def current_low_population_round(self, server_id):
        tracker = self.bot.get_cog('RoundTracker')
        state = tracker.current(server_id) if tracker else None
        if not isinstance(state, dict) or state.get('ended'):
            return None
        snapshot = state.get('snapshot')
        if not isinstance(snapshot, dict):
            return None
        players = snapshot.get('players')
        count = players.get('current') if isinstance(players, dict) else None
        highest = snapshot.get('highest')
        if (isinstance(count, bool) or not isinstance(count, int) or count < 0 or
                isinstance(highest, bool) or not isinstance(highest, (int, float)) or highest < 1):
            return None
        return state, count

    async def send_english_warning(self, srv, state):
        try:
            await asyncio.sleep(config.LOW_POPULATION_ENGLISH_DELAY_SECONDS)
            current = self.states.get(srv.id)
            observed = self.current_low_population_round(srv.id)
            if current is not state or not observed or observed[0]['round_id'] != state.round_id:
                return
            if observed[1] >= config.LOW_POPULATION_THRESHOLD or state.force_attempted:
                return
            await self.broadcast(srv.id, config.LOW_POPULATION_WARNING_EN)
            self.bot.dispatch('bot_log', '🌐 Niedrige Spielerzahl – englische Warnung',
                              f'{srv.title}: {config.LOW_POPULATION_WARNING_EN}', discord.Color.orange())
            self.bot.health.ok(f'Niedrige Spielerzahl {srv.title}')
        except asyncio.CancelledError:
            raise
        except RconError as exc:
            self.bot.health.error(f'Niedrige Spielerzahl {srv.title}', exc)
        except Exception as exc:
            self.bot.health.error(f'Niedrige Spielerzahl {srv.title}', exc)

    async def warn(self, srv, state, now):
        try:
            await self.broadcast(srv.id, config.LOW_POPULATION_WARNING_DE)
        except RconError as exc:
            self.bot.health.error(f'Niedrige Spielerzahl {srv.title}', exc)
            return
        state.warning_sent = True
        state.deadline = now + config.LOW_POPULATION_COUNTDOWN_SECONDS
        state.english_task = asyncio.create_task(self.send_english_warning(srv, state))
        self.bot.dispatch('bot_log', '⚠️ Niedrige Spielerzahl – Rundenende angekündigt',
                          f'{srv.title}: unter {config.LOW_POPULATION_THRESHOLD} Spieler für 5 Minuten. '
                          'Rundenende in 2 Minuten angekündigt.', discord.Color.orange())
        self.bot.health.ok(f'Niedrige Spielerzahl {srv.title}')

    async def cancel_countdown(self, srv):
        try:
            await self.broadcast(srv.id, config.LOW_POPULATION_RECOVERED_DE)
            self.bot.dispatch('bot_log', '✅ Niedrige Spielerzahl – Countdown abgebrochen',
                              f'{srv.title}: Spielerzahl ist wieder ausreichend.', discord.Color.green())
            self.bot.health.ok(f'Niedrige Spielerzahl {srv.title}')
        except RconError as exc:
            self.bot.health.error(f'Niedrige Spielerzahl {srv.title}', exc)
        finally:
            self.clear(srv.id)

    async def force_end(self, srv, state):
        try:
            await self.broadcast(srv.id, config.LOW_POPULATION_ENDING_DE)
        except RconError as exc:
            self.bot.health.error(f'Automatisches Rundenende {srv.title}', exc)
            return
        # Do not repeat a potentially delivered force-end request for this round.
        state.force_attempted = True
        if state.english_task:
            state.english_task.cancel()
        try:
            reply = await self.bot.rcon.request(srv.id, 'POST', '/v1/match/end')
            if reply.status == 202:
                raise RconError(202, uncertain=True)
            self.bot.dispatch('bot_log', '⏹️ Runde automatisch beendet',
                              f'{srv.title}: weniger als {config.LOW_POPULATION_THRESHOLD} Spieler nach dem Countdown.',
                              discord.Color.orange())
            self.bot.health.ok(f'Automatisches Rundenende {srv.title}')
        except RconError as exc:
            self.bot.health.error(f'Automatisches Rundenende {srv.title}', exc)
        except Exception as exc:
            self.bot.health.error(f'Automatisches Rundenende {srv.title}', exc)

    async def tick_server(self, srv, now=None):
        now = time.monotonic() if now is None else now
        observed = self.current_low_population_round(srv.id)
        state = self.states.get(srv.id)

        if not observed:
            self.clear(srv.id)
            return
        round_state, count = observed
        if state and state.round_id != round_state['round_id']:
            self.clear(srv.id)
            state = None
        if count >= config.LOW_POPULATION_THRESHOLD:
            if state and state.warning_sent and not state.force_attempted:
                await self.cancel_countdown(srv)
            else:
                self.clear(srv.id)
            return
        if state is None:
            self.states[srv.id] = LowPopulationState(round_id=round_state['round_id'], low_since=now)
            return
        if state.force_attempted:
            return
        if not state.warning_sent:
            if now - state.low_since >= config.LOW_POPULATION_GRACE_SECONDS:
                await self.warn(srv, state, now)
            return
        if state.deadline is not None and now >= state.deadline:
            await self.force_end(srv, state)

    async def safe_tick(self, srv):
        try:
            await self.tick_server(srv)
        except Exception as exc:
            self.bot.health.error(f'Niedrige Spielerzahl {srv.title}', exc)

    @tasks.loop(seconds=5)
    async def monitor(self):
        await asyncio.gather(*(self.safe_tick(srv) for srv in config.servers() if srv.enabled))

    @monitor.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(LowPopulationGuardCog(bot))

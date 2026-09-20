import asyncio
from collections import defaultdict
from copy import deepcopy
import discord
from discord.ext import tasks, commands
from discord import app_commands
from core import config
from core.permissions import require_admin
from core.runtime import read_state
from domain.rotation import entries, replace_entries, format_entry, RotationConflict
from infrastructure import storage


def _component_payload(value):
    """Remove Discord's server-assigned component IDs before comparing views."""
    if isinstance(value, list):
        return [_component_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _component_payload(item)
            for key, item in value.items()
            # Component IDs are an implementation detail added by Discord on fetch.
            # Do not remove an emoji's ID, which has no component ``type`` field.
            if not (key == 'id' and 'type' in value)
        }
    return value


def _panel_changed(message, embed, view):
    return (not message.embeds or message.embeds[0].to_dict() != embed.to_dict()
            or _component_payload([component.to_dict() for component in message.components])
            != _component_payload(view.to_components()))


class MapVoteButton(discord.ui.Button):
    def __init__(self, option, locked=False):
        super().__init__(label=option, custom_id=f'vote_{option}', style=discord.ButtonStyle.primary, disabled=locked)
        self.option = option

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog, server_id = self.view.cog, self.view.server_id
        try:
            async with cog.locks[server_id]:
                state = await cog.load(server_id)
                current = cog.bot.get_cog('RoundTracker').current(server_id)
                if (not current or not state['enabled'] or state['locked'] or
                        current['voting_closed'] or state.get('round_id') != current['round_id'] or
                        interaction.message.id != state.get('msg_id')):
                    await interaction.followup.send('Das Voting ist derzeit geschlossen oder der Serverstatus ist unbekannt.', ephemeral=True)
                    return
                state['votes'][str(interaction.user.id)] = self.option
                await cog.save(server_id, state)
                await interaction.message.edit(embed=cog.embed(server_id, state), view=MapVoteView(cog, server_id, state))
            await interaction.followup.send(f'Stimme für {self.option} gespeichert.', ephemeral=True)
        except Exception as exc:
            cog.bot.health.error(f'Voting {server_id}', exc)
            await interaction.followup.send('Voting momentan nicht verfügbar.', ephemeral=True)


class MapVoteView(discord.ui.View):
    def __init__(self, cog, server_id, state):
        super().__init__(timeout=None)
        self.cog, self.server_id = cog, server_id
        for option in config.MAP_VOTE_OPTIONS:
            self.add_item(MapVoteButton(option, not state['enabled'] or state['locked']))


class MapVoteCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = defaultdict(asyncio.Lock)
        self.states = {}
        self.check_match_scores.start()

    def cog_unload(self):
        self.check_match_scores.cancel()

    async def load(self, server_id):
        if server_id not in self.states:
            state = await storage.get_state('voting', server_id)
            if state is None:
                legacy = read_state('map_vote_state.json', {}).get(server_id, {})
                state = dict(enabled=legacy.get('enabled', True), msg_id=legacy.get('msg_id'),
                             votes=legacy.get('votes', {}), locked=legacy.get('locked', False),
                             round_id=None, change=None, winner=None, outcome=None)
                if legacy.get('injected_map') or legacy.get('waiting_to_cleanup'):
                    state['outcome'] = 'Alte Rotationsänderung vorhanden: manuelle Prüfung erforderlich.'
                    self.bot.dispatch('bot_log', '⚠️ Alte Rotation prüfen', config.server(server_id).title, discord.Color.orange())
                await storage.save_state('voting', server_id, state)
            self.states[server_id] = state
            if state.get('msg_id'):
                self.bot.add_view(MapVoteView(self, server_id, state), message_id=int(state['msg_id']))
        return deepcopy(self.states[server_id])

    async def save(self, server_id, state):
        await storage.save_state('voting', server_id, state)
        self.states[server_id] = deepcopy(state)

    def embed(self, server_id, state):
        title = config.server(server_id).title
        description = f'Stimme für die nächste Map auf {title} ab. Mindestens 5 Stimmen für die Gewinneroption.'
        if not state['enabled']:
            description = '⚠️ Voting vom Admin deaktiviert.'
        elif state['locked']:
            description = 'Voting für diese Runde geschlossen.'
        if state.get('outcome'):
            description += '\n' + state['outcome']
        embed = discord.Embed(title=f'🗺️ Map Voting: {title}', description=description,
                              color=discord.Color.red() if state['locked'] or not state['enabled'] else discord.Color.blue())
        for option in config.MAP_VOTE_OPTIONS:
            count = sum(v == option for v in state['votes'].values())
            embed.add_field(name=option, value=f'{count} Stimme(n)')
        return embed

    async def panel(self, srv, state, display=None):
        channel = self.bot.get_channel(int(srv.vote_channel_id)) or await self.bot.fetch_channel(int(srv.vote_channel_id))
        message = None
        if state.get('msg_id'):
            try:
                message = await channel.fetch_message(int(state['msg_id']))
            except discord.NotFound:
                pass
        shown = display if display is not None else state
        embed, view = self.embed(srv.id, shown), MapVoteView(self, srv.id, shown)
        if message is None:
            message = await channel.send(embed=embed, view=view)
            state['msg_id'] = message.id
            await self.save(srv.id, state)
        elif _panel_changed(message, embed, view):
            await message.edit(embed=embed, view=view)

    async def prepare_change(self, srv, state, winner, current):
        document = await self.bot.rcon.get(srv.id, '/v1/config')
        before = entries(document['text'])
        # Insert an extra entry rather than deleting an existing matching map.
        # Preserve its exact position so cleanup can restore the original array.
        slot = current['snapshot'].get('rotation', {}).get('nextIndex')
        if not isinstance(slot, int) or not 0 <= slot <= len(before):
            slot = 0
        after = before[:slot] + [format_entry(config.MAP_VOTE_OPTIONS[winner])] + before[slot:]
        state['change'] = dict(before=before, after=after, round_id=current['round_id'], status='pending')
        await self.save(srv.id, state)

    async def apply_change(self, srv, state):
        change = state['change']
        if change['status'] == 'pending':
            await self.bot.rcon.edit_config(srv.id, lambda text: replace_entries(text, change['before'], change['after']))
            change['status'] = 'applied'
            state['outcome'] = f"✅ {state['winner']} als nächste Map gesetzt."
            await self.save(srv.id, state)
            self.bot.dispatch('bot_log', '🗺️ Map-Voting angewendet', f"{srv.title}: {state['winner']}", discord.Color.blue())

    async def cleanup(self, srv, state, current):
        change = state.get('change')
        if not change or change['status'] == 'conflict' or change['round_id'] == current['round_id']:
            return
        # A pending request may have succeeded before a crash: the same CAS cleanup
        # accepts both the untouched 'before' and the applied 'after' array.
        if current['snapshot']['highest'] <= 0:
            return
        await self.bot.rcon.edit_config(srv.id, lambda text: replace_entries(text, change['after'], change['before']))
        state['change'] = None
        await self.save(srv.id, state)
        self.bot.dispatch('bot_log', '🗺️ Rotation wiederhergestellt', srv.title, discord.Color.blue())

    async def tick_server(self, srv):
        async with self.locks[srv.id]:
            state = await self.load(srv.id)
            tracker = self.bot.get_cog('RoundTracker')
            current = tracker.current(srv.id) if tracker else None
            if not current:
                # Temporary lock only for display; preserve the persisted round votes.
                display = dict(state, locked=True, outcome='Serverstatus momentan unbekannt.')
                await self.panel(srv, state, display)
                return
            try:
                await self.cleanup(srv, state, current)
                if state.get('round_id') != current['round_id']:
                    initial_import = state.get('round_id') is None
                    state.update(round_id=current['round_id'], votes=state['votes'] if initial_import else {},
                                 locked=False, winner=None)
                    if not state.get('change'):
                        state['outcome'] = None
                    # Evaluate imported votes too when the first observed round is near its end.
                    await self.save(srv.id, state)
                if current['voting_closed'] and not state['locked']:
                    state['locked'] = True
                    counts = {opt: sum(v == opt for v in state['votes'].values()) for opt in config.MAP_VOTE_OPTIONS}
                    eligible = [opt for opt in counts if counts[opt] >= 5]
                    state['winner'] = max(eligible, key=counts.get) if eligible and state['enabled'] else None
                    state['outcome'] = 'Auswertung gespeichert; Serveränderung ausstehend.' if state['winner'] else 'Kein Map-Wechsel: nicht genügend Stimmen oder Voting deaktiviert.'
                    await self.save(srv.id, state)
                    self.bot.dispatch('bot_log', '🗺️ Voting geschlossen', f"{srv.title}: {state['outcome']}", discord.Color.blue())
                if state['winner'] and state['enabled'] and not current['ended']:
                    if not state.get('change'):
                        await self.prepare_change(srv, state, state['winner'], current)
                    if state['change']['round_id'] == current['round_id']:
                        await self.apply_change(srv, state)
                elif state['winner'] and current['ended'] and not state.get('change'):
                    state['outcome'] = 'Rundenende bereits erreicht; keine verspätete Rotationsänderung.'
                    await self.save(srv.id, state)
            except RotationConflict as exc:
                if state.get('change'):
                    state['change']['status'] = 'conflict'
                state['outcome'] = '⚠️ Rotation extern verändert: manuelle Prüfung erforderlich.'
                await self.save(srv.id, state)
                self.bot.health.error(f'Rotationskonflikt {srv.title}', exc)
            await self.panel(srv, state)

    async def safe_tick(self, srv):
        try:
            await self.tick_server(srv)
            self.bot.health.ok(f'Voting {srv.title}')
        except Exception as exc:
            self.bot.health.error(f'Voting {srv.title}', exc)

    @tasks.loop(seconds=5)
    async def check_match_scores(self):
        await asyncio.gather(*(self.safe_tick(s) for s in config.servers() if s.enabled and s.vote_channel_id))

    @check_match_scores.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name='voting', description='Schaltet das Map-Voting an oder aus.')
    @app_commands.choices(
        server=[app_commands.Choice(name='Server 2', value='server2'), app_commands.Choice(name='Server 3', value='server3')],
        status=[app_commands.Choice(name='On', value='on'), app_commands.Choice(name='Off', value='off')])
    async def toggle_voting(self, interaction: discord.Interaction, server: app_commands.Choice[str], status: app_commands.Choice[str]):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.locks[server.value]:
                state = await self.load(server.value)
                state['enabled'] = status.value == 'on'
                await self.save(server.value, state)
            if config.server(server.value).vote_channel_id:
                await self.tick_server(config.server(server.value))
            await interaction.followup.send(f"✅ Voting {'aktiviert' if state['enabled'] else 'deaktiviert'}.", ephemeral=True)
            self.bot.dispatch('bot_log', '🗺️ Voting-Einstellung', f'{interaction.user.mention}: {server.value} {status.value}', discord.Color.blue())
        except Exception as exc:
            self.bot.health.error('Voting-Einstellung', exc)
            await interaction.followup.send('Einstellung konnte nicht vollständig angewendet werden; siehe Bot-Log.', ephemeral=True)

    @app_commands.command(name='forcemap', description='Setzt eine Map für die nächste Runde.')
    @app_commands.choices(
        server=[app_commands.Choice(name='Server 2', value='server2'), app_commands.Choice(name='Server 3', value='server3')],
        map_name=[app_commands.Choice(name=opt, value=opt) for opt in config.MAP_VOTE_OPTIONS])
    async def force_map(self, interaction: discord.Interaction, server: app_commands.Choice[str], map_name: app_commands.Choice[str]):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.locks[server.value]:
                state = await self.load(server.value)
                current = self.bot.get_cog('RoundTracker').current(server.value)
                if not current or current['ended'] or state.get('change'):
                    await interaction.followup.send('Serverstatus unklar, Runde beendet oder Rotationsauftrag bereits vorhanden.', ephemeral=True)
                    return
                state.update(winner=map_name.value, locked=True, round_id=current['round_id'])
                await self.prepare_change(config.server(server.value), state, map_name.value, current)
                await self.apply_change(config.server(server.value), state)
            await interaction.followup.send('✅ Map für die nächste Runde gesetzt.', ephemeral=True)
        except Exception as exc:
            self.bot.health.error('Force-Map', exc)
            await interaction.followup.send('Map-Auftrag nicht bestätigt; siehe Bot-Log.', ephemeral=True)


async def setup(bot):
    await bot.add_cog(MapVoteCog(bot))

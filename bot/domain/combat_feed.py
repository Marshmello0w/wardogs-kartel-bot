"""Pure normalization and conservative classification of kill-feed events."""
from datetime import datetime, timezone
import math

from core import config
from core.permissions import valid_steam_id
from cogs.quest_tracker import round_is_active_for_quests

HEADSHOT_TAG = 'Meta.Progression.Context.Player.KillContext.Headshot'


def map_identity(value):
    """Resolve the public map name and the game-internal map/experience aliases."""
    name = str(value or '').strip().casefold()
    for public_name, option in config.MAP_VOTE_OPTIONS.items():
        aliases = {
            public_name.casefold(),
            str(option['Map']).casefold(),
            str(option['Experience']).split('_', 1)[0].casefold(),
        }
        if name in aliases:
            return public_name.casefold()
    return name


def steam_id(value):
    candidate = str(value or '')
    return candidate if valid_steam_id(candidate) else None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def normalized_kill(event, instance_id, received_at):
    if not isinstance(event, dict) or event.get('type') != 'killed':
        return None
    event_id = event.get('eventId')
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100:
        return None
    killer, victim = steam_id(event.get('killerSteamId')), steam_id(event.get('victimSteamId'))
    tags = event.get('contextTags')
    tags = tags if isinstance(tags, list) else []
    distance = number(event.get('distance'))
    return {
        'event_id': event_id, 'instance_id': str(instance_id or '')[:100],
        'received_at': received_at, 'event_time': number(event.get('eventTime')),
        'killer': killer, 'victim': victim,
        'killer_name': str(event.get('killerName') or '')[:100],
        'victim_name': str(event.get('victimName') or '')[:100],
        'map_name': str(event.get('mapName') or '')[:100],
        'cause': str(event.get('cause') or '')[:160],
        'distance_m': distance / 100 if distance is not None else None,
        'headshot': HEADSHOT_TAG in tags,
        'suicide': killer is not None and killer == victim,
    }


def roster_for_event(sample, received_at, event_map=None):
    """Only one known, fresh, running round can prove faction relationships."""
    if not sample:
        return None
    state, players = sample
    if state.get('uncertain') or not round_is_active_for_quests(state):
        return None
    observed_map = state.get('snapshot', {}).get('map')
    if event_map and observed_map and map_identity(event_map) != map_identity(observed_map):
        return None
    try:
        observed = datetime.fromisoformat(state['observed_at'])
    except (KeyError, TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = (received_at - observed).total_seconds()
    if not 0 <= age <= 20:
        return None
    teams = {}
    for player in players:
        candidate = steam_id(player.get('steamId'))
        faction = str(player.get('faction') or '').strip()
        real_team = next((team for team in config.QUEST_TEAMS
                          if team.casefold() == faction.casefold()), None)
        if candidate and real_team:
            teams[candidate] = real_team
    return str(state['round_id']), teams


def relation(event, roster):
    if not roster or not event['killer'] or not event['victim'] or event['suicide']:
        return None
    killer_team = roster[1].get(event['killer'])
    victim_team = roster[1].get(event['victim'])
    if not killer_team or not victim_team:
        return None
    return 'teamkill' if killer_team == victim_team else 'enemy'

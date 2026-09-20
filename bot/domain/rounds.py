"""Pure round state machine. No I/O: replayable against recorded status samples."""
from copy import deepcopy
from datetime import datetime
import math
from uuid import uuid4


def normalize_status(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('map'), str):
        raise ValueError('Missing map')
    scores = raw.get('factionScores')
    if not isinstance(scores, list) or not scores:
        raise ValueError('Missing scores')
    for item in scores:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            raise ValueError('Invalid faction')
        score = item.get('score')
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0:
            raise ValueError('Invalid score')
    cap = raw.get('scoreCap', 100)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
        cap = 100
    experiences = raw.get('experiences', [])
    if not isinstance(experiences, list) or not all(isinstance(e, str) for e in experiences):
        raise ValueError('Invalid experiences')
    highest = max(item['score'] for item in scores)
    winners = [item['name'] for item in scores if item['score'] == highest]
    return dict(raw, scoreCap=cap, highest=highest, progress=highest / cap,
                experiences=experiences, winner=winners[0] if len(winners) == 1 else None)


def identity(snapshot):
    return [snapshot['map'], snapshot['experiences']]


def advance(previous, raw, now):
    snapshot = normalize_status(raw)
    state = deepcopy(previous) if previous else None
    events = []

    def event(kind, target):
        events.append(dict(id=f"{target['round_id']}:{kind}", kind=kind,
                           round_id=target['round_id'], observed_at=now,
                           data=deepcopy(target)))

    def new_round(started_at=None, quality='partial'):
        return dict(round_id=str(uuid4()), snapshot=snapshot, started_at=started_at,
                    observed_at=now, quality=quality, ended=False, voting_closed=False,
                    near_end=False, candidate=None, uncertain=False)

    if state is None:
        state = new_round()
        event('round_started', state)
    else:
        old = state['snapshot']
        gap = (datetime.fromisoformat(now) - datetime.fromisoformat(state['observed_at'])).total_seconds()
        changed = identity(snapshot) != identity(old)
        after_end = state['ended'] and snapshot['progress'] < 1
        reset = (old['progress'] >= .10 and snapshot['progress'] < .10) or after_end
        candidate = state.get('candidate')
        if changed or reset:
            if candidate and candidate['identity'] == identity(snapshot) and (
                    candidate['reason'] == 'map' or snapshot['progress'] < .10 or after_end):
                if not state['ended']:
                    state['quality'] = 'missing_end'
                    state['ended'] = True
                    state['winner'] = None
                    state['ended_at'] = candidate['at']
                    event('round_ended', state)
                state = new_round(candidate['at'] if gap <= 90 else None,
                                  'observed' if gap <= 90 else 'gap')
                event('round_started', state)
            else:
                state['candidate'] = dict(identity=identity(snapshot), at=now, reason='map' if changed else 'reset')
                state['uncertain'] = True
                return state, events
        else:
            state['candidate'] = None
            state['uncertain'] = snapshot['highest'] < old['highest']
            if gap > 90:
                state['quality'] = 'gap'
            elif state['uncertain']:
                state['quality'] = 'counter_drop'
            state['snapshot'] = snapshot
            state['observed_at'] = now

    if not state['uncertain']:
        for threshold, flag, kind in ((.95, 'voting_closed', 'round_voting_closed'),
                                      (.99, 'near_end', 'round_near_end')):
            if snapshot['progress'] >= threshold and not state[flag]:
                state[flag] = True
                event(kind, state)
        if snapshot['progress'] >= 1 and not state['ended']:
            state['ended'] = True
            state['ended_at'] = now
            state['winner'] = snapshot['winner']
            event('round_ended', state)
    return state, events


def counter_delta(previous, values, round_id, allow_initial):
    """Never infer a round reset from a single player's counter decrease."""
    if previous is None:
        return (values if allow_initial else (0, 0, 0)), 'observed' if allow_initial else 'baseline'
    if previous['round_id'] != round_id:
        return (values if allow_initial else (0, 0, 0)), 'observed' if allow_initial else 'gap'
    old = tuple(previous[k] for k in ('kills', 'deaths', 'cash'))
    return tuple(max(0, new - before) for new, before in zip(values, old)), (
        'counter_drop' if any(new < before for new, before in zip(values, old)) else 'observed')

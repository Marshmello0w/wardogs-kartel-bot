import os
import sys
from pathlib import Path
import unittest
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from rounds import advance, counter_delta


def status(score, map_name='Bakurani', cap=100):
    return dict(map=map_name, experiences=['KOTH'], factionScores=[dict(name='A', score=score), dict(name='B', score=0)], scoreCap=cap)


class RoundTests(unittest.TestCase):
    def test_thresholds_once_and_same_map_restart(self):
        state, events = advance(None, status(50), '2026-09-20T10:00:00')
        self.assertIsNone(state['started_at'])
        state, events = advance(state, status(100), '2026-09-20T10:00:05')
        self.assertEqual([e['kind'] for e in events], ['round_voting_closed', 'round_near_end', 'round_ended'])
        state, events = advance(state, status(100), '2026-09-20T10:00:10')
        self.assertFalse(events)
        old_id = state['round_id']
        state, events = advance(state, status(0), '2026-09-20T10:00:15')
        self.assertTrue(state['uncertain'])
        state, events = advance(state, status(1), '2026-09-20T10:00:20')
        self.assertNotEqual(old_id, state['round_id'])
        self.assertEqual(state['started_at'], '2026-09-20T10:00:15')
        self.assertEqual([e['kind'] for e in events], ['round_started'])

    def test_missed_end_has_no_invented_winner(self):
        state, _ = advance(None, status(80), '2026-09-20T10:00:00')
        state, _ = advance(state, status(0), '2026-09-20T10:00:05')
        state, events = advance(state, status(1), '2026-09-20T10:00:10')
        self.assertIsNone(events[0]['data']['winner'])
        self.assertEqual(events[0]['data']['quality'], 'missing_end')

    def test_map_change_confirmed_and_stale_gap(self):
        state, _ = advance(None, status(50), '2026-09-20T10:00:00')
        old = state['round_id']
        state, _ = advance(state, status(1, 'Ozeti'), '2026-09-20T10:10:00')
        state, _ = advance(state, status(2, 'Ozeti'), '2026-09-20T10:10:05')
        self.assertNotEqual(old, state['round_id'])
        self.assertIsNone(state['started_at'])

    def test_invalid_response_cannot_transition(self):
        with self.assertRaises(ValueError):
            advance(None, {}, '2026-09-20T10:00:00')

    def test_restart_after_observed_end_when_zero_was_missed(self):
        state, _ = advance(None, status(100), '2026-09-20T10:00:00')
        old_id = state['round_id']
        state, _ = advance(state, status(30), '2026-09-20T10:10:00')
        state, events = advance(state, status(31), '2026-09-20T10:10:05')
        self.assertNotEqual(state['round_id'], old_id)
        self.assertIsNone(state['started_at'])
        self.assertEqual([e['kind'] for e in events], ['round_started'])

    def test_cap_controls_thresholds(self):
        state, events = advance(None, status(95, cap=200), '2026-09-20T10:00:00')
        self.assertFalse(state['voting_closed'])
        state, events = advance(state, status(190, cap=200), '2026-09-20T10:00:05')
        self.assertEqual(events[0]['kind'], 'round_voting_closed')

    def test_counter_round_change_and_baseline(self):
        previous = dict(round_id='old', kills=10, deaths=5, cash=100)
        self.assertEqual(counter_delta(previous, (12,6,120), 'new', True)[0], (12,6,120))
        self.assertEqual(counter_delta(None, (12,6,120), 'old', False)[0], (0,0,0))
        self.assertEqual(counter_delta(previous, (8,6,90), 'old', True)[0], (0,1,0))

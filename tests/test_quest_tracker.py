import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.quest_tracker import berlin_week_start, round_is_active_for_quests, split_week_seconds, valid_team


class QuestTimeTests(unittest.TestCase):
    def test_white_and_unknown_are_not_teams(self):
        self.assertIsNone(valid_team(' White '))
        self.assertIsNone(valid_team('UNKNOWN'))
        self.assertEqual(valid_team('Valkyra'), 'Valkyra')

    def test_berlin_week_starts_monday_at_local_midnight(self):
        # 22:30 UTC on Sunday is already Monday in Berlin during summer time.
        stamp = datetime(2026, 9, 20, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(berlin_week_start(stamp).isoformat(), '2026-09-21')

    def test_interval_is_split_at_berlin_week_boundary(self):
        start = datetime(2026, 9, 20, 21, 59, 30, tzinfo=timezone.utc)  # 23:59:30 CEST
        end = datetime(2026, 9, 20, 22, 0, 30, tzinfo=timezone.utc)     # 00:00:30 CEST
        parts = list(split_week_seconds(start, end))
        self.assertEqual(parts, [(berlin_week_start(start), 30), (berlin_week_start(end), 30)])

    def test_quest_time_requires_an_active_round(self):
        def state(score, players, ended=False):
            return {'ended': ended, 'snapshot': {'highest': score, 'players': {'current': players}}}

        self.assertTrue(round_is_active_for_quests(state(1, 1)))
        self.assertTrue(round_is_active_for_quests(state(0, 21)))
        self.assertFalse(round_is_active_for_quests(state(0, 20)))
        self.assertFalse(round_is_active_for_quests(state(0, 19)))
        self.assertFalse(round_is_active_for_quests(state(10, 100, ended=True)))


if __name__ == '__main__':
    unittest.main()

import os
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.seed_tracker import advance_presence, valid_seed_faction


class SeedRulesTests(unittest.TestCase):
    def test_only_configured_real_factions_are_seed_eligible(self):
        self.assertEqual(valid_seed_faction(' valkyra '), 'Valkyra')
        self.assertIsNone(valid_seed_faction('White'))
        self.assertIsNone(valid_seed_faction('Unknown'))
        self.assertIsNone(valid_seed_faction('anything else'))

    def test_first_observation_never_backfills_time(self):
        now = datetime(2026, 9, 21, 12, 0)
        self.assertEqual(advance_presence(None, 0, 0, now), (now, 0, 0, 0))

    def test_15_minute_rewards_require_continuous_observations(self):
        start = datetime(2026, 9, 21, 12, 0)
        observed, seconds, intervals, added = advance_presence(start, 840, 0, start + timedelta(seconds=60))
        self.assertEqual((seconds, intervals, added), (900, 1, 1))
        _, seconds, intervals, added = advance_presence(observed, seconds, intervals, observed + timedelta(seconds=60))
        self.assertEqual((seconds, intervals, added), (960, 1, 0))

    def test_gap_resets_the_continuous_timer(self):
        start = datetime(2026, 9, 21, 12, 0)
        result = advance_presence(start, 840, 0, start + timedelta(seconds=91))
        self.assertEqual(result, (start + timedelta(seconds=91), 0, 0, 0))


if __name__ == '__main__':
    unittest.main()

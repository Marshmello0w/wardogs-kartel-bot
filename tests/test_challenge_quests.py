import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.challenge_quests import (DAILY_QUESTS, ROUND_QUESTS, advance_round, berlin_day,
                                   faction_name, split_berlin_seconds, valid_counters)


class ChallengeQuestTests(unittest.TestCase):
    def row(self, at, **values):
        row = dict(last_seen=at.replace(tzinfo=None), last_kills=0, last_deaths=0,
                   last_cash=0, last_faction='Valkyra', kills=0, deaths=0,
                   cash=0, active_seconds=0, streak=0, best_streak=0, active=1,
                   completed_mask=0, awarded_mask=0)
        row.update(values)
        return row

    def test_five_kills_without_death_and_overlapping_round_goal(self):
        now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        updated, delta, seconds, _ = advance_round(
            self.row(now), {'kills': 5, 'deaths': 0, 'cash': 0}, 'Valkyra',
            now + timedelta(seconds=15))
        self.assertEqual((delta['kills'], seconds, updated['best_streak']), (5, 15, 5))
        self.assertTrue(ROUND_QUESTS[0][2](updated))
        self.assertTrue(ROUND_QUESTS[1][2](updated))

    def test_death_and_kills_in_one_sample_never_extend_streak(self):
        now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        updated, delta, _, _ = advance_round(
            self.row(now, streak=4, best_streak=4, last_kills=4),
            {'kills': 9, 'deaths': 1, 'cash': 100}, 'Valkyra',
            now + timedelta(seconds=15))
        self.assertEqual(delta['kills'], 5)
        self.assertEqual(updated['streak'], 0)
        self.assertEqual(updated['best_streak'], 4)

    def test_gap_rejoin_restart_and_counter_drop_are_baselines(self):
        now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        previous = self.row(now, streak=4, last_kills=4)
        for changes, first_sample, value in (({}, False, 35),
                                             ({'active': 0}, False, 15),
                                             ({}, True, 15),
                                             ({'last_kills': 10}, False, 15)):
            updated, delta, seconds, start = advance_round(
                dict(previous, **changes), {'kills': 5, 'deaths': 0, 'cash': 0},
                'Valkyra', now + timedelta(seconds=value), first_sample=first_sample)
            self.assertEqual((delta['kills'], seconds, start, updated['streak']), (0, 0, None, 0))

    def test_only_real_factions_and_integer_counters(self):
        self.assertIsNone(faction_name('White'))
        self.assertIsNone(faction_name('Unknown'))
        self.assertEqual(faction_name(' valkyra '), 'Valkyra')
        self.assertFalse(valid_counters({'kills': True, 'deaths': 0, 'cash': 0}))

    def test_berlin_midnight_and_dst_split(self):
        start = datetime(2026, 10, 24, 21, 59, 50, tzinfo=timezone.utc)
        end = start + timedelta(seconds=20)
        self.assertEqual(list(split_berlin_seconds(start, end)),
                         [(berlin_day(start), 10), (berlin_day(end), 10)])
        self.assertEqual(berlin_day(datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc)).isoformat(),
                         '2026-10-25')

    def test_every_round_and_daily_threshold(self):
        round_row = {'kills': 10, 'deaths': 3, 'cash': 50_000,
                     'active_seconds': 40 * 60, 'best_streak': 5}
        daily_row = {'kills': 50, 'cash': 200_000, 'active_seconds': 90 * 60}
        self.assertEqual([key for key, _, reached in ROUND_QUESTS if reached(round_row)],
                         ['first', 'streak', 'cash', 'time', 'fighter'])
        self.assertEqual([key for key, _, reached in DAILY_QUESTS if reached(daily_row, 3)],
                         ['hunter', 'big_hunt', 'cash', 'time', 'consistency'])
        self.assertFalse(ROUND_QUESTS[4][2](dict(round_row, deaths=2)))
        self.assertFalse(DAILY_QUESTS[4][2](daily_row, 2))

    def test_active_time_accumulates_only_between_valid_samples(self):
        at = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        row = self.row(at)
        for offset in range(15, 40 * 60 + 1, 15):
            row, delta, seconds, _ = advance_round(
                row, {'kills': 0, 'deaths': 0, 'cash': 0}, 'Valkyra',
                at + timedelta(seconds=offset))
            self.assertEqual((delta['kills'], seconds), (0, 15))
        self.assertEqual(row['active_seconds'], 40 * 60)
        self.assertTrue(ROUND_QUESTS[3][2](row))
        row, _, seconds, _ = advance_round(
            row, {'kills': 0, 'deaths': 0, 'cash': 0}, 'Valkyra',
            at + timedelta(seconds=40 * 60 + 60))
        self.assertEqual(seconds, 0)
        self.assertEqual(row['active_seconds'], 40 * 60)


if __name__ == '__main__':
    unittest.main()

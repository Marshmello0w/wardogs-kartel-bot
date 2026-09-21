import os
from datetime import datetime
from pathlib import Path
import sys
import unittest

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from cogs.reward_shop import add_month, valid_faction


class RewardShopRulesTests(unittest.TestCase):
    def test_only_real_factions_are_accepted(self):
        self.assertEqual(valid_faction('Valkyra'), 'Valkyra')
        self.assertIsNone(valid_faction('White'))
        self.assertIsNone(valid_faction('Unknown'))
        self.assertIsNone(valid_faction('valkyra'))

    def test_calendar_month_preserves_date_or_clamps_at_month_end(self):
        self.assertEqual(add_month(datetime(2026, 1, 31, 12, 30)), datetime(2026, 2, 28, 12, 30))
        self.assertEqual(add_month(datetime(2024, 1, 31, 12, 30)), datetime(2024, 2, 29, 12, 30))
        self.assertEqual(add_month(datetime(2026, 12, 15, 12, 30)), datetime(2027, 1, 15, 12, 30))


if __name__ == '__main__':
    unittest.main()

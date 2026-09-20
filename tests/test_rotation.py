import os
import sys
from pathlib import Path
import unittest
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))
from ban_service import config_banned_ids, remove_config_ban
from rotation import entries, replace_entries, RotationConflict


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.text = '[Other]\nValue=untouched\n[/Script/WDGame.WDServerMapRotationSettings]\nMode=Ordered\n!RotationEntries=ClearArray\n.RotationEntries=A\n.RotationEntries=B\n[Last]\nValue=preserved\n'

    def test_apply_and_cleanup_preserve_existing_duplicate_entry(self):
        before = entries(self.text)
        after = [before[0]] + before
        applied = replace_entries(self.text, before, after)
        self.assertEqual(entries(applied), after)
        restored = replace_entries(applied, after, before)
        self.assertEqual(entries(restored), before)
        self.assertIn('[Last]\nValue=preserved', restored)
        self.assertEqual(replace_entries(applied, before, after), applied)

    def test_external_edit_is_not_overwritten(self):
        with self.assertRaises(RotationConflict):
            replace_entries(self.text, ['.RotationEntries=unexpected'], [])

    def test_config_unban_exact_id_at_first_line_and_crlf(self):
        steam = '76561190000000001'
        other = '76561190000000002'
        text = f'.DefaultBannedPlayerIds={steam}\r\n.DefaultBannedPlayerIds={other}\r\nX=preserved\r\n'
        self.assertEqual(config_banned_ids(text), {steam, other})
        self.assertEqual(config_banned_ids(remove_config_ban(text, steam)), {other})
        self.assertIn('X=preserved\r\n', remove_config_ban(text, steam))

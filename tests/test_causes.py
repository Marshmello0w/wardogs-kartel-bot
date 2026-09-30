"""Offline checks for the independent bot and portal display-name modules."""
import importlib.util
from pathlib import Path
import unittest

from web.causes import LABELS, cause_label

ROOT = Path(__file__).resolve().parents[1]


class CauseLabelTests(unittest.TestCase):
    def test_bot_and_web_mapping_cannot_drift(self):
        bot = ROOT / 'bot/domain/causes.py'
        self.assertEqual(bot.read_text(encoding='utf-8'),
                         (ROOT / 'web/causes.py').read_text(encoding='utf-8'))
        spec = importlib.util.spec_from_file_location('bot_cause_labels', bot)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for tag, expected in LABELS.items():
            for variant in (tag, tag.upper(), tag.lower()):
                with self.subTest(tag=variant):
                    self.assertEqual(cause_label(variant), expected)
                    self.assertEqual(module.cause_label(variant), expected)

    def test_known_weapons_explosives_vehicles_and_tools(self):
        for tag, expected in {
            'Id.Item.WEPN_029': 'Galil', 'Id.Item.AK74M': 'AK-74M',
            'Id.Item.TAR21': 'TAR-21', 'Id.Item.RPG7': 'RPG-7',
            'ID.Item.BuildTool.Hammer.Large': 'Large hammer',
            'Id.Item.IED.Explosive': 'IED',
            'Vehicle.Variant.Land.Tracked.TNK_01.AntiAir': 'Flakpanzer Gepard',
            'Id.Vehicle.WeaponExtension.TNK_01.Heavy': 'L2A6 cannon',
        }.items():
            with self.subTest(tag=tag):
                self.assertEqual(cause_label(tag), expected)

    def test_unknown_tags_remain_readable_without_guessing_names(self):
        for tag, expected in {
            'Id.Item.WEPN_028': 'WEPN 028', 'Id.Item.Rifle': 'Rifle',
            'ID.Item.FutureTool.Standard': 'Future tool Standard',
            'Vehicle.Variant.Land.Tracked.NewTank.Default': 'New tank',
            'Vehicle.Variant.Land.Tracked.NewTank.MountedMachineGuns': 'New tank (mounted machine guns)',
            'Id.Vehicle.WeaponExtension.NEW_01.MainCannon': 'NEW 01 Main cannon',
            'Id.Buildable.NewWall': 'New wall', 'Fists': 'Fists',
            'New.Cause': 'New.Cause',
        }.items():
            with self.subTest(tag=tag):
                self.assertEqual(cause_label(tag), expected)
        self.assertEqual(cause_label(None), '')
        self.assertEqual(cause_label(''), '')
        self.assertEqual(cause_label('   '), '')
        self.assertEqual(cause_label(' Id.Item.SKS '), 'SKS')

    def test_formatting_does_not_make_untrusted_text_safe_html(self):
        self.assertEqual(cause_label('<script>unsafe</script>'), '<script>unsafe</script>')


if __name__ == '__main__':
    unittest.main()

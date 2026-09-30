"""Display-only cause labels from Warcon; mirrored for independent bot/web deployment.

Source: https://github.com/warcon-app/warcon/blob/main/src/lib/causes.ts
The matching module in bot/domain/causes.py and web/causes.py must stay identical.
Raw tags remain in the database; only presentation uses these labels.

MIT License
Copyright (c) 2026 Warcon contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import re

LABELS = {
    'Id.Item.AK74M': 'AK-74M',
    'Id.Item.WEPN_029': 'Galil',
    'Id.Item.M4': 'M4',
    'Id.Item.M500': 'M500',
    'Id.Item.MP43': 'MP43',
    'Id.Item.SKS': 'SKS',
    'Id.Item.SVDM': 'SVDM',
    'Id.Item.KH2002': 'KH2002',
    'Id.Item.TAR21': 'TAR-21',
    'Id.Item.A91': 'A-91',
    'Id.Item.SV98': 'SV-98',
    'Id.Item.RPG7': 'RPG-7',
    'Id.Item.MK22': 'MK 22',
    'Id.Item.Glock17': 'Glock 17',
    'Id.Item.CombatBow': 'Combat bow',
    'Id.Item.M67Grenade': 'M67 frag grenade',
    'Id.Item.C4Explosive': 'C4 charge',
    'Id.Item.IED.Explosive': 'IED',
    'Id.Item.ATMine': 'AT mine',
    'Id.Item.Claymore': 'Claymore',
    'Id.Item.Crowbar': 'Halligan bar',
    'Id.Item.Fists': 'Fists',
    'Id.Item.Defibrillator.Standard': 'Defibrillator',
    'ID.Item.BuildTool.Hammer.Large': 'Large hammer',
    'ID.Item.BuildTool.Hammer.Medium': 'Medium hammer',
    'ID.Item.BuildTool.Hammer.Small': 'Small hammer',
    'Id.Item.VehicleSupplyCrate.Pallet.MunitionsSupply': 'Ammo supply pallet',
    'Id.Buildable.BremmerWall': 'Bremer wall',
    'Id.Buildable.BarbedWire': 'Barbed wire',
    'Id.Buildable.HBlock': 'H-block',
    'Id.Buildable.TallHBlock': 'Tall H-block',
    'Vehicle.Variant.Air.Rotary.Littlebird.Default': 'MH-6',
    'Vehicle.Variant.Air.Rotary.Littlebird.MountedMachineGuns': 'AH-6M',
    'Vehicle.Variant.Air.Rotary.Littlebird.RocketPods': 'AH-6R',
    'Vehicle.Variant.Air.Rotary.ROT_04.Default': 'Z20 Lakota',
    'Vehicle.Variant.Air.Rotary.ROT_04.MountedMachineGuns': 'Z20 Lakota (miniguns)',
    'Vehicle.Variant.Land.Tracked.TNK_01.AntiAir': 'Flakpanzer Gepard',
    'Vehicle.Variant.Land.Tracked.TNK_01.Heavy': 'L2A6',
    'Vehicle.Variant.Land.Tracked.TNK_01.Artillery': 'SPH-2',
    'Vehicle.Variant.Land.Tracked.SpawnVehicle.Lonestar': 'M113 APC',
    'Vehicle.Variant.Land.Tracked.SpawnVehicle.Valkyra': 'M113 APC',
    'Vehicle.Variant.Land.Tracked.SpawnVehicle.Manticore': 'M113 APC',
    'Vehicle.Variant.Land.Wheeled.Humvee.MachineGun': 'Humvee (M249)',
    'Vehicle.Variant.Land.Wheeled.Humvee.Minigun': 'Humvee (minigun)',
    'Vehicle.Variant.Land.Wheeled.Kodiak.MachineGun': 'Kodiak (M249)',
    'Vehicle.Variant.Land.Wheeled.Kodiak.Pickup': 'Kodiak (pickup)',
    'Vehicle.Variant.Land.Wheeled.Ural.Battle': 'Ural Defender',
    'Vehicle.Variant.Land.Wheeled.Ural.Attack': 'Ural Defender (M249)',
    'Vehicle.Variant.Stationary.Phalanx': 'Vanguard CIWS',
    'Vehicle.Variant.Stationary.Mortar': 'L81 mortar',
    'Vehicle.Variant.Stationary.MistralAA': 'Talon 9K-SAM',
    'Id.Vehicle.WeaponExtension.ROT_02.30mmCannon': 'Havoc 2A42 autocannon',
    'Id.Vehicle.WeaponExtension.ROT_02.122mm': 'Havoc B-13 rockets',
    'Id.Vehicle.WeaponExtension.ROT_03.MountedMachineGun': 'AH-6M miniguns',
    'Id.Vehicle.WeaponExtension.ROT_03.RocketPods': 'AH-6R rockets',
    'Id.Vehicle.WeaponExtension.ROT_04.MountedMachineGun': 'Z20 Lakota miniguns',
    'Id.Vehicle.WeaponExtension.TNK_01.Artillery': 'SPH-2 artillery',
    'Id.Vehicle.WeaponExtension.TNK_01.Heavy': 'L2A6 cannon',
    'Id.Vehicle.WeaponExtension.TNK_01.MachineGun': 'L2A6 machine gun',
    'Id.Vehicle.WeaponExtension.TNK_01.MountedMachineGun': 'L2A6 mounted MG',
    'Id.Vehicle.WeaponExtension.WHL_02.SUV.RingTurret': 'Kodiak M249',
    'Id.Vehicle.WeaponExtension.WHL_05.RingTurret': 'Humvee M249',
    'Id.Vehicle.WeaponExtension.WHL_05.RingMinigun': 'Humvee minigun',
    'Id.Vehicle.WeaponExtension.WHL_07.MachineGun': 'Ural Defender M249',
    'Id.Vehicle.WeaponExtension.STN_01.MistralAA': 'Talon 9K-SAM',
    'Id.Vehicle.WeaponExtension.STN_02.MainCannon': 'STN 02 main cannon',
    'Id.Vehicle.WeaponExtension.STN_03.MainBarrel': 'STN 03 main gun',
}
_BY_TAG = {tag.casefold(): label for tag, label in LABELS.items()}


def _pretty(segment):
    words = re.sub(r'([a-z])([A-Z])', r'\1 \2', segment.replace('_', ' '))
    words = re.sub(r'([A-Za-z])(\d)', r'\1 \2', words).split()
    return ' '.join(word if re.fullmatch(r'[A-Z0-9]+', word) and re.search(r'[A-Z]{2}', word)
                    else word.capitalize() if index == 0 else word.lower()
                    for index, word in enumerate(words))


def cause_label(cause):
    """Return plain text, never trusted HTML/Markdown; callers escape for their UI."""
    value = str(cause or '').strip()
    if not value:
        return ''
    known = _BY_TAG.get(value.casefold())
    if known:
        return known
    parts = [part for part in value.split('.') if part]
    prefix = value.casefold()
    if prefix.startswith('vehicle.'):
        model = parts[4] if len(parts) > 4 else parts[-1]
        variant = parts[5] if len(parts) > 5 else ''
        return (_pretty(model) + ' (' + _pretty(variant).lower() + ')'
                if variant and variant.casefold() != 'default' else _pretty(model))
    if prefix.startswith('id.vehicle.weaponextension.'):
        return ' '.join(_pretty(part) for part in parts[3:])
    if prefix.startswith('id.buildable.'):
        return _pretty(parts[-1])
    if prefix.startswith('id.item.'):
        return ' '.join(_pretty(part) for part in parts[2:])
    # Already-readable causes and unrecognised prefixes must not disappear.
    return value

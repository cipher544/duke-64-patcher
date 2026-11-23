#!/usr/bin/env python3
"""Install locally extracted PC assets into a prepared FEM-restoration ROM."""
import argparse
import json
import struct
import sys
from pathlib import Path

import duke64patcher as patcher


TILES = (1312, 1313, 1314, 1315, 1316, 1331, 1332, 1333, 2576, 2577)
SOUNDS = ((202, 'DUKE_TIP1', 277), (67, 'DUKE_TIP2', 278))
INSTALL_KEY = 'restored_femcode_assets_v1'


def install(rom_path, grp_path, output, overwrite=False):
    rom_path, grp_path, output = map(Path, (rom_path, grp_path, output))
    if output.resolve() in (rom_path.resolve(), grp_path.resolve()):
        raise ValueError('The output filename must differ from both input files.')
    if output.exists() and not overwrite:
        raise ValueError(f'{output} exists; use --overwrite or another -o filename.')
    rom = patcher.prepared_rom(rom_path)
    original_size = len(rom)
    if patcher.pool_payload(rom, INSTALL_KEY):
        raise ValueError('These restoration assets are already installed.')
    count = len(patcher.sound_info(rom)[4])
    if count != 277:
        raise ValueError(f'Expected 277 existing sounds, found {count}. Use a fresh prepared ROM so the new sounds receive IDs 277 and 278.')

    source = patcher.GRPSource(grp_path)
    tiles = source.assets('tiles')
    missing = [tile for tile in TILES if tile not in tiles]
    if missing:
        raise ValueError(f'Required PC tiles are missing: {missing}')
    if 'E1L2.MAP' not in source.files:
        raise ValueError('The supplied GRP does not contain E1L2.MAP.')
    sounds = patcher.pc_sounds(source.files)
    for pc_id, label, _ in SOUNDS:
        matches = [s for s in sounds if s.number == pc_id and s.present]
        if len(matches) != 1 or matches[0].name.upper() != label:
            raise ValueError(f'The GRP must define PC sound {pc_id} as {label}. Use a compatible PC GRP.')

    # Keep all extracted assets beside this installer, including on Linux.
    assets = Path(__file__).resolve().parent / 'femcode_assets'
    for folder in ('tiles', 'sounds', 'levels'):
        (assets / folder).mkdir(parents=True, exist_ok=True)
    print(f'Extracting required assets to {assets}', flush=True)
    for tile in TILES:
        (assets / 'tiles' / f'{tile}.png').write_bytes(source.extract('tiles', tile))
    source.assets('sounds')
    for pc_id, label, _ in SOUNDS:
        audio = assets / 'sounds' / ('tip1.voc' if label == 'DUKE_TIP1' else 'tip2.voc')
        audio.write_bytes(source.extract('sounds', pc_id))
        (assets / 'sounds' / f'{pc_id}_{label}.ref').write_text(
            json.dumps({'id': pc_id, 'name': label, 'audio': audio.name}, indent=2) + '\n', encoding='utf-8')
    level = assets / 'levels' / 'E1L2.MAP'
    level.write_bytes(bytes(source.files['E1L2.MAP']))

    # Mutate an in-memory copy. Publish no ROM until every step succeeds.
    for tile in TILES:
        print(f'Installing tile {tile}...', flush=True)
        patcher.insert_tile(rom, tile, assets / 'tiles' / f'{tile}.png')
    for pc_id, label, expected in SOUNDS:
        audio = assets / 'sounds' / ('tip1.voc' if label == 'DUKE_TIP1' else 'tip2.voc')
        rom, assigned, _ = patcher.insert_sound(rom, audio, assets / 'sounds' / f'{pc_id}_{label}.ref')
        if assigned != expected:
            raise ValueError(f'{label}: expected N64 ID {expected}, received {assigned}.')
        print(f'Installed {label} as sound {assigned}.', flush=True)
    slot, missing = patcher.insert_map(rom, level.read_bytes(), title='RED LIGHT DISTRICT')
    print(f'Installed RED LIGHT DISTRICT at level slot {slot:02d}.', flush=True)
    if missing:
        print(f'Map tiles missing from the ROM; conversion fallbacks used: {dict(missing)}', file=sys.stderr)
    names = json.loads(patcher.pool_payload(rom, 'sound_names') or b'{}')
    if len(patcher.sound_info(rom)[4]) != 279 or any(names.get(str(i)) != name for _, name, i in SOUNDS):
        raise ValueError('Final sound-ID verification failed.')
    patcher.pool_put(rom, INSTALL_KEY, b'1')
    patcher.finish_insert(rom, original_size)
    patcher.write_insert_output(output, rom, overwrite)
    print(f'Finished: {output.resolve()}\nExtracted files retained in: {assets}')
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install FEM PC assets and Red Light District into a prepared USA ROM. Scripts must already be compiled in the ROM for sound IDs 277 and 278.')
    parser.add_argument('rom', type=Path, help='Prepared N64 ROM with compiled FEM restoration')
    parser.add_argument('grp', type=Path, help='Compatible PC DUKE3D.GRP')
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--overwrite', action='store_true', help='Allow replacing an existing output ROM')
    args = parser.parse_args(argv)
    output = args.output or args.rom.with_name(args.rom.stem + '_fem_restored.z64')
    try:
        install(args.rom, args.grp, output, args.overwrite)
    except (OSError, ValueError, ImportError, struct.error, KeyError, IndexError) as error:
        print(f'Installation failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# Duke64 Patcher

A standalone command-line tool for extracting Duke Nukem 64 and Duke Nukem 3D assets, and inserting custom assets into compatible, already prepared Duke Nukem 64 ROMs.

The included BPS adds engine enhancements and expanded asset support to your own ROM. A custom TROR demonstration map created by Cipher544 is also included. No ROMs, GRP archives, original PC maps, textures, sounds or standalone game scripts are included.

## Requirements

- Python 3.10 or newer.
- Your own legally obtained game files.
- Pillow for PNG extraction and insertion.
- NumPy and mapbox-earcut for level insertion and replacement.
- Shapely 2.1 or newer for maps with crossing wall loops.

Install the optional libraries needed for all operations:

```console
python -m pip install Pillow numpy mapbox-earcut "shapely>=2.1"
```

Listing levels, extracting maps, and extracting original VOC sounds from GRP files need only Python's standard library. On Windows, `py -3` can be used instead of `python`.

## Supported operations

| Option | Operation | N64 ROM | PC GRP |
| --- | --- | --- | --- |
| `-l` | List level slots and names | Yes | No |
| `-el ID` | Extract a level as MAP | Yes | Yes |
| `-et ID` | Extract a tile as PNG | Yes | Yes |
| `-es ID` | Extract an original VOC sound | No | Yes |
| `-il MAP` | Append a new level | Prepared USA ROM only | No |
| `-rl SLOT MAP` | Replace an existing level | Prepared USA ROM only | No |
| `-it ID PNG` | Add or replace a tile | Prepared USA ROM only | No |
| `-is AUDIO REF` | Append a sound and its reference name | Prepared USA ROM only | No |

Run `python duke64patcher.py --help` for built-in help. Quote paths containing spaces.

## Extracting assets

```console
python duke64patcher.py -l duke64.z64
python duke64patcher.py -el 00 duke64.z64
python duke64patcher.py -el 00 DUKE3D.GRP
python duke64patcher.py -et 1312 DUKE3D.GRP
python duke64patcher.py -es 0 DUKE3D.GRP
```

Level slots start at zero. GRP levels are ordered naturally by filename, such as E1L1, E1L2, and E1L10. Tile numbers are tile IDs. GRP sound numbers are the IDs resolved from CON `definesound` entries, not their order in the archive.

Use `-all` instead of an ID to extract all assets of the selected type:

```console
python duke64patcher.py -el -all duke64.z64
python duke64patcher.py -et -all DUKE3D.GRP
python duke64patcher.py -es -all DUKE3D.GRP
```

`-a`, `--all`, and a positional `all` are also accepted. Nothing is extracted by default without an ID or an all option.

Output goes to `<source name>_extracted/levels`, `/tiles`, or `/sounds` beside the input. Override the directory with `-o`:

```console
python duke64patcher.py -et 1312 DUKE3D.GRP -o extracted_tiles
```

Existing output files are refused unless `--overwrite` is supplied. GRP sound extraction preserves original VOC bytes; it does not convert them to WAV or generate `.ref` sidecars. Extract-all also includes unreferenced VOC files under their archive filenames.

## Preparing an N64 ROM for insertion

Insertion requires a compatible, already patched **USA ROM of at least 16 MiB**, with the expected asset tables and shared allocation directory. A stock ROM padded to 16 MiB is not sufficient. Apply the included preparation BPS to the matching clean USA ROM with a BPS-compatible patching tool, then use the resulting ROM with this patcher. The Python patcher does not apply BPS files.

The patcher never expands a ROM or installs runtime code. Insertion fails if the existing space is insufficient. TROR maps and added sounds also require their corresponding runtime support to be present already. Supported ROM sizes are 8, 16, 32, and 64 MiB for reading; insertion requires 16, 32, or 64 MiB.

Keep a backup of your input ROM.

## Enhancements provided by the included BPS

The included BPS installs the engine and storage changes needed to experiment with custom content:

| Enhancement | What it provides |
| --- | --- |
| Expanded ROM layout | A prepared ROM of at least 16 MiB, with a shared free-space pool for added or replaced assets. |
| Expanded level directory | Up to 64 level slots, with support for appended levels and updated level routing. |
| Increased map limits | Up to 1,024 sectors and 8,192 walls per map. The sprite limit remains 1,999. |
| TROR support | Runtime support for supported EDuke32/Mapster32 v9 room-over-room connections, including supported sloped and moving connections. |
| Expanded texture support | Relocated texture tables and loaders for adding or replacing tiles, with tile IDs up to 6143 and a 4,096-record texture table. |
| Expanded sound support | Runtime support for appended sounds, up to 300 total sound IDs, with playback-rate support for added samples. |
| Expanded script storage | Capacity for up to 128 KiB of compiled script bytecode. This patcher does not edit or compile scripts. |
| Original level compatibility | Existing native N64 levels remain supported alongside supported imported custom levels. |

These changes use Expansion Pak memory. An Expansion Pak, or an emulator configured for 8 MiB of RDRAM, is required. Existing rendering, geometry-buffer, and performance limits still apply; this is not unlimited or complete EDuke32 engine compatibility.

The BPS prepares the engine; it does not install additional PC maps, textures, sounds, or restored FEM behavior. Supply and insert any required assets separately.

**The source code for the runtime patches contained in the BPS is not currently released publicly. The editor and script compiler are also not currently publicly released.** Neither is required to use this standalone patcher with a prepared ROM.
  
## Included custom TROR map

`tror64test.map` is a custom TROR demonstration map created by the project author. It is already installed by the release BPS, so no manual insertion is required. The standalone MAP file is also supplied with the release for inspection, editing, and experimentation.

You may freely use, copy, edit, redistribute, and use the author's original contributions to `tror64test.map` as a base for your own maps, including commercial projects. No attribution is required. These permissions do not grant rights to referenced Duke Nukem assets.

## Applying the FEM restoration

The restoration has two parts: compiled N64 script changes supplied through a BPS patch, and PC assets extracted locally by `restored_femcode.py`. The Python installer adds assets; it does not compile or install game scripts.

1. Make a backup of your clean USA Duke Nukem 64 ROM.
2. Apply the BPS supplied for the FEM restoration using a BPS-compatible patching tool. Follow the release's patch order if engine preparation and FEM restoration are supplied as separate patches. The resulting ROM must contain both the compatible engine patches and the compiled FEM restoration; the base engine-preparation BPS alone is not sufficient. For the examples below, name this input `prepared_fem.z64`.
3. Place `restored_femcode.py` beside `duke64patcher.py`. Install the Python dependencies listed above. Have your PC `DUKE3D.GRP` available; it is not included in this download.
4. Open a terminal in the patcher folder and run one of the commands below.
5. Wait for the `Finished:` message. Load the new output ROM, not the input ROM. Use an Expansion Pak or configure your emulator for 8 MiB of RDRAM. Use level selection to access **RED LIGHT DISTRICT** at the slot printed by the installer.

Windows:

```console
python restored_femcode.py prepared_fem.z64 DUKE3D.GRP
```

Linux:

```console
python3 restored_femcode.py prepared_fem.z64 DUKE3D.GRP
```

Quote paths containing spaces. Linux filenames are case-sensitive. To choose an output filename:

```console
python restored_femcode.py "prepared_fem.z64" "DUKE3D.GRP" -o "duke64_fem.z64"
```

Without `-o`, the output is `prepared_fem_fem_restored.z64` for the example input, or `<input name>_fem_restored.z64` generally. The input ROM stays unchanged. Use `--overwrite` only when you want to replace an existing output file. No output ROM is written until every insertion step succeeds.

The installer extracts and inserts tiles 1312–1316, 1331–1333, 2576, and 2577; PC sounds 202 (`DUKE_TIP1`) and 67 (`DUKE_TIP2`); and E1L2 as **RED LIGHT DISTRICT**. All extracted assets remain in `femcode_assets` beside the installer: PNGs under `tiles`, VOCs and JSON `.ref` sidecars under `sounds`, and `E1L2.MAP` under `levels`. Existing files for these required assets are refreshed from the supplied GRP. Extracted files may remain after a failed installation. This folder is ignored by Git and must not be included in distributed packages.

**Required sound layout:** the prepared input must have exactly 277 existing sounds, and the compiled restoration must target N64 sound IDs 277 and 278. The installer appends the two sounds in that order and verifies their assigned IDs. If you receive an existing-sound-count or already-installed error, start with a fresh ROM prepared with the appropriate BPS patches. Do not rerun against a ROM containing manually appended sounds. The installer refuses its already installed output to prevent duplicates.

**Known compatibility limitation:** E1L2 retains its PC sound references. This installer does not translate map sound IDs or install every asset used by E1L2. Some map sounds or visuals may therefore differ from the PC version, and missing-tile conversion warnings may be printed. The restoration is experimental; importing the assets does not establish complete PC behavior compatibility.

## Inserting and replacing levels

```console
python duke64patcher.py -il custom.map prepared.z64 --name "CUSTOM LEVEL"
python duke64patcher.py -rl 00 custom.map prepared.z64
```

`-il` appends a level to the list. Without `--name`, its name comes from the uppercase MAP filename, truncated to 39 characters. `-rl` replaces an existing slot and retains its title unless `--name` is supplied. Titles must be 1–39 printable ASCII characters.

Build v7, v8, and EDuke32/Mapster32 v9 TROR maps are accepted, subject to N64 limits and validation. Limits include 64 level slots, 1,024 sectors, 8,192 walls, and 1,999 sprites per map. Geometry, staging-buffer, and rendering limits still apply; acceptance does not guarantee acceptable in-game performance.

Map data blocks are automatically RNC-compressed when that produces a smaller result. Otherwise a supported uncompressed wrapper is used. Compression is verified by decoding the result and is not necessarily byte-identical to the retail compressor.

Missing tile warnings may report fallbacks used during conversion. The tool does not automatically extract or install a map's required tiles or sounds. It does not compile CON scripts or automatically translate PC sound IDs to N64 IDs; prepare compatible map references and assets yourself.

**Compatibility may vary with custom maps.** Some geometry, effects, scripts, or assets may behave differently or be unsupported on the N64. Successfully inserting a map does not guarantee correct rendering, gameplay, or performance.

## Inserting tiles

```console
python duke64patcher.py -it 6000 texture.png prepared.z64
```

An existing tile ID is replaced; an unused ID is added if table space is available. IDs range from 0 to 6143. Images are converted to the native indexed texture format, so colors may be quantized. PNG transparency is supported. Native texture size and table limits apply.

## Adding sounds

```console
python duke64patcher.py -is tip1.voc 277_DUKE_TIP1.ref prepared.z64
```

Supported WAV or VOC audio is converted for the native sound bank. A matching UTF-8 JSON `.ref` file is required. For example:

```json
{
  "id": 277,
  "name": "DUKE_TIP1",
  "audio": "tip1.voc"
}
```

`id` is the source/reference ID, **not a requested destination ID**. A sound is always appended at the next N64 ID, which the tool prints. Existing sounds are never replaced. The `name` becomes its N64 reference name. If the optional `audio` field is supplied, it must match the input audio filename, ignoring case.

The sound table supports up to 300 entries, IDs 0–299. Scripts and maps must reference the actual assigned N64 ID. Reference names do not rewrite already compiled scripts.

## Output ROMs and batch files

Insertion creates `<input name>_patched.z64` beside the input by default. The result uses big-endian Z64 byte order and retains the input ROM's size. Its header checksum is updated.

Choose a filename with `-o`:

```console
python duke64patcher.py -il custom.map prepared.z64 -o custom_build.z64
```

For multiple operations, pass the previous output to the next command. You can explicitly update a working ROM in place using the same input/output filename and `--overwrite`:

```console
python duke64patcher.py -il custom.map working.z64 -o working.z64 --overwrite
python duke64patcher.py -it 6000 texture.png working.z64 -o working.z64 --overwrite
```

Successful commands return exit code 0. Failures return a nonzero code. In a Windows BAT file, check `if errorlevel 1` after each command and stop on failure. Extraction's all option does not apply to insertion; insert one asset per command.

## Bug reports and TROR feedback
Please report errors or crashes in the patcher itself through the project’s issue tracker or release thread. Include the command used, complete error output, operating system, Python version, and patcher/BPS version.

General troubleshooting for individual custom maps is outside the scope of this release, and compatibility may vary.

However, the practical limits of TROR on the N64 are still being explored, so your experiences are welcome—including successful tests. Useful details include:
- Map name and link to download
- Map complexity and the number of TROR layers visible at once.
- Performance, rendering issues, or unusual behavior.
- Whether you tested on real hardware or an emulator, including its version and graphics plugin.
- Screenshots, videos, or small original test maps you have permission to share.
This feedback will help establish practical limits and guide future improvements. It does not guarantee support or fixes for a particular map.

Please do not upload ROMs, GRP archives, or extracted game assets.


## License and credits

Original patcher code is offered under the MIT License; see `LICENSE`. Third-party libraries retain their own licenses.

The software license does not grant rights to game ROMs, CON scripts, maps, graphics, sounds, or other third-party content. Original game materials remain the property of their respective rights holders.

This is an unofficial tool. It is not made by, supported by, affiliated with, or endorsed by 3D Realms or the game's other developers, publishers, or rights holders.

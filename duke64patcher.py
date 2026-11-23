#!/usr/bin/env python3
"""Duke64 / Duke3D GRP asset extractor and prepared-ROM asset inserter. Run --help for usage."""
import argparse
from pathlib import Path
from dataclasses import dataclass
from math import isqrt
import re
import struct
import sys
import io
import json
import zlib
import heapq
import wave
import os
import tempfile
from collections import defaultdict,deque,Counter
from fractions import Fraction
from math import floor,hypot,isclose


BOARD_TABLE = {ord('E'): 587512, ord('P'): 587560}

BOARD_ADDRESS = 2151000064

BOARD = struct.Struct('>8I4i6f')

SECTOR = struct.Struct('>2i2h2H7h2Hb3Bb6B3x')

WALL = struct.Struct('>2i3hH6hb5B2x')

SPRITE = struct.Struct('>3iH11hb4B2bx')

MAP_SECTOR = struct.Struct('<2h2i2H2hb3B2hb5B3h')

MAP_WALL = struct.Struct('<2i3hH2hb5B3h')

MAP_SPRITE = struct.Struct('<3iHhb5B2b10h')

NAMES = ('Hollywood Holocaust', 'Gun Crazy', 'Death Row', 'Toxic Dump', 'Launch Facility', 'The Abyss', 'Battlelord', 'Duke-Burger', 'Spaceport', 'Incubator', 'Warp Factor', 'Fusion Station', 'Occupied Territory', 'Tiberius Station', 'Lunar Reactor', 'Dark Side', 'Dreadnought', 'Overlord', 'Lunatic Fringe', 'Raw Meat', 'Bank Roll', 'Flood Zone', 'L.A. Rumble', 'Movie Set', 'Rabid Transit', 'Fahrenheit', 'Hotel Hell', 'Stadium', 'Area 51', 'Freeway', 'Castle Dukenstein', 'Piracy', 'Shaft', 'Noctis Labyrinthus')

def read_rom(path):
    rom = bytearray(path.read_bytes())
    if len(rom) not in (8388608, 16777216, 33554432, 67108864):
        raise ValueError('expected an 8 MiB source, 16 MiB expanded, 32 MiB, or 64 MiB expanded ROM')
    magic = rom[:4].hex()
    if magic in {'37804012', '40123780'}:
        width = 2 if magic == '37804012' else 4
        original = rom[:]
        for offset in range(width):
            rom[offset::width] = original[width - 1 - offset::width]
    elif magic != '80371240':
        raise ValueError('unrecognized N64 ROM byte order')
    if rom[32:42] != b'DUKE NUKEM' or rom[62] not in BOARD_TABLE:
        raise ValueError('expected a USA or European Duke Nukem 64 ROM')
    return rom

def make_crc_table():
    table = []
    for value in range(256):
        for _ in range(8):
            value = value >> 1 ^ (40961 if value & 1 else 0)
        table.append(value)
    return table

CRC_TABLE = make_crc_table()

def crc16(data):
    crc = 0
    for byte in data:
        crc = crc >> 8 ^ CRC_TABLE[(crc ^ byte) & 255]
    return crc

class RNCStream:

    def __init__(self, payload):
        self.payload = payload
        self.position = 0
        self.word = 0
        self.remaining = 0

    def bits(self, count):
        value = 0
        for shift in range(count):
            if self.remaining == 0:
                if self.position >= len(self.payload):
                    raise ValueError('RNC bitstream ended early')
                self.word = int.from_bytes(self.payload[self.position:self.position + 2], 'little')
                self.position += 2
                self.remaining = 16
            value |= (self.word & 1) << shift
            self.word >>= 1
            self.remaining -= 1
        return value

    def literals(self, count):
        if not count:
            return b''
        end = self.position + count
        if end > len(self.payload):
            raise ValueError('RNC literal run extends past the payload')
        data = self.payload[self.position:end]
        self.position = end
        return data

    def table(self):
        count = self.bits(5)
        if count > 16:
            raise ValueError('invalid RNC Huffman table size')
        depths = [self.bits(4) for _ in range(count)]
        codes = {}
        code = 0
        for depth in range(1, 16):
            code <<= 1
            for symbol, length in enumerate(depths):
                if length == depth:
                    if code >= 1 << depth:
                        raise ValueError('oversubscribed RNC Huffman table')
                    codes[depth, code] = symbol
                    code += 1
        return codes

    def length(self, codes):
        code = 0
        for depth in range(1, 16):
            code = code << 1 | self.bits(1)
            symbol = codes.get((depth, code))
            if symbol is not None:
                if symbol < 2:
                    return symbol
                return 1 << symbol - 1 | self.bits(symbol - 1)
        raise ValueError('invalid RNC Huffman code')

def decompress(rom, start, limit, expected):
    if start + 8 <= limit and rom[start:start + 4] == b'RNC\x00':
        size, = struct.unpack_from('>I', rom, start + 4)
        if size != expected or start + 8 + size > limit:
            raise ValueError(f'invalid stored RNC block at 0x{start:X}')
        return rom[start + 8:start + 8 + size]
    if start + 18 > limit:
        raise ValueError('truncated RNC header')
    tag, size, packed, output_crc, packed_crc, _, chunks = struct.unpack_from('>4sIIHHBB', rom, start)
    if tag != b'RNC\x01' or size != expected or start + 18 + packed > limit:
        raise ValueError(f'unexpected RNC header at 0x{start:X}')
    payload = rom[start + 18:start + 18 + packed]
    if crc16(payload) != packed_crc:
        raise ValueError(f'packed CRC mismatch at 0x{start:X}')
    stream = RNCStream(payload)
    stream.bits(2)
    output = bytearray()
    for _ in range(chunks):
        literal, distance, length = (stream.table() for _ in range(3))
        runs = stream.bits(16)
        if not runs:
            raise ValueError('empty RNC chunk')
        for run in range(runs):
            count = stream.length(literal)
            if len(output) + count > size:
                raise ValueError('RNC literals exceed the output size')
            output.extend(stream.literals(count))
            if run + 1 < runs:
                offset = stream.length(distance) + 1
                count = stream.length(length) + 2
                if offset > len(output) or len(output) + count > size:
                    raise ValueError('invalid RNC back-reference')
                for _ in range(count):
                    output.append(output[-offset])
    if len(output) != size or crc16(output) != output_crc:
        raise ValueError(f'unpacked data failed validation at 0x{start:X}')
    return output

def convert_sector(record):
    cz, fz, first, count, cs, fs, cp, ch, fp, fh, lotag, hitag, extra, _floor_mesh, _ceiling_mesh, *appearance, visibility, _floor_count, _ceiling_count = record
    return MAP_SECTOR.pack(first, count, cz, fz, cs, fs, cp, ch, *appearance[:4], fp, fh, *appearance[4:], visibility, 0, lotag, hitag, extra)

def convert_wall(record):
    return MAP_WALL.pack(*record[:8], *record[12:], *record[8:11])

def convert_sprite(record):
    x, y, z, flags, tile, *rest = record
    tags = rest[:10]
    shade, pal, clip, xrep, yrep, xoff, yoff = rest[10:]
    return MAP_SPRITE.pack(x, y, z, flags, tile, shade, pal, clip, 0, xrep, yrep, xoff, yoff, *tags)

def contains_point(walls, first, count, x, y):
    inside = False
    for index in range(first, first + count):
        x1, y1, point2 = walls[index][:3]
        x2, y2 = walls[point2][:2]
        if (y1 > y) != (y2 > y):
            cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
            if cross > 0 and y2 > y1 or (cross < 0 and y2 < y1):
                inside = not inside
    return inside

def find_start(sectors, walls, x, y, z, *, use_slopes=False):
    candidates = [i for i, (_, _, first, count, *_) in enumerate(sectors) if contains_point(walls, first, count, x, y)]
    if not candidates:
        raise ValueError('player start lies outside the map')

    def surface_height(i, side):
        s = sectors[i]
        height = s[side]
        if not use_slopes or not s[4 + side] & 2:
            return height
        first = walls[s[2]]
        end = walls[first[2]]
        dx, dy = (end[0] - first[0], end[1] - first[1])
        denominator = isqrt(dx * dx + dy * dy) << 5
        if denominator:
            numerator = s[7 if side == 0 else 9] * (dx * (y - first[1]) - dy * (x - first[0]) >> 3)
            height += numerator // denominator if numerator >= 0 else -(-numerator // denominator)
        return height
    return min(candidates, key=lambda i: (max(surface_height(i, 0) - z, z - surface_height(i, 1), 0), i != 4, i))

def read_board(rom, number):
    if not 0 <= number < level_count(rom):
        raise ValueError('Invalid level slot.')
    offset = board_table(rom) + BOARD.size * number
    start, end, sec, wall, spr, ns, np, nw, x, y, z, angle, *_ = BOARD.unpack_from(rom, offset)
    sec, wall, spr = (start + address - BOARD_ADDRESS for address in (sec, wall, spr))
    if not (0 < start < min(sec, wall, spr) < end <= len(rom) and max(sec, wall, spr) < end and (len({sec, wall, spr}) == 3) and (0 < ns <= 1024) and (0 < nw <= 8192) and (0 <= np <= 4096) and (0 <= angle < 2048)):
        raise ValueError('invalid board directory entry')
    raw_records = {}

    def records(layout, position, count):
        limit = min((p for p in (sec, wall, spr, end) if p > position))
        unpacked = decompress(rom, position, limit, count * layout.size)
        raw_records[position] = unpacked
        return list(layout.iter_unpack(unpacked))
    sectors = records(SECTOR, sec, ns)
    walls = records(WALL, wall, nw)
    sprites = records(SPRITE, spr, np)
    for _, _, first, count, *_ in sectors:
        if not 0 <= first < first + count <= nw:
            raise ValueError('invalid sector wall range')
        if any((not first <= walls[i][2] < first + count for i in range(first, first + count))):
            raise ValueError('wall loop leaves its sector')
    for _, _, _, peer, neighbor, *_ in walls:
        if not (peer == -1 and neighbor == -1 or (0 <= peer < nw and 0 <= neighbor < ns)):
            raise ValueError('invalid adjacent wall or sector')
    if any((not 0 <= sprite[5] < ns for sprite in sprites)):
        raise ValueError('invalid sprite sector')
    x, y, z = (x * 2, y * 2, z * 32)
    current = find_start(sectors, walls, x, y, z, use_slopes=any(((s[4] | s[5]) & 1024 for s in sectors)))
    data = bytearray(struct.pack('<4ihh', 7, x, y, z, angle, current))
    for items, convert in ((sectors, convert_sector), (walls, convert_wall), (sprites, convert_sprite)):
        data.extend(struct.pack('<H', len(items)))
        for item in items:
            data.extend(convert(item))
    data = restore_map_links(data, raw_records[sec])
    data = restore_origin(data, raw_records[wall])
    return (data, (start, end, ns, nw, np))

def board_filename(number, rom=None):
    name = level_name(rom, number) if rom is not None else NAMES[number]
    name = ''.join((char if char.isalnum() or char in '_-' else '_' for char in name))
    return f'{number:02d}_{name}.map'

TITLE_POINTER_TABLE = 562064

TITLE_START = 754768

TITLE_END = 755216

ROM_TO_VRAM = 2147787776

MAX_TITLE_LENGTH = 39

def level_name(rom, slot):
    try:
        name = read_level_titles(rom)[slot]
    except ValueError:
        return NAMES[slot] if slot < len(NAMES) else f'Level {slot}'
    return NAMES[slot] if slot < len(NAMES) and name == NAMES[slot].upper() else name

def level_layout(rom):
    for base in (0x890000,0x1140000):
        if rom[base:base+8]==b'D64LVL01':
            if len(rom)<base+0x4000:raise ValueError('Truncated level directory.')
            count,capacity=struct.unpack_from('>HH',rom,base+8)
            if capacity!=64 or not 1<=count<=64:raise ValueError('Invalid expanded level directory.')
            return base,count
    return None

def level_count(rom):
    layout=level_layout(rom)
    return layout[1] if layout else len(NAMES)

def board_table(rom):
    layout=level_layout(rom)
    return layout[0]+0x100 if layout else BOARD_TABLE[rom[0x3e]]

def read_level_titles(rom):
    layout=level_layout(rom)
    if layout:
        base,count=layout;result=[]
        for i in range(count):
            pointer=struct.unpack_from('>I',rom,base+0x1400+i*4)[0]-0x80720000
            if not 0x1500<=pointer<0x1f00:raise ValueError('Invalid level title pointer.')
            raw,sep,_=rom[base+pointer:base+pointer+40].partition(b'\0')
            if not sep:raise ValueError('Unterminated level title.')
            result.append(raw.decode('ascii'))
        return result
    if rom[0x3e]!=ord('E'):return list(NAMES)
    result=[]
    for i in range(34):
        p=struct.unpack_from('>I',rom,TITLE_POINTER_TABLE+4*i)[0]-ROM_TO_VRAM
        if not TITLE_START<=p<TITLE_END:raise ValueError('Invalid stock title pointer.')
        raw,sep,_=rom[p:min(p+40,TITLE_END)].partition(b'\0')
        if not sep:raise ValueError('Unterminated level title.')
        result.append(raw.decode('ascii'))
    return result


PORTAL = 1024

TAG = 8388608

BUNCH_TAG = 12582912

def boundary_walls(sectors, walls, members):
    """Exclude horizontal portals inside a bunch's combined footprint."""
    members = set(members)
    result = {}
    for index in members:
        first, count = sectors[index][:2]
        if not 0 <= first < first + count <= len(walls):
            raise ValueError(f'Sector {index}: invalid wall range.')
        for i in range(first, first + count):
            w = walls[i]
            if not first <= w[2] < first + count:
                raise ValueError(f'Sector {index}: invalid wall loop.')
            a, b = (tuple(w[:2]), tuple(walls[w[2]][:2]))
            if w[4] in members:
                peer = w[3]
                if not 0 <= peer < len(walls) or walls[peer][3] != i or (not 0 <= walls[peer][2] < len(walls)) or (walls[peer][4] != index) or (tuple(walls[peer][:2]) != b) or (tuple(walls[walls[peer][2]][:2]) != a):
                    raise ValueError(f'Wall {i}: invalid internal bunch portal.')
                continue
            key = tuple(sorted((a, b)))
            if key in result:
                raise ValueError('TROR bunch has overlapping boundary walls.')
            result[key] = i
    return result

def boundary_outline(edges):
    """Exact geometric boundary independent of collinear wall subdivisions."""
    from math import gcd
    lines = {}
    for a, b in edges:
        dx, dy = (b[0] - a[0], b[1] - a[1])
        divisor = gcd(abs(dx), abs(dy))
        if not divisor:
            raise ValueError('TROR boundary has a zero-length wall.')
        dx //= divisor
        dy //= divisor
        if dx < 0 or (dx == 0 and dy < 0):
            dx, dy = (-dx, -dy)
        line = (dx, dy, dx * a[1] - dy * a[0])
        span = tuple(sorted((dx * a[0] + dy * a[1], dx * b[0] + dy * b[1])))
        lines.setdefault(line, []).append(span)
    result = set()
    for line, spans in lines.items():
        start = end = None
        for a, b in sorted(spans):
            if end is not None and a < end:
                raise ValueError('TROR bunch has overlapping boundary walls.')
            if end is not None and a == end:
                end = b
            else:
                if end is not None:
                    result.add((*line, start, end))
                start, end = (a, b)
        result.add((*line, start, end))
    return frozenset(result)

def unpack_links(raw):
    value = int.from_bytes(raw, 'big')
    if value & 12582912 not in (TAG, BUNCH_TAG):
        return [-1, -1]
    return [(value & 2047) - 1, (value >> 11 & 2047) - 1]

def restore_map_links(data, raw_sectors):
    """Export current bunch metadata or old direct links as Mapster v9."""
    count = len(raw_sectors) // SECTOR.size
    links = [unpack_links(raw_sectors[i * 48 + 45:i * 48 + 48]) for i in range(count)]
    tags = [int.from_bytes(raw_sectors[i * 48 + 45:i * 48 + 48], 'big') & 12582912 for i in range(count)]
    bunches = {}
    legacy = []
    for i, pair in enumerate(links):
        for side, b in enumerate(pair):
            flags = struct.unpack_from('>H', raw_sectors, i * 48 + 12 + side * 2)[0]
            if b < 0 or not flags & PORTAL:
                continue
            if tags[i] == BUNCH_TAG:
                if b > 255:
                    raise ValueError('Invalid native TROR bunch ID.')
                bunches.setdefault(b, [[], []])[side].append(i)
            elif side == 0:
                if b >= count or tags[b] != TAG or links[b][1] != i:
                    raise ValueError('Invalid native TROR connection.')
                if not struct.unpack_from('>H', raw_sectors, b * 48 + 14)[0] & PORTAL:
                    raise ValueError('Missing native TROR floor connection.')
                legacy.append((i, b))
    for lo, hi in legacy:
        b = next((b for b in range(256) if b not in bunches), None)
        if b is None:
            raise ValueError('Too many TROR bunches for legacy Mapster v9 export.')
        bunches[b] = [[lo], [hi]]
    if not bunches:
        return data
    result = bytearray(data)
    struct.pack_into('<i', result, 0, 9)
    sectors = [list(MAP_SECTOR.unpack_from(result, 22 + i * MAP_SECTOR.size)) for i in range(count)]
    wall_start = 24 + count * MAP_SECTOR.size
    wall_count = struct.unpack_from('<H', result, wall_start - 2)[0]
    walls = [list(MAP_WALL.unpack_from(result, wall_start + i * MAP_WALL.size)) for i in range(wall_count)]
    for b, sides in bunches.items():
        if not all(sides):
            raise ValueError(f'Native TROR bunch {b} has only one side.')
        for side, members in enumerate(sides):
            for i in members:
                sectors[i][4 + side] |= PORTAL
                sectors[i][10 + side * 6] = b
        lower, upper = [boundary_walls(sectors, walls, members) for members in sides]
        if boundary_outline(lower) != boundary_outline(upper):
            raise ValueError(f'Native TROR bunch {b} outlines no longer match.')
        for edge in lower.keys() & upper.keys():
            low_wall = lower[edge]
            high_wall = upper[edge]
            walls[low_wall][5] |= 1024
            walls[low_wall][14] = high_wall
            walls[high_wall][5] |= 2048
            walls[high_wall][16] = low_wall
    for i, sector in enumerate(sectors):
        MAP_SECTOR.pack_into(result, 22 + i * MAP_SECTOR.size, *sector)
    for i, wall in enumerate(walls):
        MAP_WALL.pack_into(result, wall_start + i * MAP_WALL.size, *wall)
    return result

ORIGIN_MAGIC = b'D64ORG4'

def restore_origin(data, raw_walls):
    if len(raw_walls) < 8 * 36:
        return data
    metadata = b''.join((bytes(raw_walls[i * 36 + 34:i * 36 + 36]) for i in range(8)))
    if metadata[:7] != ORIGIN_MAGIC:
        return data
    bits = metadata[7]
    ox, oy = struct.unpack_from('>ii', metadata, 8)
    result = bytearray(data)
    version, x, y, z = struct.unpack_from('<4i', result)
    struct.pack_into('<4i', result, 0, version, x + ox + (bits & 1), y + oy + (bits >> 1 & 1), z + (bits >> 2 & 31))
    ns = struct.unpack_from('<H', result, 20)[0]
    position = 22 + ns * MAP_SECTOR.size
    for fmt in (MAP_WALL, MAP_SPRITE):
        count = struct.unpack_from('<H', result, position)[0]
        position += 2
        for i in range(count):
            p = position + i * fmt.size
            x, y = struct.unpack_from('<ii', result, p)
            struct.pack_into('<ii', result, p, x + ox, y + oy)
        position += count * fmt.size
    return result

STOCK_COUNT = 2851

TILE = struct.Struct('>3I8H')

ART_OFFSETS = {ord('E'): (592880, 795248, 788616), ord('P'): (592928, 795296, 788664)}

TEXTURE_TABLE_COUNT = 4096

@dataclass(frozen=True)
class N64Tile:
    number: int
    record_offset: int
    width: int
    height: int
    stored_width: int
    stored_height: int
    size: int
    offset: int
    indexed8: bool
    imported: bool

def list_tiles(rom):
    base, data, _ = ART_OFFSETS[rom[62]]
    count = STOCK_COUNT
    if has_pc_texture_patch(rom):
        base, count = (texture_table(rom), TEXTURE_TABLE_COUNT)
    for i in range(count):
        fileoff, _, _, w, h, size, dx, dy, flags, number, _ = TILE.unpack_from(rom, base + i * TILE.size)
        if w and h and dx and dy:
            yield N64Tile(number, base + i * TILE.size, w, h, dx, dy, size, data + fileoff, bool(flags & 32768), i >= STOCK_COUNT)

def rgba5551(word):
    r, g, b = (word >> shift & 31 for shift in (11, 6, 1))
    return bytes((r << 3 | r >> 2, g << 3 | g >> 2, b << 3 | b >> 2, 255 if word & 1 else 0))

def decode_tile(rom, tile):
    """Return row-major RGBA pixels at the tile's stored dimensions."""
    end = tile.offset + tile.size
    if tile.offset < 0 or end > len(rom):
        raise ValueError('Tile data lies outside the ROM.')
    pixels = tile.stored_width * tile.stored_height
    length = pixels if tile.indexed8 else 32 + (pixels + 1) // 2
    if length > 65536:
        raise ValueError('Tile dimensions exceed the supported range.')
    if rom[tile.offset:tile.offset + 3] == b'RNC':
        raw = decompress(rom, tile.offset, end, length)
    else:
        raw = rom[tile.offset:end]
    if len(raw) != length:
        raise ValueError('Unexpected tile data size.')
    if tile.indexed8:
        _, _, offset = ART_OFFSETS[rom[62]]
        palette = [rgba5551(word) for word in struct.unpack_from('>256H', rom, offset)]
        return b''.join((palette[index] for index in raw))
    palette = [rgba5551(word) for word in struct.unpack_from('<16H', raw)]
    pairs = [palette[value >> 4] + palette[value & 15] for value in range(256)]
    return b''.join((pairs[value] for value in raw[32:]))[:pixels * 4]

TEXTURE_TABLE_OFFSET=0x870000

def texture_table(rom):
    for marker,table in ((0x88c000,0x870000),(0xe1c000,0xe00000)):
        if rom[marker:marker+16]==b'DN64PCART01\0\0\0\0\0':
            if table+4096*28>len(rom):raise ValueError('Truncated texture table.')
            return table
    return None

def has_pc_texture_patch(rom):return texture_table(rom) is not None


GRP_MAGIC = b'KenSilverman'

ENTRY = struct.Struct('<12sI')

NAME = re.compile("[A-Z0-9_!~#$%&'()@^+-]{1,8}(?:\\.[A-Z0-9_!~#$%&'()@^+-]{1,3})?")

def dos_name(name):
    name = name.strip().upper()
    if not NAME.fullmatch(name) or re.fullmatch('CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name.split('.')[0]):
        raise ValueError('Use a DOS 8.3 filename, such as E1L1.MAP.')
    return name

def read_grp(path):
    blob = memoryview(path.read_bytes())
    if len(blob) < 16 or blob[:12] != GRP_MAGIC:
        raise ValueError('Not a Build GRP archive.')
    count, = struct.unpack_from('<I', blob, 12)
    directory_end = 16 + count * ENTRY.size
    if directory_end > len(blob):
        raise ValueError('Truncated GRP directory.')
    position = directory_end
    files = {}
    for offset in range(16, directory_end, ENTRY.size):
        raw, size = ENTRY.unpack_from(blob, offset)
        name = dos_name(raw.split(b'\x00', 1)[0].decode('ascii'))
        if name in files or position + size > len(blob):
            raise ValueError(f'Duplicate or truncated entry {name}.')
        files[name] = blob[position:position + size]
        position += size
    if position != len(blob):
        raise ValueError('Unexpected trailing data in GRP.')
    return files

@dataclass(frozen=True)
class PCTile:
    width: int
    height: int
    animation: int
    pixels: bytes

def read_art(data):
    if len(data) < 16:
        raise ValueError('Truncated ART header.')
    version, total, first, last = struct.unpack_from('<4I', data)
    if version != 1 or not first <= last <= 32767:
        raise ValueError('Unsupported ART header.')
    count = last - first + 1
    pos = 16 + count * 8
    if pos > len(data):
        raise ValueError('Truncated ART directory.')
    tiles = {}
    for i in range(count):
        width, = struct.unpack_from('<H', data, 16 + 2 * i)
        height, = struct.unpack_from('<H', data, 16 + 2 * (count + i))
        animation, = struct.unpack_from('<I', data, 16 + 4 * (count + i))
        end = pos + width * height
        if end > len(data):
            raise ValueError('Truncated ART tile.')
        tiles[first + i] = PCTile(width, height, animation, bytes(data[pos:end]))
        pos = end
    if pos != len(data):
        raise ValueError('Unexpected trailing ART data.')
    return (total, tiles)

def pc_tiles(files):
    archives, locations, tiles = ({}, {}, {})
    for name, data in sorted(files.items()):
        if re.fullmatch('TILES\\d{3}\\.ART', name):
            total, records = read_art(data)
            archives[name] = (total, records)
            locations.update(dict.fromkeys(records, name))
            tiles.update(records)
    return (archives, locations, tiles)

def pc_palette(files):
    if 'PALETTE.DAT' not in files:
        return bytes((value for n in range(256) for value in (n, n, n)))
    values = files['PALETTE.DAT'][:768]
    if len(values) != 768 or max(values) > 63:
        raise ValueError('Invalid Duke3D VGA palette.')
    return bytes((v << 2 | v >> 4 for v in values))

def pc_preview(tile, colors):
    from PIL import Image
    img = Image.frombytes('P', (tile.height, tile.width), tile.pixels)
    img.putpalette(colors)
    img.info['transparency'] = 255
    return img.transpose(Image.Transpose.TRANSPOSE).convert('RGBA')

TOKEN = re.compile('//[^\\n]*|/\\*.*?(?:\\*/|\\Z)|"[^"\\n]*"|[^/\\s{}"]+|/', re.S)

@dataclass(frozen=True)
class Sound:
    number: int | None
    name: str
    filename: str
    present: bool

def pc_sounds(files):
    sources = {name.upper(): bytes(data).decode('cp437').rstrip('\x00\x1a') for name, data in files.items() if name.upper().endswith('.CON')}
    tokens = {name: [m[0] for m in TOKEN.finditer(source) if not m[0].startswith(('//', '/*'))] for name, source in sources.items()}
    words, visited = ([], set())

    def include(name):
        name = name.strip('"').upper()
        if name in visited or name not in tokens:
            return
        visited.add(name)
        items = tokens[name]
        i = 0
        while i < len(items):
            if items[i].lower() == 'include' and i + 1 < len(items):
                include(items[i + 1])
                i += 2
            else:
                words.append(items[i])
                i += 1
    include('GAME.CON')
    for name in sorted(tokens):
        include(name)
    defines = {words[i + 1].upper(): words[i + 2] for i in range(len(words) - 2) if words[i].lower() == 'define'}

    def resolve(value):
        seen = set()
        while value.upper() in defines and value.upper() not in seen:
            seen.add(value.upper())
            value = defines[value.upper()]
        try:
            return int(value, 16 if value.lstrip('+-').lower().startswith('0x') else 10)
        except ValueError:
            return None
    entries = {}
    filenames = {name.upper() for name in files}
    for i in range(len(words) - 2):
        if words[i].lower() != 'definesound':
            continue
        label, filename = words[i + 1:i + 3]
        number = resolve(label)
        key = number if number is not None else label.upper()
        filename = filename.strip('"')
        entries[key] = Sound(number, label, filename, filename.upper() in filenames)
    return sorted(entries.values(), key=lambda s: (s.number is None, s.number or 0, s.name))


def number(value):
    """Leading zeroes are decimal; explicit 0x prefixes select hexadecimal."""
    try:
        result=int(value,16 if value.lower().startswith('0x') else 10)
    except ValueError as error:
        raise ValueError(f'Invalid ID {value!r}; use a non-negative number.') from error
    if result<0:raise ValueError('IDs must be non-negative.')
    return result


def natural_key(name):
    return tuple((1,int(p)) if p.isdecimal() else (0,p.casefold())
                 for p in re.split(r'(\d+)',name))


def png_bytes(image):
    output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()


class N64Source:
    def __init__(self,path):self.rom=read_rom(path)
    def assets(self,kind):
        if kind=='levels':return {i:board_filename(i,self.rom) for i in range(level_count(self.rom))}
        if kind=='sounds':raise ValueError('Sound extraction is supported for GRP files only, not N64 ROMs.')
        self.tiles={tile.number:tile for tile in list_tiles(self.rom)}
        return {i:f'{i:02d}.png' for i in self.tiles}
    def extract(self,kind,ident):
        if kind=='levels':return bytes(read_board(self.rom,ident)[0])
        from PIL import Image
        tile=self.tiles[ident]
        image=Image.frombytes('RGBA',(tile.stored_width,tile.stored_height),decode_tile(self.rom,tile))
        if tile.width<=tile.stored_width and tile.height<=tile.stored_height:
            image=image.crop((0,0,tile.width,tile.height))
        return png_bytes(image)


class GRPSource:
    def __init__(self,path):self.files=read_grp(path)
    def assets(self,kind):
        if kind=='levels':
            self.levels=sorted((name for name in self.files if name.endswith('.MAP')),key=natural_key)
            return {i:f'{i:02d}_{name}' for i,name in enumerate(self.levels)}
        if kind=='tiles':
            self.tiles={i:t for i,t in pc_tiles(self.files)[2].items() if t.width and t.height}
            if 'PALETTE.DAT' not in self.files:raise ValueError('This GRP has no PALETTE.DAT; accurate PNG colors require it.')
            self.colors=pc_palette(self.files)
            return {i:f'{i:02d}.png' for i in self.tiles}
        sounds=pc_sounds(self.files)
        self.sounds={s.number:s.filename.upper() for s in sounds
                     if s.number is not None and s.present and s.filename.upper().endswith('.VOC')}
        # Unreferenced VOC files have filenames but no numeric CON sound ID.
        referenced=set(self.sounds.values())
        self.extra_sounds={name:name for name in self.files if name.endswith('.VOC') and name not in referenced}
        return {**{i:f'{i:02d}.voc' for i in self.sounds},**self.extra_sounds}
    def extract(self,kind,ident):
        if kind=='levels':return bytes(self.files[self.levels[ident]])
        if kind=='tiles':return png_bytes(pc_preview(self.tiles[ident],self.colors))
        name=self.sounds[ident] if isinstance(ident,int) else self.extra_sounds[ident]
        return bytes(self.files[name])



# Insertion data encoders (no runtime patch installation).
pool_BASE = 8388608

pool_DIRECTORY_SIZE = 262144

pool_MAGIC = b'D64POOL1'

pool_VERSION = 1

pool_SIZES = (16777216, 33554432, 67108864)

pool_SCRIPT = 8650752

pool_SCRIPT_HEADER = 8781824

pool_TEXTURES = 8847360

pool_TEXTURE_MARKER = 8962048

pool_LEVELS = 8978432

pool_SOUND_CONTROL = 9043968

pool_SOUND_CONTROL_SIZE = 52160

pool_FIXED = {'directory': (pool_BASE, pool_DIRECTORY_SIZE), 'script_header': (pool_SCRIPT_HEADER, 4096), 'textures': (pool_TEXTURES, 114688), 'texture_marker': (pool_TEXTURE_MARKER, 16), 'levels': (pool_LEVELS, 16384), 'sound_control': (pool_SOUND_CONTROL, pool_SOUND_CONTROL_SIZE)}

def pool_active(rom):
    return bytes(rom[pool_BASE:pool_BASE + 8]) == pool_MAGIC

def pool_read(rom):
    if not pool_active(rom):
        raise ValueError('Insertion requires a ROM already prepared by the compatible editor; this tool will not install patches.')
    version, size, crc = struct.unpack_from('>3I', rom, pool_BASE + 8)
    if version != pool_VERSION or not 0 < size <= pool_DIRECTORY_SIZE - 20:
        raise ValueError('Invalid shared ROM directory.')
    raw = bytes(rom[pool_BASE + 20:pool_BASE + 20 + size])
    if zlib.crc32(raw) != crc:
        raise ValueError('Shared ROM directory checksum failed.')
    data = json.loads(raw)
    occupied = []
    for key, record in data.items():
        if not isinstance(key, str) or len(record) != 3:
            raise ValueError('Invalid ROM allocation record.')
        start, size, used = record
        if start % 16 or start < pool_BASE or size <= 0 or size % 16 or (not 0 <= used <= size) or (start + size > len(rom)):
            raise ValueError('Invalid ROM allocation bounds.')
        occupied.append((start, start + size))
    occupied.sort()
    if any((a[1] > b[0] for a, b in zip(occupied, occupied[1:]))):
        raise ValueError('Overlapping ROM allocations.')
    for key, (a, n) in pool_FIXED.items():
        if data.get('@' + key) != [a, n + 15 & ~15, n]:
            raise ValueError('Invalid resident ROM allocation.')
    return data

def pool_write(rom, data):
    raw = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > pool_DIRECTORY_SIZE - 20:
        raise ValueError('Shared ROM allocation directory is full.')
    rom[pool_BASE:pool_BASE + pool_DIRECTORY_SIZE] = (pool_MAGIC + struct.pack('>3I', pool_VERSION, len(raw), zlib.crc32(raw)) + raw).ljust(pool_DIRECTORY_SIZE, b'\x00')

def pool_get(rom, key):
    return pool_read(rom).get(key)

def pool_payload(rom, key):
    row = pool_get(rom, key)
    return bytes(rom[row[0]:row[0] + row[2]]) if row else b''

def pool_put(rom, key, payload):
    if key.startswith('@'):
        raise ValueError('Resident blocks cannot be replaced through the asset allocator.')
    payload = bytes(payload)
    if not payload:
        pool_release(rom, key)
        return 0
    data = pool_read(rom)
    old = data.pop(key, None)
    size = len(payload) + 15 & ~15
    cursor = pool_BASE
    for start, n, used in sorted(data.values()):
        if start - cursor >= size:
            break
        cursor = max(cursor, start + n)
    if cursor + size > len(rom):
        pool_expand(rom, cursor + size)
    proposed = dict(data)
    proposed[key] = [cursor, size, len(payload)]
    raw = json.dumps(proposed, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > pool_DIRECTORY_SIZE - 20:
        raise ValueError('Shared ROM allocation directory is full.')
    if old:
        rom[old[0]:old[0] + old[1]] = bytes(old[1])
    rom[cursor:cursor + size] = payload.ljust(size, b'\x00')
    pool_write(rom, proposed)
    return cursor

def pool_release(rom, key):
    data = pool_read(rom)
    if key.startswith('@') or key == 'stock_prefix':
        raise ValueError('Cannot free a resident or stock backup block.')
    old = data.pop(key, None)
    if old:
        rom[old[0]:old[0] + old[1]] = bytes(old[1])
        pool_write(rom, data)

def pool_expand(rom,minimum):
    raise ValueError('Insufficient free space in the current ROM. This tool will not expand it.')

def links_for_map(version, sectors, walls, sprites):
    """Validate supported EDuke32 connections and return ceiling/floor bunch IDs."""
    links = [[-1, -1] for _ in sectors]
    bunches = {}
    for i, s in enumerate(sectors):
        for side, flags, pan in ((0, s[4], s[10]), (1, s[5], s[16])):
            if not flags & PORTAL:
                continue
            if version != 9:
                raise ValueError(f'Sector {i}: TROR connections require a Mapster v9 map.')
            first, count = s[:2]
            if not 0 <= first < first + count <= len(walls):
                raise ValueError(f'Sector {i}: invalid wall range.')
            ceiling, _ = _surface_plane(s, 0, walls)
            floor, _ = _surface_plane(s, 1, walls)
            room_heights = [(floor[0] - ceiling[0]) * w[0] + (floor[1] - ceiling[1]) * w[1] + floor[2] - ceiling[2] for w in walls[first:first + count]]
            if min(room_heights) < 0 or max(room_heights) <= 0:
                raise ValueError(f'Sector {i}: inverted or zero-height TROR room.')
            bunches.setdefault(pan, [[], []])[side].append(i)
            links[i][side] = pan
    for bunch, (lower, upper) in bunches.items():
        if not lower or not upper:
            raise ValueError(f'TROR bunch {bunch}: missing ceiling or floor side.')
        lo = boundary_walls(sectors, walls, lower)
        hi = boundary_walls(sectors, walls, upper)
        if boundary_outline(lo) != boundary_outline(hi):
            raise ValueError(f'TROR bunch {bunch}: the combined ceiling and floor outlines do not match.')
        planes = {_surface_plane(sectors[i], side, walls)[0] for side, members in enumerate((lower, upper)) for i in members}
        if len(planes) != 1:
            raise ValueError(f'TROR bunch {bunch}: the connected surfaces do not share the same plane.')
    return links

def _surface_plane(sector, side, walls):
    z = sector[2 + side]
    h = sector[7 + side * 6] if sector[4 + side] & 2 else 0
    if not h:
        return ((Fraction(0), Fraction(0), Fraction(z)), None)
    first = walls[sector[0]]
    if not 0 <= first[2] < len(walls):
        raise ValueError('A sloped TROR connection has an invalid first-wall endpoint.')
    ox, oy = first[:2]
    end = walls[first[2]]
    dx, dy = (end[0] - ox, end[1] - oy)
    length = isqrt(dx * dx + dy * dy)
    if not length:
        raise ValueError('A sloped TROR connection has a zero-length first wall.')
    a, b = (Fraction(-h * dy, 256 * length), Fraction(h * dx, 256 * length))
    return ((a, b, Fraction(z) - a * ox - b * oy), (ox, oy, dx, dy, h, z))

def pack_bunches(links):
    return _pack(links, BUNCH_TAG, 256)

def _pack(links, tag, limit):
    if tuple(links) == (-1, -1):
        return bytes(3)
    if any((n < -1 or n >= limit for n in links)):
        raise ValueError('TROR connection is outside the supported range.')
    return (tag | links[0] + 1 | links[1] + 1 << 11).to_bytes(3, 'big')

def prepare_origin(start, walls, sprites, version):
    """Return copied coordinates and optional 16-byte wall-padding metadata."""
    xs = [w[0] for w in walls] + [s[0] for s in sprites] + [start[0]]
    ys = [w[1] for w in walls] + [s[1] for s in sprites] + [start[1]]
    origins = []
    for values in (xs, ys):
        low, high = (min(values), max(values))
        if low >= -65536 and high <= 65535:
            origins.append(0)
            continue
        if version != 9:
            raise ValueError(f'Map coordinates {low}..{high} exceed the N64 renderer range -65536..65535.')
        origin = (low + high) // 2 & ~1
        if low - origin < -65536 or high - origin > 65535:
            raise ValueError(f'Map span {high - low:,} exceeds the N64 renderer coordinate range; reduce the map in Mapster.')
        origins.append(origin)
    ox, oy = origins
    bits = start[0] & 1 | (start[1] & 1) << 1 | (start[2] & 31) << 2
    metadata = ORIGIN_MAGIC + bytes([bits]) + struct.pack('>ii', ox, oy) if version == 9 and (ox or oy or bits) else b''
    if metadata and len(walls) < 8:
        raise ValueError('This v9 map needs coordinate rebasing or a more precise start position; at least eight walls are required.')
    if ox or oy:
        start = (start[0] - ox, start[1] - oy, *start[2:])
        walls = [(w[0] - ox, w[1] - oy, *w[2:]) for w in walls]
        sprites = [(s[0] - ox, s[1] - oy, *s[2:]) for s in sprites]
    return (start, walls, sprites, metadata, (ox, oy))

def store_origin(packed_walls, metadata):
    if metadata:
        for i in range(8):
            packed_walls[i * 36 + 34:i * 36 + 36] = metadata[i * 2:i * 2 + 2]

HORIZONTAL_EFFECTORS = {0, 5, 6, 11, 14, 15, 16, 26, 30}

def plan(sectors, walls, sprites):
    movers = {s[13] for s in sprites if s[4] == 1 and s[20] in HORIZONTAL_EFFECTORS}
    adjacent = [{w[4] for w in walls[s[0]:s[0] + s[1]] if w[4] >= 0} for s in sectors]
    pending = list(movers)
    while pending:
        i = pending.pop()
        tag = sectors[i][-3]
        if tag:
            for j in adjacent[i]:
                if j not in movers and sectors[j][-3] == tag:
                    movers.add(j)
                    pending.append(j)
    additions = [defaultdict(set), defaultdict(set)]
    omit = [set(), set()]
    for i in sorted(movers):
        reachable = {i}
        todo = [i]
        candidates = set()
        while todo:
            j = todo.pop()
            for k in adjacent[j]:
                if k not in movers:
                    candidates.add(k)
                elif k not in reachable:
                    reachable.add(k)
                    todo.append(k)
        for side in (0, 1):
            zindex = 2 + side
            flagindex = 4 + side
            s = sectors[i]
            if s[flagindex] & 3:
                continue
            eligible = []
            for j in candidates:
                a = sectors[j]
                if a[flagindex] & 3:
                    continue
                delta = (a[zindex] - s[zindex]) * (1 if side else -1)
                if delta >= 0:
                    eligible.append((delta, 0 if j in adjacent[i] else 1, j))
            if not eligible:
                continue
            owner = min(eligible)[2]
            additions[side][owner].add(i)
            if not s[flagindex] & 64:
                omit[side].add(i)
    return (movers, additions, omit)

def union_rings(sectors, walls, members):
    """Join portal-linked polygons by removing internal paired wall edges."""
    boundary = {w for i in members for w in range(sectors[i][0], sectors[i][0] + sectors[i][1]) if walls[w][4] not in members}

    def following(w):
        point = walls[w][2]
        seen = set()
        while point not in boundary:
            if point in seen:
                raise TessellationError('Moving-sector underlay has a cyclic internal boundary.')
            seen.add(point)
            peer = walls[point][3]
            if peer < 0:
                raise TessellationError('Moving-sector underlay has an unpaired portal.')
            point = walls[peer][2]
        return point
    rings = []
    remaining = set(boundary)
    while remaining:
        start = min(remaining)
        point = start
        ring = []
        while point in remaining:
            remaining.remove(point)
            xy = tuple(walls[point][:2])
            if not ring or ring[-1] != xy:
                ring.append(xy)
            point = following(point)
        if point != start:
            raise TessellationError('Moving-sector underlay boundary does not close.')
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(ring) >= 3 and signed_area(ring):
            rings.append(ring)
    return rings

class RNCWriter:

    def __init__(self):
        self.data = bytearray()
        self.word = None
        self.used = 16

    def bits(self, value, count):
        for bit in range(count):
            if self.used == 16:
                self.word = len(self.data)
                self.data.extend(b'\x00\x00')
                self.used = 0
            if value >> bit & 1:
                self.data[self.word + self.used // 8] |= 1 << self.used % 8
            self.used += 1

    def literals(self, data):
        self.data.extend(data)

def symbol(value):
    return value.bit_length() if value >= 2 else value

def huffman(values):
    frequencies = Counter(map(symbol, values))
    if not frequencies:
        return ([], {})
    heap = [(freq, i, i) for i, freq in frequencies.items()]
    heapq.heapify(heap)
    serial = 16
    while len(heap) > 1:
        a = heapq.heappop(heap)
        b = heapq.heappop(heap)
        heapq.heappush(heap, (a[0] + b[0], serial, (a[2], b[2])))
        serial += 1
    depths = [0] * (max(frequencies) + 1)

    def visit(node, depth):
        if isinstance(node, int):
            depths[node] = max(1, depth)
        else:
            visit(node[0], depth + 1)
            visit(node[1], depth + 1)
    visit(heap[0][2], 0)
    if max(depths) > 15:
        depth = max(1, (len(frequencies) - 1).bit_length())
        depths = [depth if i in frequencies else 0 for i in range(len(depths))]
    codes = {}
    code = 0
    for depth in range(1, 16):
        code <<= 1
        for i, n in enumerate(depths):
            if n == depth:
                reverse = int(f'{code:0{depth}b}'[::-1], 2)
                codes[i] = (reverse, depth)
                code += 1
    return (depths, codes)

def emit_table(writer, table):
    depths, _ = table
    writer.bits(len(depths), 5)
    for depth in depths:
        writer.bits(depth, 4)

def emit_value(writer, value, table):
    category = symbol(value)
    code, count = table[1][category]
    writer.bits(code, count)
    if category >= 2:
        writer.bits(value - (1 << category - 1), category - 1)

def rnc_pack(data):
    """Return an RNC1 stream, independently chunked at 12 KiB boundaries."""
    data = bytes(data)
    writer = RNCWriter()
    writer.bits(0, 2)
    chunks = 0
    chains = defaultdict(deque)
    for base in range(0, len(data), 12288):
        end = min(len(data), base + 12288)
        position = base
        literal_start = base
        runs = []

        def remember(p):
            if p + 3 <= len(data):
                chain = chains[data[p:p + 3]]
                chain.append(p)
                while chain and (p - chain[0] > 32768 or len(chain) > 128):
                    chain.popleft()
        while position < end:
            best = 0
            distance = 0
            if position + 3 <= end:
                candidates = chains.get(data[position:position + 3], ())
                for previous in reversed(candidates):
                    offset = position - previous
                    if offset > 32768:
                        break
                    count = 3
                    maximum = end - position
                    while count < maximum and data[previous + count] == data[position + count]:
                        count += 1
                    if count > best:
                        best = count
                        distance = offset
                    if best == maximum:
                        break
            if best >= 3:
                runs.append((data[literal_start:position], distance - 1, best - 2))
                for p in range(position, position + best):
                    remember(p)
                position += best
                literal_start = position
            else:
                remember(position)
                position += 1
        runs.append((data[literal_start:end], None, None))
        literal = huffman((len(r[0]) for r in runs))
        distance = huffman((r[1] for r in runs[:-1]))
        length = huffman((r[2] for r in runs[:-1]))
        for table in (literal, distance, length):
            emit_table(writer, table)
        writer.bits(len(runs), 16)
        for raw, offset, count in runs:
            emit_value(writer, len(raw), literal)
            writer.literals(raw)
            if offset is not None:
                emit_value(writer, offset, distance)
                emit_value(writer, count, length)
        chunks += 1
    if chunks > 255:
        raise ValueError('RNC1 input exceeds 255 chunks.')
    payload = bytes(writer.data)
    return struct.pack('>4sIIHHBB', b'RNC\x01', len(data), len(payload), crc16(data), crc16(payload), 0, chunks) + payload

def encode_block(data, *, use_compression=True):
    """Use compression only when smaller than the supported stored wrapper."""
    data = bytes(data)
    stored = struct.pack('>4sI', b'RNC\x00', len(data)) + data
    compressed = rnc_pack(data) if use_compression else stored
    result = compressed if len(compressed) < len(stored) else stored
    if bytes(decompress(result, 0, len(result), len(data))) != data:
        raise ValueError('RNC compression validation failed.')
    return result

N64_WALL_LIMIT = 3712

BUILD_V7_WALL_LIMIT = 8192

def parse_map(data, *, ignore_wall_limit=False, sector_limit=657, sprite_limit=1999):
    if len(data) < 26:
        raise ValueError('truncated Build map')
    version, x, y, z, angle, sector = struct.unpack_from('<4ihh', data)
    if version not in (7, 8, 9):
        raise ValueError(f'Build map v{version} is unsupported; use v7, v8 or legacy TROR v9')
    position = 20
    arrays = []
    wall_limit = BUILD_V7_WALL_LIMIT if ignore_wall_limit else N64_WALL_LIMIT
    for name, layout, limit in (('sectors', MAP_SECTOR, sector_limit), ('walls', MAP_WALL, wall_limit), ('sprites', MAP_SPRITE, sprite_limit)):
        if position + 2 > len(data):
            raise ValueError('truncated Build map counts')
        count, = struct.unpack_from('<H', data, position)
        if count > limit:
            if name == 'walls' and (not ignore_wall_limit):
                raise ValueError(f'map has {count} walls; N64 limit is {limit}. Apply Duke64_USA_8192_Walls.ips to a clean USA ROM to raise it.')
            raise ValueError(f'map has {count} {name}; import limit is {limit}')
        position += 2
        end = position + count * layout.size
        if end > len(data):
            raise ValueError('truncated Build map records')
        arrays.append(list(layout.iter_unpack(data[position:end])))
        position = end
    if position != len(data):
        raise ValueError('unexpected data after Build sprite records')
    if not arrays[0] or not 0 <= angle < 2048:
        raise ValueError('invalid map start angle or empty map')
    return ((x, y, z, angle, sector), arrays)

def rings_for_sector(walls, first, count, ownership, sector):
    if not 0 <= first < first + count <= len(walls):
        raise ValueError(f'sector {sector} has an invalid wall range')
    remaining = set(range(first, first + count))
    rings = []
    for index in range(first, first + count):
        if ownership[index] != -1:
            raise ValueError(f'wall {index} is assigned to multiple sectors')
        ownership[index] = sector
        if index not in remaining:
            continue
        ring = []
        current = index
        while current in remaining:
            remaining.remove(current)
            x, y, current = walls[current][:3]
            if not ring or ring[-1] != (x, y):
                ring.append((x, y))
        if current != index:
            raise ValueError(f'sector {sector} has a broken wall loop')
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring.pop()
        if len(ring) >= 3 and signed_area(ring):
            rings.append(ring)
    return rings

def signed_area(ring):
    return sum((x1 * y2 - y1 * x2 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])))

def inside(ring, point):
    x, y = point
    crossed = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            crossed = not crossed
    return crossed

class TessellationError(ValueError):
    pass

def triangulate(rings):
    import mapbox_earcut
    import numpy as np
    areas = [abs(signed_area(ring)) for ring in rings]
    parents = []
    depths = []
    for i, ring in enumerate(rings):
        enclosing = [j for j, outer in enumerate(rings) if areas[j] > areas[i] and inside(outer, ring[0])]
        parents.append(min(enclosing, key=areas.__getitem__) if enclosing else None)
        depths.append(len(enclosing))
    triangles = []
    for i, outer in enumerate(rings):
        if depths[i] % 2:
            continue
        group = [outer] + [ring for j, ring in enumerate(rings) if parents[j] == i]
        points = []
        ends = []
        for ring in group:
            points.extend(ring)
            ends.append(len(points))
        indices = mapbox_earcut.triangulate_float64(np.asarray(points, dtype=np.float64), np.asarray(ends, dtype=np.uint32))
        covered = 0
        for a, b, c in zip(indices[::3], indices[1::3], indices[2::3]):
            triangle = [points[a], points[b], points[c]]
            area = signed_area(triangle)
            if area:
                covered += abs(area)
                if area < 0:
                    triangle.reverse()
                triangles.append(triangle)
        expected = abs(signed_area(outer)) - sum((abs(signed_area(ring)) for ring in group[1:]))
        if covered != expected:
            raise TessellationError('tessellation did not cover the sector area')
    return triangles

def triangulate_crossing_loops(rings):
    try:
        from shapely import constrained_delaunay_triangles
    except ImportError as error:
        raise TessellationError('Crossing wall loops require Shapely 2.1 or newer. Run py -3 -m pip install "shapely>=2.1".') from error
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union
    edges = [LineString(ring + ring[:1]) for ring in rings]
    faces = list(polygonize(unary_union(edges)))
    triangles = []
    for face in faces:
        sample = face.representative_point().coords[0]
        if sum((inside(ring, sample) for ring in rings)) % 2 == 0:
            continue
        pieces = constrained_delaunay_triangles(face).geoms
        if not isclose(sum((piece.area for piece in pieces)), face.area, abs_tol=1e-07):
            raise TessellationError('could not tessellate a face of the sector')
        for piece in pieces:
            triangle = list(piece.exterior.coords)[:-1]
            if signed_area(triangle) < 0:
                triangle.reverse()
            triangles.append(triangle)
    if not triangles:
        raise TessellationError('could not find a filled face in the sector')
    return triangles

def write_triangles(mesh, triangles, periods):
    first = len(mesh) // 10
    count = 0
    pending = list(reversed(triangles))
    while pending:
        triangle = pending.pop()
        shifts = []
        for axis, period in zip((3, 4), periods):
            low, high = (min((v[axis] for v in triangle)), max((v[axis] for v in triangle)))
            shift = (low + 32768) // period * period
            if high - shift > 32767:
                break
            shifts.append(shift)
        if len(shifts) == 2:
            count += 1
            if count > 255 or first + count * 3 > 65535:
                raise ValueError('N64 surface triangle count or mesh index overflow')
            du, dv = shifts
            for x, y, z, u, v in triangle:
                if any((not -32768 <= value <= 32767 for value in (x, y, z))):
                    raise ValueError('surface geometry exceeds the N64 signed 16-bit vertex range')
                mesh.extend(struct.pack('>5h', x, y, z, u - du, v - dv))
        else:
            start = max(range(3), key=lambda i: abs(triangle[i][axis] - triangle[(i + 1) % 3][axis]))
            a, b, c = (triangle[(start + i) % 3] for i in range(3))
            mid = tuple(((av + bv) // 2 for av, bv in zip(a, b)))
            pending.extend(((mid, b, c), (a, mid, c)))
    return (first, count)

def surface(mesh, triangles, origin, edge, z, flags, slope, pan, tile, uv_origin=(0, 0)):
    if flags & 1:
        return (len(mesh) // 10, 0)
    ox, oy = origin
    dx, dy = edge
    length = hypot(dx, dy) or 1
    slope_denominator = isqrt(dx * dx + dy * dy) << 5
    scale = 4 if flags & 8 else 2
    width, height, tex_width, tex_height = tile
    xperiod = 1 << width.bit_length() - 1
    yperiod = 1 << height.bit_length() - 1
    projected = {}
    for triangle in triangles:
        for x, y in triangle:
            if (x, y) in projected:
                continue
            vx, vy = (floor(x / 2), floor(y / 2))
            vx = 32767 if vx == 32768 else vx
            vy = 32767 if vy == 32768 else vy
            rx, ry = (vx * 2 - ox, vy * 2 - oy)
            cross = dx * ry - dy * rx
            vz = z
            if flags & 2 and slope_denominator:
                vz += int(slope * (cross >> 3) / slope_denominator)
            if flags & 64:
                u = (dx * rx + dy * ry) / length
                v = cross / length
                if flags & 2:
                    v *= hypot(1, slope / 4096)
            else:
                u, v = (vx * 2 + uv_origin[0], -(vy * 2 + uv_origin[1]))
            if flags & 4:
                u, v = (v, u)
            if flags & 16:
                u = -u
            if flags & 32:
                v = -v
            u = round(u * scale * tex_width / xperiod + pan[0] * tex_width / 8)
            v = round(v * scale * tex_height / yperiod + pan[1] * tex_height / 8)
            projected[x, y] = (vx, vy, vz >> 5, u, v)
    return write_triangles(mesh, [[projected[p] for p in triangle] for triangle in triangles], (tex_width * 32, tex_height * 32))

def convert_map(data, rom, *, ignore_wall_limit=False, use_compression=True, moving_sector_mesh=True):
    start, (sectors, walls, sprites) = parse_map(data, ignore_wall_limit=ignore_wall_limit, sector_limit=SECTOR_LIMIT if has_sector_patch(rom) else STOCK_LIMIT)
    version = struct.unpack_from('<i', data)[0]
    vertical_links = links_for_map(version, sectors, walls, sprites)
    start, walls, sprites, origin_metadata, uv_origin = prepare_origin(start, walls, sprites, version)
    x, y, z, angle, current = start
    x, y, z = (x >> 1, y >> 1, z >> 5)
    if not any((0 <= s[0] < s[0] + s[1] <= len(walls) and all((0 <= w[2] < len(walls) for w in walls[s[0]:s[0] + s[1]])) and contains_point(walls, s[0], s[1], x * 2, y * 2) for s in sectors)):
        raise ValueError(f'player start is outside the board (PC start sector {current})')
    owners = [-1] * len(walls)
    mesh, packed_sectors, packed_walls, packed_sprites = (bytearray() for _ in range(4))
    tiles = tile_info(rom)
    missing = defaultdict(set)
    fallback = 716
    moving_plan = None
    if moving_sector_mesh:
        moving_plan = plan(sectors, walls, sprites)

    def material(number, usage, *, parallax=False):
        if number not in tiles:
            missing[usage].add(number)
            return number if parallax and 0 <= number < 6144 else fallback
        return number
    for i, sector in enumerate(sectors):
        first, count, cz, fz, cs, fs, cp, ch, cshade, cpal, cx, cy, fp, fh, fshade, fpal, fx, fy, vis, _, low, high, extra = sector
        if vertical_links[i][0] >= 0:
            cx = 0
        if vertical_links[i][1] >= 0:
            fx = 0
        rings = rings_for_sector(walls, first, count, owners, i)
        try:
            try:
                triangles = triangulate(rings)
            except TessellationError:
                triangles = triangulate_crossing_loops(rings)
        except TessellationError as error:
            raise TessellationError(f'sector {i} mesh: {error}') from error
        floor_triangles = ceiling_triangles = triangles
        if moving_plan is not None:
            _, additions, omit = moving_plan
            surfaces = []
            for side in (0, 1):
                if i in omit[side]:
                    surfaces.append([])
                    continue
                if i not in additions[side]:
                    surfaces.append(triangles)
                    continue
                combined = union_rings(sectors, walls, {i} | additions[side][i])
                try:
                    surfaces.append(triangulate(combined))
                except TessellationError:
                    surfaces.append(triangulate_crossing_loops(combined))
            ceiling_triangles, floor_triangles = surfaces
        if vertical_links[i][0] >= 0:
            ceiling_triangles = triangles
        if vertical_links[i][1] >= 0:
            floor_triangles = triangles
        ox, oy, next_wall = walls[first][:3]
        ex, ey = walls[next_wall][:2]
        origin, edge = ((ox, oy), (ex - ox, ey - oy))
        cp = material(cp, 'parallax ceiling' if cs & 1 else 'surface', parallax=bool(cs & 1))
        fp = material(fp, 'surface')
        try:
            fi, fn = surface(mesh, [t[::-1] for t in floor_triangles], origin, edge, fz, fs, fh, (fx, fy), tiles[fp], uv_origin)
            ci, cn = surface(mesh, ceiling_triangles, origin, edge, cz, cs, ch, (cx, cy), tiles.get(cp))
        except (ValueError, struct.error) as error:
            raise ValueError(f'sector {i} mesh: {error}') from error
        packed_sectors.extend(SECTOR.pack(cz, fz, first, count, cs, fs, cp, ch, fp, fh, low, high, extra, fi, ci, cshade, cpal, cx, cy, fshade, fpal, fx, fy, vis, fn, cn))
        if any((n >= 0 for n in vertical_links[i])):
            packed_sectors[-3:] = pack_bunches(vertical_links[i])
    for i, wall in enumerate(walls):
        if version == 9:
            wall = list(wall)
            if wall[5] & 1024:
                wall[14] = 0
            if wall[5] & 2048:
                wall[16] = -1
            wall[5] &= ~(1024 | 2048)
        peer, neighbor, flags, pic, over = wall[3:8]
        if owners[i] == -1:
            raise ValueError(f'wall {i} has no sector')
        if not (peer == -1 and neighbor == -1 or (0 <= peer < len(walls) and 0 <= neighbor < len(sectors))):
            raise ValueError(f'wall {i} has an invalid portal')
        pic = material(pic, 'wall')
        if over >= 0 and (over != 0 or flags & 48):
            over = material(over, 'masked wall')
        packed_walls.extend(WALL.pack(*wall[:6], pic, over, *wall[-3:], owners[i], *wall[8:14]))
    for i, sprite in enumerate(sprites):
        pic = sprite[4]
        if not (0 <= sprite[13] < len(sectors) and 0 <= sprite[14] < 1024):
            raise ValueError(f'sprite {i} has an invalid sector or status')
        if not 0 <= pic < 6144:
            raise ValueError(f'sprite {i} has an invalid tile ID')
        if pic not in tiles and pic > 10:
            missing['sprite (ID retained)'].add(pic)
        sprite = list(sprite)
        if sprite[3] & 48 == 16:
            sprite[3] |= 16384
        packed_sprites.extend(SPRITE.pack(*sprite[:5], *sprite[13:], *sprite[5:8], *sprite[9:13]))
    store_origin(packed_walls, origin_metadata)
    streams = (mesh, packed_sectors, packed_walls, packed_sprites)
    if sum((len(raw) + 8 + 15 & ~15 for raw in streams)) > 524288:
        raise ValueError('uncompressed board exceeds the 512 KiB N64 staging area')
    block = bytearray()
    pointers = []
    for raw in streams:
        if block:
            pointers.append(BOARD_ADDRESS + len(block))
        block.extend(encode_block(raw, use_compression=use_compression))
        block.extend(bytes(-len(block) % 16))
    if len(block) > 524288:
        raise ValueError('packed board exceeds the 512 KiB N64 staging area')
    header = (*pointers, len(sectors), len(sprites), len(walls), x, y, z, angle)
    return (block, header, missing)

def crc6102(rom):
    mask = 4294967295
    seed = 4174007772
    t1 = t2 = t3 = t4 = t5 = t6 = seed
    for word, in struct.iter_unpack('>I', memoryview(rom)[4096:1052672]):
        total = t6 + word
        t4 = t4 + (total > mask) & mask
        t6 = total & mask
        t3 ^= word
        shift = word & 31
        rotated = (word << shift | word >> 32 - shift) & mask
        t5 = t5 + rotated & mask
        t2 ^= rotated if t2 > word else t6 ^ word
        t1 = t1 + (t5 ^ word) & mask
    return (t6 ^ t4 ^ t3, t5 ^ t2 ^ t1)

TILE_DMA_LIMIT = 37252

def tile_info(rom):
    return {tile.number: (tile.width, tile.height, tile.stored_width, tile.stored_height) for tile in list_tiles(rom)}

def pad_texture(image, indexed8=False):
    """Align rows, keeping sprite padding transparent and surface edges extended."""
    from PIL import Image
    width, height = image.size
    alignment = 8 if indexed8 else 16
    stored_width = (width + alignment - 1) // alignment * alignment
    size = stored_width * height
    if not width or not height or max(stored_width, height) > 32767:
        raise ValueError(f'Texture dimensions {width} × {height} exceed the N64 range.')
    if not indexed8:
        size = size // 2 + 32
    if size > TILE_DMA_LIMIT:
        raise ValueError(f'Texture {width} × {height} needs {size:,} bytes, above the supported {TILE_DMA_LIMIT:,}-byte load size.')
    if width == stored_width:
        return image
    padded = Image.new(image.mode, (stored_width, height))
    padded.paste(image)
    if image.mode == 'RGBA' and image.getchannel('A').getextrema()[0] < 128:
        return padded
    edge = image.crop((width - 1, 0, width, height))
    padded.paste(edge.resize((stored_width - width, height), Image.Resampling.NEAREST), (width, 0))
    return padded

def encode_replacement(rom, tile, image):
    from PIL import Image
    image = image.convert('RGBA').resize((tile.stored_width, tile.stored_height), Image.Resampling.NEAREST)
    if tile.indexed8:
        pixels = list(image.getdata())
        _, _, offset = ART_OFFSETS[rom[62]]
        palette = [rgba5551(word) for word in struct.unpack_from('>256H', rom, offset)]
        matches = {}
        for r, g, b, a in set(pixels):

            def distance(index):
                pr, pg, pb, pa = palette[index]
                return ((a >= 128) != bool(pa), (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2)
            matches[r, g, b, a] = min(range(256), key=distance)
        return bytes((matches[pixel] for pixel in pixels))
    return encode_ci4(image)

def encode_ci4(image):
    from PIL import Image
    pixels = list(image.convert('RGBA').getdata())
    transparent = any((a < 128 for _, _, _, a in pixels))
    fill = next((pixel[:3] for pixel in pixels if pixel[3] >= 128), (0, 0, 0))
    rgb = Image.new('RGB', image.size)
    rgb.putdata([pixel[:3] if pixel[3] >= 128 else fill for pixel in pixels])
    quantized = rgb.quantize(colors=15 if transparent else 16, dither=Image.Dither.NONE)
    colors = quantized.getpalette()
    start = int(transparent)
    palette = [0] * 16
    for index in set(quantized.tobytes()):
        r, g, b = colors[index * 3:index * 3 + 3]
        palette[index + start] = r >> 3 << 11 | g >> 3 << 6 | b >> 3 << 1 | 1
    indices = [0 if a < 128 else index + start for (_, _, _, a), index in zip(pixels, quantized.tobytes())]
    if len(indices) % 2:
        indices.append(0)
    return struct.pack('<16H', *palette) + bytes((a << 4 | b for a, b in zip(indices[::2], indices[1::2])))

def voc_to_wav(data):
    if len(data) < 26 or data[:20] != b'Creative Voice File\x1a':
        raise ValueError('Invalid Creative VOC header.')
    offset, version, checksum = struct.unpack_from('<3H', data, 20)
    if not 26 <= offset <= len(data) or checksum != ~version + 4660 & 65535:
        raise ValueError('Invalid VOC header offset/checksum.')
    output = []
    out_rate = None
    current = None
    extended = None

    def emit(raw, rate, bits, channels, codec):
        nonlocal out_rate, current
        if codec == 0 and bits == 0:
            bits = 8
        if (codec, bits) not in ((0, 8), (4, 16)):
            raise ValueError(f'Unsupported VOC encoding {codec}, {bits}-bit; use PCM 8/16-bit VOC or WAV.')
        if channels not in (1, 2) or not 1000 <= rate <= 192000:
            raise ValueError('Invalid VOC channel count/sample rate.')
        width = bits // 8
        if len(raw) % (channels * width):
            raise ValueError('Truncated VOC sample frame.')
        current = (rate, bits, channels, codec)
        values = []
        for i in range(0, len(raw), channels * width):
            samples = [(raw[i + j] - 128) * 256 for j in range(channels)] if bits == 8 else struct.unpack_from('<' + str(channels) + 'h', raw, i)
            values.append(round(sum(samples) / channels))
        if out_rate is None:
            out_rate = rate
        n = round(len(values) * out_rate / rate)
        if (len(output) + n) / out_rate > 120:
            raise ValueError('Audio must be at most 120 seconds.')
        if rate == out_rate:
            output.extend(values)
        elif values:
            for i in range(n):
                pos = i * rate / out_rate
                left = min(int(pos), len(values) - 1)
                f = pos - left
                output.append(round(values[left] * (1 - f) + values[min(left + 1, len(values) - 1)] * f))
    while offset < len(data):
        kind = data[offset]
        offset += 1
        if kind == 0:
            break
        if offset + 3 > len(data):
            raise ValueError('Truncated VOC block header.')
        size = int.from_bytes(data[offset:offset + 3], 'little')
        offset += 3
        if offset + size > len(data):
            raise ValueError('Truncated VOC block.')
        block = data[offset:offset + size]
        offset += size
        if kind == 1:
            if size < 2:
                raise ValueError('Truncated VOC sound header.')
            fmt = extended or (round(1000000 / (256 - block[0])), 8, 1, block[1])
            extended = None
            emit(block[2:], *fmt)
        elif kind == 2:
            if current is None:
                raise ValueError('VOC continuation has no sound format.')
            emit(block, *current)
        elif kind == 3:
            if size != 3:
                raise ValueError('Invalid VOC silence block.')
            n = struct.unpack_from('<H', block)[0] + 1
            rate = round(1000000 / (256 - block[2]))
            previous = current
            emit(bytes([128]) * n, rate, 8, 1, 0)
            current = previous
        elif kind == 8:
            if size != 4:
                raise ValueError('Invalid VOC extended-format block.')
            tc, codec, mode = struct.unpack('<HBB', block)
            channels = mode + 1
            extended = (round(256000000 / (65536 - tc) / channels), 8, channels, codec)
        elif kind == 9:
            if size < 12:
                raise ValueError('Truncated VOC new-format header.')
            rate, bits, channels, codec = struct.unpack_from('<IBBH', block)
            emit(block[12:], rate, bits, channels, codec)
        elif kind in (4, 5, 6, 7):
            continue
        else:
            raise ValueError(f'Unsupported VOC block type {kind}.')
    if not output or out_rate is None:
        raise ValueError('VOC contains no PCM audio.')
    result = io.BytesIO()
    with wave.open(result, 'wb') as wav:
        wav.setparams((1, 2, out_rate, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack('<' + str(len(output)) + 'h', *output))
    return result.getvalue()

def audio_as_wav(data):
    return voc_to_wav(data) if data.startswith(b'Creative Voice File') else data

def audio_rate(data):
    with wave.open(io.BytesIO(audio_as_wav(data)), 'rb') as wav:
        rate = wav.getframerate()
    return rate if 1000 <= rate <= 32767 else 22050

SOUND_RATE_TABLE = 566440

SOUND_RATE_COUNT = 277

def sound_bank(rom):
    """Locate the effects bank and its instrument in the original asset area."""
    candidates = [SOUND_CONTROL] if active(rom) else range(5242880, min(len(rom), 8388608) - 16, 16)
    for base in candidates:
        if rom[base:base + 4] != b'B1\x00\x01':
            continue
        try:

            def pointer(offset):
                relative = struct.unpack_from('>I', rom, offset)[0]
                if not 8 <= relative < 131072:
                    raise ValueError('Invalid sound bank pointer')
                return base + relative
            bank = pointer(base + 4)
            if struct.unpack_from('>H', rom, bank)[0] != 1:
                continue
            instrument = pointer(bank + 12)
            count = struct.unpack_from('>H', rom, instrument + 14)[0]
            if not 1 <= count <= 450:
                continue
            for index in range(count):
                sound = pointer(instrument + 16 + index * 4)
                pointer(sound)
                pointer(sound + 4)
                pointer(sound + 8)
            return (base, bank, instrument, count)
        except (ValueError, struct.error):
            continue
    raise ValueError('Sound-effect bank not found in this ROM.')

def sound_info(rom):
    base, bank, instrument, count = sound_bank(rom)
    if base not in (6285168, SOUND_CONTROL):
        raise ValueError('WAV operations currently support the USA sound bank only.')
    table = 6337328
    pointer = lambda offset: base + struct.unpack_from('>I', rom, offset)[0]
    records = []
    for index in range(count):
        sound = pointer(instrument + 16 + index * 4)
        wave = pointer(sound + 8)
        sample, length = struct.unpack_from('>II', rom, wave)
        kind = rom[wave + 8]
        if kind not in (0, 1) or table + sample + length > len(rom):
            raise ValueError(f'Invalid sample data for sound {index}.')
        loop = struct.unpack_from('>I', rom, wave + 12)[0]
        book = struct.unpack_from('>I', rom, wave + 16)[0] if kind == 0 else 0
        records.append(dict(sound=sound, wave=wave, bank_index=index, sample=table + sample, length=length, kind=kind, loop=base + loop if loop else 0, book=base + book if book else 0))
    records.sort(key=lambda info: info['sound'])
    expanded = sound_capacity_patched(rom)
    for sound_id, info in enumerate(records):
        if sound_id >= (CAPACITY if expanded else SOUND_RATE_COUNT):
            raise ValueError(f'No verified playback-rate entry for sound {sound_id}.')
        rate = struct.unpack_from('>h', rom, (RATE_ROM if expanded else SOUND_RATE_TABLE) + sound_id * 2)[0]
        if not 1000 <= rate <= 32767:
            raise ValueError(f'Invalid playback rate for sound {sound_id}: {rate}.')
        info['rate'] = rate
    return (base, bank, instrument, table, records)

def read_wav(data, rate):
    data = audio_as_wav(data)
    import io
    import wave
    try:
        with wave.open(io.BytesIO(data), 'rb') as wav:
            channels, width, source_rate, frames = wav.getparams()[:4]
            if channels not in (1, 2) or width not in (1, 2, 3, 4) or (not 1000 <= source_rate <= 192000):
                raise ValueError('Use a mono or stereo PCM WAV with 8, 16, 24, or 32-bit samples.')
            if not frames or frames / source_rate > 120:
                raise ValueError('WAV must contain between one sample and 120 seconds of audio.')
            raw = wav.readframes(frames)
    except (wave.Error, EOFError) as error:
        raise ValueError(f'Unsupported WAV: {error}') from error
    if len(raw) != frames * channels * width:
        raise ValueError('Truncated WAV audio.')
    values = []
    for frame in range(frames):
        total = 0
        for channel in range(channels):
            offset = (frame * channels + channel) * width
            value = int.from_bytes(raw[offset:offset + width], 'little', signed=width != 1)
            total += (value - 128) * 256 if width == 1 else value >> 8 * (width - 2) if width >= 2 else value
        values.append(round(total / channels))
    count = max(1, round(frames * rate / source_rate))
    if rate != source_rate:
        converted = []
        for i in range(count):
            position = i * source_rate / rate
            left = min(int(position), frames - 1)
            fraction = position - left
            converted.append(round(values[left] * (1 - fraction) + values[min(left + 1, frames - 1)] * fraction))
        values = converted
    return values

def replace_sounds(rom, replacements, *, additions=(), stock_restore=None):
    """Return a revised ROM, rebuilding control objects to isolate shared IDs.

    Replacement audio uses native RAW16 rather than a lossy ADPCM encoder.
    Original key maps and sample rates are retained; non-looping envelopes are
    retimed to match imported audio. Existing loops
    retain their proportional position within the replacement sample.
    """
    base, bank, instrument, table, records = sound_info(rom)
    if any((not isinstance(i, int) or not 0 <= i < len(records) for i in replacements)):
        raise ValueError('Unknown sound ID.')
    if not replacements and (not additions) and (not stock_restore):
        raise ValueError('No sound imports were supplied.')
    if has_legacy_sound_imports(rom):
        raise ValueError('Earlier sound imports need repair. Use Repair Legacy Bank and select an original USA ROM first.')
    revised = bytearray(rom)
    if additions:
        if len(records) + len(additions) > CAPACITY:
            raise ValueError(f'This patch supports {CAPACITY} sounds (IDs 0–299); {CAPACITY - len(records)} slots remain.')
        replacements = dict(replacements)
        rates = {}
        for data in additions:
            i = len(records)
            rate = audio_rate(data)
            records.append(dict(records[0], rate=rate, loop=0))
            replacements[i] = data
            rates[i] = rate
        patch_sound_capacity(revised, len(records), rates)
    if stock_restore:
        donor, ids = stock_restore
        donor_records = sound_info(donor)[4]
        patch_sound_capacity(revised, len(records))
        for i in ids:
            revised[RATE_ROM + i * 2:RATE_ROM + i * 2 + 2] = donor[566440 + i * 2:566442 + i * 2]
            revised[VOICE_ROM + i * 2:VOICE_ROM + i * 2 + 2] = donor[567008 + i * 2:567010 + i * 2]
        for i in ids:
            release(revised, f'sound:{i}')
            records[i] = dict(donor_records[i], _source=donor, _base=sound_info(donor)[0])
            a = records[i]['sample']
            b = a + records[i]['length']
            revised[a:b] = donor[a:b]
    converted = {i: read_wav(data, records[i]['rate']) for i, data in replacements.items()}
    control = bytearray(8)
    cache = {}

    def add(data, key=None):
        if key is not None and key in cache:
            return cache[key]
        while len(control) % 8:
            control.append(0)
        result = len(control)
        control.extend(data)
        if key is not None:
            cache[key] = result
        return result
    for i, info in enumerate(records):
        if i not in converted:
            continue
        samples = converted[i]
        payload = struct.pack(f'>{len(samples)}h', *samples)
        position = put(revised, f'sound:{i}', payload + bytes(16))
        info['replacement'] = (position - table, len(payload))
    sound_pointers = []
    for i, info in enumerate(records):
        source = info.get('_source', rom)
        source_key = 1 if '_source' in info else 0
        sound = bytearray(source[info['sound']:info['sound'] + 16])
        for offset, size in ((0, 16), (4, 8)):
            old = info.get('_base', base) + struct.unpack_from('>I', sound, offset)[0]
            payload = bytearray(source[old:old + size])
            key = ('object', source_key, old)
            if offset == 0 and i in converted:
                looping = info['loop'] and struct.unpack_from('>I', rom, info['loop'] + 8)[0] != 0
                if not looping:
                    duration = max(1, round(len(converted[i]) * 1000000 / 22050))
                    struct.pack_into('>2i', payload, 0, 0, duration)
                if additions and i >= len(records) - len(additions):
                    payload[12:14] = bytes((127, 127))
                key = None
            struct.pack_into('>I', sound, offset, add(payload, key))
        wave = bytearray(source[info['wave']:info['wave'] + 24])
        replacement = i in converted
        loop_pointer = 0
        if info['loop']:
            size = 44 if info['kind'] == 0 else 12
            loop = bytearray(source[info['loop']:info['loop'] + size])
            if replacement:
                start, end, count = struct.unpack_from('>III', loop)
                original_count = info['length'] // 9 * 16 if info['kind'] == 0 else info['length'] // 2
                length = len(converted[i])
                start = min(length - 1, round(start * length / max(1, original_count)))
                end = max(start + 1, min(length, round(end * length / max(1, original_count))))
                loop = struct.pack('>III', start, end, count)
            loop_pointer = add(loop, None if replacement else ('loop', source_key, info['loop']))
        struct.pack_into('>I', wave, 12, loop_pointer)
        if replacement:
            struct.pack_into('>II', wave, 0, *info['replacement'])
            wave[8:12] = bytes((1, 0, 0, 0))
            wave[16:24] = bytes(8)
        elif info['kind'] == 0:
            order, count = struct.unpack_from('>II', source, info['book'])
            size = 8 + order * count * 16
            book = add(source[info['book']:info['book'] + size], ('book', source_key, info['book']))
            struct.pack_into('>I', wave, 16, book)
        wave_pointer = add(wave, None if replacement else ('wave', source_key, info['wave']))
        struct.pack_into('>I', sound, 8, wave_pointer)
        sound_pointers.append(add(sound))
    inst = bytearray(rom[instrument:instrument + 16])
    struct.pack_into('>H', inst, 14, len(sound_pointers))
    inst.extend(struct.pack(f'>{len(sound_pointers)}I', *sound_pointers))
    inst_pointer = add(inst)
    bank_data = bytearray(rom[bank:bank + 16])
    if struct.unpack_from('>I', bank_data, 8)[0]:
        raise ValueError('Unexpected percussion instrument in sound-effect bank.')
    struct.pack_into('>I', bank_data, 12, inst_pointer)
    bank_pointer = add(bank_data)
    control[:8] = struct.pack('>HHI', 16945, 1, bank_pointer)
    if len(control) > SOUND_CONTROL_SIZE:
        raise ValueError('Rebuilt sound metadata exceeds the original control-bank allocation.')
    revised[base:base + SOUND_CONTROL_SIZE] = control + bytes(SOUND_CONTROL_SIZE - len(control))
    if additions:
        first = len(records) - len(additions)
        for i in range(first, len(records)):
            for address in (2148527504, 2148528104, 2148528704):
                struct.pack_into('>h', revised, address - 2147787776 + i * 2, 0)
            revised[2148529304 - 2147787776 + i] = 0
    return revised

SECRET_LINKS = {1: (7, 14176), 15: (18, 14180), 23: (28, 14184), 26: (29, 14188), 32: (33, 14192)}

SECRET_ORIGINS = {7, 18, 28, 29}

DM_ORIGINS = {30, 31, 32, 33}

def routes(entries):
    positions = {e['origin']: i for i, e in enumerate(entries)}
    regular = [i for i, e in enumerate(entries) if e['origin'] not in SECRET_ORIGINS | DM_ORIGINS]
    if not regular:
        raise ValueError('Keep at least one campaign level.')
    normal, secret, unlock = ([], [], [])
    for i, entry in enumerate(entries):
        origin = entry['origin']
        after = i
        if origin in SECRET_ORIGINS:
            source = next((s for s, (target, _) in SECRET_LINKS.items() if target == origin))
            after = positions.get(source, i)
        if origin in DM_ORIGINS:
            candidates = [j for j in range(i + 1, len(entries)) if entries[j]['origin'] in DM_ORIGINS - {33}]
        else:
            candidates = [j for j in regular if j > after]
        normal.append(candidates[0] if candidates else regular[0])
        target, flag = SECRET_LINKS.get(origin, (-1, 0))
        secret.append(positions.get(target, 65535))
        unlock.append(flag if target in positions else 0)
    return (normal, secret, unlock, regular[-1])

# Existing runtime capacities and data locations; these are not installers.
STOCK_LIMIT=657
SECTOR_LIMIT=1024
CAPACITY=300
SOUND_CONTROL=pool_SOUND_CONTROL
SOUND_CONTROL_SIZE=pool_SOUND_CONTROL_SIZE
RATE_ROM=0xA7000
VOICE_ROM=0xA7260
active=pool_active
put=pool_put
release=pool_release

def has_sector_patch(rom):return pool_active(rom)
def has_legacy_sound_imports(rom):return False

def sound_capacity_patched(rom):
    if rom[0xa7500:0xa7508]!=b'D64SFX01':return False
    count=struct.unpack_from('>H',rom,0xa7508)[0]
    return 277<=count<=300 and struct.unpack_from('>I',rom,0xd5d0)[0]&65535==count

def patch_sound_capacity(rom,count,rates=None):
    if not sound_capacity_patched(rom):raise ValueError('Sound insertion requires the existing expanded-sound runtime patch.')
    if not 277<=count<=300:raise ValueError('The sound limit is 300 IDs.')
    word=struct.unpack_from('>I',rom,0xd5d0)[0]
    struct.pack_into('>I',rom,0xd5d0,(word&0xffff0000)|count)
    struct.pack_into('>H',rom,0xa7508,count)
    for ident,rate in (rates or {}).items():
        if not 277<=ident<count or not 1000<=rate<=32767:raise ValueError('Invalid added sound rate or ID.')
        struct.pack_into('>h',rom,RATE_ROM+ident*2,rate)

def prepared_rom(path):
    rom=read_rom(path)
    if len(rom)<0x1000000:raise ValueError('Insertion requires a ROM of at least 16 MiB. No expansion will be performed.')
    if rom[0x3e:0x40]!=b'E\0':raise ValueError('Insertion currently supports USA ROMs only.')
    pool_read(rom)
    if level_layout(rom) is None or level_layout(rom)[0]!=pool_LEVELS:
        raise ValueError('The ROM needs an existing compatible level-directory patch.')
    if texture_table(rom)!=pool_TEXTURES:raise ValueError('The ROM needs an existing compatible texture-table patch.')
    return rom

def finish_insert(rom,size):
    if len(rom)!=size:raise ValueError('Insertion would change the ROM size; operation cancelled.')
    pool_read(rom)
    struct.pack_into('>2I',rom,0x10,*crc6102(rom))

def configure_halfword(rom,offset,old,new):
    word=struct.unpack_from('>I',rom,offset)[0]
    if word&65535!=old:raise ValueError(f'Unexpected existing runtime configuration at 0x{offset:X}.')
    struct.pack_into('>I',rom,offset,(word&0xffff0000)|new)

def level_entries(rom):
    base,count=level_layout(rom);titles=read_level_titles(rom)
    return [dict(origin=struct.unpack_from('>H',rom,base+0x20+i*2)[0],title=titles[i],
                 entry=bytes(rom[base+0x100+i*BOARD.size:base+0x100+(i+1)*BOARD.size])) for i in range(count)]

def insert_map(rom,data,slot=None,title=None):
    entries=level_entries(rom);old_count=len(entries)
    if slot is None:
        if old_count>=64:raise ValueError('The 64-level directory is full.')
        origin=next(i for i in range(64,65535) if i not in {e['origin'] for e in entries})
        entries.append(dict(entries[0],origin=origin,title=title or 'NEW LEVEL'));slot=old_count
    elif not 0<=slot<old_count:raise ValueError('Selected level slot does not exist.')
    elif title is not None:entries[slot]['title']=title
    if title is not None and (not title or len(title)>39 or not title.isascii() or any(ord(c)<32 for c in title)):
        raise ValueError('Level names must contain 1–39 printable ASCII characters.')
    _,arrays=parse_map(data,ignore_wall_limit=True,sector_limit=1024)
    links=links_for_map(struct.unpack_from('<I',data)[0],*arrays)
    if any(any(b>=0 for b in pair) for pair in links) and rom[0xb1700:0xb1708]!=b'D64TROR4':
        raise ValueError('This map requires the existing TROR runtime patch. This tool will not install it.')
    block,header,missing=convert_map(data,rom,ignore_wall_limit=True)
    entry=entries[slot];old=BOARD.unpack(entry['entry'])
    start=pool_put(rom,f"map:{entry['origin']}",block)
    entry['entry']=BOARD.pack(start,start+len(block),*header,*old[-6:])
    base=pool_LEVELS
    if len(entries)!=old_count:
        old_final=routes(entries[:-1])[3];normal,secret,unlock,final=routes(entries)
        configure_halfword(rom,0x6fd4c,old_count-1,len(entries)-1)
        configure_halfword(rom,0x6fd50,old_count,len(entries))
        for offset in (0x3cf8,0x3d04,0x3d18):configure_halfword(rom,offset,old_final,final)
        struct.pack_into('>H',rom,base+8,len(entries))
        for offset,values in ((0x2000,normal),(0x2080,secret),(0x2100,unlock)):
            struct.pack_into(f'>{len(values)}H',rom,base+offset,*values)
    struct.pack_into('>H',rom,base+0x20+slot*2,entry['origin'])
    rom[base+0x100+slot*BOARD.size:base+0x100+(slot+1)*BOARD.size]=entry['entry']
    name_offset=0x1500+slot*40
    rom[base+name_offset:base+name_offset+40]=entry['title'].encode('ascii').ljust(40,b'\0')
    struct.pack_into('>I',rom,base+0x1400+slot*4,0x80720000+name_offset)
    read_board(rom,slot)
    return slot,missing

def insert_tile(rom,ident,path):
    from PIL import Image
    if not 0<=ident<6144:raise ValueError('Tile IDs must be between 0 and 6143.')
    tiles={t.number:t for t in list_tiles(rom)};old=tiles.get(ident)
    with Image.open(path) as original:
        image=original.convert('RGBA');width,height=image.size
        image=pad_texture(image,old.indexed8 if old else False)
        raw=encode_replacement(rom,old,image) if old else encode_ci4(image)
    if raw.startswith(b'RNC'):raw=struct.pack('>4sI',b'RNC\0',len(raw))+raw
    if len(raw)>TILE_DMA_LIMIT:raise ValueError('Tile exceeds the native texture DMA size.')
    if old:record=old.record_offset
    else:
        record=next((pool_TEXTURES+i*TILE.size for i in range(STOCK_COUNT,TEXTURE_TABLE_COUNT)
                     if struct.unpack_from('>H',rom,pool_TEXTURES+i*TILE.size+12)[0]==0),None)
        if record is None:raise ValueError('The expanded tile table is full.')
    position=pool_put(rom,f'tile:{record}',raw)
    if old:
        struct.pack_into('>I',rom,record,position-ART_OFFSETS[ord('E')][1])
        struct.pack_into('>5H',rom,record+12,width,height,len(raw),image.width,image.height)
    else:TILE.pack_into(rom,record,position-ART_OFFSETS[ord('E')][1],0,0,width,height,len(raw),image.width,image.height,0,ident,0)
    return old is not None

def insert_sound(rom,audio,ref):
    if ref.suffix.lower()!='.ref':raise ValueError('Sound insertion requires a .ref sidecar.')
    info=json.loads(ref.read_text(encoding='utf-8-sig'))
    if not isinstance(info,dict) or type(info.get('id')) is not int or info['id']<0:raise ValueError('Invalid .ref sound ID.')
    label=info.get('name')
    if not isinstance(label,str) or not label.strip() or len(label)>128 or any(ord(c)<32 for c in label):raise ValueError('Invalid .ref sound name.')
    if info.get('audio') and str(info['audio']).casefold()!=audio.name.casefold():raise ValueError('The .ref audio filename does not match the input sound.')
    count=len(sound_info(rom)[4])
    if count>=300:raise ValueError('All 300 sound IDs are in use; sound insertion never replaces existing sounds.')
    revised=replace_sounds(rom,{},additions=[audio.read_bytes()])
    raw=pool_payload(revised,'sound_names');names=json.loads(raw) if raw else {}
    names[str(count)]=label;pool_put(revised,'sound_names',json.dumps(names,sort_keys=True,ensure_ascii=False).encode())
    return revised,count,label

def write_insert_output(path,data,overwrite):
    if path.exists() and not overwrite:raise ValueError(f'{path} exists; use --overwrite or another -o filename.')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
            temporary=Path(stream.name);stream.write(data)
        os.replace(temporary,path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def insertion(args):
    counts={'insert_level':2,'replace_level':3,'insert_tile':3,'insert_sound':3}
    if len(args.items)!=counts[args.kind]:raise ValueError('Insertion syntax: -il MAP ROM; -rl SLOT MAP ROM; -it ID PNG ROM; -is AUDIO REF ROM.')
    if args.all_assets:raise ValueError('-all is for extraction only.')
    if args.name is not None and args.kind not in ('insert_level','replace_level'):raise ValueError('--name is for levels only.')
    path=Path(args.items[-1]);rom=prepared_rom(path);size=len(rom)
    if args.kind in ('insert_level','replace_level'):
        map_path=Path(args.items[0] if args.kind=='insert_level' else args.items[1])
        title=args.name
        if args.kind=='insert_level' and title is None:title=map_path.stem.upper()[:39]
        slot,missing=insert_map(rom,map_path.read_bytes(),None if args.kind=='insert_level' else number(args.items[0]),title)
        message=f'Level slot {slot:02d}'
        if missing:print('Missing tiles (fallbacks used): '+str(dict(missing)),file=sys.stderr)
    elif args.kind=='insert_tile':
        ident=number(args.items[0]);replaced=insert_tile(rom,ident,Path(args.items[1]));message=f'Tile {ident} '+('replaced' if replaced else 'added')
    else:
        rom,ident,label=insert_sound(rom,Path(args.items[0]),Path(args.items[1]));message=f'Added sound {ident}: {label}'
    finish_insert(rom,size)
    output=args.output if args.output is not None else path
    in_place=output.resolve()==path.resolve()
    write_insert_output(output,rom,args.overwrite or in_place)
    print(message+f' -> {output.resolve()} ({size//1048576} MiB; unchanged size)')
    return 0

def main(argv=None):
    parser=argparse.ArgumentParser(prog='duke64patcher.py',description=
        'Extract assets, list N64 levels, or insert assets into an already prepared N64 ROM.',
        epilog='Examples:\n  python duke64patcher.py -l duke64.z64\n  python duke64patcher.py -el 00 duke64.z64\n'
               '  python duke64patcher.py -et 1312 DUKE3D.GRP\n'
               '  python duke64patcher.py -es 277 DUKE3D.GRP\n'
               '  python duke64patcher.py -el -all DUKE3D.GRP\n'
               'Level IDs are zero-based; GRP maps use natural filename order (E1L1, E1L2, ...).\n'
               'Tiles use tile IDs; sounds use CON definesound IDs. N64 sounds are unsupported.\n'
               'Insertion: -il MAP ROM; -rl SLOT MAP ROM; -it ID PNG ROM; -is AUDIO REF ROM.\n'
               'Insertion never expands ROMs or installs runtime code. Insertion updates the input ROM by default; use -o to save separately. Back up your ROM first.\n'
               'PNG operations require Pillow; MAP insertion requires numpy and mapbox-earcut.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-l',dest='kind',action='store_const',const='list',help='list N64 level slots and names only')
    group.add_argument('-el',dest='kind',action='store_const',const='levels',help='extract level(s) as MAP')
    group.add_argument('-et',dest='kind',action='store_const',const='tiles',help='extract tile(s) as PNG')
    group.add_argument('-es',dest='kind',action='store_const',const='sounds',help='extract GRP sound(s) as original VOC')
    for flag,kind in (('-il','insert_level'),('-rl','replace_level'),('-it','insert_tile'),('-is','insert_sound')):
        group.add_argument(flag,dest='kind',action='store_const',const=kind,help=kind.replace('_',' ')+' (prepared N64 ROM only)')
    parser.add_argument('-a','-all','--all',dest='all_assets',action='store_true',help='extract all assets of the chosen type')
    parser.add_argument('items',nargs='+',help='ID and source for extraction; asset file(s) and N64 ROM for insertion')
    parser.add_argument('--name',help='level title when inserting or replacing a level')
    parser.add_argument('-o','--output',type=Path,help='extraction directory, or separate output ROM filename (insertion defaults to input ROM)')
    parser.add_argument('--overwrite',action='store_true',help='allow replacing existing output files')
    args=parser.parse_args(argv)
    if args.kind.startswith(('insert_','replace_')):
        try:return insertion(args)
        except (OSError,ValueError,struct.error,UnicodeError,IndexError,KeyError,ImportError) as error:
            print(f'Error: {error}',file=sys.stderr);return 1
    if len(args.items) not in (1,2):parser.error('Supply an ID and source, or -all and source.')
    args.source=Path(args.items[-1]);args.id=args.items[0] if len(args.items)==2 else None
    if args.name is not None:parser.error('--name is for level insertion/replacement only.')
    if args.kind=='list' and (args.id is not None or args.all_assets or args.output is not None or args.overwrite):
        parser.error('-l takes only an N64 ROM path; extraction arguments do not apply.')
    if args.all_assets and args.id is not None:parser.error('Use an ID or -all, not both.')
    all_assets=args.all_assets or (args.id is not None and args.id.lower()=='all')
    if args.kind!='list' and not all_assets and args.id is None:parser.error('Supply an ID or -all; nothing is extracted by default.')
    try:
        with args.source.open('rb') as stream:magic=stream.read(12)
        if args.kind=='list':
            if magic==b'KenSilverman':raise ValueError('-l lists levels in N64 ROMs only; GRP files are unsupported.')
            rom=read_rom(args.source)
            for slot in range(level_count(rom)):print(f'{slot:02d}  {level_name(rom,slot)}')
            return 0
        source=GRPSource(args.source) if magic==b'KenSilverman' else N64Source(args.source)
        assets=source.assets(args.kind)
        if not assets:raise ValueError(f'No extractable {args.kind} were found.')
        if all_assets:chosen=list(assets)
        else:
            ident=number(args.id)
            if ident not in assets:raise ValueError(f'{args.kind.capitalize()} ID {ident} is absent, empty, or unavailable in this source.')
            chosen=[ident]
        output=args.output or args.source.parent/(args.source.stem+'_extracted')/args.kind
        targets=[output/assets[i] for i in chosen]
        if any(p.resolve()==args.source.resolve() for p in targets):
            raise ValueError('The output would overwrite the input file. Choose another -o directory.')
        if not args.overwrite:
            existing=next((p for p in targets if p.exists()),None)
            if existing:raise ValueError(f'{existing} already exists; use --overwrite or another -o directory.')
        if args.kind=='tiles':
            try:import PIL
            except ImportError as error:raise ValueError('PNG extraction requires Pillow. Run: python -m pip install Pillow') from error
        output.mkdir(parents=True,exist_ok=True)
        for ident,target in zip(chosen,targets):
            data=source.extract(args.kind,ident)
            with target.open('wb' if args.overwrite else 'xb') as stream:stream.write(data)
        print(f'Extracted {len(chosen)} {args.kind} to {output.resolve()}')
        if len(chosen)==1:print(targets[0].name)
        return 0
    except (OSError,ValueError,struct.error,UnicodeError,IndexError,KeyError) as error:
        print(f'Error: {error}',file=sys.stderr);return 1


if __name__=='__main__':
    raise SystemExit(main())

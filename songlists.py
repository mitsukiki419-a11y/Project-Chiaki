"""Song lists of the discs that are not Megapro KARA* discs, for the emulator's songbook.

K55 (Ace MIDI DVD K55 / "Standard Midi Karaoke" discs): /idx/MEGIDX (also /PLF/idx/MEGIDX), plain.
  u32 header words; W[13] = base of the offsets below:
    W[3]  artists: records 05 len b b b name           (in artist-number order)
    W[4]  songs:   records 01 len b lang u32 who u32 at title
                   who: bits 0-11 = singer (artist number, 0xFFF = none)
    W[12] number table: per song record, 4 bytes [9, n >> 16, n & 0xFF, (n >> 8) & 0xFF]
  Checked: record 4871 = 123 "Knocking On Heaven's Door", singer 770 = Bob Dylan (as the player shows it).

KS5 (Platinum KS5 discs): /IDX/5pidx has the same layout but is encrypted and packed, and the player unpacks it
  at boot. The list is read from the emulated player's memory (header 0x8000AC90: +16 song records,
  [0x8000ACC0] number table, [0x8000ACAE] count) once it has booted, and kept in a cache file for later.
  Titles only: the singer links are not decoded yet.
"""
import json, os, struct

LANG = {1: 'Chinese', 2: 'English', 3: 'Vietnamese'}


def _text(raw, lang=None):
    raw = raw.rstrip(b'\x00').strip()
    if all(c < 0x80 for c in raw): return raw.decode('ascii')
    if lang == 1:
        try: return raw.decode('gbk')
        except UnicodeDecodeError: pass
    return raw.decode('cp1252', 'replace')


class Book:
    """The same listing() as songdisc.KaraokeDisc, for a read-only list of dict(number, title, artist)."""
    def __init__(self, songs, kind):
        self.kind = kind
        self.songs = sorted(songs, key=lambda s: s['number'])

    def listing(self, query='', start=0, n=100, show_deleted=True, only=None):
        q = (query or '').strip().lower()
        allsongs = [] if only == 'mv' else self.songs
        if q:
            allsongs = [s for s in allsongs if q in ('%06d' % s['number']) or q in s['title'].lower() or q in s['artist'].lower()]
        page = [dict(number=s['number'], title=s['title'], artist=s['artist'], mv=False, chorus=False)
                for s in allsongs[start:start + n]]
        return dict(total=len(allsongs), start=start, songs=page, disc_songs=len(self.songs), added=0, deleted=0,
                    mvs=0, has_mvv=False)

    def find(self, number):
        for s in self.songs:
            if s['number'] == number: return s
        return None


# ------------------------------------------------------------------------------------------------- K55
def parse_megidx(b):
    if len(b) < 64: raise ValueError('MEGIDX too short')
    W = struct.unpack_from('<16I', b, 0); base = W[13]
    if not (0 < base < len(b) and base + W[12] <= len(b)): raise ValueError('not a MEGIDX song list')
    arts = []; p = base + W[3]
    while p + 5 <= len(b) and b[p] == 5 and b[p + 1] >= 5:
        L = b[p + 1]; arts.append(_text(b[p + 5:p + L])); p += L
    recs = []; p = base + W[4]
    while p + 12 <= len(b) and b[p] == 1 and b[p + 1] >= 12:
        L = b[p + 1]; recs.append(b[p:p + L]); p += L
    nt = base + W[12]
    songs = []
    for k, r in enumerate(recs):
        e = b[nt + 4 * k:nt + 4 * k + 4]
        if len(e) < 4: break
        num = (e[1] << 16) | e[2] | (e[3] << 8)
        who = struct.unpack_from('<I', r, 4)[0] & 0xFFF
        songs.append(dict(number=num, title=_text(r[12:], r[3] & 0x7F), artist=arts[who] if who < len(arts) else ''))
    if not songs: raise ValueError('no songs in MEGIDX')
    return songs


def k55_book(path):
    """The song list of a K55 disc (image or folder), or None when the disc has no MEGIDX."""
    for p in ('/idx/MEGIDX', '/PLF/idx/MEGIDX', '/IDX/MEGIDX'):
        try:
            if os.path.isdir(path):
                f = os.path.join(path, *[x for x in p.split('/') if x])
                if not os.path.isfile(f): continue
                data = open(f, 'rb').read()
            else:
                import isoread
                d = isoread.Disc(path)
                try:
                    try: d.lookup(p)
                    except Exception: continue
                    data = d.read(p)
                finally:
                    try: d.close()
                    except Exception: pass
            return Book(parse_megidx(data), 'k55')
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------------------------------------- KS5
def ks5_from_ram(e):
    """The song list from the booted KS5's memory (see the module notes), or None if it is not there yet."""
    rd32 = lambda a: e.rd32(a); rd16 = lambda a: e.rd16(a)
    lo = e.low
    A = lambda a: a & 0x1FFFFFFF
    hdr = 0x8000AC90
    recs_at = rd32(hdr + 16); tbl = rd32(0x8000ACC0); cnt = rd16(0x8000ACAE)
    if not (0x80000000 <= recs_at < 0x84000000 and 0x80000000 <= tbl < 0x84000000 and 0 < cnt < 200000): return None
    p = A(recs_at); songs = []
    for k in range(cnt):
        if lo[p] != 1 or lo[p + 1] < 12: break
        L = lo[p + 1]; r = bytes(lo[p:p + L]); p += L
        t = A(tbl) + 4 * k; ent = bytes(lo[t:t + 4])
        t_ = r[3] & 0x7F; h = 18 if r[3] & 0x80 else 12      # type bit 7: an MP3 song, 6 more header bytes
        songs.append(dict(number=(ent[1] << 16) | ent[2] | (ent[3] << 8), title=_text(r[h:], t_), artist='',
                          lang=LANG.get(t_, '')))
    return songs if len(songs) == cnt else None


def ks5_cache_path(folder, disc_path):
    return os.path.join(folder, os.path.splitext(os.path.basename(disc_path))[0] + '.ks5songs.json')


def ks5_book(disc_path, stamp, folder, emu=None):
    """KS5 songbook: from the running player when it has booted (written to the cache), else from the cache."""
    cache = ks5_cache_path(folder, disc_path) if folder else None
    if emu is not None:
        try: songs = ks5_from_ram(emu)
        except Exception: songs = None
        if songs:
            if cache:
                try:
                    with open(cache, 'w', encoding='utf-8') as f: json.dump(dict(stamp=list(stamp or ()), songs=songs), f)
                except OSError: pass
            return Book(songs, 'ks5')
    if cache and os.path.isfile(cache):
        try:
            d = json.load(open(cache, encoding='utf-8'))
            if list(stamp or ()) == d.get('stamp'): return Book(d['songs'], 'ks5')
        except Exception: pass
    return None

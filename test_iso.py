#!/usr/bin/env python3
"""Self-contained tests for isoread.py. Builds disc images from scratch (no external ISO tools)."""
import os, sys, struct, subprocess, tempfile, shutil, hashlib, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import isoread

S = 2048


def pad(b, n=S): return b + b'\x00' * (-len(b) % n)


# ---------------------------------------------------------------- ISO9660 writer (test only)
def both32(v): return struct.pack('<I', v) + struct.pack('>I', v)
def both16(v): return struct.pack('<H', v) + struct.pack('>H', v)


def dirrec(name, lba, size, is_dir, last=True):
    nb = name if isinstance(name, bytes) else name.encode('latin-1')
    l = 33 + len(nb); l += l % 2
    r = bytearray(l)
    r[0] = l; r[2:10] = both32(lba); r[10:18] = both32(size)
    r[18:25] = bytes([126, 1, 2, 3, 4, 5, 0])
    r[25] = (2 if is_dir else 0) | (0 if last else 0x80)
    r[28:32] = both16(1); r[32] = len(nb); r[33:33 + len(nb)] = nb
    return bytes(r)


def build_iso9660(tree, volid='TESTDISC', joliet=False):
    """tree: {'DIR': {...}, 'FILE.EXT': bytes}. Returns image bytes (2048-byte sectors)."""
    # assign sectors: 16 PVD, 17 (SVD if joliet), 18 terminator, then dirs, then files
    dirs = []
    def collect(t, path):
        dirs.append((path, t))
        for n, v in t.items():
            if isinstance(v, dict): collect(v, path + (n,))
    collect(tree, ())
    nvd = 3 + (1 if joliet else 0)
    next_lba = 16 + nvd
    dir_lba, dir_size = {}, {}
    def recs_for(path, t, lba_of, parent_lba, self_lba, enc):
        out = [dirrec(b'\x00', self_lba, 2048, True), dirrec(b'\x01', parent_lba, 2048, True)]
        for n, v in sorted(t.items()):
            if isinstance(v, dict):
                out.append(dirrec(enc(n), lba_of[path + (n,)], 2048, True))
            else:
                for i, (lba, size, last) in enumerate(lba_of[path + (n,)]):
                    out.append(dirrec(enc(n), lba, size, False, last))
        return out
    for p, t in dirs:
        dir_lba[p] = next_lba; next_lba += 1
        if joliet: dir_lba[('J',) + p] = next_lba; next_lba += 1
    file_lba = {}
    for p, t in dirs:
        for n, v in sorted(t.items()):
            if not isinstance(v, dict):
                nsec = (len(v) + S - 1) // S
                ext, off = [], 0
                # split into <=1 GiB-ish extents (use tiny limit for large test files to exercise multi-extent)
                limit = 64 * S if len(v) > 100 * S else 1 << 30
                while off < len(v):
                    chunk = min(len(v) - off, limit)
                    ext.append((next_lba, chunk, off + chunk >= len(v)))
                    next_lba += (chunk + S - 1) // S; off += chunk
                file_lba[p + (n,)] = ext
    total = next_lba
    img = bytearray(total * S)
    lba_of = {}
    for p, t in dirs:
        for n, v in t.items():
            lba_of[p + (n,)] = dir_lba[p + (n,)] if isinstance(v, dict) else file_lba[p + (n,)]
    jlba_of = {k: v for k, v in lba_of.items()}
    if joliet:
        for p, t in dirs:
            for n, v in t.items():
                if isinstance(v, dict): jlba_of[p + (n,)] = dir_lba[('J',) + p + (n,)]
    def put_dir(path, t, base, parent_base, lba_map, enc):
        recs = recs_for(path, t, lba_map, parent_base, base, enc)
        data = b''; sect = b''
        for r in recs:
            if len(sect) + len(r) > S: data += pad(sect); sect = b''
            sect += r
        data += pad(sect)
        assert len(data) == S, 'test writer: directory too big for one sector'
        img[base * S: base * S + S] = data
    for p, t in dirs:
        parent = dir_lba[p[:-1]] if p else dir_lba[p]
        put_dir(p, t, dir_lba[p], parent, lba_of, lambda n: (n + ';1').encode('latin-1') if '.' in n else n.encode('latin-1'))
        if joliet:
            jparent = dir_lba[('J',) + p[:-1]] if p else dir_lba[('J',) + p]
            put_dir(p, t, dir_lba[('J',) + p], jparent, jlba_of, lambda n: n.encode('utf-16-be'))
    for p, ext in file_lba.items():
        v = None
        t = tree
        for k in p[:-1]: t = t[k]
        v = t[p[-1]]
        off = 0
        for lba, size, last in ext:
            img[lba * S: lba * S + size] = v[off:off + size]; off += size
    def pvd(t, rootlba, esc=None):
        d = bytearray(S)
        d[0] = t; d[1:6] = b'CD001'; d[6] = 1
        idb = volid.encode('latin-1' if esc is None else 'utf-16-be')
        d[8:40] = b' ' * 32; d[40:72] = idb.ljust(32, b' ' if esc is None else b'\x00')[:32]
        d[80:88] = both32(total); d[120:124] = both16(1); d[124:128] = both16(1); d[128:132] = both16(2048)
        if esc: d[88:91] = esc
        d[156:190] = dirrec(b'\x00', rootlba, 2048, True)[:34].ljust(34, b'\x00')
        d[881] = 1
        return bytes(d)
    img[16 * S:17 * S] = pvd(1, dir_lba[()])
    k = 17
    if joliet: img[k * S:(k + 1) * S] = pvd(2, dir_lba[('J',)], b'%/E'); k += 1
    term = bytearray(S); term[0] = 255; term[1:6] = b'CD001'; term[6] = 1
    img[k * S:(k + 1) * S] = term
    return bytes(img)


# ---------------------------------------------------------------- UDF writer (test only, single partition, short ADs)
def tag(ident, sector, body_len=0):
    t = bytearray(16); struct.pack_into('<H', t, 0, ident); struct.pack_into('<H', t, 2, 3); struct.pack_into('<I', t, 12, sector)
    return t


def build_udf(tree, part_start=300):
    img = {}
    def setsec(n, data): img[n] = bytes(data).ljust(S, b'\x00')
    setsec(16, b'\x00BEA01\x01'); setsec(17, b'\x00NSR02\x01'); setsec(18, b'\x00TEA01\x01')
    avdp = bytearray(S); avdp[:16] = tag(2, 256); struct.pack_into('<II', avdp, 16, 3 * S, 32); setsec(256, avdp)
    pd = bytearray(S); pd[:16] = tag(5, 32); struct.pack_into('<H', pd, 22, 0)
    nxt = [part_start + 2]                               # lbn 0: FSD, 1: root FE
    def alloc(nsec):
        r = nxt[0] - part_start; nxt[0] += nsec; return r
    lvd = bytearray(S); lvd[:16] = tag(6, 33); struct.pack_into('<I', lvd, 212, 2048)
    struct.pack_into('<IIH', lvd, 248, 2048, 0, 0)
    td = bytearray(S); td[:16] = tag(8, 34)
    def fe_bytes(lbn, is_dir, size, ads):
        fe = bytearray(S); fe[:16] = tag(261, part_start + lbn)
        fe[27] = 4 if is_dir else 5
        struct.pack_into('<H', fe, 34, 0)                         # short ADs
        struct.pack_into('<Q', fe, 56, size)
        struct.pack_into('<II', fe, 168, 0, len(ads) * 8)
        for i, (ln, pos) in enumerate(ads): struct.pack_into('<II', fe, 176 + 8 * i, ln, pos)
        return fe
    def write_dir(t, lbn, parent_lbn):
        fids = bytearray()
        # parent FID
        def fid(name, chars, icb_lbn):
            nb = b'' if name is None else b'\x08' + name.encode('latin-1')
            f = bytearray(38 + len(nb)); f[:16] = tag(257, 0)
            struct.pack_into('<H', f, 16, 1); f[18] = chars; f[19] = len(nb)
            struct.pack_into('<IIH', f, 20, 2048, icb_lbn, 0)
            f[38:] = nb
            return bytes(f).ljust((len(f) + 3) & ~3, b'\x00')
        fids += fid(None, 0x0A, parent_lbn)
        kids = {}
        for n, v in sorted(t.items()):
            if isinstance(v, dict):
                klbn = alloc(1); kids[n] = klbn
                write_dir(v, klbn, lbn)
                fids += fid(n, 0x02, klbn)
            else:
                nsec = max(1, (len(v) + S - 1) // S)
                dlbn = alloc(nsec)
                for i in range(nsec): img[part_start + dlbn + i] = v[i * S:(i + 1) * S].ljust(S, b'\x00')
                flbn = alloc(1)
                ads = [(len(v), dlbn)] if v else []
                img[part_start + flbn] = bytes(fe_bytes(flbn, False, len(v), ads))
                fids += fid(n, 0x00, flbn)
        dl = alloc(max(1, (len(fids) + S - 1) // S))
        for i in range(0, max(1, (len(fids) + S - 1) // S)): img[part_start + dl + i] = bytes(fids[i * S:(i + 1) * S]).ljust(S, b'\x00')
        img[part_start + lbn] = bytes(fe_bytes(lbn, True, len(fids), [(len(fids), dl)]))
    write_dir(tree, 1, 1)
    nsec_total = nxt[0]
    struct.pack_into('<I', pd, 188, part_start); struct.pack_into('<I', pd, 192, nsec_total - part_start)
    setsec(32, pd); setsec(33, lvd); setsec(34, td)
    fsd = bytearray(S); fsd[:16] = tag(256, part_start); struct.pack_into('<IIH', fsd, 400, 2048, 1, 0)
    img[part_start] = bytes(fsd)
    out = bytearray(max(img) * S + S)
    for k, v in img.items(): out[k * S:(k + 1) * S] = v
    return bytes(out)


# ---------------------------------------------------------------- raw wrappers
def wrap_raw_xa(iso, form2_ranges=(), form2_payloads=None):
    """Wrap 2048-byte sectors as CD-ROM XA Mode2 2352-byte sectors; Form2 sectors carry 2324-byte payloads."""
    out = bytearray(); n = len(iso) // S
    for i in range(n):
        sec = bytearray(2352); sec[0] = 0; sec[1:11] = b'\xff' * 10; sec[11] = 0
        sec[12:15] = bytes([0, 2, i % 75]); sec[15] = 2
        f2 = any(a <= i < b for a, b in form2_ranges)
        sec[16:24] = bytes([1, 1, 0x20 if f2 else 0x08, 0, 1, 1, 0x20 if f2 else 0x08, 0])
        if f2: sec[24:24 + 2324] = form2_payloads[i]
        else: sec[24:24 + 2048] = iso[i * S:(i + 1) * S]
        out += sec
    return bytes(out)


# ---------------------------------------------------------------- fixtures
def make_vob(path):
    ff = isoread.find_ffmpeg()
    if not ff: return None
    r = subprocess.run([ff, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=720x480:rate=30000/1001',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2', '-c:v', 'mpeg2video',
                        '-b:v', '3M', '-c:a', 'ac3', '-f', 'vob', path], capture_output=True)
    return path if r.returncode == 0 and os.path.exists(path) else None


def bcd(n): return ((n // 10) << 4) | (n % 10)


def fake_ifos():
    vmg = bytearray(4 * S); vmg[:12] = b'DVDVIDEO-VMG'; struct.pack_into('>H', vmg, 0x3E, 1)
    struct.pack_into('>I', vmg, 0xC4, 1); struct.pack_into('>H', vmg, S, 1)
    vts = bytearray(4 * S); vts[:12] = b'DVDVIDEO-VTS'; struct.pack_into('>I', vts, 0xCC, 1)
    base = S; struct.pack_into('>H', vts, base, 1)
    struct.pack_into('>II', vts, base + 8, 0, 0x100)            # category, offset
    pgc = base + 0x100; vts[pgc + 4:pgc + 8] = bytes([bcd(1), bcd(23), bcd(45), 0xC0])
    return bytes(vmg), bytes(vts)


PASS, FAIL = [], []
def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('  PASS  ' if cond else '  FAIL  ') + name + (('   ' + str(extra)) if (extra and not cond) else ''))


def exercise(label, imgbytes, tree, tmp, raw_xa=False, expect_kind=None):
    print(f'[{label}]')
    p = os.path.join(tmp, label.replace(' ', '_') + '.iso'); open(p, 'wb').write(imgbytes)
    d = isoread.Disc(p)
    check(f'{label}: filesystem detected', expect_kind is None or d.kind.startswith(expect_kind), d.kind)
    names = {pp.upper(): n for pp, n in d.walk()}
    check(f'{label}: directory walk finds all files', '/VIDEO_TS/VTS_01_1.VOB' in names and '/README.TXT' in names, list(names))
    for path in ('/README.TXT', '/VIDEO_TS/VTS_01_0.IFO'):
        want = tree['README.TXT'] if path == '/README.TXT' else tree['VIDEO_TS']['VTS_01_0.IFO']
        check(f'{label}: byte-exact read {path}', d.read(path) == want)
    check(f'{label}: case-insensitive lookup', d.lookup('/video_ts/vts_01_1.vob').size == len(tree['VIDEO_TS']['VTS_01_1.VOB']))
    vob = tree['VIDEO_TS']['VTS_01_1.VOB']
    check(f'{label}: large (multi-extent) file hash', hashlib.md5(d.read('/VIDEO_TS/VTS_01_1.VOB')).digest() == hashlib.md5(vob).digest())
    mid = len(vob) // 2 + 123
    check(f'{label}: ranged read', d.read('/VIDEO_TS/VTS_01_1.VOB', mid, 5000) == vob[mid:mid + 5000])
    info = d.identify()
    check(f'{label}: identified as DVD-Video', info.get('type') == 'DVD-Video', info)
    check(f'{label}: IFO parsed (titles/title sets)', info.get('titles') == 1 and info.get('title_sets') == 1, info)
    check(f'{label}: PGC playback time parsed', info.get('vts') and info['vts'][0].get('pgc_play_times') == ['1:23:45'], info.get('vts'))
    out = os.path.join(tmp, 'x.vob'); d.extract('/VIDEO_TS/VTS_01_1.VOB', out)
    check(f'{label}: extract to file', open(out, 'rb').read() == vob)
    if isoread.find_ffmpeg() and vob[:4] == b'\x00\x00\x01\xba':
        png = os.path.join(tmp, label.replace(' ', '_') + '.png')
        try:
            used = d.thumbnail(png); check(f'{label}: thumbnail decoded via ffmpeg ({used})', os.path.getsize(png) > 1000)
        except Exception as e:
            check(f'{label}: thumbnail', False, e)
    d.close()


def main():
    tmp = tempfile.mkdtemp()
    try:
        vobp = make_vob(os.path.join(tmp, 'v.vob'))
        vob = open(vobp, 'rb').read() if vobp else os.urandom(300 * 1024)
        print('test VOB:', 'ffmpeg-generated' if vobp else 'random bytes (no ffmpeg)', len(vob), 'bytes')
        vmg, vts = fake_ifos()
        tree = {'README.TXT': b'hello karaoke\n' * 50,
                'VIDEO_TS': {'VIDEO_TS.IFO': vmg, 'VTS_01_0.IFO': vts, 'VTS_01_1.VOB': vob}}
        exercise('ISO9660', build_iso9660(tree), tree, tmp, expect_kind='ISO9660')
        exercise('ISO9660 Joliet', build_iso9660(tree, joliet=True), tree, tmp, expect_kind='ISO9660+Joliet')
        exercise('UDF only', build_udf(tree), tree, tmp, expect_kind='UDF')
        iso = build_iso9660(tree)
        exercise('Raw XA 2352', wrap_raw_xa(iso), tree, tmp, expect_kind='ISO9660')
        # VCD-style Form2 payload read
        print('[VCD form2]')
        vt = {'MPEGAV': {'AVSEQ01.DAT': b'\x00' * (4 * S)}, 'VCD': {'INFO.VCD': b'VIDEO_CD'}}
        viso = build_iso9660(vt)
        d0 = isoread.SectorSource  # find extent of the DAT in the built image
        tmp_iso = os.path.join(tmp, 'tmp.iso'); open(tmp_iso, 'wb').write(viso)
        probe = isoread.Disc(tmp_iso); lba, ln = probe.lookup('/MPEGAV/AVSEQ01.DAT').extents[0]; probe.close()
        payloads = {i: bytes([(i * 7 + j) & 0xFF for j in range(2324)]) for i in range(lba, lba + 4)}
        raw = wrap_raw_xa(viso, [(lba, lba + 4)], payloads)
        rp = os.path.join(tmp, 'vcd.bin'); open(rp, 'wb').write(raw)
        dv = isoread.Disc(rp)
        info = dv.identify()
        check('VCD: identified as Video CD', info['type'] == 'Video CD' and info.get('raw_sector_image') is True, info)
        got = dv.read('/MPEGAV/AVSEQ01.DAT', raw_xa=True)
        want = b''.join(payloads[i] for i in range(lba, lba + 4))
        check('VCD: Form2 payloads (2324 B/sector) read back exactly', got == want, (len(got), len(want)))
        dv.close()
        # data disc classification
        print('[Data disc]')
        dt = {'SONGS': {'a.mp3': b'ID3' + b'\x00' * 100, 'b.kar': b'MThd'}, 'x.cdg': b'\x00' * 300}
        dp = os.path.join(tmp, 'data.iso'); open(dp, 'wb').write(build_iso9660(dt))
        di = isoread.Disc(dp).identify()
        check('Data disc: audio + karaoke data recognised', di['type'] == 'data disc' and di['contains'] and 'audio files' in di['contains'] and any('karaoke' in c for c in di['contains']), di)
        # error handling
        print('[Errors]')
        bad = os.path.join(tmp, 'bad.iso'); open(bad, 'wb').write(os.urandom(100000))
        try: isoread.Disc(bad); check('garbage file rejected', False)
        except isoread.DiscError: check('garbage file rejected cleanly', True)
        d = isoread.Disc(p := os.path.join(tmp, 'ISO9660.iso'))
        try: d.lookup('/NOPE'); check('missing path raises', False)
        except isoread.DiscError: check('missing path raises DiscError', True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%d passed, %d failed' % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

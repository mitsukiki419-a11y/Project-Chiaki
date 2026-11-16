#!/usr/bin/env python3
"""Project : Chiaki (formerly KaraokeEmu) - Sunplus karaoke-DVD firmware emulator (MP100NSP.BIN) with disc-image reader.

  "Project Chiaki"                -> opens the browser UI (http://127.0.0.1:8765)
  "Project Chiaki" gui [--fw FILE] [--iso FILE] [--port N] [--no-browser] [--browser]
  "Project Chiaki" boot [--fw FILE] [--out DIR] [--budget N]     headless: boot firmware, save what it displays
  "Project Chiaki" iso DISC.iso info|ls [path]|tree|extract <path> <dest>|thumb <out.png>
  "Project Chiaki" version

Scope: the firmware boots, reads a karaoke disc through a virtual drive, draws its OSD, takes remote-control keys
(song numbers, Enter), streams the background video and plays songs (its MIDI output is captured). The CPU runs in
a C core (kecpu.dll / libkecpu.so, ~100-150 M instructions/s) and the player is held to real time; when the
firmware starts a song the program plays that song with the SoundFont, lined up with the firmware's song clock.
"""
import os, re, sys, json, time, struct, threading, secrets, shutil, subprocess, argparse, webbrowser, traceback, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The CPU core is C, but it calls back into Python for every hooked register and address, and each callback needs
# the interpreter lock. With Python's default 5 ms switch interval, any other busy thread (picture encoding, the
# web server, the songbook) made each callback wait up to 5 ms: the emulation dropped to a fraction of real time
# and the music dragged. A short interval hands the lock back quickly (measured: 0.03x -> 0.4x of full speed
# while another thread is busy, no change when idle).
sys.setswitchinterval(0.0001)

VERSION = '1.0.0'
FROZEN = getattr(sys, 'frozen', False)
# Android (the app embeds Python with Chaquopy): the program's own files are inside the app; the user's folders
# (BIOS, Discs, Soundfont, BGV, Saved Settings, Logs) are in KE_HOME, a folder on the phone's storage
ANDROID = hasattr(sys, 'getandroidapilevel') or 'ANDROID_ROOT' in os.environ
APPDIR = os.environ.get('KE_HOME') or (os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import descramble, isoread

if sys.platform == 'win32':
    # The app has no console of its own (built --windowed), so every ffmpeg Windows starts would get a console
    # window of its own: a black box flashing up at boot, on each still picture and each song. No helper program
    # ever needs one, so all of them start without (also the ones in isoread / songdisc that do not ask).
    _Popen_init = subprocess.Popen.__init__
    def _popen_no_window(self, *a, **kw):
        kw['creationflags'] = kw.get('creationflags', 0) | 0x08000000      # CREATE_NO_WINDOW
        return _Popen_init(self, *a, **kw)
    subprocess.Popen.__init__ = _popen_no_window
from emu import Emu, Stop
import soc, run3, snapshot, midiout
try:
    import songbank
except Exception as _ex:                               # numpy missing: songs/SoundFont playback unavailable
    songbank = None; SONG_IMPORT_ERROR = str(_ex)

def _I(n): return int(n * soc.IPS / 108e6)       # an instruction count written for 108 MHz, at the current clock
INSTR_PER_SEC = soc.IPS        # emulated CPU speed (108 MHz from the firmware's own 1 ms delay loop; KE_IPUS lowers it)


# ------------------------------------------------------------------------------------ folders
# Next to the program:  BIOS\ (player firmware files), Soundfont\ (.sf2), Discs\ (disc images, or any folder chosen
# in the page), Saved Settings\ (settings, the player's EEPROM, quick-boot states), Logs\ (KaraokeEmu.log).
FOLDERS = dict(bios='BIOS', soundfont='Soundfont', discs='Discs', saved='Saved Settings', logs='Logs', bgv='BGV')
# Override BGV: video files from a folder (BGV next to the program, or one chosen in Settings) played instead of
# the disc's background videos
BGV_EXT = ('.mp4', '.m4v', '.mkv', '.avi', '.mov', '.wmv', '.webm', '.flv', '.mpg', '.mpeg', '.vob', '.ts', '.m2ts', '.dat', '.3gp')


def app_folder(kind, create=True):
    d = os.path.join(APPDIR, FOLDERS[kind])
    if create:
        try: os.makedirs(d, exist_ok=True)
        except OSError: pass
    return d


def make_folders():
    """Create the folders and move files from older versions (kept next to the program) into them."""
    for k in FOLDERS: app_folder(k)
    moves = [('KaraokeEmu.settings.json', 'saved'), ('KaraokeEmu.eeprom.json', 'saved'), ('KaraokeEmu.log', 'logs')]
    try: moves += [(f, 'saved') for f in os.listdir(APPDIR) if f.lower().endswith('.quickboot')]
    except OSError: pass
    for name, kind in moves:
        src = os.path.join(APPDIR, name); dst = os.path.join(app_folder(kind), name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try: os.replace(src, dst)
            except OSError: pass


def disp_path(p):
    """A path as the page shows it: relative to the program's folder when inside it (BIOS\\MP100NSP.BIN), else with
    the user's home folder written as ~, so the Windows user name does not show on screen or in screenshots."""
    if not p: return ''
    a = os.path.abspath(p)
    try:
        r = os.path.relpath(a, APPDIR)
        if not r.startswith('..') and not os.path.isabs(r): return r
    except ValueError:
        pass
    h = os.path.expanduser('~')
    if h and os.path.normcase(a).startswith(os.path.normcase(h) + os.sep): return '~' + a[len(h):]
    return a


def real_path(p):
    """The other way round: ~ and paths relative to the program's folder (as shown in the page) become full paths."""
    p = (p or '').strip().strip('"')
    if not p: return p
    p = os.path.expanduser(p)
    if not os.path.isabs(p): p = os.path.join(APPDIR, p)
    return os.path.abspath(p)


def log_path():
    d = app_folder('logs')
    return os.path.join(d, 'KaraokeEmu.log') if os.path.isdir(d) else None


def _synth_log():
    try:
        import livesynth; livesynth.LOG_PATH = log_path()
    except Exception: pass


FW_EXT = ('.bin', '.rom', '.img')
DISC_EXT = ('.iso', '.sample', '.img', '.nrg', '.mdf', '.bin')


def _scan(dirs, exts, limit=400):
    out = []; seen = set()
    for d in dirs:
        try: names = sorted(os.listdir(d), key=str.lower)
        except OSError: continue
        for n in names:
            p = os.path.join(d, n)
            if n.lower().endswith(exts) and os.path.isfile(p) and os.path.normcase(os.path.abspath(p)) not in seen:
                seen.add(os.path.normcase(os.path.abspath(p))); out.append(p)
                if len(out) >= limit: return out
    return out


_FW_INFO = {}
def firmware_info(path):
    """Which player a firmware file is for (cached by path, size and time)."""
    try: st = os.stat(path)
    except OSError: return None
    key = (path, st.st_size, st.st_mtime)
    if key in _FW_INFO: return _FW_INFO[key]
    info = None
    try:
        raw = open(path, 'rb').read(0x400)
        dec = raw if b'SUNPLUS' in raw else descramble.xform(raw)
        if b'SUNPLUS' in dec:
            import fwprofile
            fid = fwprofile.detect(dec)
            info = dict(id=fid, player=fwprofile.NAMES.get(fid, fid))
    except Exception:
        info = None
    _FW_INFO[key] = info
    return info


_DISC_INFO = {}
def disc_info(path):
    """What kind of karaoke disc an image is (cached): Megapro (KARA125), Platinum KS (IDX + MOVIES), or other."""
    try: st = os.stat(path)
    except OSError: return None
    key = (path, st.st_size, st.st_mtime)
    if key in _DISC_INFO: return _DISC_INFO[key]
    info = dict(kind='disc image')
    try:
        names = {x['name'].upper() for x in isoread.Disc(path).listdir('/')}
        if 'KARA125' in names: info = dict(kind='Megapro karaoke disc', player='mp100')
        elif 'IDX' in names and ('MOVIES' in names or '_FONT' in names): info = dict(kind='Platinum karaoke disc', player='ks5')
        elif 'VIDEO_TS' in names: info = dict(kind='DVD video')
    except Exception:
        info = dict(kind='not readable')
    _DISC_INFO[key] = info
    return info


def library(disc_dir=None):
    """Everything found in the program's folders: firmware files, SoundFonts and disc images."""
    fws = []
    for p in _scan([app_folder('bios'), APPDIR], FW_EXT):
        i = firmware_info(p)
        if i: fws.append(dict(path=disp_path(p), name=os.path.basename(p), player=i['player'], id=i['id'], size=os.path.getsize(p)))
    sfs = [dict(path=disp_path(p), name=os.path.basename(p), size=os.path.getsize(p))
           for p in _scan([app_folder('soundfont'), APPDIR], ('.sf2',))]
    dd = disc_dir or app_folder('discs')
    discs = []
    for p in _scan([dd], DISC_EXT):
        if p.lower().endswith('.bin') and firmware_info(p): continue          # a firmware file, not a disc
        i = disc_info(p) or {}
        discs.append(dict(path=disp_path(p), name=os.path.basename(p), size=os.path.getsize(p), kind=i.get('kind', ''), player=i.get('player')))
    return dict(firmware=fws, soundfonts=sfs, discs=discs, disc_dir=disp_path(dd), folders={k: disp_path(app_folder(k, False)) for k in FOLDERS})


# ------------------------------------------------------------------------------------ firmware
def find_firmware(hint=None):
    if hint:                                  # an explicit path is honoured or rejected - never silently replaced
        hint = os.path.abspath(os.path.expanduser(hint))
        return hint if os.path.isfile(hint) else None
    cands = []
    cands += [os.path.join(d, n) for d in (os.getcwd(), app_folder('bios', False), APPDIR) for n in ('MP100NSP.BIN', 'MP100NSP.bin', 'MP100NSP.dec.bin')]
    for c in cands:
        if c and os.path.isfile(c): return os.path.abspath(c)
    for p in _scan([app_folder('bios', False)], FW_EXT):     # any other player firmware in BIOS\
        if firmware_info(p): return os.path.abspath(p)
    return None


def load_firmware_image(path):
    """Return the descrambled firmware image (accepts the original .BIN or an already-decoded file)."""
    raw = open(path, 'rb').read()
    if len(raw) < 0x400: raise ValueError('file too small to be the firmware')
    if b'SUNPLUS' in raw[:0x400]: return raw, 'already descrambled'
    dec = descramble.xform(raw)
    if b'SUNPLUS' in dec[:0x400]: return dec, 'descrambled'
    raise ValueError('not recognised as MP100NSP firmware (no SUNPLUS marker after descrambling)')


# The firmware build the emulator was made for (all drive, MIDI, inflate and main-loop hooks are at addresses of
# this build). Another build boots, but its drive is never answered, so it only shows the DVD screensaver.
KNOWN_FIRMWARE = {'cf8b47863c60612286635808d95855ff9587e3ba': 'MP100NSP (1,756,160 bytes)'}


def firmware_warning(image):
    import hashlib
    h = hashlib.sha1(bytes(image)).hexdigest()
    if h in KNOWN_FIRMWARE: return ''
    import fwprofile
    fid = fwprofile.detect(image)
    if fid != fwprofile.MP100: return ''                 # another supported player (fwprofile.py)
    return ('WARNING: this is a different firmware build (%d bytes, sha1 %s...) from the one the emulator supports '
            '(Megapro MP100NSP.BIN, Megapro MIGAEL_R.BIN or Platinum KS5 aprom_4m.bin). Its disc drive and sound hooks may '
            'not line up, so the disc may not be seen. Choose one of the supported files in the BIOS folder.' % (len(image), h[:10]))


SETTINGS_NAME = 'KaraokeEmu.settings.json'     # data file names kept from before the rename (settings carry over)
APP_NAME = 'Project : Chiaki'


def settings_path():
    """Where the remembered settings live: 'Saved Settings' next to the program, or ~/.karaoke_emu if read-only."""
    for d in (app_folder('saved', False), os.path.join(os.path.expanduser('~'), '.karaoke_emu')):
        p = os.path.join(d, SETTINGS_NAME)
        if os.path.exists(p): return p
    for d in (app_folder('saved', False), os.path.join(os.path.expanduser('~'), '.karaoke_emu')):
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, '.write_test'); open(t, 'w').close(); os.remove(t)
            return os.path.join(d, SETTINGS_NAME)
        except OSError:
            continue
    return None


def load_settings():
    p = settings_path()
    try:
        with open(p, 'r', encoding='utf-8') as f: d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_FF = [None, 0.0]
def _sys_cpu_times():
    """(idle, total) CPU time of the whole computer, all cores (for the log: how busy the PC is)."""
    try:
        if sys.platform == 'win32':
            import ctypes
            class FT(ctypes.Structure): _fields_ = [('lo', ctypes.c_uint32), ('hi', ctypes.c_uint32)]
            i, k, u = FT(), FT(), FT()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)): return None
            v = lambda f: (f.hi << 32) | f.lo
            return v(i), v(k) + v(u)                  # kernel time includes idle
        with open('/proc/stat') as f: p = [int(x) for x in f.readline().split()[1:]]
        return p[3] + p[4], sum(p)
    except Exception:
        return None


def _log_line(text):
    """One line in KaraokeEmu.log (what happened when, for a problem seen on another computer)."""
    try:
        p = log_path()
        if p:
            with open(p, 'a', encoding='utf-8') as f: f.write('%s %s\n' % (time.strftime('%H:%M:%S'), text))
    except Exception: pass


def _log_tail(path, words, n=12):
    """The last lines of the log about these things (shown in the app, e.g. why there is no sound on a phone)."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f: lines = f.readlines()[-400:]
        return ''.join([l for l in lines if any(w in l for w in words)][-n:])
    except Exception:
        return ''


def ffmpeg_path():
    # Asked many times a second by the video code: looking at the disk each time cost the emulator thread a few
    # percent on Windows (more with an antivirus scanning every check). Looked up again every 10 s.
    now = time.time()
    if now - _FF[1] > 10.0:
        ff = isoread.find_ffmpeg(); _FF[1] = now
        if ff and ff not in _FF_OK:
            # a file that cannot run here (Android: an ffmpeg for another processor - "Exec format error") counts as
            # no ffmpeg: no pictures / video, but the emulator runs (it stopped with an error)
            try:
                ok_ = subprocess.run([ff, '-version'], capture_output=True, timeout=20,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode == 0
            except Exception:
                ok_ = False
            _FF_OK[ff] = ok_
            if not ok_: _log_line('ffmpeg cannot run here: %s' % ff)
        _FF[0] = ff if ff and _FF_OK.get(ff) else None
    return _FF[0]
_FF_OK = {}


def m2v_to_png(m2v_bytes):
    ff = ffmpeg_path()
    if not ff: return None
    p = subprocess.run([ff, '-v', 'error', '-y', '-f', 'mpegvideo', '-i', 'pipe:0', '-frames:v', '1', '-f', 'image2pipe',
                        '-vcodec', 'png', 'pipe:1'], input=m2v_bytes, capture_output=True)
    return p.stdout if p.returncode == 0 and p.stdout[:4] == b'\x89PNG' else None


# Settings > Resolution: the size the background picture is made at and the screen's shape.
# 720x480 = the player's own picture (shown 3:2, as before); 640x480 = 4:3; 1920x1080 = full HD 16:9 (the picture
# is scaled up, filling a widescreen TV the way the player's wide-screen setting does).
RESOLUTIONS = {'640x480': ((640, 480), 4 / 3), '720x480': ((720, 480), 3 / 2), '1920x1080': ((1920, 1080), 16 / 9)}
def resolution(name):
    return RESOLUTIONS.get(name or '720x480', RESOLUTIONS['720x480'])


_BLACK = {}
def black_jpg(size):
    if size not in _BLACK:
        ff = ffmpeg_path()
        if not ff: return None
        p = subprocess.run([ff, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=%dx%d' % size, '-frames:v', '1',
                            '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'], capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _BLACK[size] = p.stdout if p.returncode == 0 else None
    return _BLACK[size]


def m2v_to_jpg(m2v_bytes, size=None):
    ff = ffmpeg_path()
    if not ff: return None
    vf = ['-vf', 'scale=%d:%d:flags=lanczos' % size] if size else []
    p = subprocess.run([ff, '-v', 'error', '-y', '-f', 'mpegvideo', '-i', 'pipe:0', '-frames:v', '1'] + vf + ['-q:v', '3',
                        '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'], input=m2v_bytes, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return p.stdout if p.returncode == 0 and p.stdout[:2] == b'\xff\xd8' else None


class LiveVideo:
    """Moving background video: one ffmpeg process decodes the MPEG stream the firmware feeds its video decoder
    (as it arrives, in real time) into JPEG frames; the page shows them as a motion-JPEG stream."""
    def __init__(self):
        self.proc = None; self.mark = 0; self.jpg = None; self.version = 0; self.cond = threading.Condition()
        self.error = None; self.restarts = 0; self.fed = 0

    enabled = True
    def available(self): return self.enabled and not os.environ.get('KE_NOVIDEO') and bool(ffmpeg_path())

    def _start(self, follow=False, fmt='mpeg'):
        ff = ffmpeg_path()
        if not ff: self.error = 'ffmpeg not found'; return False
        try:
            avi = fmt == 'avi'          # music videos: paced by the realtime filter (see _start_local), not -re
            self.proc = subprocess.Popen([ff, '-v', os.environ.get('KE_FFV', 'quiet')] + (['-re'] if follow and not avi else []) + ['-fflags', '+genpts+nobuffer', '-flags', 'low_delay',
                                          '-probesize', '65536', '-analyzeduration', '0', '-f', fmt, '-i', 'pipe:0',
                                          '-an', '-vf', ('realtime=limit=2,' if follow and avi else '') + (('yadif=deint=interlaced,' + ('fps=%s,' % getattr(self, 'out_fps', '20') if getattr(self, 'out_fps', '20') else '')) if follow else '') + 'scale=%d:%d' % self.out_size() + (':flags=lanczos' if self.out_size()[0] > 720 else ''), '-q:v', '6', '-threads', '1'] + self._passthrough(ff) + ['-f', 'mjpeg', 'pipe:1'],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(os.environ['KE_FFLOG'], 'ab') if os.environ.get('KE_FFLOG') else subprocess.DEVNULL, bufsize=0,
                                         # normal priority: still below the emulator (above normal) and the synth (high), but
                                         # no longer below every game or browser - 'below normal' was starved by them (stutter)
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                         preexec_fn=(None if os.name == 'nt' else (lambda: os.nice(5))))
        except Exception as ex:
            self.error = str(ex); self.proc = None; return False
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        import queue
        self.q = queue.Queue(maxsize=64)
        if not follow: threading.Thread(target=self._writer, args=(self.proc, self.q), daemon=True).start()
        return True

    _pt = None
    @classmethod
    def _passthrough(cls, ff):
        """Output each decoded picture once (ffmpeg's default for this output repeats pictures to fill timestamp gaps,
        thousands of copies when the stream's clock jumps, which stalled the decoder)."""
        if cls._pt is None:
            try:
                h = subprocess.run([ff, '-hide_banner', '-h', 'full'], capture_output=True, timeout=20,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
                cls._pt = ['-fps_mode', 'passthrough'] if b'fps_mode' in h else ['-vsync', '0']
            except Exception:
                cls._pt = ['-vsync', '0']
        return cls._pt

    def _writer(self, proc, q):
        while True:
            data = q.get()
            if data is None: break
            try: proc.stdin.write(data)
            except Exception:
                try: proc.kill()
                except Exception: pass
                break

    def _reader(self, proc):
        buf = b''
        while True:
            try: chunk = proc.stdout.read(65536)
            except Exception: break
            if not chunk: break
            buf += chunk
            while True:
                a = buf.find(b'\xff\xd8'); b = buf.find(b'\xff\xd9', a + 2) if a >= 0 else -1
                if a < 0 or b < 0: break
                frame = buf[a:b + 2]; buf = buf[b + 2:]
                now_ = time.perf_counter(); lt = getattr(self, '_last_fr', None); self._last_fr = now_
                if lt is not None:            # for the log: the longest wait between two decoded pictures
                    self.gap_max = max(getattr(self, 'gap_max', 0.0), now_ - lt); self.gap_n = getattr(self, 'gap_n', 0) + 1
                with self.cond:
                    self.jpg = frame; self.version += 1; self.cond.notify_all()
            if len(buf) > 4 << 20: buf = b''

    STALL_BYTES = 8 << 20          # stream fed without a single new picture -> the decoder is stuck: restart it

    def feed(self, fifo):
        """Pass the stream bytes the firmware produced since the last call to the decoder."""
        data, self.mark = fifo.since(self.mark)
        if not data: return
        alive = self.proc is not None and self.proc.poll() is None
        if alive and self.version == getattr(self, 'v_seen', -1) and self.fed - getattr(self, 'fed_seen', 0) > self.STALL_BYTES:
            self._kill(); alive = False
        if self.version != getattr(self, 'v_seen', -1): self.v_seen = self.version; self.fed_seen = self.fed
        if getattr(self, 'resync', False):
            # data was dropped (the decoder fell behind): continue only from the next pack header followed by a
            # sequence header, so the decoder restarts cleanly at a whole picture group instead of decoding a
            # damaged stretch (a mostly flat, smeared picture that never recovers while drops continue)
            j = data.find(b'\x00\x00\x01\xb3')
            i = data.rfind(b'\x00\x00\x01\xba', 0, j) if j >= 0 else -1
            if i < 0: self.dropped = getattr(self, 'dropped', 0) + len(data); return
            data = data[i:]; self.resync = False
        if not alive:
            # a new decoder must start on a pack header followed by a sequence header (picture size), or ffmpeg
            # gives up ("Invalid frame dimensions 0x0") or hangs
            j = data.find(b'\x00\x00\x01\xb3')
            if j < 0: return
            i = data.rfind(b'\x00\x00\x01\xba', 0, j)
            if i < 0: return
            if time.time() - getattr(self, 'started_at', 0) < 0.5 or not self._start(): return     # at most 2 restarts/s
            self.started_at = time.time()
            self.restarts += 1; self.v_seen = self.version; self.fed_seen = self.fed
            data = data[i:]
        # handed to a writer thread: the emulator never waits for the decoder (if the decoder falls behind,
        # e.g. at Instant speed, stream data is dropped and the decoder resyncs at the next pack header)
        try:
            self.q.put_nowait(data); self.fed += len(data)
        except Exception:
            self.dropped = getattr(self, 'dropped', 0) + len(data); self.resync = True

    # ---- follow mode: play the background video file the firmware is streaming, straight from the disc
    # The emulated firmware has no video decoder holding it back, so it pulls its stream several times faster than
    # real time and skips through it; whatever it hands on is not a watchable stream. The picture is therefore
    # decoded from the disc itself: from the sector where the firmware is reading, in real time (ffmpeg -re),
    # restarting whenever the firmware moves to another video file (a new clip, or the Change video key).
    files = None
    def _file_of(self, lba):
        for a, b, name in self.files or ():
            if a <= lba < b: return (a, b, name)
        return None

    mv_audio = None; mv_stop = None                # fn(pcm s16le 32 kHz stereo, emulated seconds) / fn()
    mv_allowed = None                               # fn(number) -> True when the person picked this video (else: background, silent)
    @staticmethod
    def _mv_number(f):
        m = re.search(r'(\d+)\.(?:mvv|avi)$', f[2], re.I)
        return int(m.group(1)) if m else None
    def picked(self, numbers):
        """A song record was read after its video already started (the reads race): give it its sound now."""
        f = getattr(self, 'mv_file', None)
        if f is None or getattr(self, 'mv_sound', False) or self._mv_number(f) not in numbers: return
        if self.mv_allowed and self.mv_allowed(self._mv_number(f)):
            self.mv_sound = True
            threading.Thread(target=self._mv_audio, args=(self.mv_disc, f, self.mv_t), daemon=True).start()
    MV_DELAY = 0.0                                  # seconds the video picture and sound start after the player (sync)
    def _mv_stop(self):
        if getattr(self, 'mv_file', None) is not None:
            self.mv_file = None
            if self.mv_stop:
                try: self.mv_stop()
                except Exception: pass

    def _mv_audio(self, disc_path, f, t):
        try:
            if self.reader_factory: src = self.reader_factory(disc_path)
            else: src = isoread.SampleSource(disc_path) if open(disc_path, 'rb').read(8) == b'KEMUSPL1' else isoread.SectorSource(disc_path)
            a, b, name = f
            data = src.read(a, b - a)
            ff = ffmpeg_path()
            r = subprocess.run([ff, '-v', 'quiet', '-f', 'avi', '-i', 'pipe:0', '-vn', '-f', 's16le', '-ar', '32000', '-ac', '2', 'pipe:1'],
                               input=data, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if r.stdout and self.mv_audio and getattr(self, 'mv_file', None) == f: self.mv_audio(r.stdout, t)
        except Exception as ex:
            self.error = 'music video sound: %s' % ex

    def follow(self, stream, disc_path, now_t=None):
        self._stream = stream
        if self.mode != 'on': return                                  # paused / switched off by the user
        st = stream.st; lba = st.get('lba')
        if lba is None or not disc_path: return
        if self.files is None:
            self.files = []
            try:
                d = isoread.Disc(disc_path)
                def walk(p, depth=0):
                    for x in d.listdir(p):
                        q = p.rstrip('/') + '/' + x['name']
                        if x['is_dir'] and depth < 3: walk(q, depth + 1)
                        elif x['name'].lower() == 'movie.idx':
                            try: self.movie_idx = [int(v) for v in re.findall(rb'\d+', d.read(q)[:4096])]
                            except Exception: self.movie_idx = None
                        elif x['name'].lower().endswith(('.vob', '.mpg', '.mpeg', '.dat', '.mvv', '.plf', '.avi')):
                            a, n = d.lookup(q).extents[0]; self.files.append((a, a + (n + 2047) // 2048, q))
                walk('/')
            except Exception as ex:
                self.error = 'cannot list the disc: %s' % ex
        if getattr(self, 'still_total', None) is not None:
            # a firmware picture (the SCORE screen) stays up - over the chosen / own background videos too - until
            # the firmware reads the disc again. (The stream byte count was used: the SCORE picture is played twice
            # from flash, and on a slow PC the picture was ready between the two plays, so the second play took it
            # down at once.)
            if st.get('lba') == getattr(self, 'still_lba', None): return
            # some discs (an edited MPPV42) keep streaming the background video under the score: the SCORE
            # picture stays its few seconds anyway
            if time.time() - getattr(self, 'still_at', 0) < self.STILL_MIN: return
            _log_line('picture: firmware picture taken down after %.1f s (disc read at %s)' % (time.time() - self.still_at, st.get('lba')))
            self.still_total = None
        fmv = self._file_of(lba)
        mv_now = fmv is not None and fmv[2].lower().endswith(('.mvv', '.avi'))
        if self.custom is not None and not self.override and not mv_now:
            # (a music video plays as itself: Change BGV is for the background videos only)
            # Change BGV: the user's choice from the disc's background videos, played on its own and looped
            groups = self.bgv_groups()
            if not groups: self.custom = None
            else:
                name_, grp = groups[self.custom % len(groups)]
                cur = getattr(self, 'cur_file', None)
                alive = self.proc is not None and self.proc.poll() is None and getattr(self, 'feeding', False)
                if alive and cur in grp: return
                if time.time() - getattr(self, 'started_at', 0) < 0.5: return
                # the group's videos one after another (VIEW1, VIEW2, ... / the KS5's movie.idx range), then again
                f = grp[(grp.index(cur) + 1) % len(grp)] if cur in grp else grp[0]
                if self.proc is not None: self._kill()
                self._mv_stop()
                self.out_fps = '30' if f[2].lower().endswith('.plf') else '25' if f[2].lower().endswith('.vob') else '20'
                self.free = True
                if not self._start(follow=True, fmt='mpeg'): return
                self.started_at = time.time(); self.restarts += 1; self.cur_file = f; self.feeding = True
                threading.Thread(target=self._feed_file, args=(self.proc, disc_path, f, f[0], True), daemon=True).start()
                return
        self.free = False
        f = self._file_of(lba)
        if f is not None and f[2].lower().endswith(('.mvv', '.avi')) and f != getattr(self, 'cur_file', None) and lba >= f[0] + 64:
            # a music video: the player first reads its index at the end of the file; its clock (and the lyrics)
            # starts with the first data from the beginning, so the picture and sound start there too
            f = None
        now_total = stream.total
        if now_total != getattr(self, 'seen_total', None): self.seen_total = now_total; self.seen_at = time.time()
        if self.override and not (f is not None and f[2].lower().endswith(('.mvv', '.avi'))):
            self._play_override(); return                              # Override BGV (music videos still play)
        idle = time.time() - getattr(self, 'seen_at', 0) > 3.0          # the firmware stopped streaming
        alive = self.proc is not None and self.proc.poll() is None and getattr(self, 'feeding', False)
        if idle:
            if alive: self._kill()
            self._mv_stop()
            return
        cur = getattr(self, 'cur_file', None)
        if f is None or (alive and f == cur): return
        if (self.free and cur is not None and f != cur and self._group_of(f) is not None
                and self._group_of(f) == self._group_of(cur)):
            # a free-running KS5 background video plays its group (movie.idx) one file after another on its own
            if alive: return
            if f != cur and not alive and time.time() - getattr(self, 'started_at', 0) >= 1.0:
                grp = self._group_of(cur)[1]; f = grp[(grp.index(cur) + 1) % len(grp)]
        if time.time() - getattr(self, 'started_at', 0) < 1.0: return
        if self.proc is not None: self._kill()
        is_mv = f[2].lower().endswith(('.mvv', '.avi'))        # music videos (AVI): Megapro .mvv, KS5 Mtv/*.avi
        if getattr(self, 'mv_file', None) != (f if is_mv else None): self._mv_stop()
        # KS5 background videos (.PLF) are 29.97 pictures/s: shown at their own rate (20/s dropped every third
        # picture unevenly, which looked like stutter)
        # music videos (AVI: KS5 Mtv/*.avi, Megapro .mvv) keep their own picture rate (25 or 29.97/s): cut to 20/s,
        # every fourth or third picture was dropped unevenly, which looked like stutter
        self.out_fps = None if is_mv else '30' if f[2].lower().endswith('.plf') else '20'
        if not self._start(follow=True, fmt='avi' if is_mv else 'mpeg'): return
        if is_mv and getattr(self, 'mv_file', None) != f:
            # a music video: its sound (MP3 in the AVI) is played too, from the moment the player started it
            self.mv_file = f; self.mv_disc = disc_path; self.mv_t = (now_t or 0) + self.MV_DELAY
            self.mv_sound = self.mv_allowed is None or bool(self.mv_allowed(self._mv_number(f)))
            if self.mv_sound:
                threading.Thread(target=self._mv_audio, args=(disc_path, f, self.mv_t), daemon=True).start()
        # KS5 background videos (.PLF) play at their own rate: the emulated KS5 reads them at ~1/8 of the real
        # rate (~54 of ~415 sectors/s), so a picture tied to its read position played ~2 s and waited (stutter).
        # The firmware's position only picks the file and where to start; at the end the same file loops from
        # its beginning.
        # (the WOWE WL8202RD reads the same discs' .PLF videos slowly too: its background video lagged)
        self.free = soc.FW_ID in ('ks5', 'wl8202') and f[2].lower().endswith('.plf')
        g_ = self._group_of(f) if self.free else None
        start_at = f[0] if (self.free and (f == cur or (g_ is not None and cur in g_[1]))) else lba
        if not (f[0] <= start_at < f[1]): start_at = f[0]
        self.started_at = time.time(); self.restarts += 1; self.cur_file = f
        self.feeding = True
        threading.Thread(target=self._feed_file, args=(self.proc, disc_path, f, start_at, self.free), daemon=True).start()

    reader_factory = None
    STILL_MIN = 7.0               # seconds a firmware picture (the SCORE screen) stays up at least
    LEAD = 1024                   # sectors (2 MB, ~1 s of a KS5 background video) the picture may be fed ahead of
                                  # the firmware's own read position: room for small hiccups (32 made it stall)
    def _feed_file(self, proc, disc_path, f, lba, free=False):
        try:
            if self.reader_factory: src = self.reader_factory(disc_path)
            else: src = isoread.SampleSource(disc_path) if open(disc_path, 'rb').read(8) == b'KEMUSPL1' else isoread.SectorSource(disc_path)
            a, b, name = f
            # start at a pack that holds a sequence header (picture size), at most ~4 MB ahead
            pos = lba
            if name.lower().endswith(('.mvv', '.avi')):
                pos = a; k = 0                                         # a music video (AVI): from its beginning
                if self.MV_DELAY > 0: time.sleep(self.MV_DELAY)
            first = None
            for k in (range(0) if name.lower().endswith(('.mvv', '.avi')) else range(2048)):
                if pos + k >= b: break
                sec = src.read(pos + k, 1)
                if b'\x00\x00\x01\xb3' in sec and sec[:4] == b'\x00\x00\x01\xba': pos += k; break
            else:
                if not name.lower().endswith(('.mvv', '.avi')):
                    # no sequence header ahead (the KS5's .PLF videos have one at the start of the file only):
                    # start at the next GOP and put the file's sequence header in front of it, so the decoder
                    # knows the picture format
                    first, pos = self._with_seq_header(src, a, b, pos)
            if first is not None:
                proc.stdin.write(first); self.fed += len(first); pos += 1
            start = pos; ok_fl = pos; jump_at = None
            stream = getattr(self, '_stream', None)
            follow_fw = stream is not None and not name.lower().endswith(('.mvv', '.avi')) and not free
            while pos < b and proc.poll() is None and self.proc is proc:
                if follow_fw:
                    # never ahead of the player: feed only what the firmware has read itself. Fed freely, the
                    # picture ran ahead whenever the emulator was below 1x, reached the end of the file and was
                    # started again at the firmware's (earlier) position: a jump back every second or so
                    fl = stream.st.get('lba')
                    if fl is None or not (a <= fl < b):
                        if pos >= b - 16: break
                        time.sleep(0.02); continue
                    if fl < pos - 4096: break                           # the player went back (looped): restart there
                    if fl > ok_fl + 2048:
                        # a read far ahead (the player now and then reads near the end of the file): not the
                        # play position. Followed only if it lasts (a real jump): then started again there
                        if jump_at is None: jump_at = time.time()
                        elif time.time() - jump_at > 2.0: break
                        fl = ok_fl
                    else:
                        ok_fl = max(ok_fl, fl); jump_at = None
                    if pos >= max(fl + self.LEAD, start + 128):     # the first 256 KB at once: ffmpeg probes it
                        time.sleep(0.01); continue
                n = min(16, b - pos)
                data = src.read(pos, n); pos += n
                proc.stdin.write(data); self.fed += len(data)        # ffmpeg -re paces this
            if follow_fw and pos >= b:
                # the whole file is fed: keep the decoder (and its last picture) until the player moves on, rather
                # than ending it and starting the same file again at its last sectors
                while self.proc is proc and proc.poll() is None:
                    fl = stream.st.get('lba')
                    if fl is None or not (a <= fl < b): break
                    time.sleep(0.05)
            try: proc.stdin.close()
            except Exception: pass
        except Exception as ex:
            if self.proc is proc: self.error = str(ex)      # not when a newer decoder replaced this one
        finally:
            if self.proc is proc: self.feeding = False

    @staticmethod
    def _with_seq_header(src, a, b, pos):
        """(first sector with the file's sequence header inserted before its GOP, its LBA) or (None, pos)."""
        try:
            head = src.read(a, 8); i = head.find(b'\x00\x00\x01\xb3')
            if i < 0: return None, pos
            j = i + 4                                                   # the header and its extensions, up to the GOP
            while True:
                j = head.find(b'\x00\x00\x01', j)
                if j < 0 or head[j + 3] not in (0xB5, 0xB2): break
                j += 4
            seq = head[i:j] if j > i else b''
            if not seq or len(seq) > 400: return None, pos
            for k in range(4096):
                if pos + k >= b: break
                sec = src.read(pos + k, 1); g = sec.find(b'\x00\x00\x01\xb8')
                if g < 0 or sec[:4] != b'\x00\x00\x01\xba': continue
                pes = sec.rfind(b'\x00\x00\x01\xe0', 0, g)
                if pes < 0: continue
                ln = int.from_bytes(sec[pes + 4:pes + 6], 'big')
                if ln == 0: continue
                out = sec[:pes + 4] + (ln + len(seq)).to_bytes(2, 'big') + sec[pes + 6:g] + seq + sec[g:]
                return out, pos + k
        except Exception:
            pass
        return None, pos

    def _kill(self):
        self.feeding = False
        try: self.q.put_nowait(None)
        except Exception: pass
        try: self.proc.kill()
        except Exception: pass
        self.proc = None

    def show_still(self, m2v, stream=None):
        """A picture the firmware shows from its own memory (the Megapro's SCORE screen after a song): the moving
        background stops and the picture stays until the firmware streams from the disc again. Pictures are taken
        in the order the firmware pushed them (after Stop: its logo, then the SCORE picture); only the last stays."""
        import queue
        if getattr(self, '_still_q', None) is None:
            self._still_q = queue.Queue()
            threading.Thread(target=self._still_worker, daemon=True).start()
        self._still_q.put((m2v, stream))

    def _still_worker(self):
        while True:
            m2v, stream = self._still_q.get()
            while not self._still_q.empty():                   # a newer picture is waiting: that one wins
                m2v, stream = self._still_q.get()
            try: self._show_still(m2v, stream)
            except Exception: pass

    def _show_still(self, m2v, stream=None):
        if self.mode != 'on': return False
        jpg = m2v_to_jpg(m2v, self.out_size())
        if not jpg: return False
        if self.proc is not None: self._kill()
        self._mv_stop(); self.cur_file = None
        self.still_total = stream.total if stream is not None else None
        self.still_lba = stream.st.get('lba') if stream is not None else None
        self.still_at = time.time()
        _log_line('picture: firmware picture shown (%d KB)' % (len(m2v) // 1024))
        with self.cond:
            self.jpg = jpg; self.version += 1; self.cond.notify_all()
        return True

    # ---- Override BGV: videos from a folder on this computer instead of the disc's background videos
    override = None                      # list of video files (played in turn, looped); None: the disc's own
    override_i = 0
    def set_override(self, files):
        self.override = list(files) if files else None; self.override_i = 0
        if self.proc is not None: self._kill()
        self.cur_file = None; self.started_at = 0

    def _play_override(self):
        files = self.override
        alive = self.proc is not None and self.proc.poll() is None and getattr(self, 'feeding', False)
        cur = getattr(self, 'cur_file', None)
        if alive and isinstance(cur, tuple) and cur and cur[0] == 'local': return
        if time.time() - getattr(self, 'started_at', 0) < 0.5: return
        if isinstance(cur, tuple) and cur and cur[0] == 'local':
            self.override_i += 1                                          # that one ended: the next (or again)
        if self.proc is not None: self._kill()
        self._mv_stop()
        path = files[self.override_i % len(files)]
        self.started_at = time.time()
        if not self._start_local(path, loop=len(files) == 1): return
        self.restarts += 1; self.cur_file = ('local', path); self.feeding = True

    def _start_local(self, path, loop=False):
        ff = ffmpeg_path()
        if not ff: self.error = 'ffmpeg not found'; return False
        if not os.path.isfile(path): self.error = 'background video not found: %s' % disp_path(path); return False
        try:
            # paced by the pictures' own times (the realtime filter), not by reading the file at its rate (-re):
            # AVI files keep sound and picture in big interleaved chunks, and -re let the pictures through in
            # bursts (stutter only with AVI files, also renamed ones)
            self.proc = subprocess.Popen([ff, '-v', os.environ.get('KE_FFV', 'quiet')] + (['-stream_loop', '-1'] if loop else []) +
                                         ['-i', path, '-an', '-vf', 'realtime=limit=2,scale=%d:%d' % self.out_size() + (':flags=lanczos' if self.out_size()[0] > 720 else ''),
                                          '-q:v', '6', '-threads', '2'] + self._passthrough(ff) + ['-f', 'mjpeg', 'pipe:1'],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),     # normal priority (see _start)
                                         preexec_fn=(None if os.name == 'nt' else (lambda: os.nice(5))))
        except Exception as ex:
            self.error = str(ex); self.proc = None; return False
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        return True

    custom = None                        # Change BGV: index into bgv_groups() (None: what the player plays)
    movie_idx = None                     # KS5 /MOVIES/movie.idx: "00,03,06,..." first file of each background-video group
    def bgv_files(self):
        """The disc's background videos (not music videos), in disc order."""
        return [f for f in (self.files or []) if not f[2].lower().endswith(('.mvv', '.avi')) and f[1] - f[0] > 256]
    @staticmethod
    def _is_bgv(f):
        p = f[2].lower(); base = p.rsplit('/', 1)[-1]
        if '/video_ts/' in p or base.startswith('vts_') or base.startswith('audio.'): return False   # DVD menus, sound only
        if p.endswith('.plf') and not re.match(r'midi\d+\.plf$', base): return False               # KS5: MOVIES/MIDIxx.PLF
        return True
    def bgv_groups(self):
        """The background videos as the player groups them: [(name, [files in play order])].
        Megapro: one group per name without its number (V1_VIEW/VIEW1.vob, VIEW2.vob ... = "VIEW").
        KS5: the ranges of /MOVIES/movie.idx (00,03,06,... = MIDI00-02, MIDI03-05, ...: the player plays a range
        through and starts it again, the next number is the barrier)."""
        files = [f for f in self.bgv_files() if self._is_bgv(f)]
        key = (id(self.files), len(files), tuple(self.movie_idx or ()))
        if getattr(self, '_groups_key', None) == key: return self._groups
        groups = []
        plf = sorted((f for f in files if f[2].lower().endswith('.plf')), key=lambda f: f[2].lower())
        rest = [f for f in files if f not in plf]
        if plf and self.movie_idx:
            bars = [b for b in self.movie_idx]
            for i in range(len(bars) - 1):
                part = plf[bars[i]:min(bars[i + 1], len(plf))] if bars[i + 1] > bars[i] else []
                if part: groups.append(('BGV %d' % (len(groups) + 1), part))
            used = [f for _, g in groups for f in g]
            rest = [f for f in plf if f not in used] + rest
        named = {}
        for f in rest:
            folder, base = f[2].rsplit('/', 1) if '/' in f[2] else ('', f[2])
            stem = os.path.splitext(base)[0]
            m = re.match(r'^(.*?)[ _-]*(\d*)$', stem)
            nm = (m.group(1) or stem).upper(); num = int(m.group(2)) if m.group(2) else 0
            k = (folder.upper(), nm)
            if k not in named: named[k] = (nm, []); groups.append(named[k])
            named[k][1].append((num, f))
        out = []
        for nm, g in groups:
            if g and isinstance(g[0], tuple) and len(g[0]) == 2 and isinstance(g[0][0], int) and isinstance(g[0][1], tuple):
                g = [f for _, f in sorted(g, key=lambda t: t[0])]
            out.append((nm, g))
        self._groups_key = key; self._groups = out
        return out
    def _group_of(self, f):
        for name, g in self.bgv_groups():
            if f in g: return name, g
        return None
    def change_bgv(self, step=1):
        if self.override:                                              # Override BGV: the next video of the folder
            self.override_i = (self.override_i + step) % len(self.override)
            if self.proc is not None: self._kill()
            self.cur_file = None; self.started_at = 0
            return os.path.basename(self.override[self.override_i])
        groups = self.bgv_groups()
        if not groups: return None
        if step == 0: self.custom = None
        elif self.custom is not None and self.custom + step >= len(groups):
            self.custom = None                                         # after the last one: the player's own again
        else:
            cur = getattr(self, 'cur_file', None); g = self._group_of(cur) if cur else None
            i = groups.index(g) if (self.custom is None and g in groups) else (self.custom if self.custom is not None else -1)
            self.custom = (i + step) % len(groups)
        self.cur_file = None; self.started_at = 0
        return None if self.custom is None else groups[self.custom][0]

    mode = 'on'                          # background video: 'on', 'paused' (last picture stays) or 'off' (black)
    def set_mode(self, mode):
        if mode not in ('on', 'paused', 'off') or mode == self.mode: return
        self.mode = mode
        if mode != 'on':
            if self.proc is not None: self._kill()
            self._mv_stop(); self.cur_file = None
            if mode == 'off':
                jpg = black_jpg(self.out_size())
                if jpg:
                    with self.cond: self.jpg = jpg; self.version += 1; self.cond.notify_all()
        else:
            self.cur_file = None; self.started_at = 0                # follow() starts it again at once

    size = (720, 480)                    # output picture size (Settings > Resolution)
    MAX_H = 480
    def out_size(self):
        """Size of the pictures sent to the page: the chosen screen's shape, at most 480 lines. The disc videos are
        720x480; making 1920x1080 pictures here (one ffmpeg thread, 30 big JPEGs a second for the browser to
        decode) only cost time and made the picture stutter. The page scales them to the screen (graphics card)."""
        w, h = self.size
        if h > self.MAX_H: w, h = int(round(w * self.MAX_H / h / 2.0)) * 2, self.MAX_H
        return (w, h)

    def wait(self, after, timeout=1.0):
        with self.cond:
            if self.version <= after: self.cond.wait(timeout)
            return self.version, self.jpg

    def close(self):
        if self.proc:
            try: self.q.put_nowait(None)
            except Exception: pass
            try: self.proc.stdin.close()
            except Exception: pass
            try: self.proc.kill()
            except Exception: pass
            self.proc = None


# The player's settings live in an I2C EEPROM. A new player's EEPROM holds what Setup > Default Setup writes; the
# emulated one started blank (all 0), which left e.g. the key range at 0 (Key up/down did nothing). These are the
# bytes the firmware's own Default Setup wrote (device 0xA0, captured in the emulator); the rest of the page is 0.
# One change: the Setup password is 0000 (Default Setup stores 99999 = none, and that path leaves the menu not
# answering the arrow keys in the emulator).
DEFAULT_EEPROM = {0xA0: bytes.fromhex('ac91000000000000000000000000000001000202000103000000000100010300010100000000000000000000000004060000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001000100000000000000000000000001').ljust(256, b'\0')}


# remote keys the PC keyboard can press: (firmware key, label). The default keyboard map is below; the page edits it
# (settings 'keymap': {keyboard key: firmware key}) and all three pages (main, pop-out screen, pop-out remote) use it.
REMOTE_ACTIONS = [(n, str(n)) for n in range(10)] + [
    (72, 'Enter / OK (reserve)'), (109, 'First reserve (plays next)'), (74, 'Cancel (clear the number / delete a reservation)'), (25, 'Stop'), (75, 'Setup'), (41, 'Up'), (42, 'Down'), (43, 'Left'), (44, 'Right'),
    (17, 'Volume +'), (18, 'Volume -'), (14, 'Key #'), (16, 'Key b'), (15, 'Key reset'), (13, 'Mute'),
    (29, 'Previous'), (30, 'Next'), (31, 'Display'), (66, 'Karaoke / vocal'), (73, 'Book'),
    (70, 'Menu'), (69, 'Reading'), (52, 'Intro'), (40, 'Zoom'), (34, 'Play mode'), (33, 'Repeat (Remix: Random / Seq / None)'), (19, 'Slow'),
    (27, 'Rewind'), (28, 'Forward'), (114, 'Change video'), (12, 'Eject'), (39, 'Power')]
DEFAULT_KEYMAP = {str(n): n for n in range(10)}
DEFAULT_KEYMAP.update({'Enter': 72, 'r': 109, 'Backspace': 74, 'Escape': 25, 's': 75, 'ArrowUp': 41, 'ArrowDown': 42, 'ArrowLeft': 43,
                       'ArrowRight': 44, '+': 17, '=': 17, '-': 18, 'PageUp': 14, 'PageDown': 16, 'Home': 15, 'm': 13,
                       'p': 29, 'n': 30, 'd': 31, 'k': 66, 'b': 73, 'v': 114, ' ': 72})


def eeprom_path():
    p = settings_path()
    return os.path.join(os.path.dirname(p), 'KaraokeEmu.eeprom.json') if p else None


def load_eeprom():
    p = eeprom_path()
    try:
        with open(p, encoding='utf-8') as f: d = json.load(f)
        pages = {int(k): bytes.fromhex(v) for k, v in d.get('pages', {}).items()}
        if pages: return pages
    except Exception:
        pass
    return dict(DEFAULT_EEPROM)


def save_eeprom(pages):
    p = eeprom_path()
    if not p: return
    try:
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dict(note='Megapro player settings (EEPROM) kept by Project : Chiaki; delete this file for factory settings',
                           pages={str(k): v.hex() for k, v in pages.items()}), f, indent=1)
        os.replace(tmp, p)
    except OSError:
        pass


def flash_path():
    """One file per player model (KS5, K55): their settings areas differ."""
    p = settings_path()
    fid = getattr(soc, 'FW_ID', 'ks5')
    return os.path.join(os.path.dirname(p), 'KaraokeEmu.%ssettings.json' % (fid if fid != 'ks5' else 'ks5')) if p else None


def load_flash():
    """The Platinum KS5's saved settings (its flash settings area), {address: 4096 bytes}; {} = factory settings."""
    try:
        with open(flash_path(), encoding='utf-8') as f: d = json.load(f)
        return {int(k): bytes.fromhex(v) for k, v in d.get('sectors', {}).items()}
    except Exception:
        return {}


def save_flash(sectors):
    p = flash_path()
    if not p: return
    try:
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dict(note='Player settings (flash settings area: Platinum KS5, Ace K55) kept by the emulator; delete this '
                                'file for factory settings', sectors={str(k): v.hex() for k, v in sectors.items()}), f, indent=1)
        os.replace(tmp, p)
    except OSError:
        pass


def quickboot_path(fw_path):
    """Where the quick-boot state lives: 'Saved Settings' (one per firmware file), else the user's home folder."""
    name = os.path.basename(fw_path) + '.quickboot'
    for d in (app_folder('saved', False), os.path.join(os.path.expanduser('~'), '.karaoke_emu')):
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, '.write_test'); open(test, 'w').close(); os.remove(test)
            return os.path.join(d, name)
        except OSError:
            continue
    return None


class VoiceOut:
    """Plays the firmware's spoken digits (MP3 clips handed over by soc.KeyVoice) on this PC.
    Each clip is turned into a WAV once with ffmpeg (kept in a cache folder), then played asynchronously with the
    Windows sound API; a new digit cuts off the one still playing, as on the player."""
    VOLUME = 1.6
    def __init__(self, port_getter=None):
        import queue, tempfile
        self.q = queue.Queue(); self.played = []; self.error = None; self.port_getter = port_getter; self._raws = {}
        self.dir = os.path.join(tempfile.gettempdir(), 'karaoke_emu_voice')
        threading.Thread(target=self._run, daemon=True).start()
    def push(self, seq, data):                   # called on the emulator thread: only queue it
        self.q.put(data)
    def _wav(self, data):
        import hashlib
        p = os.path.join(self.dir, hashlib.md5(data).hexdigest() + '.wav')
        if os.path.isfile(p): return p
        ff = ffmpeg_path()
        if not ff: self.error = 'ffmpeg not found'; return None
        os.makedirs(self.dir, exist_ok=True)
        tmp = p + '.tmp.wav'
        r = subprocess.run([ff, '-v', 'error', '-y', '-f', 'mp3', '-i', 'pipe:0', '-af', 'volume=%g' % self.VOLUME,
                            '-ar', '44100', '-ac', '1', tmp], input=data, capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode or not os.path.isfile(tmp): self.error = r.stderr.decode(errors='replace')[-200:]; return None
        os.replace(tmp, p); return p
    def _run(self):
        while True:
            data = self.q.get()
            while not self.q.empty(): data = self.q.get()          # behind: only the newest digit
            try:
                p = self._wav(data)
                if not p: continue
                self.played.append(p); del self.played[:-20]
                if sys.platform == 'win32':
                    import winsound
                    winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                else:
                    # Android (no Windows sound API): mixed into the built-in synth's output, played at once
                    port = self.port_getter() if self.port_getter else None
                    syn = getattr(port, 'synth', None) if port is not None else None
                    if syn is not None and hasattr(syn, 'pcm'):
                        raw = self._raws.get(p)
                        if raw is None:
                            with open(p, 'rb') as f: w = f.read()
                            import numpy as np
                            i = w.find(b'data'); pcm_ = w[i + 8:] if i >= 0 else b''
                            mono = np.frombuffer(pcm_[:len(pcm_) // 2 * 2], dtype='<i2').astype(np.float32)
                            # 44.1 kHz mono -> the synth's 32 kHz stereo, interpolated (picking the nearest samples
                            # sounded scratchy)
                            n = int(len(mono) * 32000 / 44100)
                            x = np.interp(np.arange(n) * (44100 / 32000), np.arange(len(mono)), mono) if len(mono) else np.zeros(0)
                            raw = self._raws[p] = np.repeat(np.clip(x, -32768, 32767).astype('<i2'), 2).tobytes()
                        syn.pcm(None, raw)
            except Exception as ex:
                self.error = str(ex)


class StreamPCM:
    """KS5 MP3 stream (soc.KS5Ring): decoded as it arrives by one ffmpeg process per stream and mixed into the
    built-in synth at its emulated time, so recorded music and spoken digits stay in step with the MIDI and
    lyrics. Without the built-in synth, start() refuses and soc falls back to the key-voice output for clips."""
    RATE = 32000
    def __init__(self, port_getter):
        self.port_getter = port_getter; self.proc = None; self.t = 0.0; self.error = None; self.lock = threading.Lock()
        import weakref
        self.killed = weakref.WeakSet()          # streams stopped: their remaining sound is thrown away
    def _synth(self):
        port = self.port_getter()
        return getattr(port, 'synth', None) if port is not None else None
    DELAY = 0.12               # decoded audio arrives in bursts (~0.1 s): played this much later so none is skipped
    spare = None               # a decoder started ahead of time: starting ffmpeg takes 0.1-0.5 s on Windows (more with a
                               # virus scanner), which made spoken numbers late, cut short or lost
    def _spawn(self, ff):
        # low-latency decode: no probing / buffering, so each frame comes out as soon as it is fed (with the
        # defaults ffmpeg waited for ~1 s of input or the end, and the sound arrived after its time and was skipped)
        return subprocess.Popen([ff, '-v', 'error', '-probesize', '32', '-analyzeduration', '0', '-fflags', 'nobuffer',
                                 '-f', 'mp3', '-i', 'pipe:0', '-f', 's16le', '-ar', str(self.RATE), '-ac', '2',
                                 '-flush_packets', '1', 'pipe:1'],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    def _refill(self, ff):
        def go():
            try: p = self._spawn(ff)
            except Exception: return
            with self.lock:
                if self.closed: old = p
                else: old = self.spare; self.spare = p
            if old is not None and old is not p:
                try: old.kill()
                except Exception: pass
        threading.Thread(target=go, daemon=True).start()
    def prepare(self):
        """Start a decoder now so the first clip does not wait for one."""
        ff = ffmpeg_path()
        if ff and self.spare is None: self._refill(ff)
    def start(self, t):
        self.stop(quiet=True); t += self.DELAY
        ff = ffmpeg_path(); synth = self._synth()
        if not ff or synth is None or not hasattr(synth, 'pcm'):
            self.error = 'no ffmpeg' if not ff else 'not the built-in synth'; raise RuntimeError(self.error)
        with self.lock:
            p = self.spare; self.spare = None
        if p is None or p.poll() is not None: p = self._spawn(ff)
        self._refill(ff)                                          # the next one, ready for the next clip
        import queue
        q = queue.Queue()
        with self.lock: self.proc = p; self.q = q
        def writer():
            while True:
                b = q.get()
                if b is None: break
                try: p.stdin.write(b); p.stdin.flush()
                except Exception: break
            try: p.stdin.close()
            except Exception: pass
        def reader(t0):
            t = t0; chunk = self.RATE * 4 // 50                   # 20 ms pieces
            while True:
                try: b = p.stdout.read1(chunk) if hasattr(p.stdout, 'read1') else p.stdout.read(chunk)
                except Exception: break
                if not b: break
                syn = self._synth()
                if syn is not None and p not in self.killed:
                    try: syn.pcm(t, b, sid=id(p))
                    except TypeError: syn.pcm(t, b)
                    except Exception: pass
                t += len(b) / (4.0 * self.RATE); self.decoded += len(b)
        threading.Thread(target=writer, daemon=True).start()
        threading.Thread(target=reader, args=(t,), daemon=True).start()
    fed = 0; decoded = 0
    def close(self):
        self.stop(quiet=True)
        with self.lock:
            p = self.spare; self.spare = None; self.closed = True
        if p is not None:
            try: p.kill()
            except Exception: pass
    closed = False
    def feed(self, b):
        with self.lock:
            if self.proc is not None: self.q.put(b); self.fed += len(b)
    def end(self):
        with self.lock:
            if self.proc is not None: self.q.put(None); self.proc = None
    def stop(self, quiet=False):
        with self.lock:
            p = self.proc; self.proc = None
        if p is not None:
            self.killed.add(p)
            try: self.q.put(None); p.kill()
            except Exception: pass
            if not quiet:
                syn = self._synth()
                if syn is not None and hasattr(syn, 'pcm_stop'):
                    try: syn.pcm_stop()
                    except Exception: pass


class ChorusOut:
    """Plays a song's recorded voices (chorus clips, WMA, from soc.Chorus) along with the music.
    Decoded once with ffmpeg (cached). With the built-in SoundFont synth the clip is mixed into its output at the
    emulated time the firmware started it (sample-exact with the MIDI; a late decode skips ahead to stay in step);
    with another MIDI device it plays through the Windows sound API."""
    RATE = 32000
    def __init__(self, port_getter):
        import queue, tempfile
        self.port_getter = port_getter; self.q = queue.Queue(); self.played = []; self.error = None
        self.dir = os.path.join(tempfile.gettempdir(), 'karaoke_emu_chorus')
        threading.Thread(target=self._run, daemon=True).start()
    def push(self, seq, data, t):                # emulator thread: only queue it
        self.q.put(('play', data, t))
    def push_pcm(self, raw, t):                  # already decoded (a music video's sound)
        self.q.put(('pcm', raw, t))
    def stop(self, t):
        self.q.put(('stop', None, t))
    def _decode(self, data):
        import hashlib
        h = hashlib.md5(data).hexdigest(); raw = os.path.join(self.dir, h + '.raw'); wav = os.path.join(self.dir, h + '.wav')
        if os.path.isfile(raw) and os.path.isfile(wav): return raw, wav
        ff = ffmpeg_path()
        if not ff: self.error = 'ffmpeg not found'; return None, None
        os.makedirs(self.dir, exist_ok=True)
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        r = subprocess.run([ff, '-v', 'error', '-y', '-i', 'pipe:0', '-f', 's16le', '-ar', str(self.RATE), '-ac', '2', raw + '.tmp'],
                           input=data, capture_output=True, creationflags=flags)
        if r.returncode: self.error = r.stderr.decode(errors='replace')[-200:]; return None, None
        os.replace(raw + '.tmp', raw)
        pcm = open(raw, 'rb').read()
        with open(wav + '.tmp', 'wb') as f:
            f.write(b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 2, self.RATE, self.RATE * 4, 4, 16)
                    + b'data' + struct.pack('<I', len(pcm)) + pcm)
        os.replace(wav + '.tmp', wav)
        return raw, wav
    def _run(self):
        while True:
            what, data, t = self.q.get()
            try:
                port = self.port_getter()
                synth = getattr(port, 'synth', None) if port is not None else None
                if what == 'stop':
                    if synth is not None and hasattr(synth, 'pcm_stop'): synth.pcm_stop()
                    elif sys.platform == 'win32':
                        import winsound; winsound.PlaySound(None, 0)
                    continue
                if what == 'pcm':
                    self.played.append((t, 'pcm', len(data))); del self.played[:-20]
                    if synth is not None and hasattr(synth, 'pcm'): synth.pcm(t, data)
                    elif sys.platform == 'win32':
                        import winsound, tempfile
                        wav = os.path.join(tempfile.gettempdir(), 'karaoke_emu_mv.wav')
                        with open(wav, 'wb') as fh:
                            fh.write(b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 2, self.RATE, self.RATE * 4, 4, 16)
                                     + b'data' + struct.pack('<I', len(data)) + data)
                        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                    continue
                raw, wav = self._decode(data)
                if not raw: continue
                self.played.append((t, raw)); del self.played[:-20]
                if synth is not None and hasattr(synth, 'pcm'): synth.pcm(t, open(raw, 'rb').read())
                elif sys.platform == 'win32':
                    import winsound
                    winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            except Exception as ex:
                self.error = str(ex)


class EmuJob(threading.Thread):
    MAIN_LOOP, PLAY_LOOP = 0x8802BFE8, 0x88084774     # startup's main-loop entry / the stream play loop
    AFTER_MAIN = 150_000_000                          # instructions to keep running after the main loop is reached
    AFTER_MAIN_DISC = 10 ** 15                        # with a disc: keep running until Stop (so the remote can be used)

    def __init__(self, fw_path, budget=700_000_000, max_frames=2, assist=True, vdrive=True, disc=None, quickboot=True, on_song=None,
                 realtime_boot=False, midi_port=None, speed='auto', voice=None, key_voice=True, chorus=None,
                 chorus_on=True, random_score=True):
        super().__init__(daemon=True)
        self.voice_out = voice; self.key_voice = key_voice
        self.chorus_out = chorus; self.chorus_on = chorus_on; self.random_score = random_score
        self.realtime_boot = realtime_boot; self.midi_port = midi_port; self.video = LiveVideo()
        self.speed_mode = speed if speed in ('auto', 'realtime', 'instant') else 'auto'; self.ready_at = None
        self.osd_cond = threading.Condition()
        self.on_song = on_song; self.song_reads = []; self.stream = None; self._vd_seen = 0
        self.realtime = True; self.rt_ref = None; self.speed = None
        self.bgv_jpg = None; self.bgv_version = 0; self.bgv_at = None; self._bgv_busy = False; self._bgv_mark = (0, 0.0)
        self.remote = None; self.pending_keys = []; self.video_on = True
        self.quickboot = quickboot; self.quick = 'off' if not quickboot else 'cold boot'; self.state_file = None
        self.fw_path, self.budget, self.max_frames, self.assist = fw_path, budget, max_frames, assist
        self.vdrive, self.disc = vdrive, disc; self.vd = None
        if disc: self.AFTER_MAIN = max(self.AFTER_MAIN, self.AFTER_MAIN_DISC)
        self.main_at = None; self.stops = 0; self.plays = 0; self.fw_state = None; self.panel_hex = ''
        self.drive = {}; self.fw_debug = []
        self.osd_png = None; self.osd_sig = None; self.osd_version = 0; self.osd_regions = []; self.osd_at = None
        self.banner = None                   # (text, until): the emulator's own short message over the picture
        self.state, self.error, self.note = 'starting', None, ''
        self.n, self.uart, self.frames, self.stop_flag = 0, '', [], False
        self.irqs, self.hooks, self.flash_ops = {}, 0, 0
        self.t0 = time.time()

    def run(self):
        try:
            import winperf
            self.perf = '%s / thread %s' % (winperf.process('above'), winperf.thread('highest'))
            image, how = load_firmware_image(self.fw_path)
            self.note = 'firmware %s (%d bytes)' % (how, len(image))
            warn = firmware_warning(image)
            if warn: self.note = warn
            if self.disc:
                # a disc made for another player: that player's firmware cannot read it (the screen stays black)
                import fwprofile
                fam = lambda x: 'ks5' if x == 'ks5' else ('megapro' if x in ('mp100', 'migael') else None)
                dp = (disc_info(self.disc) or {}).get('player'); fp = fwprofile.detect(image)
                if dp and fam(dp) != fam(fp):
                    self.note = ('WARNING: this disc (%s) is for the %s, but the firmware is %s. Pick the matching BIOS '
                                 'in Settings, or the disc will not load.' % (os.path.basename(self.disc),
                                 'Platinum KS5' if dp == 'ks5' else 'Megapro', fwprofile.NAMES.get(fp, fp)))
            # the newer Sunplus players ('026.' builds) use memory up to phys 0x0BFFFFFF behind their flash
            e = Emu(image, ram_mb=64 if __import__("fwprofile").detect(image) in __import__("fwprofile").NEW_GEN else 32); u = soc.attach(e, echo=False); self.emu_ref = e
            # the player's settings (EEPROM): kept between runs, so Setup changes (sound, greetings, ...) stay
            pages = load_eeprom(); e.i2c.load(pages)
            self._eeprom_digest = __import__('hashlib').sha1(repr(sorted(pages.items())).encode()).hexdigest()[:8]
            def _ee_changed():
                self._eeprom_dirty = True
            e.i2c.on_write = _ee_changed
            if e.flash.nv is not None:                      # KS5: its Setup settings live in the flash instead
                secs = load_flash(); e.flash.load(secs)
                self._eeprom_digest += __import__('hashlib').sha1(repr(sorted(secs.items())).encode()).hexdigest()[:8]
                def _fl_changed():
                    self._flash_dirty = time.time()
                e.flash.on_write = _fl_changed
            irq = soc.IRQC(e, soc.DEFAULT_SOURCES); fifo = soc.VideoFIFO(e); self._fifo = fifo
            self._image, self._u, self._irq = image, u, irq
            self.remote = soc.IRRemote(e)
            self._boot_setup()
            self.set_midi(self.midi_port)
            e.voice.enabled = bool(self.key_voice)
            e.voice.sink = self.voice_out.push if self.voice_out is not None else None
            e.chorus.enabled = bool(self.chorus_on); e.score.enabled = bool(self.random_score)
            if self.chorus_out is not None:
                if hasattr(e, 'ks5_ring'):
                    e.ks5_audio = StreamPCM(self.chorus_out.port_getter)   # KS5 MP3 audio
                    e.ks5_audio.prepare()
                e.chorus.sink = self.chorus_out.push; e.chorus.stop_sink = self.chorus_out.stop
                self.video.mv_audio = self.chorus_out.push_pcm; self.video.mv_stop = lambda: self.chorus_out.stop(None)
            for k in self.pending_keys: self.remote.press(k)
            self.pending_keys = []
            if self.vdrive:
                self.vd = soc.VirtualDrive(e)
                if self.disc:
                    self.vd.attach_disc(self.disc)
                    self.stream = soc.StreamDMA(e, self.vd)        # background-video streaming (READ CD on DVD media)
            patches = {}; self.state = 'running'; seen = 0; fp_seen = 0
            self.MAIN_LOOP, self.PLAY_LOOP = soc.T(EmuJob.MAIN_LOOP), soc.T(EmuJob.PLAY_LOOP)   # this firmware build's
            e.bp |= {x for x in (self.MAIN_LOOP, self.PLAY_LOOP) if x}       # add to (do not replace) the hooks soc installed
            # the quick-boot state holds the settings read at startup: a different EEPROM needs a cold boot
            qkey = snapshot.key_for(image) + self._eeprom_digest + ("" if soc.IPS == 108e6 else "@%d" % (soc.IPS // 1e6)); self._qkey = qkey
            if self.quickboot and self.assist:
                self.state_file = quickboot_path(self.fw_path)
                # 'real time from power-on' shows the whole startup, so it does not resume (the state it saves at the main
                # loop is the same either way: pacing only waits between steps, it does not change the emulation)
                extra = snapshot.load(self.state_file, qkey, e, u, irq, fifo) if (self.state_file and not self.realtime_boot) else None
                if extra is not None:
                    self.main_at = e.n; self.stops = extra.get('stops', 0); self.plays = extra.get('plays', 0)
                    self.quick = 'resumed from saved state (%.2f s skipped)' % (e.n / INSTR_PER_SEC)
                    self.budget += e.n                          # the budget counts from where the saved state starts
                    self.uart = bytes(u.out).decode('latin1').replace('\r', '')
            def finished():
                if self.stop_flag: return True
                if self.assist: return self.main_at is not None and e.n >= self.main_at + _I(self.AFTER_MAIN)
                return len(self.frames) >= self.max_frames
            self._tm = [0.0] * 5
            while e.n < self.budget and not finished():
                try:
                    step = 50_000 if (self.assist and self.main_at is None and e.n > 200_000_000) else _I(2_000_000)
                    if self.remote.queue or self.remote.inject_q: step = _I(500_000)      # keep key presses snappy
                    elif self.midi_port is not None and self.main_at is not None: step = _I(1_000_000)   # finer MIDI timing
                    sp = getattr(e, 'song_player', None)
                    if sp is not None and (sp.playing or e.ks5_ring.playing): step = min(step, _I(1_000_000))    # KS5 song: ~10 ms steps
                    _w0 = time.perf_counter(); _c0 = time.thread_time()
                    e.run(min(step, self.budget - e.n))
                    if sp is not None: sp.pump(e)
                    _w1 = time.perf_counter(); _c1 = time.thread_time()
                    self._pace(e)
                    _w2 = time.perf_counter()
                    tm = self._tm; tm[0] += _w1 - _w0; tm[1] += _c1 - _c0; tm[2] += _w2 - _w1; self._tm_mark = _w2
                except Stop as s:
                    why = str(s)
                    if not why.startswith('spin'):
                        self.error = 'CPU stopped: ' + why; break
                    r = run3.analyze_poll(e)
                    if not r:
                        self.error = 'unresolved polling loop at %08x' % e.cur_pc; break
                    p, mask, clear = r; cm, sm = patches.get(p & ~3, (0, 0))
                    if clear: cm |= mask; sm &= ~mask
                    else: sm |= mask; cm &= ~mask
                    patches[p & ~3] = (cm, sm); e.read_hooks[p & ~3] = run3.forced_hook(p & ~3, cm, sm); e.ring.clear()
                if self.assist: self._assist(e, fifo)
                _w3 = time.perf_counter(); _c3 = time.thread_time()
                self._peek(e)
                self._osd(e); self.video_on = soc.video_plane_on(e)
                self._watch_reads(e); self._bgv(e, fifo); self._sync(e); self._diag(e)
                if self.video.available():
                    if self.stream is not None and self.disc and os.path.isfile(self.disc) and not os.environ.get('KE_VIDEO_TAP'):
                        self.video.follow(self.stream, self.disc, e.n / INSTR_PER_SEC)
                        cf_ = getattr(self.video, 'cur_file', None)
                        if cf_ is not None: e.mv_song = cf_[2].lower().endswith(('.mvv', '.avi'))     # the song is a music video: no score
                    else: self.video.feed(fifo)
                sc_ = getattr(e, 'score', None)
                if sc_ is not None and getattr(sc_, 'count', 0) != getattr(self, '_score_seen', 0):
                    self._score_seen = sc_.count
                    _log_line('score: %s%s' % (getattr(sc_, 'last', None), ' (music video: no score)' if getattr(e, 'mv_song', False) else ''))
                self.remote.poll(e)
                self._song_watch(e)
                if self._boot_keys is not None: self._boot_step(e)
                self.n = e.n; self.irqs = dict(irq.delivered); self.hooks = len(patches); self.flash_ops = len(e.flash.log)
                self.uart = bytes(u.out).decode('latin1').replace('\r', '')
                while seen < len(fifo.frames):
                    when, m2v = fifo.frames[seen]; seen += 1
                    # a picture from the firmware's memory once it is running (Megapro: the SCORE background with
                    # its three cards, played from flash after a song): shown instead of the background video
                    if (self.main_at is not None and when > self.main_at and len(m2v) < 1 << 20 and self.video.available()
                            and not getattr(e, 'fw_pictures', None) and e.ram.find(m2v[:256], 0, 0x400000) >= 0):
                        self.video.show_still(m2v, self.stream)
                    if len(m2v) > 1 << 20: continue                                 # background video, not a picture
                    if any(f['m2v'] == m2v for f in self.frames): continue          # same picture pushed again
                    fr_ = dict(index=len(self.frames), bytes=len(m2v), at_instr=when,
                               at_seconds=round(when / INSTR_PER_SEC, 2), m2v=m2v, png=None)
                    self.frames.append(fr_)
                    # the PNG (an ffmpeg run) is made in the background: on the emulator thread it held the player
                    # up for a moment at every picture (the SCORE screen came late on slow PCs)
                    # (the power-on splash is made at once, so it shows on time)
                    if self.assist and self.main_at is not None:
                        threading.Thread(target=lambda f=fr_: f.__setitem__('png', m2v_to_png(f['m2v'])), daemon=True).start()
                    else: fr_['png'] = m2v_to_png(m2v)
                fp = getattr(e, 'fw_pictures', None)
                while fp is not None and fp_seen < len(fp.list):
                    when, m2v = fp.list[fp_seen]; fp_seen += 1        # SCORE picture, logo: from the firmware's call
                    if self.main_at is not None and when > self.main_at and self.video.available():
                        self.video.show_still(m2v, self.stream)
                tm = self._tm; tm[3] += time.perf_counter() - _w3; tm[4] += time.thread_time() - _c3
            if self.error: self.state = 'error'
            else: self.state = 'stopped' if self.stop_flag else 'done'
        except Exception as ex:
            self.error = '%s: %s' % (type(ex).__name__, ex); self.state = 'error'
            self.trace = traceback.format_exc()
            try:
                p = log_path()
                if p:
                    with open(p, 'a', encoding='utf-8') as f:
                        f.write('%s ERROR %s\n%s\n' % (time.strftime('%H:%M:%S'), self.error, self.trace))
            except Exception:
                pass
        finally:
            try:
                if getattr(self, '_eeprom_dirty', False) and self.emu_ref is not None: save_eeprom(self.emu_ref.i2c.dump())
                if getattr(self, '_flash_dirty', 0) and self.emu_ref is not None: save_flash(self.emu_ref.flash.dump())
            except Exception:
                pass
            self.video.close()
            ka_ = getattr(self.emu_ref, 'ks5_audio', None) if self.emu_ref is not None else None
            if ka_ is not None:
                try: ka_.close()
                except Exception: pass
            # powered off: the screen goes dark (no last picture left on it)
            self._off = True                                # pictures still on a delay timer are dropped
            self.osd_png = None; self.osd_version += 1
            try:
                with self.osd_cond: self.osd_cond.notify_all()
            except Exception: pass
            if self.midi_port is not None:
                try: self.midi_port.silence()
                except Exception: pass

    def set_midi(self, port):
        """Send the firmware's MIDI output live to a midiout.Port (or stop with None)."""
        self.midi_port = port
        e = getattr(self, 'emu_ref', None)
        if e is not None:
            e.midi.sink = midiout.StreamParser(port.send).feed if port is not None else None
            if port is not None: port.silence()

    def _assist(self, e, fifo):
        """EMULATION ASSIST (not reverse-engineered hardware): the real event that ends each splash play is unknown.
        We request a stop (flag bit0, which the firmware's own field handler turns into bit1) shortly after the
        play's picture has been completely sent, until the firmware reaches its main loop."""
        if self.main_at is not None:
            # a picture the firmware shows from flash once running (Megapro: the SCORE background after a song)
            # plays through the same stream loop and waits for the same stop: without it the firmware stayed in
            # that loop after a song that ended by itself (no fanfare, no return) until a key was pressed
            fp = getattr(e, 'fw_pictures', None)
            if fp is not None and fp.list and e.n - fp.list[-1][0] < _I(1_080_000_000):      # within 10 s of a call
                done = self.__dict__.setdefault('_pic_plays', set())
                calls = [w for w, _ in fp.list[-4:]]
                gp = soc.fw_gp(e); flag = (gp + (soc.G(2716) or 2716)) & 0x1FFFFFFF
                for m, pc in e.bp_hits[-64:]:
                    if pc != self.PLAY_LOOP or m in done or not any(w <= m <= w + _I(540_000_000) for w in calls): continue
                    if e.n >= m + _I(27_000_000) and gp:                          # 0.25 s: the picture is sent
                        if not (e.low[flag] & 3): e.low[flag] |= 1; done.add(m)
            return
        if any(pc == self.MAIN_LOOP for _, pc in e.bp_hits):
            self.main_at = e.n
            if self.quickboot and self.state_file:
                try:
                    snapshot.save(self.state_file, self._qkey, e, self._u, self._irq, fifo,
                                  dict(stops=self.stops, plays=self.plays))
                    self.quick = 'cold boot (state saved: next boot skips to here)'
                except Exception as ex:
                    self.quick = 'cold boot (could not save state: %s)' % ex
            return
        entries = [m for m, pc in e.bp_hits if pc == self.PLAY_LOOP]
        self.plays = len(entries)
        if len(entries) > self.stops:
            done = [w for w, _ in fifo.frames if w >= entries[self.stops]]
            gp = soc.fw_gp(e); flag = (gp + (soc.G(2716) or 2716)) & 0x1FFFFFFF
            if done and e.n >= done[0] + _I(100_000) and not (e.low[flag] & 3) and gp:
                e.low[flag] |= 1; self.stops += 1

    def _peek(self, e):
        gp = soc.fw_gp(e)
        if gp:
            G = lambda off: e.low[(gp + off) & 0x1FFFFFFF]
            self.fw_state = G(2734)
            self.drive = dict(state=G(3333), step=G(429), command=G(397), result=G(3498), tray=('closed' if e.tray.state == 'closed' else e.tray.state))
        self.panel_hex = bytes(e.low[0x6AD0:0x6AD0 + 16]).hex(' ')
        self.fw_debug = [dict(t=round(n / INSTR_PER_SEC, 3), msg=m.rstrip('\n')) for n, _, m in e.dbg.log[-40:]]
        self.fw_debug_total = e.dbg.count

    OSD_FPS = 60
    def _osd(self, e):
        """Re-render the firmware's OSD layer (text, menus, logos) whenever its display list or bitmaps change.
        The emulator thread only notices the change (a cheap signature); drawing and PNG encoding run in a worker
        thread, up to 30 times a second (they took ~15 ms each on the emulator thread, ~20% of its time in a song)."""
        now = time.time()
        fps = self.OSD_FPS
        sp = getattr(self, 'speed', None)
        if isinstance(sp, (int, float)) and 0 < sp < 0.95 and self.realtime: fps = min(fps, 20)   # a slow PC: the music first
        if now - getattr(self, '_osd_t', 0) < 1.0 / fps: return
        try: sig = soc.osd_signature(e)
        except Exception: return
        if sig == self.osd_sig: return
        if getattr(self, '_osd_busy', False): return
        self._osd_t = now; self._osd_busy = True
        threading.Thread(target=self._osd_draw, args=(e, sig), daemon=True).start()

    def _osd_draw(self, e, sig):
        try:
            rgba, regs = soc.osd_render(e)
            png = soc.png_rgba(rgba, soc.OSD_W, soc.OSD_H); self.osd_sig = sig
            d = self.LYRICS_DELAY
            if soc.FW_ID == 'ks5':
                # the KS5's music is played by the emulator itself on the same clock as the lyrics: only the sound
                # card's own delay is left to wait for (the Megapro setting compensates for its MIDI timing)
                d = min(d, self.KS5_LYRICS_DELAY)
                rg = getattr(e, 'ks5_ring', None)
                if rg is not None and rg.playing and rg.song: d += StreamPCM.DELAY     # an MP3 song: its sound is that much later
            ln = getattr(getattr(e, 'midi', None), 'last_note', None)
            if ln is None or e.n - ln > 1.5 * INSTR_PER_SEC:
                d = 0          # no song playing: menus and the typed number show at once (the remote felt slow)
            if d > 0:
                # the sound reaches the speakers ~0.1 s after the firmware sends it (synth buffer + Windows audio);
                # the picture would be on screen almost at once, so lyric wipes ran ahead of the music
                self._osd_seq = getattr(self, '_osd_seq', 0) + 1; seq = self._osd_seq
                t = threading.Timer(d, self._osd_publish, args=(png, seq, regs, e.n)); t.daemon = True; t.start()
                return
            # shown now: pictures still waiting on a delay timer are older and must not replace it afterwards
            # (typing a number during a song showed the number, then an older picture over it)
            self._osd_seq = getattr(self, '_osd_seq', 0) + 1
            self._osd_publish(png, self._osd_seq, regs, e.n)
        except Exception as ex:
            self.osd_regions = [dict(error=str(ex))]
        finally:
            self._osd_busy = False

    LYRICS_DELAY = 0.25
    KS5_LYRICS_DELAY = 0.08

    def _osd_publish(self, png, seq, regs, n):
        if getattr(self, '_off', False): return              # powered off: the screen stays dark
        if seq is not None and seq < getattr(self, '_osd_pub', 0): return     # a newer picture is already out
        if seq is not None: self._osd_pub = seq
        try:
            self.osd_png = png; self.osd_version += 1
            e = self.emu_ref
            self.osd_history = (getattr(self, 'osd_history', []) + [(round(n / INSTR_PER_SEC, 2), png)])[-40:]
            self.osd_at = round(n / INSTR_PER_SEC, 2)
            self.osd_regions = [dict(x=r['x'], y=r['y'], w=r['width'], h=r['height'], bpp=r['bpp']) for r in regs]
            with self.osd_cond: self.osd_cond.notify_all()
        except Exception as ex:
            self.osd_regions = [dict(error=str(ex))]

    def _pace(self, e):
        """Real-time pace once the firmware is in its main loop (the C core runs faster than the real player): songs,
        lyric wipes, key timeouts and the video then run at their true speed. Startup stays as fast as possible,
        unless 'real time from power-on' is on."""
        mode = self.speed_mode
        if not self.realtime_boot:
            if mode == 'instant' or self.main_at is None or not self.realtime:
                self.rt_ref = None; self.speed = None; return
            if mode == 'auto' and not self._ready(e):
                self.rt_ref = None; self.speed = None; return
        now = time.time()
        if self.rt_ref is None: self.rt_ref = (now, e.n)
        t0, n0 = self.rt_ref
        lead = (e.n - n0) / INSTR_PER_SEC - (now - t0)
        if now - t0 > 0.2: self.speed = round(((e.n - n0) / INSTR_PER_SEC) / (now - t0), 2)
        if lead > 0.005: time.sleep(min(lead, 0.25))
        elif lead < -0.5: self.rt_ref = (now, e.n)            # running behind (slow PC): don't try to catch up

    def _ready(self, e):
        """'Fast start': true once the player waits for input - karaoke number entry open (0x80012AB8 == 0) or, without
        a karaoke disc, 8 emulated seconds after the main loop. From then on the player runs in real time."""
        if self.ready_at is not None: return True
        if self.main_at is not None and self.remote is not None and (self.remote.sent or self.remote.queue):
            self.ready_at = e.n; return True              # the user is already pressing keys: real time from now
        if self.main_at is not None and soc.FW_ID == 'ks5':
            self.ready_at = e.n; return True      # other players (KS5): real time from the main loop on, so the
                                                  # disc loading, background video and new-song list run at true speed
        try:
            if self.disc and e.rd32(0x80012AB8) == 0 and e.n > (self.main_at or 0) + 0.5 * INSTR_PER_SEC: self.ready_at = e.n
        except Exception:
            pass
        if self.main_at is not None and e.n > self.main_at + (25 if self.disc else 8) * INSTR_PER_SEC: self.ready_at = e.n
        return self.ready_at is not None

    _tm = None
    def _diag(self, e):
        """Every 5 s a line in KaraokeEmu.log (next to the settings): how fast the emulator runs and what it does.
        It is how a slow or stuck run on another computer can be looked at afterwards."""
        if getattr(self, '_eeprom_dirty', False):          # Setup changed a setting: keep it for the next run
            self._eeprom_dirty = False
            threading.Thread(target=save_eeprom, args=(e.i2c.dump(),), daemon=True).start()
        fd = getattr(self, '_flash_dirty', 0)
        if fd and time.time() - fd > 1.0:                  # KS5 Setup saved (a burst of flash writes): keep it
            self._flash_dirty = 0
            save_flash(e.flash.dump())
        now = time.time()
        if now - getattr(self, '_diag_t', 0) < 5.0: return
        prev = getattr(self, '_diag_prev', None); self._diag_t = now; self._diag_prev = (now, e.n)
        tcpu = time.thread_time(); pcpu = getattr(self, '_diag_cpu', None); self._diag_cpu = tcpu
        if prev is None: return
        try:
            speed = (e.n - prev[1]) / INSTR_PER_SEC / max(1e-6, now - prev[0])
            st = self.stream
            line = '%s emu=%.1fs speed=%.2fx mode=%s ready=%d song=%s reads/s=%d stream_lba=%s video=%d frames midi=%d bytes keys_waiting=%d main_loop=%s emu_thread_cpu=%d%%\n' % (
                time.strftime('%H:%M:%S'), e.n / INSTR_PER_SEC, speed, self.speed_mode, self.ready_at is not None,
                'on' if self.song_clock() is not None else 'off',
                (sum(1 for n, *_ in st.log if n > e.n - INSTR_PER_SEC) if st else 0), (st.lba if st else None),
                self.video.version, e.midi.total, len(self.remote.queue) + len(self.remote.inject_q) if self.remote else 0,
                'yes' if self.main_at else 'no', round(100 * (tcpu - (pcpu or tcpu)) / max(1e-6, now - prev[0])))
            # where the emulator thread's time went (wall %): running the CPU core (and how much of that it really
            # got the processor), waiting for real time, the rest of the loop (OSD, video, keys). A slow PC shows
            # run% high with run_cpu% low (the thread waited: other programs, or other threads of this program)
            tm = self._tm or [0.0] * 5; wall = max(1e-6, now - prev[0])
            line = line.rstrip('\n') + ' run=%d%% run_cpu=%d%% pace_sleep=%d%% other=%d%% other_cpu=%d%%\n' % tuple(
                round(100 * x / wall) for x in (tm[0], tm[1], tm[2], tm[3], tm[4]))
            self._tm = [0.0] * 5
            # the whole program's CPU (all its threads) and the whole computer's: run_cpu far below run with the
            # PC near 100% = other programs take the processor; with the PC idle = this program's other threads
            pc_ = time.process_time(); pp_ = getattr(self, '_diag_pcpu', None); self._diag_pcpu = pc_
            st_ = _sys_cpu_times(); sp_ = getattr(self, '_diag_sys', None); self._diag_sys = st_
            busy = round(100 * (1 - (st_[0] - sp_[0]) / max(1, st_[1] - sp_[1]))) if (st_ and sp_) else -1
            line = line.rstrip('\n') + ' app_cpu=%d%% pc_busy=%d%%\n' % (round(100 * (pc_ - (pp_ or pc_)) / wall), busy)
            vd = self.video     # the background video: pictures decoded / sent to the window, longest gaps (stutter)
            line = line.rstrip('\n') + ' vid_dec=%d/%dms vid_sent=%d/%dms jpg=%dKB\n' % (
                getattr(vd, 'gap_n', 0), round(1000 * getattr(vd, 'gap_max', 0.0)), getattr(vd, 'sent_n', 0),
                round(1000 * getattr(vd, 'sent_gap_max', 0.0)), getattr(vd, 'jpg_kb', 0))
            vd.gap_n = 0; vd.gap_max = 0.0; vd.sent_n = 0; vd.sent_gap_max = 0.0
            ka = getattr(e, 'ks5_audio', None); rg = getattr(e, 'ks5_ring', None)
            if rg is not None:          # KS5 sound from the ring (spoken numbers, MP3 songs): where it stops, if it does
                line = line.rstrip('\n') + ' ks5_audio=%s plays=%d fed=%d decoded=%d err=%s\n' % (
                    'none' if ka is None else ('ok' if not rg.no_out else 'failed'), rg.plays,
                    getattr(ka, 'fed', 0), getattr(ka, 'decoded', 0), getattr(ka, 'error', None))
            lp = log_path()
            if not lp: return
            if os.path.exists(lp) and os.path.getsize(lp) > 2 << 20:
                with open(lp, 'rb') as f: f.seek(-(1 << 20), 2); keep = f.read()
                with open(lp, 'wb') as f: f.write(keep)
            with open(lp, 'a', encoding='utf-8') as f:
                if not getattr(self, '_diag_head', False):
                    import platform
                    # machine: what the program runs as - on a PC Android emulator "aarch64" means the ARM build is
                    # being translated (several times slower) instead of the x86_64 one running
                    f.write('--- Project Chiaki %s boot %s firmware=%s disc=%s cpu=%s cores=%s machine=%s sound=%s windows=%s\n' % (
                        VERSION, time.strftime('%Y-%m-%d %H:%M:%S'), os.path.basename(getattr(self, 'fw_path', '') or ''), os.path.basename(self.disc or ''),
                        type(e).__name__, os.cpu_count(), platform.machine(), self.midi_port.name if self.midi_port is not None else 'none', getattr(self, 'perf', '')))
                    self._diag_head = True
                f.write(line)
        except Exception:
            if __import__('os').environ.get('KE_DIAGDBG'): traceback.print_exc()

    def _watch_reads(self, e):
        """Song starts: the firmware reads a song record from the song bank with READ(10)/READ(12)."""
        vd = self.vd
        if vd is None: return
        total = getattr(vd, 'log_count', len(vd.log))
        k = min(total - self._vd_seen, len(vd.log)); self._vd_seen = total
        new = vd.log[len(vd.log) - k:] if k > 0 else []
        for n, pkt, res, _, _ in new:
            b = bytes.fromhex(pkt)
            if b[0] in (0x28, 0xA8) and res == 'ok':
                lba = int.from_bytes(b[2:6], 'big')
                self.song_reads.append((n, lba)); del self.song_reads[:-50]
                if self.on_song:                          # off the emulator thread: reading/decoding takes a moment
                    threading.Thread(target=self._on_song_safe, args=(lba, n), daemon=True).start()

    def _on_song_safe(self, lba, n):
        try: self.on_song(lba, n)
        except Exception as ex:
            self.song_error = '%s: %s' % (type(ex).__name__, ex)

    def _bgv(self, e, fifo):
        """Background video: decode the newest part of the MPEG stream the firmware feeds its decoder (ffmpeg, in a
        worker thread) and keep the last picture."""
        if self._bgv_busy or not self.video_on: return
        if self.video.available() and self.disc and os.path.isfile(self.disc): return   # the live picture covers it
        total = getattr(self.stream, 'total', 0) if self.stream else 0
        last_total, last_t = self._bgv_mark
        if total - last_total < 150_000 or time.time() - last_t < 2.0: return
        ff = ffmpeg_path()
        if not ff: return
        tail = bytes(fifo.buf[-700_000:]); i = tail.find(b'\x00\x00\x01\xba')
        if i < 0: return
        self._bgv_mark = (total, time.time()); self._bgv_busy = True; at = round(e.n / INSTR_PER_SEC, 2)
        def work():
            try:
                p = subprocess.run([ff, '-v', 'error', '-f', 'mpeg', '-i', 'pipe:0', '-an', '-vsync', '0', '-q:v', '4',
                                    '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'], input=tail[i:], capture_output=True, timeout=60)
                jp = p.stdout; k = jp.rfind(b'\xff\xd8\xff')
                if k >= 0 and len(jp) - k > 1000:
                    self.bgv_jpg = jp[k:]; self.bgv_version += 1; self.bgv_at = at
            except Exception:
                pass
            finally:
                self._bgv_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _sync(self, e):
        sy = getattr(self, 'sync', None)
        if not sy or sy.get('done'): return
        for n, msg in e.midi.events(sy['after']):
            if (msg[0] & 0xF0) == 0x90 and len(msg) > 2 and msg[2]:
                ch, note = msg[0] & 15, msg[1]
                for t, c2, n2 in sy['notes']:
                    if c2 == ch and n2 == note:
                        self.song_t0 = round(n / INSTR_PER_SEC - t, 3); break
                sy['done'] = True; return
        if e.n - sy['after'] > 60 * INSTR_PER_SEC: sy['done'] = True

    def song_clock(self):
        """Emulated time (s) at which the firmware's current song started: its first note-on minus the first note-on
        of the song file (set by the app), so the app's own playback can be lined up with the firmware."""
        return getattr(self, 'song_t0', None)

    def fw_midi(self):
        e = getattr(self, 'emu_ref', None)
        return e.midi.to_midi() if e is not None and hasattr(e, 'midi') else None

    _boot_keys = None
    def _boot_setup(self):
        """Keys a player gets once after it has started (fwprofile boot_keys: (keys, first second, gap seconds)).
        The Remix K-8005 starts in Repeat 'Random': with nothing reserved it plays random songs on its own after
        ~10 s and Stop only moves to the next one. Repeat (key 33) three times sets 'None' (it is not saved by the
        player, so this is done at every start); the Repeat key can set Random or Seq again."""
        try:
            bk = __import__('fwprofile').extra(soc.FW_ID).get('boot_keys')
            if bk and not getattr(self, 'app_settings', {}).get('keep_player_repeat'):
                keys, at, gap = bk; self._boot_keys = list(keys); self._boot_at = at; self._boot_gap = gap
        except Exception: self._boot_keys = None
    on_song_end = None                                  # set by the app: puts the synth's Melody and M/F back
    def _song_watch(self, e):
        """Melody, M/F and tempo go back to the song's own after every song.
        Megapro builds: the firmware's own settings (soc.PlayerKeys): its key is brought back to 0 with Key presses
        once the next song plays (the firmware takes them only then). Other players (melody / M/F done by the
        built-in synth): when a song ends, on_song_end() resets the synth's melody and transposition."""
        dp = getattr(e, 'dsp_poll', None)
        if dp is not None: dp(e)                # (KS5: Stop or Key press - see soc.dsp_poll)
        if not getattr(self, '_fw_ctl_init', False) and self.midi_port is not None:
            self._fw_ctl_init = True; self._fk_sent = 0; self._fm_sent = True     # (the synth outlives a restart)
            try: self.midi_port.control(fw_key=0, fw_melody=True)
            except Exception: pass
        fk = getattr(e, 'fw_key', 0)
        if fk != getattr(self, '_fk_sent', 0) and self.midi_port is not None:
            # the player's Key setting where its DSP plays the song (KS5): the synth transposes the MIDI and shifts
            # the recorded music
            self._fk_sent = fk
            try: self.midi_port.control(fw_key=fk)
            except Exception: pass
        fm = getattr(e, 'fw_melody', True)
        if fm != getattr(self, '_fm_sent', True) and self.midi_port is not None:
            self._fm_sent = fm                  # (KS5) its Melody setting: the synth plays / leaves out the melody
            try: self.midi_port.control(fw_melody=fm)
            except Exception: pass
        pk = getattr(e, 'player_keys', None)
        if pk is not None and pk.on:
            if pk.key_reset and pk.in_song():
                pk.key_reset = False
                for k in pk.presses_to(0): self.press(k)
            return
        st = getattr(self, '_sw', None)
        if st is None: st = self._sw = dict(total=0, note_at=0, active=False, since=0, idle_at=0)
        tot = getattr(e.midi, 'total', 0)
        if tot != st['total']: st['total'] = tot; st['note_at'] = e.n
        sp = getattr(e, 'song_player', None); ring = getattr(e, 'ks5_ring', None)
        active = e.n - st['note_at'] < 5 * INSTR_PER_SEC
        if sp is not None and getattr(sp, 'playing', False): active = True
        if ring is not None and getattr(ring, 'playing', False): active = True
        if soc.FW_ID == 'ks5':
            try: active = active or e.rd8(soc.FW_GP + 4120) == 2
            except Exception: pass
        if active:
            if not st['active']: st['active'] = True; st['since'] = e.n
            st['idle_at'] = 0
        elif st['active']:
            if not st['idle_at']: st['idle_at'] = e.n
            elif e.n - st['idle_at'] > 2 * INSTR_PER_SEC:   # (not just the gap between two songs' parts)
                st['active'] = False
                if e.n - st['since'] > 8 * INSTR_PER_SEC and self.on_song_end:
                    try: self.on_song_end()
                    except Exception: pass
    def _boot_step(self, e):
        if not self._boot_keys: self._boot_keys = None; return
        if e.n < self._boot_at * INSTR_PER_SEC: return
        self.remote.press(self._boot_keys.pop(0)); self._boot_at = e.n / INSTR_PER_SEC + self._boot_gap

    SETUP_OPEN = 1888                                  # gp offset: non-zero while the Setup menu is shown
    def press(self, key):
        """Queue a remote-control / front-panel key (firmware key number)."""
        key = int(key)
        e = getattr(self, 'emu_ref', None)
        if key == 109 and e is not None and soc.fw_gp(e):
            # in the Setup menu the firmware takes key 72 as Enter/Select (109 only counts presses there), so the
            # remote's Enter / OK opens items such as Option > Greetings
            try:
                if e.rd32(soc.FW_GP + self.SETUP_OPEN): key = 72
            except Exception: pass
        if key == 114:
            # Change video: a player whose firmware has its own background-video step (KS5) runs that routine, so
            # the player picks the next video group (movie.idx) and shows its own "BGV: n" message
            bc = __import__('fwprofile').extra(soc.FW_ID).get('bgv_call')
            if bc and self.remote is not None and getattr(self.remote, 'direct', False):
                try: self.video.custom = None
                except Exception: pass
                self.remote.press(('call', bc)); return
        if self.remote is None: self.pending_keys.append(int(key))
        else: self.remote.press(int(key))

    def _vd_status(self):
        vd = self.vd
        if vd is None: return dict(enabled=False)
        import collections
        ops = collections.Counter(p.split()[0] for _, p, _, _, _ in vd.log)
        return dict(enabled=True, disc=os.path.basename(vd.disc) if vd.disc else None, packets=len(vd.log),
                    sectors_read=getattr(vd, 'reads', 0), opcodes=dict(ops),
                    unknown={('%02x' % k): v for k, v in getattr(vd, 'unknown', {}).items()},
                    last=[dict(t=round(n / INSTR_PER_SEC, 3), pkt=p, res=r) for n, p, r, _, _ in vd.log[-12:]])

    def status(self):
        return dict(state=self.state, error=self.error, note=self.note, instructions=self.n,
                    emulated_seconds=round(self.n / INSTR_PER_SEC, 2), wall_seconds=round(time.time() - self.t0, 1),
                    main_loop_reached=self.main_at is not None, main_at_seconds=(round(self.main_at / INSTR_PER_SEC, 2) if self.main_at else None),
                    fw_state=self.fw_state, panel_hex=self.panel_hex, drive=self.drive, vdrive=self._vd_status(), fw_debug=self.fw_debug, fw_debug_total=getattr(self, 'fw_debug_total', 0), plays=self.plays, stop_requests=self.stops, assist=self.assist,
                    budget=self.budget, uart=self.uart[-4000:], irqs=self.irqs, auto_hooks=self.hooks, flash_ops=self.flash_ops,
                    frames=[dict(index=f['index'], bytes=f['bytes'], at_seconds=f['at_seconds'], has_png=f['png'] is not None)
                            for f in self.frames], ffmpeg=bool(ffmpeg_path()),
                    video_on=self.video_on, bgv=dict(version=self.bgv_version, at_seconds=self.bgv_at,
                    streaming=bool(self.stream and self.stream.lba is not None), lba=(self.stream.lba if self.stream else None),
                    bytes=(self.stream.total if self.stream else 0),
                    reads_per_s=(sum(1 for n, *_ in self.stream.log if n > self.n - INSTR_PER_SEC) if self.stream else 0),
                    file=((getattr(self.video, 'cur_file', None) or (0, 0, ''))[2])),
                    fw_midi=dict(bytes=(self.emu_ref.midi.total if getattr(self, 'emu_ref', None) is not None else 0)),
                    cpu=('C core' if type(getattr(self, 'emu_ref', None)).__name__ == 'FastEmu' else 'Python core (slow: kecpu library not found)'),
                    last_note_seconds=(round(self.emu_ref.midi.last_note / INSTR_PER_SEC, 2) if getattr(self, 'emu_ref', None) is not None and self.emu_ref.midi.last_note else None),
                    realtime=self.realtime, speed=self.speed, speed_mode=self.speed_mode, ready=self.ready_at is not None, song_clock=self.song_clock(), realtime_boot=self.realtime_boot,
                    video_live=self.video.available(), video_frames=self.video.version,
                    video_dropped=getattr(self.video, 'dropped', 0), video_restarts=self.video.restarts,
                    midi_out=(self.midi_port.name if self.midi_port is not None else None),
                    quickboot=self.quick, fw_id=soc.FW_ID, keys_sent=[dict(t=round(n / INSTR_PER_SEC, 2), key=k, how=('remote' if isinstance(r, int) else r or 'none')) for n, k, r in (self.remote.sent[-12:] if self.remote else [])], osd=dict(version=self.osd_version, at_seconds=self.osd_at, regions=self.osd_regions))


def _render_job(mid, sf_path):
    """Runs in the render process: the song as WAV bytes with the SoundFont (cached in that process)."""
    import songbank as sb
    pcm = sb.sf2synth.render(mid, sb.soundfont(sf_path))
    return sb.sf2synth.wav_bytes(pcm), round(len(pcm) / 32000, 1)


_RENDER_POOL = None
def _pool():
    global _RENDER_POOL
    if _RENDER_POOL is None:
        import concurrent.futures, multiprocessing
        if ANDROID:                       # no second process on Android: a thread does the rendering
            _RENDER_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        else:
            _RENDER_POOL = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'))
    return _RENDER_POOL


def _warm_job(sf_path):
    import songbank as sb
    if sf_path: sb.soundfont(sf_path)
    return True


def warm_renderer(sf_path):
    """Start the render process and load the SoundFont there ahead of the first song."""
    try: _pool().submit(_warm_job, sf_path)
    except Exception: pass


def render_song(mid, sf_path):
    """Render in a separate process: in a thread of this process the synthesizer and the emulator take turns on
    Python's lock, which slowed both (a 1-minute song took ~9 s instead of 0.2 s, and the emulator lagged)."""
    global _RENDER_POOL
    try:
        return _pool().submit(_render_job, mid, sf_path).result(timeout=600)
    except Exception:
        _RENDER_POOL = None
        return _render_job(mid, sf_path)                # fall back to rendering here


def note_times(midi):
    """[(seconds, channel, note)] of the note-ons in a MIDI file (tempo map applied)."""
    if songbank is None or not midi: return []
    try:
        ev, tpb = songbank.sf2synth.read_midi(midi)
    except Exception:
        return []
    out = []; tempo = 500000; t = 0.0; last = 0
    for tick, _, ch, kind, a, b in ev:
        t += (tick - last) * tempo / tpb / 1e6; last = tick
        if kind == 'tempo': tempo = a
        elif kind == 144 and b: out.append((t, ch, a))
        if len(out) > 400: break
    return out


# ------------------------------------------------------------------------------------ app state
class App:
    def __init__(self, fw=None, iso=None):
        self.token = secrets.token_hex(16)
        self.phone_token = secrets.token_hex(16); self.phone_pin = '%04d' % secrets.randbelow(10000); self.phone_srv = None
        self.settings = load_settings(); st = self.settings
        # remembered paths are used when nothing is given on the command line and the file still exists
        if not fw and st.get('fw') and os.path.isfile(st['fw']): fw = st['fw']
        if not iso and st.get('iso') and os.path.isfile(st['iso']): iso = st['iso']
        self.fw = find_firmware(fw)
        self.job = None
        self.disc = None; self.disc_path = None
        self.thumb = None
        self.lock = threading.RLock()                # one disc file handle: all reads go through this lock
        self.songs = None; self.player = dict(state='idle'); self.fw_now = None; self._ranges = None
        here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, 'frozen', False) else __file__))
        self.sf_path = songbank.default_soundfont(app_folder('soundfont', False), here, os.getcwd(), os.path.dirname(self.fw) if self.fw else None) if songbank else None
        # sound: 'live' = the firmware's MIDI goes straight to a MIDI device (Windows: GS Wavetable Synth or a
        # SoundFont synth such as VirtualMIDISynth); 'render' = the program renders the song with the SoundFont
        if st.get('soundfont') and os.path.isfile(st['soundfont']): self.sf_path = st['soundfont']
        # all sound is live now: the firmware's MIDI goes straight to a MIDI device. With a SoundFont there is a
        # built-in one ('SoundFont synth', livesynth.py) that plays it in real time - it is the default.
        midiout.set_soundfont(self.sf_path)
        self.voice_out = VoiceOut(lambda: self.port)
        self.chorus_out = ChorusOut(lambda: self.port)
        self.midi_devs = midiout.devices()
        real = [d for d, _ in self.midi_devs if d != 'capture']
        self.midi_dev = real[0] if real else 'capture'; self.sound = 'live'
        self.port = None; self.mplayer = None; self.speed_mode = 'auto'
        if st.get('speed') in ('auto', 'realtime', 'instant'): self.speed_mode = st['speed']
        if st.get('sound') == 'live':
            names = {n: d for d, n in self.midi_devs}
            # (a saved "No sound" counts only when it was picked: the program saved it by itself when it first ran
            # without a SoundFont - the Android app always starts like that - and stayed silent from then on)
            if st.get('device') in names and (names[st['device']] != 'capture' or st.get('capture_chosen')):
                self.midi_dev = names[st['device']]; self.sound = 'live'
        if iso:
            try: self.open_disc(iso)
            except Exception: pass

    def save_settings(self, **kw):
        """Remember paths and choices (KaraokeEmu.settings.json next to the program)."""
        self.settings.update({k: v for k, v in kw.items() if v is not None})
        p = settings_path()
        if not p: return
        try:
            with open(p, 'w', encoding='utf-8') as f: json.dump(self.settings, f, indent=1)
        except OSError:
            pass

    def _keymap(self):
        km = self.settings.get('keymap')
        if not km: return DEFAULT_KEYMAP
        if not self.settings.get('keymap_v2'):             # 0.9.2 sent key 109 (first reserve) for Enter
            km = dict(km)
            for key in ('Enter', ' '):
                if km.get(key) == 109: km[key] = 72
            km.setdefault('r', 109); km.setdefault('Backspace', 74)
            self.settings['keymap'] = km; self.settings['keymap_v2'] = True; self.save_settings()
        return km

    def config(self):
        st = self.settings
        return dict(fw=disp_path(self.fw), iso=disp_path(self.disc_path), soundfont=disp_path(self.sf_path), version=VERSION, disc_dir=disp_path(st.get('disc_dir', '')),
                    speed=self.speed_mode, insert=st.get('insert', True), quickboot=st.get('quickboot', True),
                    assist=st.get('assist', True), realtime_boot=st.get('realtime_boot', False), osd=st.get('osd', True),
                    key_voice=st.get('key_voice', True), chorus=st.get('chorus', True),
                    random_score=st.get('random_score', True), keymap=self._keymap(), mv_delay=st.get('mv_delay', 0.0), lyrics_delay=st.get('lyrics_delay', 250),
                    resolution=st.get('resolution', '720x480'), window_mode=st.get('window_mode', 'windowed'), safe_area=st.get('safe_area', 0), zoom=st.get('zoom', 0), bgv=getattr(self, 'bgv_mode', 'on'), bgv_override=st.get('bgv_override', ''),
                    bgv_dir=disp_path(self.bgv_folder()), bgv_files=self.bgv_list(), default_keymap=DEFAULT_KEYMAP, actions=REMOTE_ACTIONS, settings_file=disp_path(settings_path()))

    def note(self, text, secs=3.0):
        """A short message over the picture (both screens), like the player's own OSD: e.g. after Change BGV."""
        j = self.job
        if not j: return
        j.banner = (text, time.time() + secs)
        with j.osd_cond:
            j.osd_version += 1; j.osd_cond.notify_all()

    def bgv_folder(self):
        d = self.settings.get('bgv_dir') or ''
        return d if d and os.path.isdir(d) else app_folder('bgv')

    def bgv_list(self):
        try:
            d = self.bgv_folder()
            return sorted((f for f in os.listdir(d) if f.lower().endswith(BGV_EXT) and os.path.isfile(os.path.join(d, f))), key=str.lower)
        except OSError:
            return []

    def apply_bgv_override(self):
        """Settings 'bgv_override': '' = the disc's own videos, '*' = every video in the folder in turn, else one file."""
        if not self.job: return
        ov = self.settings.get('bgv_override', '') or ''; d = self.bgv_folder(); names = self.bgv_list()
        files = [os.path.join(d, n) for n in names] if ov == '*' else ([os.path.join(d, ov)] if ov in names else [])
        self.job.video.set_override(files)

    def get_port(self):
        if self.sound != 'live': return None
        if self.port is None or self.port.dev != self.midi_dev:
            if self.port is not None: self.port.close()
            try: self.port = midiout.Port(self.midi_dev)
            except Exception as ex:                    # e.g. the built-in synth could not start: no sound, still run
                self.sound_error = str(ex); self.midi_dev = 'capture'; self.port = midiout.Port('capture')
            self.mplayer = midiout.Player(self.port)
            self.port.control(**self.synth_ctl())
        return self.port

    SYNTH_DEFAULT = dict(master=1.0, mute=[False] * 16, chvol=[1.0] * 16, transpose=0, filter=True, prog=[-1] * 16,
                         reverb=0.0, eq=[0.0, 0.0, 0.0], melody=True,
                         mic=False, mic_dev=-1, mic_vol=1.0, echo=0.3, echo_ms=120)
    def synth_ctl(self):
        d = dict(self.SYNTH_DEFAULT); d.update(self.settings.get('synth') or {})
        return d

    def synth_state(self, full=False):
        """The synthesizer controller: settings plus what each channel is playing."""
        port = self.port; d = self.synth_ctl()
        names = getattr(self, '_sf_names', None)
        if names is None or getattr(self, '_sf_names_for', None) != self.sf_path:
            names = {}
            try:
                import sf2synth
                sf = sf2synth.SoundFont(self.sf_path); names = {(b, p): n for (n, p, b, _) in sf.phdr[:-1]}
            except Exception:
                pass
            self._sf_names = names; self._sf_names_for = self.sf_path
        now = time.time(); chans = []
        for c in range(16):
            C = port.chans[c] if port is not None else dict(prog=0, bank=0, drum=c == 9, vol=100, notes=0, last=0)
            bank = 128 if C['drum'] else C['bank']
            name = names.get((bank, C['prog'])) or names.get((128, 0) if C['drum'] else (0, C['prog'])) or ''
            ov = d['prog'][c] if c < len(d['prog']) else -1
            if ov >= 0: name = names.get((ov // 128, ov % 128), name)
            chans.append(dict(ch=c + 1, prog=C['prog'], bank=C['bank'], drum=C['drum'], name=name, vol=C['vol'],
                              active=now - C['last'] < 0.4, notes=C['notes'], mute=d['mute'][c], chvol=d['chvol'][c],
                              override=ov))
        out = dict(d, chans=chans, builtin=bool(port is not None and port.synth is not None),
                   device=port.name if port is not None else '')
        pk = self._player_keys()
        if pk is not None:          # the buttons show the firmware's own settings
            lv = pk.melody(); out['mf'] = pk.key()
            out['mel_text'] = ['🎵 Melody off', '🎵 Melody small', '🎵 Melody', '🎵 Melody big'][lv & 3]; out['mel_on'] = lv != 2
        elif self._fw_calls() is not None:
            e, _x = self._fw_calls(); mf = e.rd8(soc.FW_GP + 944)
            out['mf'] = {1: -1, 2: 1}.get(mf, 0)
            on = getattr(e, 'fw_melody', True)
            out['mel_text'] = '🎵 Melody' if on else '🎵 Melody off'; out['mel_on'] = not on
        else:
            out['mf'] = d.get('transpose', 0)
            out['mel_text'] = '🎵 Melody' if d.get('melody', True) is not False else '🎵 Melody off'; out['mel_on'] = d.get('melody', True) is False
        try:
            import livesynth
            out['mics'] = [[i, n] for i, n in livesynth.mic_devices()]
        except Exception:
            out['mics'] = [[-1, 'Default microphone']]
        if full: out['presets'] = sorted([b, p, n] for (b, p), n in names.items())
        return out

    def disc_stamp(self):
        """Changes whenever the disc's song index can have changed: the image file (size, time), or for a disc
        folder its song list files. The songbook and the song-record map are rebuilt when it changes, so a disc
        edited with the Song Manager (or a different file put in its place) shows its new songs right away."""
        p = self.disc_path
        if not p: return None
        try:
            if os.path.isdir(p):
                parts = []
                for sub in ('KARA125', 'kara125', 'IDX', 'idx'):
                    d = os.path.join(p, sub)
                    if os.path.isdir(d):
                        for f in sorted(os.listdir(d)):
                            st = os.stat(os.path.join(d, f)); parts.append((f, st.st_size, st.st_mtime))
                return (p, tuple(parts))
            st = os.stat(p); return (p, st.st_size, st.st_mtime)
        except OSError:
            return (p,)

    def _songdisc(self):
        stamp = self.disc_stamp()
        sb = getattr(self, '_songbook', None)
        if sb is None or sb[0] != stamp:
            now = time.time()
            if sb is not None and sb[0] and stamp and sb[0][0] == stamp[0] and now - getattr(self, '_songbook_t', 0) < 2:
                return sb[1]                              # (a disc being written: check again in a moment)
            if now - getattr(self, '_songbook_miss', 0) < 5 and getattr(self, '_songbook_miss_stamp', None) == stamp:
                raise ValueError(self._songbook_err)
            book = self._other_book(stamp)
            sb = self._songbook = (stamp, book); self._songbook_t = now
            self._recnums = None
        return sb[1]

    def _other_book(self, stamp):
        """The song index of the disc: a Megapro KARA* disc, else a K55 MEGIDX, else (KS5) the list the running
        player unpacked from its encrypted /IDX/5pidx (kept in Saved Settings for later)."""
        import songdisc, songlists
        try:
            return songdisc.KaraokeDisc(self.disc_path)
        except Exception as ex:
            err = ex
        try:
            b = songlists.k55_book(self.disc_path)
            if b is not None: return b
        except Exception:
            pass
        emu = None
        j = self.job
        if j is not None and j.is_alive() and soc.FW_ID == 'ks5' and getattr(j, 'main_at', None) is not None:
            emu = getattr(j, 'emu_ref', None)
        try:
            folder = app_folder('saved', False)
        except Exception:
            folder = None
        b = songlists.ks5_book(self.disc_path, stamp, folder, emu)
        if b is not None: return b
        is_ks5 = False
        try:
            import isoread
            d = isoread.Disc(self.disc_path) if not os.path.isdir(self.disc_path) else None
            if d is not None:
                try: d.lookup('/IDX/5pidx'); is_ks5 = True
                except Exception: pass
            elif os.path.isfile(os.path.join(self.disc_path, 'IDX', '5pidx')): is_ks5 = True
        except Exception:
            pass
        self._songbook_err = ('KS5 disc: its song list is encrypted; start the emulator with this disc once and the '
                              'songbook fills in when the player has started' if is_ks5 else str(err))
        self._songbook_miss = time.time(); self._songbook_miss_stamp = stamp
        raise ValueError(self._songbook_err)

    def songbook(self, query='', only=None, start=0, n=50):
        """The disc's own song list (numbers, titles, singers, music videos), as the player's songbook. The song index
        is checked on every request (see disc_stamp)."""
        if not self.disc_path: return dict(total=0, songs=[], error='open a disc image first')
        try:
            d = self._songdisc()
        except Exception as ex:
            return dict(total=0, songs=[], error='no song index on this disc (%s)' % ex)
        r = d.listing(query, start, n, show_deleted=False, only=only if only in ('mv', 'songs') else None)
        import hashlib
        rev = hashlib.md5(repr(self._songbook[0]).encode()).hexdigest()[:10]
        return dict(total=r['total'], start=start, rev=rev, songs=[dict(number=s['number'], title=s['title'], artist=s['artist'],
                    mv=s['mv'], chorus=s['chorus']) for s in r['songs']])

    def songbook_play(self, number, enter=True):
        """Type a song number on the remote (and Enter): the player itself starts the song."""
        if not (self.job and self.job.is_alive()): raise RuntimeError('start the emulator first')
        for ch in str(int(number)): self.job.press(int(ch))
        if enter: self.job.press(72)                   # Enter = reserve (key 109 would put it first)
        return dict(ok=True, number=int(number))

    SMART_PORT = 8770
    def smartlink(self, on=None):
        """Smart link: switch the phone server on / off (None: leave it), return what the settings page shows."""
        if on is not None:
            self.save_settings(smart_link=bool(on))
            if on and self.phone_srv is None:
                for p in range(self.SMART_PORT, self.SMART_PORT + 20):
                    try:
                        self.phone_srv = ThreadingHTTPServer(('0.0.0.0', p), make_phone_handler(self)); break
                    except OSError:
                        continue
                if self.phone_srv is not None:
                    threading.Thread(target=self.phone_srv.serve_forever, daemon=True).start()
                    threading.Thread(target=self._smart_beacon, daemon=True).start()
            elif not on and self.phone_srv is not None:
                srv, self.phone_srv = self.phone_srv, None
                threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
        if self.phone_srv is None: return dict(on=False, error='' if not self.settings.get('smart_link') else 'could not open a network port')
        url = 'http://%s:%d/' % (lan_address(), self.phone_srv.server_address[1])
        return dict(on=True, url=url, pin=self.phone_pin, qr=qr_svg(url))

    SMART_UDP = 8771
    def _smart_beacon(self):
        """Answers the Android app's search for computers on the Wi-Fi ('CHIAKI?' broadcast on UDP 8771) with this
        computer's Smart link address and name, while Smart link is on."""
        import socket
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sk.bind(('0.0.0.0', self.SMART_UDP)); sk.settimeout(1.0)
        except OSError:
            return
        try:
            while self.phone_srv is not None:
                try: data, addr = sk.recvfrom(256)
                except socket.timeout: continue
                except OSError: break
                if data.strip() != b'CHIAKI?' or self.phone_srv is None: continue
                url = 'http://%s:%d/' % (lan_address(), self.phone_srv.server_address[1])
                try: sk.sendto(('CHIAKI %s %s' % (url, socket.gethostname()[:40])).encode('utf-8', 'replace'), addr)
                except OSError: pass
        finally:
            sk.close()

    def _player_keys(self):
        """The firmware's own Melody / Key settings (Megapro builds), while its emulator runs; else None."""
        j = self.job; e = getattr(j, 'emu_ref', None) if j is not None and j.is_alive() else None
        pk = getattr(e, 'player_keys', None)
        return pk if pk is not None and pk.on else None
    def _fw_calls(self):
        """(emu, profile extras) when the running firmware has its own M/F and Melody routines (KS5); else None."""
        j = self.job; e = getattr(j, 'emu_ref', None) if j is not None and j.is_alive() else None
        if e is None or not getattr(getattr(j, 'remote', None), 'direct', False): return None
        x = __import__('fwprofile').extra(soc.FW_ID)
        return (e, x) if x.get('mf_call') and x.get('melody_call') else None
    def song_ended(self):
        """A song ended (players whose melody and M/F the built-in synth does): back to the song's own."""
        d = self.synth_ctl()
        if d.get('transpose', 0) or d.get('melody', True) is False:
            self.set_synth({'transpose': 0, 'melody': True})
    def set_synth(self, body):
        d = self.synth_ctl()
        if 'master' in body: d['master'] = min(2.0, max(0.0, float(body['master'])))
        if 'transpose' in body: d['transpose'] = min(12, max(-12, int(body['transpose'])))
        if 'filter' in body: d['filter'] = bool(body['filter'])
        if 'mute' in body and len(body['mute']) == 16: d['mute'] = [bool(x) for x in body['mute']]
        if 'chvol' in body and len(body['chvol']) == 16: d['chvol'] = [min(2.0, max(0.0, float(x))) for x in body['chvol']]
        if 'prog' in body and len(body['prog']) == 16: d['prog'] = [int(x) if x is not None and int(x) >= 0 else -1 for x in body['prog']]
        if 'reverb' in body: d['reverb'] = min(1.0, max(0.0, float(body['reverb'])))
        if 'eq' in body and len(body['eq']) == 3: d['eq'] = [min(12.0, max(-12.0, float(x))) for x in body['eq']]
        if 'melody' in body: d['melody'] = bool(body['melody'])
        pk = self._player_keys()
        if pk is not None and (body.get('melody_toggle') or body.get('mf_cycle')):
            # Megapro: the firmware's own Melody key (OFF / SMALL / NORMAL / BIG, its "MELODY : .." message) and its
            # Key up / down for M/F (its KEY message): taken while a song plays
            if pk.in_song():
                before = (pk.melody(), pk.key())
                if body.get('melody_toggle'): self.job.press(pk.MELODY_KEY)
                if body.get('mf_cycle'):
                    for k in pk.presses_to({0: -5, -5: 5}.get(pk.key(), 0)): self.job.press(k)
                t_ = time.time()
                while time.time() - t_ < 1.0 and (pk.melody(), pk.key()) == before: time.sleep(0.05)
            body = {k: v for k, v in body.items() if k not in ('melody_toggle', 'mf_cycle')}
        fx = self._fw_calls()
        if fx is not None and (body.get('melody_toggle') or body.get('mf_cycle')):
            # KS5: its own M/F (MALE / FEMALE / ORIGINAL) and MELODY ON / OFF routines, run as its remote would
            e, x = fx; G_ = lambda o: e.rd8(soc.FW_GP + o)
            if G_(4120) == 2:
                if body.get('mf_cycle'): self.job.remote.press(('call', x['mf_call']))
                if body.get('melody_toggle') and G_(4126) != 1 and G_(4151) == 0: self.job.remote.press(('call', x['melody_call']))
                time.sleep(0.4)
            body = {k: v for k, v in body.items() if k not in ('melody_toggle', 'mf_cycle')}
        if body.get('melody_toggle'): d['melody'] = not d.get('melody', True)
        if body.get('mf_cycle'):                    # M/F: original key -> male (5 lower) -> female (5 higher) -> original
            d['transpose'] = {0: -5, -5: 5}.get(int(d.get('transpose', 0)), 0)
        if 'mic' in body: d['mic'] = bool(body['mic'])
        if 'mic_dev' in body: d['mic_dev'] = int(body['mic_dev'])
        if 'mic_vol' in body: d['mic_vol'] = min(2.0, max(0.0, float(body['mic_vol'])))
        if 'echo' in body: d['echo'] = min(0.9, max(0.0, float(body['echo'])))
        if 'echo_ms' in body: d['echo_ms'] = int(min(900, max(30, float(body['echo_ms']))))
        if body.get('reset'):
            keep = {k: d[k] for k in ('mic', 'mic_dev', 'mic_vol', 'echo', 'echo_ms')}
            d = dict(self.SYNTH_DEFAULT); d.update(keep)
        self.save_settings(synth=d)
        if self.port is not None:
            self.port.control(**d)
            if body.get('panic'): self.port.silence()
        return self.synth_state()

    def set_sound(self, sound=None, dev=None):
        if sound in ('live', 'render'): self.sound = sound
        if sound == 'render':                       # the old render mode: now the built-in SoundFont synth
            self.sound = 'live'
            if dev is None and any(d == 'sf2' for d, _ in midiout.devices()): dev = 'sf2'
        if dev is not None:
            ids = {str(d): d for d, _ in midiout.devices()}
            if str(dev) not in ids: raise ValueError('no such MIDI device')
            self.midi_dev = ids[str(dev)]
            self.settings['capture_chosen'] = self.midi_dev == 'capture'; self.save_settings(capture_chosen=self.midi_dev == 'capture')
        if self.mplayer: self.mplayer.stop()
        port = self.get_port() if self.sound == 'live' else None
        if self.sound != 'live' and self.port is not None: self.port.close(); self.port = None; self.mplayer = None
        if self.job and self.job.is_alive(): self.job.set_midi(port)
        if self.sound == 'render' and self.sf_path and songbank: warm_renderer(self.sf_path)
        self.save_settings(sound=self.sound, device=dict(midiout.devices()).get(self.midi_dev, '') if self.sound == 'live' else None)
        return self.sound_status()

    def _sound_refresh(self):
        """No sound because there was no SoundFont when the program started (put in the Soundfont folder since):
        take it now, and the built-in synth with it - unless "No sound" was picked on purpose."""
        if self.midi_dev != 'capture' or self.settings.get('capture_chosen') or songbank is None: return
        try:
            if not (self.sf_path and os.path.isfile(self.sf_path)):
                here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, 'frozen', False) else __file__))
                self.sf_path = songbank.default_soundfont(app_folder('soundfont', False), here, os.getcwd(),
                                                          os.path.dirname(self.fw) if self.fw else None)
            if self.sf_path:
                midiout.set_soundfont(self.sf_path)
                if any(d == 'sf2' for d, _ in midiout.devices()):
                    self.midi_dev = 'sf2'; self.sound = 'live'
                    if self.port is not None and self.port.dev != 'sf2': self.port.close(); self.port = None
        except Exception as ex:
            self.sound_error = str(ex)

    def sound_status(self):
        self.midi_devs = midiout.devices()
        # what is wrong with the sound, shown next to the Sound choice (on a phone there is no other way to see it)
        problem = getattr(self, 'sound_error', None)
        syn = getattr(self.port, 'synth', None) if self.port is not None else None
        if syn is not None and hasattr(syn, 'proc') and not syn.proc.is_alive(): problem = 'the synthesizer stopped (see Logs)'
        if self.midi_dev == 'capture' and not problem:
            problem = 'no SoundFont found: put one (.sf2) into the Soundfont folder' if not self.sf_path else None
        lp = log_path()
        return dict(sound=self.sound, device=str(self.midi_dev), devices=[dict(id=str(d), name=n) for d, n in self.midi_devs],
                    windows=midiout.IS_WIN, problem=problem, soundfont=os.path.basename(self.sf_path) if self.sf_path else None,
                    log=_log_tail(lp, ('synth', 'sound', 'ffmpeg', 'Traceback', 'Error')) if lp else '')

    def start_job(self, fw=None, budget=None, assist=True, insert=False, quickboot=True, realtime_boot=False, speed=None):
        if speed: self.speed_mode = speed
        self._sound_refresh()
        if self.job and self.job.is_alive():
            # Boot while one runs (another player, another disc): power the old one off first
            self.job.stop_flag = True; self.job.join(8.0)
            if self.job.is_alive(): raise RuntimeError('the running emulator did not stop - press Stop and try again')
        path = find_firmware(fw) if fw else self.fw
        if not path: raise FileNotFoundError('firmware file not found (put MP100NSP.BIN next to the program, or enter its path)')
        self.fw = path
        self.fw_now = None
        if self.sound == 'render' and self.sf_path and songbank: warm_renderer(self.sf_path)
        self.save_settings(fw=path, insert=bool(insert), quickboot=bool(quickboot), assist=bool(assist),
                           realtime_boot=bool(realtime_boot), speed=self.speed_mode)
        self.job = EmuJob(path, budget=budget or (10 ** 15 if insert else 700_000_000), assist=assist, disc=(self.disc_path if insert else None),
                          quickboot=quickboot, on_song=self._fw_song if insert else None, realtime_boot=realtime_boot,
                          midi_port=self.get_port(), speed=self.speed_mode, voice=self.voice_out,
                          key_voice=self.settings.get('key_voice', True), chorus=self.chorus_out,
                          chorus_on=self.settings.get('chorus', True), random_score=self.settings.get('random_score', True))
        self.job.on_song_end = self.song_ended
        self.job.video.MV_DELAY = float(self.settings.get('mv_delay', 0.0) or 0.0)
        self.job.video.size = resolution(self.settings.get('resolution'))[0]
        self.job.video.mode = getattr(self, 'bgv_mode', 'on')
        self.apply_bgv_override()
        self.job.LYRICS_DELAY = float(self.settings.get('lyrics_delay', 250) or 0) / 1000.0
        self.job.video.mv_allowed = self.mv_allowed; self.fw_pick = None
        self.job.start()

    def _record_numbers(self):
        """{(bank file, offset): {song numbers}}: which songs a song-record read belongs to."""
        rn = getattr(self, '_recnums', None)
        stamp = self.disc_stamp()
        if rn is None or rn[0] != stamp:
            m = {}
            try:
                d = self._songdisc()
                for s_ in d.songs:
                    try: path, off = d._locate(s_.bank, s_.offset)
                    except Exception: continue
                    m.setdefault((path.lower(), off), set()).add(s_.number)
            except Exception:
                pass
            rn = self._recnums = (stamp, m)
        return rn[1]

    def mv_allowed(self, number):
        """Play a music video's sound only when the person picked that video. The player also shows music videos
        as the background picture of MIDI songs and while idle; those stay silent."""
        pk = getattr(self, 'fw_pick', None)
        if not pk or pk['used'] or number not in pk['numbers']: return False
        pk['used'] = True
        return True

    def _bank_ranges(self):
        if getattr(self, '_ranges', None) is None:
            sb = self.song_bank(); out = []
            for path, size in sb.files:
                pos = 0
                for lba, nbytes in self.disc.lookup(path).extents:
                    out.append((lba, lba + (nbytes + 2047) // 2048, path, pos)); pos += nbytes
            self._ranges = out
        return self._ranges

    def _fw_song(self, lba, n):
        """Called by the emulator for every sector read: if it is the start of a song record, the firmware has
        started that song - play it here in real time."""
        with self.lock:
            return self._fw_song_locked(lba, n)

    def _fw_song_locked(self, lba, n):
        import struct
        if songbank is None or not self.disc: return
        for a, b, path, pos in self._bank_ranges():
            if a <= lba < b: break
        else:
            return
        off = pos + (lba - a) * 2048
        nums = self._record_numbers().get((path.lower(), off))
        if nums:                                       # the song the person picked (its record holds the lyrics)
            self.fw_pick = dict(numbers=nums, at=n, used=False)
            v = self.job.video if self.job else None
            if v is not None: v.picked(nums)
        h = self.disc.read(path, off, 40)
        o, rsize, usize, nx, L = struct.unpack('<5I', h[:20])
        if not (rsize and rsize % 2048 == 0 and 0 < L <= rsize and usize and rsize < 16 << 20): return
        last = self.fw_now
        if last and last['file'] == path and last['offset'] == off and n - last['at'] < 30 * INSTR_PER_SEC: return
        self.fw_now = dict(file=path, offset=off, at=n)
        if self.sound == 'live':                       # the firmware's own MIDI is already playing on the device
            if self.mplayer: self.mplayer.stop()
            try:
                mid = self.song_bank().midi(path, off); info = songbank.sf2synth.midi_info(mid)
            except Exception:
                mid, info = None, dict(title='', artist='', lyrics='')
            self.player = dict(state='live', title=info['title'], artist=info['artist'], lyrics=info['lyrics'][:4000], file=path,
                               offset=off, mid=mid, source='started by the emulated player', device=self.port.name if self.port else '')
            return
        # always the song the player just started: a song still rendering from an earlier request is dropped
        # (before, a new song was ignored while the previous one rendered, so the previous one played instead)
        if True:
            self.play_song(path, off); self.player['source'] = 'started by the emulated player'
            if self.job:
                self.job.song_t0 = None
                self.job.sync = dict(after=n, notes=note_times(self.player.get('mid') or b''))

    def open_disc(self, path):
        path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
        if not os.path.isfile(path): raise FileNotFoundError('disc image not found: ' + disp_path(path))
        d = isoread.Disc(path)
        with self.lock:
            if self.disc: self.disc.close()
            self.disc, self.disc_path, self.thumb = d, path, None
            self.songs = None; self._ranges = None
            if songbank and not self.sf_path: self.sf_path = songbank.default_soundfont(os.path.dirname(path))
        self.save_settings(iso=path)
        info = d.identify()
        info['path'] = disp_path(path); info['size'] = os.path.getsize(path)
        return info


    # ---- songs + SoundFont
    def song_bank(self):
        if songbank is None: raise RuntimeError('song playback needs numpy (' + SONG_IMPORT_ERROR + ')')
        if not self.disc: raise RuntimeError('open a disc image first')
        if self.songs is None: self.songs = songbank.SongBank(self.disc)
        if not self.songs.files: raise RuntimeError('no karaoke song bank (KARA*/audio*.da*) on this disc')
        return self.songs

    def list_songs(self, file=None, start=0, n=40):
        sb = self.song_bank(); file = file or sb.files[0][0]
        with self.lock: r = sb.listing(file, start, n)
        r['files'] = [f for f, _ in sb.files]; return r

    def set_soundfont(self, path):
        path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
        if not os.path.isfile(path): raise FileNotFoundError(path)
        sf = songbank.soundfont(path); self.sf_path = path
        midiout.set_soundfont(path)
        if self.port is not None and self.port.dev == 'sf2':      # reload the built-in synth with the new SoundFont
            if self.mplayer: self.mplayer.stop()
            self.port.close(); self.port = None
            port = self.get_port()
            if self.job and self.job.is_alive(): self.job.set_midi(port)
        self.save_settings(soundfont=path)
        return dict(path=disp_path(path), name=sf.name, presets=len(sf.presets))

    def play_song(self, file, offset):
        sb = self.song_bank()
        if self.sound == 'live' and self.get_port() is not None:
            with self.lock: mid = sb.midi(file, int(offset))
            info = songbank.sf2synth.midi_info(mid)
            self.mplayer.play(mid, info['title'])
            self.player = dict(state='live-file', title=info['title'], artist=info['artist'], lyrics=info['lyrics'][:4000],
                               file=file, offset=int(offset), mid=mid, device=self.port.name)
            return self.player_status()
        if not self.sf_path: raise RuntimeError('choose a SoundFont (.sf2) first')
        if self.player.get('state') == 'rendering': self.player['cancelled'] = True       # superseded
        with self.lock: mid = sb.midi(file, int(offset))
        info = songbank.sf2synth.midi_info(mid)
        self.player = dict(state='rendering', progress=0.0, title=info['title'], artist=info['artist'], lyrics=info['lyrics'][:4000],
                           file=file, offset=int(offset), soundfont=os.path.basename(self.sf_path), mid=mid, wav=None)
        p = self.player
        def work():
            try:
                wav, sec = render_song(mid, self.sf_path)
                if p.get('cancelled') or p is not self.player:          # stopped or replaced meanwhile: never play it
                    if p.get('state') == 'rendering': p['state'] = 'stopped'
                    return
                p['wav'], p['seconds'] = wav, sec; p['progress'] = 1.0; p['state'] = 'ready'
            except Exception as ex:
                p['state'] = 'error'; p['error'] = '%s: %s' % (type(ex).__name__, ex)
        threading.Thread(target=work, daemon=True).start()
        return self.player_status()

    def player_status(self):
        p = self.player
        d = {k: v for k, v in p.items() if k not in ('mid', 'wav')}; d['soundfont_path'] = disp_path(self.sf_path)
        if p.get('state') == 'live-file' and self.mplayer: d['midi'] = self.mplayer.status()
        return d


# ------------------------------------------------------------------------------------ HTTP
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Project : Chiaki</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#10131a;--card:#181d28;--ink:#e8ecf4;--mut:#8b95a9;--acc:#ffb020;--ok:#3ddc97;--bad:#ff6b6b;--line:#2a3142}
:root[data-theme=light]{--bg:#f3f5f9;--card:#fff;--ink:#1b2130;--mut:#667089;--acc:#c77700;--ok:#12805a;--bad:#c62828;--line:#dde2ec}
html{color-scheme:dark}html[data-theme=light]{color-scheme:light}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
h1{margin:0;font-size:20px}header span{color:var(--mut);font-size:13px}
.social{margin-left:auto;display:flex;gap:8px;align-self:center}.social a{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border:1px solid var(--line);border-radius:99px;color:var(--mut);text-decoration:none;font-size:12.5px}.social a:hover{color:var(--fg,inherit);border-color:var(--acc)}.social svg{width:15px;height:15px;fill:currentColor}.social a span{color:inherit;font-size:12.5px}@media(max-width:700px){.social a span{display:none}.social{margin-left:0}}
main{display:grid;gap:14px;padding:0 14px 14px;margin:auto;max-width:1900px;grid-template-columns:minmax(0,1fr);
 grid-template-areas:"screen" "disc" "songs" "synth" "fw"}
.stage{padding:0;background:#000;border:none;border-radius:0;margin:0 -14px;position:relative}
.stage .screen{height:calc(100vh - 62px);aspect-ratio:auto;border-radius:0;width:100%}
.stage .screen img{object-fit:contain}
.screen,#tv{container-type:size}
.banner{position:absolute;left:6%;top:7%;display:none;color:#3cf03c;font:bold 6.2cqh/1 'Courier New',monospace;letter-spacing:.06em;text-shadow:0 0 .12em #000,.08em .08em 0 #000,-.06em -.06em 0 #000;pointer-events:none;z-index:5}
.stage>h2{display:none}.stage .below{background:var(--card);padding:10px 16px 14px;border-bottom:1px solid var(--line)}
.fab{position:absolute;top:12px;right:14px;display:flex;gap:8px;z-index:20}
/* fullscreen: on the PC the buttons go away with the rest; in the Android app they stay over the picture (tap ⛶
   again to leave) */
body.pfs:not(.appwv) .fab{display:none!important}
body.appwv.pfs .fab{position:fixed;z-index:10000}
body.appwv.pfs .float{z-index:10001}   /* its panels (settings, remote ...) open over the picture */
body.appwv.pfs .float{z-index:10001}   /* its panels (settings, remote ...) open over the fullscreen picture */
/* the Android app has one window: no pop-out windows */
body.appwv #popscreen,body.appwv #popremote,body.appwv .pdetach{display:none!important}
.fab button{background:rgba(20,24,34,.72);color:#fff;border:1px solid rgba(255,255,255,.18);border-radius:12px;padding:10px 13px;font-size:18px;backdrop-filter:blur(6px)}
.fab button.on{background:var(--acc);color:#1b1300}
.float{position:fixed;z-index:40;top:70px;right:22px;width:min(560px,94vw);max-height:84vh;overflow:auto;display:none;box-shadow:0 16px 48px rgba(0,0,0,.55)}
.float.open{display:block}.stab.on{background:var(--acc);color:#1b1300}
main{grid-template-areas:"screen"!important}.stage .screen{height:calc(100vh - 62px)}.float>h2{cursor:move;display:flex;align-items:center;gap:8px;user-select:none}
.float>h2 .sp{flex:1}.float>h2 button{padding:3px 9px;font-size:13px;text-transform:none;letter-spacing:0}
.rgrid{display:grid;grid-template-columns:auto auto 1fr;gap:12px;align-items:start}
.rpad{display:grid;grid-template-columns:repeat(3,52px);gap:6px}.rpad button{padding:9px 0;font-size:16px}
.rnav{display:grid;grid-template-columns:repeat(3,44px);gap:5px}.rnav button{padding:8px 0}
.rfn{display:flex;flex-wrap:wrap;gap:6px;align-content:start}.rfn button{padding:7px 10px;font-size:13px}
#sbtab{display:block;max-height:58vh;overflow:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
h2{margin:0 0 10px;font-size:15px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
input[type=text]{width:100%;padding:9px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
#fwsel,#isosel,#sfsel{padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit;min-width:0}
button{padding:9px 14px;border-radius:8px;border:1px solid var(--line);background:var(--acc);color:#1b1300;font-weight:600;cursor:pointer}
button.sec{background:transparent;color:var(--ink)}button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap;align-items:center}.grow{flex:1;min-width:200px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:190px;overflow:auto;margin:8px 0;font-size:12.5px;white-space:pre-wrap}
:root{--ar:1.5}.screen{background:#000;border-radius:8px;aspect-ratio:var(--ar);display:flex;align-items:center;justify-content:center;color:#667;overflow:hidden}
#calib .cbtn{display:flex;gap:10px;justify-content:center;margin-top:2cqh;pointer-events:auto}#calib .cbtn button{font-size:max(13px,3.2cqh);padding:1.2cqh 2.4cqh}
.screen{position:relative}#calib{display:none;position:absolute;inset:0;z-index:20;overflow:hidden;background:radial-gradient(ellipse at center,#2a2e3a 0%,#0d0f15 80%);container-type:size}#calib.on{display:block}#calib .c{position:absolute;width:36cqh;height:36cqh;border-radius:50%;background:radial-gradient(circle,rgba(255,255,255,.28),rgba(255,255,255,.08) 60%,transparent 72%);border:.4cqh solid rgba(255,255,255,.55)}#calib .tl{left:-18cqh;top:-18cqh}#calib .tr{right:-18cqh;top:-18cqh}#calib .bl{left:-18cqh;bottom:-18cqh}#calib .br{right:-18cqh;bottom:-18cqh}#calib .ct{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:70%;text-align:center;color:#e8ebf2;font:3.4cqh/1.4 system-ui,sans-serif;display:flex;flex-direction:column;gap:2.4cqh}#calib .ct b{font-weight:400;font-size:6cqh}#calib .ct em{font-style:normal;font-size:7cqh;color:#fff}#calib .ct .k{color:#9aa3b5;font-size:3cqh}.screen{position:relative}.screen img,.screen canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:fill}
.screen:fullscreen{border-radius:0;aspect-ratio:auto;width:100vw;height:100vh;background:#000}.screen:fullscreen img{object-fit:fill;inset:auto;left:50%;top:50%;transform:translate(-50%,-50%);width:min(100vw,calc(100vh * var(--ar)));height:min(100vh,calc(100vw / var(--ar)))}
.bar{height:8px;border-radius:5px;background:var(--bg);border:1px solid var(--line);overflow:hidden}.bar i{display:block;height:100%;background:var(--acc);width:0}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:13.5px}.kv b{color:var(--mut);font-weight:500}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.mut{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13.5px}td{padding:4px 6px;border-bottom:1px solid var(--line)}tr.f:hover{background:var(--bg);cursor:pointer}td.sz{text-align:right;color:var(--mut)}
.note{font-size:12.5px;color:var(--mut);margin-top:8px}.thumbs{display:flex;gap:8px;flex-wrap:wrap}.thumbs img{width:48%;border-radius:6px;border:1px solid var(--line)}
body.pfs{overflow:hidden}body.pfs #screen{position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;max-width:none!important;max-height:none!important;z-index:9999;margin:0!important;border-radius:0!important;background:#000}
@media (max-width:900px),(max-height:520px){
 .fab{top:6px;right:6px;gap:5px}.fab button{padding:5px 8px;font-size:14px;border-radius:9px}
 header{padding:8px 12px;gap:8px}h1{font-size:16px}header>span{display:none}
 .card{padding:10px}.card h2{font-size:14px}body{font-size:13.5px}
 button{padding:6px 10px;font-size:12.5px}select,input{font-size:12.5px}.row{gap:5px;margin:5px 0}
 .float{top:44px;right:6px;width:min(460px,96vw);max-height:calc(100vh - 52px);padding:10px}
 #panelsettings{top:6px!important;max-height:calc(100vh - 12px)!important;width:96vw!important}.float>h2{font-size:13px;margin:0 0 6px}
 .rpad{grid-template-columns:repeat(3,40px);gap:4px}.rpad button{padding:6px 0;font-size:13px}
 .rnav{grid-template-columns:repeat(3,36px);gap:4px}.rnav button{padding:5px 0}
 .rfn{gap:4px}.rfn button{padding:5px 7px;font-size:11.5px}.rgrid{gap:8px}
}
</style></head><body>
<header><h1>Project : Chiaki</h1><span>Sunplus karaoke player emulator (Megapro MP100NSP · Platinum KS5) · v__VERSION__</span><nav class="social" aria-label="Social links"><a href="https://linktr.ee/mitsukisato" data-ext title="Linktree"><svg viewBox="0 0 24 24"><path d="M13.5 2v6.2l4.4-4.4 1.9 1.9-4.6 4.5h6.4v2.7h-6.4l4.6 4.5-1.9 1.9-6.4-6.3-6.4 6.3-1.9-1.9 4.6-4.5H2.3v-2.7h6.4L4.1 5.7 6 3.8l4.4 4.4V2h3.1zm-3.1 14.6h3.1V22h-3.1z"/></svg><span>Linktree</span></a><a href="https://www.tiktok.com/@mitsukikaraoke" data-ext title="TikTok"><svg viewBox="0 0 24 24"><path d="M16.6 2h-3.3v13.3a2.9 2.9 0 1 1-2.9-2.9c.3 0 .6 0 .9.1V9.1a6.3 6.3 0 1 0 5.3 6.2V8.6a7.9 7.9 0 0 0 4.4 1.4V6.6c-2.4 0-4.4-2-4.4-4.6z"/></svg><span>TikTok</span></a><a href="https://www.facebook.com/profile.php?id=61593864883476" data-ext title="Facebook"><svg viewBox="0 0 24 24"><path d="M13.4 22v-8.2h2.8l.4-3.2h-3.2V8.5c0-.9.3-1.6 1.6-1.6h1.7V4.1a23 23 0 0 0-2.5-.1c-2.5 0-4.2 1.5-4.2 4.3v2.4H7.2v3.2H10V22z"/></svg><span>Facebook</span></a></nav></header>
<main>
<section class="card" style="grid-area:fw"><h2>1 · Firmware</h2>
 <div class="row"><label class="mut" for="fwsel">💾 BIOS</label><select id="fwsel" class="grow" title="firmware files found in the BIOS folder"></select></div>
 <div class="row"><input type="text" id="fw" class="grow" placeholder="path to MP100NSP.BIN"><button id="boot">Boot firmware</button><button class="sec" id="stop" disabled>Stop</button></div>
 <div class="row mut" id="setnote" style="font-size:12.5px"></div>
 <div class="row"><label><input type="checkbox" id="insert"> Insert the disc image opened below into the emulated player <span class="mut">(virtual drive)</span></label></div>
 <div class="row"><label><input type="checkbox" id="quick" checked> Quick boot <span class="mut">(the first boot saves the state at the main loop in the Saved Settings folder; later boots resume from it in about a second)</span></label></div>
 <div class="row"><label>Speed <select id="speed"><option value="auto" selected>Fast start (instant until the player is ready, then real time)</option><option value="realtime">Real time (like the real player)</option><option value="instant">Instant (no limit)</option></select></label><span class="mut" id="speedinfo"></span></div>
 <div class="note">Instant runs as fast as your PC allows: the player's own timers run fast too, so songs, the video and the number-entry timeout go faster than normal. Use it to get somewhere quickly, then switch back. Can be changed while running.</div>
 <div class="row"><label><input type="checkbox" id="rtboot"> Real time from power-on <span class="mut">(slow: watch READ and the MP100 loading screens at the player's true speed; ignores the saved state for this boot only - the cold-boot state it saves is the same)</span></label></div>
 <div class="row"><label><input type="checkbox" id="assist" checked> Startup assist <span class="mut">(emulation shortcut: ends the splash waits so the firmware reaches its main loop)</span></label></div>
 <div class="kv"><b>State</b><span id="st">idle</span><b>Main loop</b><span id="ml">–</span><b>Boot</b><span id="qb">–</span><b>Firmware state</b><span id="fs">–</span><b>Front-panel buffer</b><span id="pb" style="font-family:monospace;font-size:12px">–</span><b>Emulated time</b><span id="et">–</span><b>Instructions</b><span id="ni">–</span><b>Interrupts</b><span id="irq">–</span></div>
 <div class="bar" style="margin-top:10px"><i id="prog"></i></div>
 <div class="note" id="fwnote"></div>
 <h2 style="margin-top:14px">Firmware console (UART)</h2><pre id="uart">(not started)</pre>
 <h2 style="margin-top:14px">Drive module</h2>
 <div class="kv"><b>Drive state</b><span id="dst">–</span><b>Tray switches</b><span id="dtray">–</span></div>
 <h2 style="margin-top:14px">Virtual drive (ATAPI commands from the firmware)</h2><pre id="vdl">–</pre>
 <h2 style="margin-top:14px">Firmware debug messages</h2><pre id="fwdbg">(none yet)</pre>
 <div class="note">The firmware's own debug printf is compiled out; the emulator hooks it so its messages appear here.</div>
 <div class="note">The CPU runs in a C core (kecpu.dll) at about the real chip's speed, and the player is held to real time once it reaches its main loop. If the page says "Python core", kecpu.dll was not found next to the program and everything runs ~100x slower.</div>
</section>
<section class="card stage" style="grid-area:screen"><h2>2 · What the firmware displays</h2>
 <div class="fab"><button id="fabboot" title="start / stop the player">⏻</button><button id="fabremote" title="remote control">📱</button><button id="fabbook" title="songbook">📖</button><button id="fabsynth" title="synthesizer">🎛</button><button id="fabbgv" title="background video: on → paused → off">🎞</button><button id="fabnext" title="change background video: the disc's videos one by one, then the player's own again">⏭</button><button id="fabfs" title="fullscreen (double-click the picture too)">⛶</button><button id="fabtheme" title="dark / light theme">🌓</button><button id="fabdown" title="settings: player, sound, disc, SoundFont, firmware">⚙</button></div>
 <div class="screen" id="screen"><span id="scrmsg">no picture yet</span><img id="bgimg" alt="" style="display:none"><canvas id="bgcv" style="display:none"></canvas><img id="osdimg" alt="" style="display:none"><div class="banner" id="banner"></div><div id="calib"><i class="c tl"></i><i class="c tr"></i><i class="c bl"></i><i class="c br"></i><div class="ct"><b>Adjust Display Area</b><span>The circles should fill the screen without showing black borders. If the circles are cut off, or you see black borders, press ↑ (larger) or ↓ (smaller).</span><em id="calibv">100%</em><span class="k">↑ Larger · ↓ Smaller · Enter / Esc: done</span><span class="cbtn"><button type="button" id="calminus">− Smaller</button><button type="button" id="calplus">+ Larger</button><button type="button" id="caldone">✔ Done</button></span></div></div></div>
 <div class="below">
 <div class="row"><button class="sec" id="fsbtn">⛶ Fullscreen</button><button class="sec" id="popscreen" title="the picture in its own window: drag it to the TV / second monitor and double-click for fullscreen">🖥 Pop-out screen</button><button class="sec" id="popremote">📱 Pop-out remote</button><button class="sec key" data-k="114" title="remote key 114: the player switches to another background video">🎞 Change video</button><button class="sec key" data-k="75" title="remote key 75: the player's Setup menu (password 0000)">⚙ Setup</button>
  <label>Sound <select id="sound"></select></label><span class="mut" id="soundinfo"></span></div>
 <div class="row"><label><input type="checkbox" id="osdon" checked> show OSD layer (text, menus, logos)</label><label><input type="checkbox" id="kvoice" checked> 🔊 spoken numbers</label><label><input type="checkbox" id="kchorus" checked> 🎤 chorus (recorded voices)</label><label><input type="checkbox" id="kscore" checked> 🎲 random score</label><label title="the music reaches your speakers a little after the picture: hold the lyrics back this much so the wipes match the singing (raise it if the lyrics run ahead, lower it if they lag)">🎵 lyrics delay <input type="number" id="lyrdelay" min="0" max="1000" step="10" value="250" style="width:64px"> ms</label><label title="the size of the picture: 720x480 is the player's own; 1920x1080 fills a full-HD widescreen TV">🖥 resolution <select id="resol"><option value="640x480">640x480 (4:3)</option><option value="720x480">720x480 (normal)</option><option value="1920x1080">1920x1080 (full HD)</option></select></label><select id="safearea" style="display:none"><option value="0">off</option></select><label title="zoom: enlarges the whole picture (video and on-screen text together) and cuts off its edges, like a TV's overscan - for discs with black borders or untidy edges">🔍 zoom <select id="zoom"><option value="0">off</option><option value="2.5">2.5%</option><option value="5">5%</option><option value="7.5">7.5%</option><option value="10">10%</option><option value="12.5">12.5%</option><option value="15">15%</option><option value="20">20%</option><option value="25">25%</option></select></label><button class="sec" id="calibbtn" type="button" title="like a game console's Adjust Display Area: four circles at the picture's corners; make the picture larger or smaller until they just fit your screen">🎯 Adjust display area…</button><label title="how the program window fills the screen (the app window; in a web browser use the browser's own F11)">🪟 window <select id="winmode"><option value="windowed">Windowed</option><option value="maximized">Windowed fullscreen (maximized)</option><option value="borderless">Borderless fullscreen</option><option value="fullscreen">Fullscreen (picture only)</option></select></label><label title="play your own videos instead of the disc's background videos: put them in the BGV folder (next to the program) or set another folder. Music videos of songs still play. ⏭ goes to the next one">🎞 override BGV <select id="bgvov"><option value="">off (the disc's own)</option></select></label><label title="the folder the override videos come from (empty: the BGV folder next to the program)">folder <input id="bgvdir" style="width:170px" placeholder="BGV"></label><button id="bgvdirset" title="use this folder (and refresh the list after adding videos)">set</button><button id="bgvopen" title="open the folder (to add videos)">📂</button><label title="if the lyrics of music videos come late, start the video picture and sound this much later">🎬 music video delay <input type="number" id="mvdelay" min="0" max="5" step="0.1" value="0" style="width:64px"> s</label><span class="mut" id="osdinfo"></span><span class="mut" id="bgvinfo"></span><span class="mut" id="fwmidi"></span></div>
 <div class="note">Sound: the firmware's own MIDI plays live, in step with the lyrics. "SoundFont synth (built in)" plays it with the chosen SoundFont (dreamsam_2695.sf2 = the real player's sound) - nothing to install. Other MIDI devices of the computer (e.g. Microsoft GS Wavetable Synth) work too. The moving background video needs ffmpeg.exe next to the program.</div>
 <div class="row" id="frames"></div>
 <div class="note" id="ffnote"></div>
 <div class="note">These are the MPEG-2 pictures the firmware itself pushes to its video decoder. The first is the startup splash. The SCORE picture seen with the assist switched off is just the data that follows the splash in flash while the feeder is still running - it is not a screen the firmware chose to show. The OSD layer (text and graphics the firmware draws on top of the video) is rebuilt from the firmware's own OSD memory and drawn over the picture.</div>
 </div>
</section>
<section class="card float" id="panelsettings" style="width:min(1100px,96vw);left:50%;right:auto;transform:translateX(-50%);top:64px;max-height:88vh"><h2>⚙ Settings<span class="sp"></span><button class="sec pclose">✕</button></h2>
 <div class="row" id="settabs"><button class="sec stab" data-t="0">🎵 Player &amp; sound</button><button class="sec stab" data-t="1">💿 Disc</button><button class="sec stab" data-t="2">🎼 Songs &amp; SoundFont</button><button class="sec stab" data-t="3">🔧 Firmware &amp; debug</button><button class="sec stab" data-t="4">🖥 Display</button><button class="sec stab" data-t="5">🎤 Mic</button><button class="sec stab" data-t="6">📶 Smart link</button></div>
 <div id="setbody"></div>
</section>
<section class="card" style="grid-area:disc"><h2>3 · Disc image (.iso / .bin)</h2>
 <div class="row"><label class="mut" for="discdir">📁 Disc folder</label><input type="text" id="discdir" class="grow" placeholder="folder with your disc images (default: Discs next to the program)"><button class="sec" id="discscan">Scan</button></div>
 <div class="row"><label class="mut" for="isosel">💿 Disc</label><select id="isosel" class="grow" title="disc images found in the disc folder"></select></div>
 <div class="row"><input type="text" id="iso" class="grow" placeholder="path to a disc image, e.g. C:\Discs\karaoke.iso"><button id="open">Open disc</button></div>
 <div id="disc" class="mut">No disc loaded.</div>
 <div class="note">Tick <b>Insert the disc image</b> before booting and the emulated firmware reads this disc through a virtual drive (karaoke discs: it identifies KARA125, passes the key-chip check and loads title.hdr). The browser below uses this program's own ISO9660/Joliet/UDF reader.</div>
</section>
<section class="card float" id="panelremote"><h2>📱 Remote control<span class="sp"></span><button class="sec pdetach" data-u="/remote" data-w="360" data-h="720" title="open in its own window">⇱ Detach</button><button class="sec pclose">✕</button></h2>
 <div class="rgrid">
  <div class="rpad"><button class="sec key" data-k="1">1</button><button class="sec key" data-k="2">2</button><button class="sec key" data-k="3">3</button><button class="sec key" data-k="4">4</button><button class="sec key" data-k="5">5</button><button class="sec key" data-k="6">6</button><button class="sec key" data-k="7">7</button><button class="sec key" data-k="8">8</button><button class="sec key" data-k="9">9</button><button class="sec key" data-k="75">⚙ Setup</button><button class="sec key" data-k="0">0</button><button class="key" data-k="72" style="background:var(--acc)">Enter ⏎</button></div>
  <div class="rnav"><span></span><button class="sec key" data-k="41">▲</button><span></span><button class="sec key" data-k="43">◀</button><button class="key" data-k="72">OK</button><button class="sec key" data-k="44">▶</button><span></span><button class="sec key" data-k="42">▼</button><span></span></div>
  <div class="rfn"><button class="sec key" data-k="109" title="reserve the number typed at the top of the list (plays next)">⏫ First reserve</button><button class="sec key" data-k="74" title="clears the number being typed; with nothing typed it opens the reserved list, press again to delete the highlighted reservation">✖ Cancel</button><button class="sec key" data-k="25">■ Stop</button><button class="sec key" data-k="29">⏮ Prev</button><button class="sec key" data-k="30">Next ⏭</button><button class="sec key" data-k="17">Vol +</button><button class="sec key" data-k="18">Vol −</button><button class="sec key" data-k="14">Key ♯</button><button class="sec key" data-k="16">Key ♭</button><button class="sec key" data-k="66">Karaoke</button><button class="sec" id="rmelody" title="the guide melody of the song on / off (the built-in synth)">🎵 Melody</button><button class="sec" id="rmf" title="M/F: original key → male (5 lower) → female (5 higher)">⚥ M/F</button><button class="sec key" data-k="73">Book</button><button class="sec key" data-k="31">Display</button><button class="sec key" data-k="114">🎞 Video</button><button class="sec key" data-k="13">🔇 Mute</button></div>
 </div>
 <details id="kmbox"><summary class="mut" style="cursor:pointer;margin-top:8px">⌨ Keyboard keys (click a key to change it)</summary>
  <div id="kmtab" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:4px 10px;margin-top:6px;font-size:13px"></div>
  <div class="row"><button class="sec" id="kmreset">Default keys</button><span class="mut" id="kmmsg">Works here, in the pop-out screen and in the pop-out remote. Typing in a text box is never taken as a key.</span></div>
 </details>
 <details><summary class="mut" style="cursor:pointer;margin-top:8px">All keys &amp; help</summary>
 <div class="note">Keys go into the emulated player through its real infra-red decoder register (front-panel-only keys through the firmware's own key slot). Type a song number, then <b>Enter</b> (key 109) to reserve it; the player starts it like the real machine (you can also use the keyboard's number keys and Enter while this page has focus). When the emulated player starts a song, this program plays the same song in real time in section 5. Names of the other keys are <b>best guesses</b> from the order of the firmware's key-name table – the number is what is sent.</div>
 <div class="row" id="pad"><button class="sec key" data-k="1" style="min-width:44px">1</button><button class="sec key" data-k="2" style="min-width:44px">2</button><button class="sec key" data-k="3" style="min-width:44px">3</button><button class="sec key" data-k="4" style="min-width:44px">4</button><button class="sec key" data-k="5" style="min-width:44px">5</button><button class="sec key" data-k="6" style="min-width:44px">6</button><button class="sec key" data-k="7" style="min-width:44px">7</button><button class="sec key" data-k="8" style="min-width:44px">8</button><button class="sec key" data-k="9" style="min-width:44px">9</button><button class="sec key" data-k="0" style="min-width:44px">0</button><button class="key" data-k="72" title="key 72: Enter - reserves the typed number (starts it when nothing is playing)">Enter ⏎</button></div>
 <div class="row" id="keys"><button class="sec key" data-k="12" title="key 12">eject<span class="mut"> #12</span></button><button class="sec key" data-k="13" title="key 13">mute<span class="mut"> #13</span></button><button class="sec key" data-k="14" title="key 14">key up<span class="mut"> #14</span></button><button class="sec key" data-k="16" title="key 16">key down<span class="mut"> #16</span></button><button class="sec key" data-k="17" title="key 17">vol up<span class="mut"> #17</span></button><button class="sec key" data-k="18" title="key 18">vol down<span class="mut"> #18</span></button><button class="sec key" data-k="19" title="key 19">slow<span class="mut"> #19</span></button><button class="sec key" data-k="25" title="key 25">stop<span class="mut"> #25</span></button><button class="sec key" data-k="27" title="key 27">rewind<span class="mut"> #27</span></button><button class="sec key" data-k="28" title="key 28">forward<span class="mut"> #28</span></button><button class="sec key" data-k="29" title="key 29">prev<span class="mut"> #29</span></button><button class="sec key" data-k="30" title="key 30">next<span class="mut"> #30</span></button><button class="sec key" data-k="31" title="key 31">display<span class="mut"> #31</span></button><button class="sec key" data-k="32" title="key 32">repeat A-B<span class="mut"> #32</span></button><button class="sec key" data-k="33" title="key 33">repeat all<span class="mut"> #33</span></button><button class="sec key" data-k="34" title="key 34">play mode<span class="mut"> #34</span></button><button class="sec key" data-k="39" title="key 39">power<span class="mut"> #39</span></button><button class="sec key" data-k="40" title="key 40">zoom<span class="mut"> #40</span></button><button class="sec key" data-k="41" title="key 41">up<span class="mut"> #41</span></button><button class="sec key" data-k="42" title="key 42">down<span class="mut"> #42</span></button><button class="sec key" data-k="43" title="key 43">left<span class="mut"> #43</span></button><button class="sec key" data-k="44" title="key 44">right<span class="mut"> #44</span></button><button class="sec key" data-k="47" title="key 47">channel down<span class="mut"> #47</span></button><button class="sec key" data-k="49" title="key 49">9 frames<span class="mut"> #49</span></button><button class="sec key" data-k="52" title="key 52">intro / reserve<span class="mut"> #52</span></button><button class="sec key" data-k="66" title="key 66">karaoke<span class="mut"> #66</span></button><button class="sec key" data-k="69" title="key 69">reading<span class="mut"> #69</span></button><button class="sec key" data-k="70" title="key 70">root menu<span class="mut"> #70</span></button><button class="sec key" data-k="72" title="key 72">enter / select<span class="mut"> #72</span></button><button class="sec key" data-k="73" title="key 73">book<span class="mut"> #73</span></button><button class="sec key" data-k="74" title="key 74">cancel / reserved list<span class="mut"> #74</span></button><button class="sec key" data-k="75" title="key 75">setup<span class="mut"> #75</span></button><button class="sec key" data-k="80" title="key 80">P-scan<span class="mut"> #80</span></button><button class="sec key" data-k="101" title="key 101">key<span class="mut"> #101</span></button><button class="sec key" data-k="102" title="key 102">key<span class="mut"> #102</span></button><button class="sec key" data-k="103" title="key 103">key<span class="mut"> #103</span></button><button class="sec key" data-k="104" title="key 104">key<span class="mut"> #104</span></button><button class="sec key" data-k="105" title="key 105">key<span class="mut"> #105</span></button><button class="sec key" data-k="106" title="key 106">key<span class="mut"> #106</span></button><button class="sec key" data-k="109" title="key 109">first reserve<span class="mut"> #109</span></button><button class="sec key" data-k="110" title="key 110">key<span class="mut"> #110</span></button><button class="sec key" data-k="111" title="key 111">key<span class="mut"> #111</span></button><button class="sec key" data-k="112" title="key 112">key<span class="mut"> #112</span></button><button class="sec key" data-k="113" title="key 113">key<span class="mut"> #113</span></button><button class="sec key" data-k="114" title="key 114: change background video">BGV<span class="mut"> #114</span></button><button class="sec key" data-k="144" title="key 144">key<span class="mut"> #144</span></button><button class="sec key" data-k="145" title="key 145">key<span class="mut"> #145</span></button><button class="sec key" data-k="147" title="key 147">key<span class="mut"> #147</span></button><button class="sec key" data-k="148" title="key 148">key<span class="mut"> #148</span></button><button class="sec key" data-k="149" title="key 149">key<span class="mut"> #149</span></button><button class="sec key" data-k="151" title="key 151">key<span class="mut"> #151</span></button><button class="sec key" data-k="152" title="key 152">key<span class="mut"> #152</span></button><button class="sec key" data-k="153" title="key 153">key<span class="mut"> #153</span></button></div>
 </details>
 <div class="mut" id="keylog">–</div>
</section>
<section class="card float" id="panelbook" style="width:min(640px,94vw);left:22px;right:auto"><h2>📖 Songbook<span class="sp"></span><button class="sec pdetach" data-u="/songbook" data-w="640" data-h="760" title="open in its own window">⇱ Detach</button><button class="sec pclose">✕</button></h2>
 <div class="row"><input type="text" id="sbq" class="grow" placeholder="search a title, singer or number"><select id="sbonly"><option value="">all</option><option value="songs">karaoke songs</option><option value="mv">music videos</option></select>
  <button class="sec" id="sbprev">◀</button><button class="sec" id="sbnext">▶</button><span class="mut" id="sbinfo"></span></div>
 <table id="sbtab" style="width:100%;font-size:13px"></table>
</section>
<section class="card float" id="panelsynth" style="width:min(980px,96vw);left:50%;right:auto;transform:translateX(-50%);top:110px"><h2>🎛 Synthesizer<span class="sp"></span><button class="sec pclose">✕</button></h2>
 <div class="row"><span class="mut" id="synthdev">–</span></div>
 <div class="row"><label>Master <input type="range" id="smaster" min="0" max="200" value="100" style="width:160px"> <span id="smasterv">100%</span></label>
  <label>Transpose <select id="stransp"></select></label>
  <label><input type="checkbox" id="sfilter" checked> filters (soft, as on the player)</label>
  <label title="the song's guide tune (the melody instrument): off = sing it yourself"><input type="checkbox" id="smelody" checked> melody</label></div>
 <div class="row"><label title="room echo on the music">Reverb <input type="range" id="sreverb" min="0" max="100" value="0" style="width:110px"> <span id="sreverbv">0%</span></label>
  <label title="equalizer: bass (200 Hz)">Bass <input type="range" id="seq0" min="-12" max="12" value="0" style="width:90px"> <span id="seq0v">0 dB</span></label>
  <label title="equalizer: middle (1 kHz)">Mid <input type="range" id="seq1" min="-12" max="12" value="0" style="width:90px"> <span id="seq1v">0 dB</span></label>
  <label title="equalizer: treble (4 kHz)">Treble <input type="range" id="seq2" min="-12" max="12" value="0" style="width:90px"> <span id="seq2v">0 dB</span></label>
  <button class="sec" id="spanic" title="stop every sounding note">⏹ All notes off</button>
  <button class="sec" id="sreset" title="master 100%, no transpose, all channels on">Reset</button></div>
 <div id="schans" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px;margin-top:8px"></div>
</section>
<section class="card" style="grid-area:songs"><h2>5 · Songs on the disc &amp; SoundFont</h2>
 <div class="note">The karaoke songs are MIDI files inside KARA125/audio1.da1 and audio.dat, scrambled and compressed by the maker. This program unpacks them the same way the firmware does and plays them with the SoundFont you choose (dreamsam_2695.sf2 is used by default when it sits next to the program, the firmware or the disc image).</div>
 <div class="row"><label class="mut" for="sfsel">🎹 SoundFont</label><select id="sfsel" class="grow" title="SoundFonts found in the Soundfont folder"></select></div>
 <div class="row"><input type="text" id="sfpath" class="grow" placeholder="SoundFont .sf2 path"><button class="sec" id="sfset">Use SoundFont</button></div>
 <div class="row"><select id="songfile"></select><button class="sec" id="songlist">List songs</button><button class="sec" id="songprev">◀</button><button class="sec" id="songnext">▶</button><span class="mut" id="songinfo"></span></div>
 <table id="songtab"></table>
 <div id="player" class="mut">–</div>
</section>
</main>
<footer style="text-align:center;color:var(--mut);font-size:12px;padding:18px 12px 22px;border-top:1px solid var(--line);margin-top:18px">Project : Chiaki by <b style="color:var(--ink)">ChiakiMK</b> · built with Claude (Anthropic)</footer>
<script>
const TOKEN="__TOKEN__";
// Live background video: the MJPEG stream read with fetch and drawn on a canvas once per screen refresh. As an
// <img> stream the browser painted the pictures when they arrived (uneven: stutter, worse while OBS captures).
function mkLive(cvId,afterStop){let ctl=null,raf=0;const cv=$(cvId),cx=cv.getContext('2d');
 const q=[];let iv=33.3,lastIn=0,nextAt=0;
 // pictures arrive with a few ms of jitter: shown straight away, a 30 fps video on a 60 Hz screen got 1 / 2 / 3
 // refreshes per picture (judder). They wait in a short queue and are shown at the video's own steady rate.
 function draw(t){raf=0;if(!q.length){nextAt=0;return}
  if(!nextAt||t-nextAt>3*iv)nextAt=t;
  if(t+4>=nextAt){const b=q.shift();if(cv.width!==b.width||cv.height!==b.height){cv.width=b.width;cv.height=b.height}cx.drawImage(b,0,0);b.close();nextAt+=iv;
   if(q.length>3){while(q.length>2)q.shift().close();nextAt=t+iv}}
  if(q.length)raf=requestAnimationFrame(draw)}
 function stop(){if(ctl){ctl.abort();ctl=null}cv.style.display='none';while(q.length)q.shift().close();nextAt=0}
 async function run(c){let buf=new Uint8Array(0);
  try{const r=await fetch('/api/bgv.mjpg?t='+TOKEN,{signal:c.signal});if(!r.ok)throw 0;const rd=r.body.getReader();
   while(true){const {done,value}=await rd.read();if(done)break;const nb=new Uint8Array(buf.length+value.length);nb.set(buf);nb.set(value,buf.length);buf=nb;
    while(true){let h=-1;for(let i=0;i+3<buf.length;i++){if(buf[i]===13&&buf[i+1]===10&&buf[i+2]===13&&buf[i+3]===10){h=i;break}}
     if(h<0)break;const head=new TextDecoder().decode(buf.subarray(0,h));const m=/Content-Length:\s*(\d+)/i.exec(head);if(!m){buf=buf.subarray(h+4);continue}
     const n=+m[1],st=h+4;if(buf.length<st+n)break;const jpg=buf.slice(st,st+n);buf=buf.subarray(st+n);
     try{const bmp=await createImageBitmap(new Blob([jpg],{type:'image/jpeg'}));if(c.signal.aborted){bmp.close();return}const now=performance.now();if(lastIn){const d=now-lastIn;if(d>8&&d<120)iv=iv*0.9+d*0.1}lastIn=now;q.push(bmp);cv.style.display='';if(!raf)raf=requestAnimationFrame(draw)}catch(e){}}}
  }catch(e){}
  if(ctl===c&&!c.signal.aborted)setTimeout(()=>{if(ctl===c){ctl=new AbortController();run(ctl)}},1000)}
 return {start(){stop();ctl=new AbortController();run(ctl)},stop(){stop();if(afterStop)afterStop()},on(){return !!ctl}}}
const $=id=>document.getElementById(id);
document.querySelectorAll('a[data-ext]').forEach(a=>a.onclick=ev=>{ev.preventDefault();api('/api/open_link',{url:a.href}).then(r=>{if(!r||!r.ok)window.open(a.href,'_blank')}).catch(()=>window.open(a.href,'_blank'))});
const LIVE=mkLive('bgcv');
async function api(path,body){const o={headers:{'X-Token':TOKEN}};if(body!==undefined){o.method='POST';o.body=JSON.stringify(body);o.headers['Content-Type']='application/json'}
 const r=await fetch(path,o);const t=await r.text();let j;try{j=JSON.parse(t)}catch(e){j={error:t}}if(!r.ok&&!j.error)j.error='HTTP '+r.status;return j}
function fmt(n){const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++}return (i?n.toFixed(1):n)+' '+u[i]}
let shown=-1, osdShown=0, bgvShown=0;
let splashDone=false,splashUntil=0;
async function poll(){
 const s=await api('/api/status');
 if(s.error&&!s.state){$('st').textContent=s.error;return}
 {const kn={mp100:'⏯ Play/Pause',migael:'⏯ Play/Pause'}[s.fw_id]||'Karaoke';document.querySelectorAll('#panelremote button[data-k="66"]').forEach(b=>{if(b.textContent!==kn)b.textContent=kn})}
 const run=s.state==='running'||s.state==='starting';
 $('st').innerHTML='<span class="pill '+(s.state==='error'?'bad':s.state==='done'?'ok':'')+'">'+s.state+'</span>'+(s.error?' <span class="bad">'+s.error+'</span>':'');
 $('et').textContent=s.emulated_seconds+' s  (wall '+s.wall_seconds+' s)'+(s.speed?'  · '+s.speed+'x real time':(run&&s.main_loop_reached?'  · running flat out':''));
 if(s.speed_mode&&document.activeElement!==$('speed'))$('speed').value=s.speed_mode;
 $('speedinfo').textContent=s.cpu?s.cpu:'';
 $('ni').textContent=(s.instructions||0).toLocaleString();
 $('irq').textContent=Object.keys(s.irqs||{}).length?JSON.stringify(s.irqs):'–';
 $('prog').style.width=Math.min(100,100*(s.instructions||0)/(s.budget||1))+'%';
 $('fwnote').textContent=s.note||'';$('fwnote').style.color=(s.note||'').startsWith('WARNING')?'#ff6b6b':'';$('fwnote').style.fontWeight=(s.note||'').startsWith('WARNING')?'600':'';
 $('ml').innerHTML=s.main_loop_reached?'<span class="ok">reached</span> at '+s.main_at_seconds+' s emulated ('+s.stop_requests+' stop requests, '+s.plays+' plays)':(run?'not yet':'–');
 $('qb').textContent=s.quickboot||'–';
 $('fs').textContent=(s.fw_state===null||s.fw_state===undefined)?'–':s.fw_state+(s.fw_state===0?'  (state machine not started)':'');
 $('pb').textContent=s.panel_hex||'–';
 const v=s.vdrive||{};$('vdl').textContent=!v.enabled?'off':('disc: '+(v.disc||'none (answers "medium not present")')+' · '+v.packets+' commands · '+v.sectors_read+' sectors read'+(Object.keys(v.unknown||{}).length?' · unanswered opcodes '+JSON.stringify(v.unknown):'')+'\n'+(v.last||[]).map(x=>x.t.toFixed(3)+'s  '+x.pkt+'  '+x.res).join('\n'));
 const d=s.drive||{};$('dst').textContent=(d.state===undefined)?'–':('state '+d.state+' · sequencer step '+d.step+' · command '+(d.command===255?'none':d.command)+' · result '+d.result);
 $('dtray').textContent=d.tray?('modelled as '+d.tray):'–';
 if(s.fw_debug&&s.fw_debug.length)$('fwdbg').textContent=s.fw_debug.map(x=>x.t.toFixed(3)+'s  '+x.msg).join('\n')+(s.fw_debug_total>s.fw_debug.length?'\n('+s.fw_debug_total+' total)':'');
 if(s.uart)$('uart').textContent=s.uart;
 if(s.keys_sent&&s.keys_sent.length)$('keylog').textContent='sent: '+s.keys_sent.map(k=>'#'+k.key+' @'+k.t+'s ('+k.how+')').join(', ');
 $('boot').disabled=run;$('stop').disabled=!run;
 $('ffnote').innerHTML=s.ffmpeg?'':'<span class="bad">ffmpeg not found</span> – pictures are captured but cannot be shown. Put ffmpeg(.exe) next to this program or in PATH.';
 const fr=s.frames||[];
 $('frames').innerHTML=fr.map(f=>'<span class="pill">frame '+f.index+' · '+fmt(f.bytes)+' · @'+f.at_seconds+'s · <a href="/api/frame/'+f.index+'.m2v?t='+TOKEN+'" style="color:var(--acc)">.m2v</a></span>').join(' ');
 if(run&&fr.length&&fr[0].has_png&&!splashDone){splashDone=true;splashUntil=Date.now()+4000;    // the power-on splash, shown for a few seconds
   const r=await fetch('/api/frame/0.png',{headers:{'X-Token':TOKEN}});if(r.ok){LIVE.stop();const im=$('bgimg');im.src=URL.createObjectURL(await r.blob());im.style.display='';$('scrmsg').style.display='none';shown=0;liveSet=false}}
 if(!run)splashDone=false;
 if(!s.video_live&&Date.now()>splashUntil&&fr.length&&fr[fr.length-1].index!==shown&&fr[fr.length-1].has_png){const f=fr[fr.length-1];shown=f.index;
   const r=await fetch('/api/frame/'+f.index+'.png',{headers:{'X-Token':TOKEN}});LIVE.stop();const im=$('bgimg');im.src=URL.createObjectURL(await r.blob());im.style.display='';$('scrmsg').style.display='none'}
 const bg=s.bgv||{};
 if(!s.video_live&&bg.version&&bg.version!==bgvShown){bgvShown=bg.version;const r=await fetch('/api/bgv.jpg',{headers:{'X-Token':TOKEN}});if(r.ok){const im=$('bgimg');im.src=URL.createObjectURL(await r.blob());im.style.display='';$('scrmsg').style.display='none'}}
 $('bgvinfo').textContent=(bg.streaming?(' · background video '+(bg.file?bg.file.split('/').pop()+' ':'')+'(sector '+bg.lba+', '+fmt(bg.bytes)+', '+bg.reads_per_s+' reads/s)'):'')+(s.video_live?' · live video '+(s.video_frames||0)+' frames'+(s.video_dropped?' ('+fmt(s.video_dropped)+' skipped, decoder behind)':''):(bg.streaming?' · no ffmpeg: background video not shown':''))+(s.speed?' · speed '+s.speed+'x':'')+(s.cpu?' · '+s.cpu:'');
 $('fwmidi').innerHTML=(s.fw_midi&&s.fw_midi.bytes)?' · <a href="/api/fw_midi.mid?t='+TOKEN+'" download="from_emulated_player.mid" style="color:var(--acc)">MIDI sent by the emulated player</a> ('+s.fw_midi.bytes+' bytes)':'';
 $('bgimg').style.visibility=$('bgcv').style.visibility=(s.video_on===false)?'hidden':'visible';
 const o=s.osd||{};
 $('osdinfo').textContent=o.version?(' · '+(o.regions||[]).length+' region(s), updated '+o.at_seconds+' s'):'';
 if(s.video_live&&run&&!liveSet&&Date.now()>splashUntil){liveSet=true;const im=$('bgimg');im.removeAttribute('src');im.style.display='none';LIVE.start();$('scrmsg').style.display='none'}
 if(!run){liveSet=false;LIVE.stop();if(s.state!=='idle'){const b=$('bgimg');if(b.style.display!=='none'){b.removeAttribute('src');b.style.display='none'}osdShown=0}}
 $('osdimg').style.display=($('osdon').checked&&osdShown>0)?'':'none';
}
setInterval(poll,900);poll();
// ---- synthesizer controller
let SY=null,SYP=null;
(function(){const s=$('stransp');for(let i=-12;i<=12;i++){const o=document.createElement('option');o.value=i;o.textContent=(i>0?'+':'')+i;s.appendChild(o)}})();
function mfLabel(t){const b=$('rmf');if(b)b.textContent=t<0?'♂ M/F':t>0?'♀ M/F':'⚥ M/F'}
function melLabel(r){const b=$('rmelody');if(b){b.textContent=r.mel_text||(r.melody!==false?'🎵 Melody':'🎵 Melody off');b.classList.toggle('on',r.mel_on??(r.melody===false))}}
function micDraw(r){const ms=$('micdev');if(!ms)return;if(ms.options.length!==(r.mics||[]).length){ms.innerHTML='';(r.mics||[]).forEach(([i,n])=>{const o=document.createElement('option');o.value=i;o.textContent=n;ms.appendChild(o)})}
 if(document.activeElement!==ms)ms.value=String(r.mic_dev??-1);$('micon').checked=!!r.mic;
 [['micvol','micvolv',Math.round((r.mic_vol??1)*100),'%'],['micecho','micechov',Math.round((r.echo??0.3)*100),'%'],['micdly','micdlyv',r.echo_ms??120,' ms']].forEach(([id,lab,v,u])=>{if(document.activeElement!==$(id)){$(id).value=v;$(lab).textContent=v+u}})}
function synthDraw(r){SY=r;$('synthdev').textContent=r.builtin?('Built-in SoundFont synth · '+r.device):('These controls work with the built-in SoundFont synth (now: '+(r.device||'none')+')');
 if(document.activeElement!==$('smaster')){$('smaster').value=Math.round(r.master*100);$('smasterv').textContent=Math.round(r.master*100)+'%'}
 if(document.activeElement!==$('stransp'))$('stransp').value=r.transpose;$('sfilter').checked=r.filter;$('smelody').checked=r.melody!==false;
 if(document.activeElement!==$('sreverb')){$('sreverb').value=Math.round((r.reverb||0)*100);$('sreverbv').textContent=Math.round((r.reverb||0)*100)+'%'}
 [0,1,2].forEach(i=>{const el=$('seq'+i);if(document.activeElement!==el){const v=Math.round((r.eq||[0,0,0])[i]);el.value=v;$('seq'+i+'v').textContent=v+' dB'}});
 mfLabel(r.mf??r.transpose);melLabel(r);micDraw(r);
 const box=$('schans');if(box.children.length!==16){box.innerHTML='';for(let c=0;c<16;c++){const d=document.createElement('div');d.className='sch';d.style.cssText='border:1px solid #2a3142;border-radius:8px;padding:6px 8px;font-size:12px';
  d.innerHTML='<div style="display:flex;gap:6px;align-items:center"><span class="dot" style="width:8px;height:8px;border-radius:50%;background:#333;display:inline-block"></span><b>'+(c+1)+'</b><span class="nm" style="flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis"></span><label><input type="checkbox" class="mu"> mute</label></div><input type="range" class="cv" min="0" max="200" style="width:100%"><select class="pg" style="width:100%;font-size:11px"></select>';
  const sel=d.querySelector('.pg');sel.innerHTML='<option value="-1">♪ the song\'s instrument</option>'+(SYP||[]).map(p=>'<option value="'+(p[0]*128+p[1])+'">'+(p[0]==128?'🥁 ':'')+p[0]+':'+p[1]+' '+p[2].replace(/</g,'')+'</option>').join('');
  sel.addEventListener('change',()=>{const v=SY.chans.map(x=>x.override);v[c]=+sel.value;api('/api/synth',{prog:v}).then(synthDraw)});
  d.querySelector('.mu').addEventListener('change',()=>{const m=SY.chans.map(x=>x.mute);m[c]=d.querySelector('.mu').checked;api('/api/synth',{mute:m}).then(synthDraw)});
  d.querySelector('.cv').addEventListener('change',()=>{const v=SY.chans.map(x=>x.chvol);v[c]=d.querySelector('.cv').value/100;api('/api/synth',{chvol:v}).then(synthDraw)});
  box.appendChild(d)}}
 r.chans.forEach((ch,i)=>{const d=box.children[i];d.querySelector('.nm').textContent=(ch.drum?'🥁 ':'')+(ch.name||('program '+ch.prog))+(ch.drum?'':' · '+ch.prog);
  d.querySelector('.dot').style.background=ch.active?'#4ade80':'#333';d.querySelector('.mu').checked=ch.mute;
  const cv=d.querySelector('.cv');if(document.activeElement!==cv)cv.value=Math.round(ch.chvol*100);d.style.opacity=ch.mute?0.5:1;
  const sel=d.querySelector('.pg');if(document.activeElement!==sel)sel.value=String(ch.override)})}
async function synthPoll(){try{if(!SYP){const r=await api('/api/synth?full=1');SYP=r.presets||[];$('schans').innerHTML='';synthDraw(r)}else synthDraw(await api('/api/synth'))}catch(e){}}
$('smaster').addEventListener('input',()=>{$('smasterv').textContent=$('smaster').value+'%'});
$('smaster').addEventListener('change',()=>api('/api/synth',{master:$('smaster').value/100}).then(synthDraw));
$('stransp').addEventListener('change',()=>api('/api/synth',{transpose:+$('stransp').value}).then(synthDraw));
$('sfilter').addEventListener('change',()=>api('/api/synth',{filter:$('sfilter').checked}).then(synthDraw));
$('rmelody').onclick=()=>api('/api/synth',{melody_toggle:1}).then(synthDraw);$('rmf').onclick=()=>api('/api/synth',{mf_cycle:1}).then(synthDraw);
$('smelody').addEventListener('change',()=>api('/api/synth',{melody:$('smelody').checked}).then(synthDraw));
$('sreverb').addEventListener('input',()=>{$('sreverbv').textContent=$('sreverb').value+'%'});
$('sreverb').addEventListener('change',()=>api('/api/synth',{reverb:$('sreverb').value/100}).then(synthDraw));
[0,1,2].forEach(i=>{const el=$('seq'+i);el.addEventListener('input',()=>{$('seq'+i+'v').textContent=el.value+' dB'});
 el.addEventListener('change',()=>api('/api/synth',{eq:[0,1,2].map(k=>+$('seq'+k).value)}).then(synthDraw))});
$('spanic').addEventListener('click',()=>api('/api/synth',{panic:1}).then(synthDraw));
$('sreset').addEventListener('click',()=>api('/api/synth',{reset:1}).then(synthDraw));
setInterval(synthPoll,600);synthPoll();
// ---- floating panels (remote, songbook, synth): icons on the picture, draggable, detachable
function panel(id,btn){const p=$(id),b=$(btn);b.onclick=()=>{p.classList.toggle('open');b.classList.toggle('on',p.classList.contains('open'));try{localStorage.setItem('kemu_'+id,p.classList.contains('open')?'1':'')}catch(e){}};
 p.querySelector('.pclose').onclick=()=>{p.classList.remove('open');b.classList.remove('on');try{localStorage.setItem('kemu_'+id,'')}catch(e){}};
 const d=p.querySelector('.pdetach');if(d)d.onclick=()=>{window.open(d.dataset.u+'?t='+TOKEN,'kemu'+id,'width='+d.dataset.w+',height='+d.dataset.h);p.classList.remove('open');b.classList.remove('on')};
 const h=p.querySelector('h2');h.addEventListener('mousedown',ev=>{if(ev.target.tagName==='BUTTON')return;const r=p.getBoundingClientRect(),ox=ev.clientX-r.left,oy=ev.clientY-r.top;
  const mv=e=>{p.style.left=Math.max(0,Math.min(innerWidth-60,e.clientX-ox))+'px';p.style.top=Math.max(0,Math.min(innerHeight-40,e.clientY-oy))+'px';p.style.right='auto'};
  const up=()=>{document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up)};document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up)});
 try{if(localStorage.getItem('kemu_'+id)){p.classList.add('open');b.classList.add('on')}}catch(e){}}
panel('panelremote','fabremote');panel('panelbook','fabbook');panel('panelsynth','fabsynth');
$('fabfs').onclick=()=>$('fsbtn').click();
// settings: every option lives in the ⚙ panel (moved there, so the page is only the picture)
(function(){const body=$('setbody');const parts=[document.querySelector('.stage .below'),...['disc','songs','fw'].map(a=>document.querySelector('section[style*="grid-area:'+a+'"]'))];
 parts.forEach((p,i)=>{if(!p)return;const w=document.createElement('div');w.className='stabp';w.dataset.t=i;
  if(p.tagName==='SECTION'){p.style.gridArea='';p.classList.remove('card');p.style.padding='0';p.style.border='0';p.style.background='transparent'}else{p.style.background='transparent';p.style.border='0';p.style.padding='0'}
  w.appendChild(p);body.appendChild(w)});
 // 📶 Smart link: phones on the same Wi-Fi as the remote control and song search
 {const w=document.createElement('div');w.className='stabp';w.dataset.t=6;
  w.innerHTML='<label><input type="checkbox" id="slon"> Let phones on my Wi-Fi connect (remote control and song search)</label>'+
   '<div id="slinfo" style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-top:10px"></div>'+
   '<div class="mut" style="margin-top:8px">On the phone: connect to the same Wi-Fi, open the address (or scan the code) and type the PIN. Windows may ask to allow Project Chiaki on the network the first time: allow it for private networks. The PIN changes each time the program starts.</div>';
  body.appendChild(w);
  const show=r=>{$('slon').checked=!!r.on;const b=$('slinfo');b.innerHTML='';if(r.error){b.textContent=r.error;return}if(!r.on)return;
   const q=document.createElement('div');q.style.cssText='width:150px;background:#fff;border-radius:8px;padding:4px';q.innerHTML=r.qr||'';if(r.qr){const sv=q.querySelector('svg');if(sv){sv.setAttribute('width','142');sv.setAttribute('height','142')}}else q.style.display='none';
   const t=document.createElement('div');t.innerHTML='<div class="mut">Address</div><div style="font:600 18px ui-monospace,monospace;user-select:all"></div><div class="mut" style="margin-top:8px">PIN</div><div style="font:700 28px ui-monospace,monospace;letter-spacing:.2em"></div>';
   t.children[1].textContent=r.url;t.children[3].textContent=r.pin;b.appendChild(q);b.appendChild(t)};
  $('slon').onchange=()=>api('/api/smartlink',{on:$('slon').checked}).then(show);
  api('/api/smartlink').then(show)}
 // 🎤 Mic: a microphone of this computer, heard through the speakers with the music (echo, level)
 {const w=document.createElement('div');w.className='stabp';w.dataset.t=5;
  w.innerHTML='<div class="row" style="flex-wrap:wrap;gap:12px"><label><input type="checkbox" id="micon"> microphone on</label><label>Microphone <select id="micdev"></select></label>'+
   '<label>Mic volume <input type="range" id="micvol" min="0" max="200" value="100" style="width:120px"> <span id="micvolv">100%</span></label>'+
   '<label>Echo <input type="range" id="micecho" min="0" max="90" value="30" style="width:120px"> <span id="micechov">30%</span></label>'+
   '<label>Echo delay <input type="range" id="micdly" min="30" max="400" value="120" style="width:120px"> <span id="micdlyv">120 ms</span></label></div>'+
   '<div class="mut" style="margin-top:8px">Sing into a microphone on this computer and hear it in the speakers with the music, like the player\'s mic input. Use headphones or keep the volume moderate to avoid feedback. A phone as the microphone is not possible over Wi-Fi: the delay (a quarter second or more) is too long to sing with.</div>';
  body.appendChild(w);
  const send=b=>api('/api/synth',b).then(synthDraw);
  $('micon').onchange=()=>send({mic:$('micon').checked});$('micdev').onchange=()=>send({mic_dev:+$('micdev').value});
  [['micvol','micvolv',v=>v+'%',v=>({mic_vol:v/100})],['micecho','micechov',v=>v+'%',v=>({echo:v/100})],['micdly','micdlyv',v=>v+' ms',v=>({echo_ms:+v})]].forEach(([id,lab,fmt,mk])=>{
   $(id).addEventListener('input',()=>{$(lab).textContent=fmt($(id).value)});$(id).addEventListener('change',()=>send(mk(+$(id).value)))})}
 // 🖥 Display: the picture / window settings gathered on their own tab (zoom stays with the player settings)
 {const w=document.createElement('div');w.className='stabp';w.dataset.t=4;const box=document.createElement('div');box.className='row';box.style.flexWrap='wrap';box.style.gap='10px';
  ['resol','winmode'].forEach(id=>{const el=$(id);const l=el&&el.closest('label');if(l)box.appendChild(l)});
  ['calibbtn','fsbtn','popscreen'].forEach(id=>{const el=$(id);if(el)box.appendChild(el)});
  const note=document.createElement('div');note.className='mut';note.style.marginTop='8px';note.textContent='Adjust display area: four circles at the corners of the picture - press ↑ / ↓ until they just fit your TV or monitor, then Enter.';
  w.appendChild(box);w.appendChild(note);body.appendChild(w)}
 function show(t){document.querySelectorAll('.stabp').forEach(x=>x.style.display=x.dataset.t==t?'':'none');document.querySelectorAll('.stab').forEach(b=>b.classList.toggle('on',b.dataset.t==t));try{localStorage.setItem('kemu_tab',t)}catch(e){}}
 document.querySelectorAll('.stab').forEach(b=>b.onclick=()=>show(b.dataset.t));let t='0';try{t=localStorage.getItem('kemu_tab')||'0'}catch(e){}show(t)})();
panel('panelsettings','fabdown');
(function(){const set=t=>{if(t==='light')document.documentElement.dataset.theme='light';else delete document.documentElement.dataset.theme;try{localStorage.setItem('kemu_theme',t)}catch(e){}};
 let t='dark';try{t=localStorage.getItem('kemu_theme')||'dark'}catch(e){}set(t);
 $('fabtheme').onclick=()=>{t=t==='light'?'dark':'light';set(t)}})();
let BGV='on';function bgvDraw(){const b=$('fabbgv');b.textContent=BGV==='on'?'🎞':BGV==='paused'?'⏸':'⏹';b.title='background video: '+BGV+' (click: '+(BGV==='on'?'pause':BGV==='paused'?'switch off':'switch on')+')';b.style.opacity=BGV==='on'?'':'0.75'}
$('fabnext').onclick=async()=>{const r=await api('/api/settings',{bgv_change:1});if(r&&r.bgv_name){const b=$('fabnext');b.title='background video: '+r.bgv_name+' (click: next)';if(typeof msg==='function')try{msg('Background: '+r.bgv_name)}catch(e){}}};
$('fabbgv').onclick=async()=>{BGV=BGV==='on'?'paused':BGV==='paused'?'off':'on';bgvDraw();await api('/api/settings',{bgv:BGV})};
(async()=>{try{const c=await api('/api/config');BGV=c.bgv||'on';bgvDraw()}catch(e){}})();
$('fabboot').onclick=()=>{const st=$('stop');if(st&&!st.disabled)st.click();else $('boot').click()};
setInterval(()=>{const st=$('stop');$('fabboot').classList.toggle('on',!!(st&&!st.disabled))},1000);

// ---- keyboard map editor
let KMCFG=null,kmCapture=null;
const kmName=k=>({' ':'Space','ArrowUp':'↑','ArrowDown':'↓','ArrowLeft':'←','ArrowRight':'→','Escape':'Esc'}[k]||k);
async function kmDraw(){if(!KMCFG){const c=await api('/api/config');KMCFG=c}const keys={};for(const[k,v]of Object.entries(KEYMAP))(keys[v]=keys[v]||[]).push(k);
 $('kmtab').innerHTML=KMCFG.actions.map(([code,label])=>'<div style="display:flex;justify-content:space-between;gap:6px;align-items:center"><span>'+label+' <span class="mut">#'+code+'</span></span><button class="sec kmset" data-c="'+code+'" style="padding:3px 8px;min-width:60px">'+((keys[code]||[]).map(kmName).join(' ')||'—')+'</button></div>').join('');
 document.querySelectorAll('.kmset').forEach(b=>b.onclick=()=>{kmCapture=+b.dataset.c;b.textContent='press…';$('kmmsg').textContent='Press the key for this button (Backspace: none, Esc: cancel).'})}
async function kmSave(){await api('/api/settings',{keymap:KEYMAP});kmDraw()}
function kmCaptureKey(ev){ev.preventDefault();const c=kmCapture;kmCapture=null;$('kmmsg').textContent='Saved.';
 if(ev.key==='Escape'){kmDraw();return}
 for(const[k,v]of Object.entries(KEYMAP))if(v===c)delete KEYMAP[k];
 if(ev.key!=='Backspace')KEYMAP[kmKey(ev)]=c;kmSave()}
$('kmbox').addEventListener('toggle',()=>{if($('kmbox').open){loadKeymap().then(kmDraw)}});
$('kmreset').onclick=async()=>{const c=await api('/api/config');KEYMAP=Object.assign({},c.default_keymap);kmSave()};

// ---- songbook
let sbStart=0,sbTimer=null;
let sbRev=null;
async function sbLoad(){const r=await api('/api/songbook?q='+encodeURIComponent($('sbq').value)+'&only='+$('sbonly').value+'&start='+sbStart);
 if(r.error){$('sbinfo').textContent=r.error;$('sbtab').innerHTML='';sbRev='none';return}
 sbRev=r.rev;
 $('sbinfo').textContent=r.total?((sbStart+1)+'–'+Math.min(sbStart+50,r.total)+' of '+r.total):'no songs found';
 const esc=s=>String(s||'').replace(/[<&]/g,c=>c==='<'?'&lt;':'&amp;');
 $('sbtab').innerHTML=r.songs.map(s=>'<tr><td style="font-family:monospace;color:var(--acc)">'+s.number+'</td><td>'+(s.mv?'<span class="pill">MV</span> ':'')+esc(s.title)+(s.chorus?' <span class="pill" title="this song has a chorus (recorded voices)">🎤 chorus</span>':'')+'</td><td class="mut">'+esc(s.artist)+'</td><td style="white-space:nowrap"><button class="sec sbp" data-n="'+s.number+'">▶ Sing</button> <button class="sec sbt" data-n="'+s.number+'" title="type the number only (then Enter yourself)">#</button></td></tr>').join('');
 document.querySelectorAll('.sbp').forEach(b=>b.onclick=async()=>{const p=await api('/api/songbook/play',{number:+b.dataset.n});if(p.error)alert(p.error)});
 document.querySelectorAll('.sbt').forEach(b=>b.onclick=async()=>{const p=await api('/api/songbook/play',{number:+b.dataset.n,enter:false});if(p.error)alert(p.error)})}
// the song index is checked on the server at every request: when it changed (disc edited / replaced), reload
setInterval(async()=>{if(sbRev===null)return;try{const r=await api('/api/songbook?start=0');if(r.rev&&r.rev!==sbRev)sbLoad()}catch(e){}},4000);
$('sbq').addEventListener('input',()=>{clearTimeout(sbTimer);sbTimer=setTimeout(()=>{sbStart=0;sbLoad()},300)});
$('sbonly').addEventListener('change',()=>{sbStart=0;sbLoad()});
$('sbprev').addEventListener('click',()=>{sbStart=Math.max(0,sbStart-50);sbLoad()});
$('sbnext').addEventListener('click',()=>{sbStart+=50;sbLoad()});
sbLoad();


let songStart=0, songFile=null, playerShown=null;
async function listSongs(){const r=await api('/api/songs?start='+songStart+(songFile?'&file='+encodeURIComponent(songFile):''));
 if(r.error){$('songinfo').textContent=r.error;return}
 songFile=r.file;$('songfile').innerHTML=r.files.map(f=>'<option'+(f===r.file?' selected':'')+'>'+f+'</option>').join('');
 $('songinfo').textContent='songs '+(r.start+1)+'–'+(r.start+r.songs.length)+' of '+r.total;
 $('songtab').innerHTML=r.songs.map((s,i)=>'<tr><td class="mut">'+(r.start+i+1)+'</td><td>'+(s.ok?(s.title||'(no title)'):'<span class="bad">'+s.error+'</span>')+'</td><td class="mut">'+(s.artist||'')+'</td><td>'+(s.ok?'<button class="sec play" data-o="'+s.offset+'">▶ play</button>':'')+'</td></tr>').join('');
 document.querySelectorAll('button.play').forEach(b=>b.onclick=async()=>{const p=await api('/api/song/play',{file:songFile,offset:+b.dataset.o});if(p.error)alert(p.error)})}
$('songlist').onclick=()=>{songStart=0;songFile=$('songfile').value||null;listSongs()};
$('songprev').onclick=()=>{songStart=Math.max(0,songStart-40);listSongs()};
$('songnext').onclick=()=>{songStart+=40;listSongs()};
$('songfile').onchange=()=>{songFile=$('songfile').value;songStart=0;listSongs()};
$('sfset').onclick=async()=>{const r=await api('/api/soundfont',{path:$('sfpath').value});$('player').textContent=r.error?r.error:('SoundFont: '+r.name+' ('+r.presets+' presets)')};
const CID=Math.random().toString(36).slice(2);
function stopAudio(){document.querySelectorAll('#player audio').forEach(a=>{try{a.pause();a.removeAttribute('src');a.load()}catch(e){}})}
async function pollPlayer(){const p=await api('/api/player');if(p.error)return;
 if(p.state==='ready'&&p.owner&&p.owner!==CID){if(playerShown!=='other'+p.title+p.offset){stopAudio();playerShown='other'+p.title+p.offset;$('player').innerHTML='<span class="mut">“'+(p.title||'')+'” is playing in another Project : Chiaki window.</span>'}return}
 if(p.state==='stopped'){if(document.querySelector('#player audio')){stopAudio();$('player').innerHTML='<span class="mut">Stopped.</span>'}playerShown=null;return}
 if(p.soundfont_path&&!$('sfpath').value)$('sfpath').value=p.soundfont_path;
 if(p.state==='live'||p.state==='live-file'){const key=p.state+p.title+p.offset;if(playerShown!==key){playerShown=key;
   $('player').innerHTML='<span class="pill ok">'+(p.source||'playing')+'</span> <b>'+(p.title||'')+'</b> '+(p.artist?'– '+p.artist:'')+' <span class="mut">(live MIDI: '+(p.device||'')+')</span> '+(p.state==='live-file'?'<button class="sec" id="mstop">■ stop</button>':'')+
   '<div class="row"><a href="/api/song.mid?t='+TOKEN+'" download="song.mid" style="color:var(--acc)">download .mid</a></div><pre style="white-space:pre-wrap;max-height:120px;overflow:auto">'+(p.lyrics||'').replace(/[<&]/g,c=>c==='<'?'&lt;':'&amp;')+'</pre>';
   const b=$('mstop');if(b)b.onclick=()=>api('/api/song/stop',{})}stopAudio();return}
 if(p.state==='rendering'){stopAudio();$('player').textContent='rendering “'+p.title+'” with '+p.soundfont+' … '+Math.round(100*(p.progress||0))+'%'}
 else if(p.state==='error')$('player').innerHTML='<span class="bad">'+p.error+'</span>';
 else if(p.state==='ready'&&playerShown!==p.title+p.offset+p.soundfont){playerShown=p.title+p.offset+p.soundfont;
   stopAudio();const r=await fetch('/api/song.wav?client='+CID,{headers:{'X-Token':TOKEN}});const url=URL.createObjectURL(await r.blob());window._songSrc=p.source;
   $('player').innerHTML=(p.source?'<span class="pill ok">'+p.source+'</span> ':'')+'<b>'+p.title+'</b> '+(p.artist?'– '+p.artist:'')+' <span class="mut">('+p.seconds+' s, '+p.soundfont+')</span><br><audio controls autoplay src="'+url+'" style="width:100%"></audio>'+
     '<div class="row"><a href="/api/song.mid?t='+TOKEN+'" download="song.mid" style="color:var(--acc)">download .mid</a></div><pre style="white-space:pre-wrap;max-height:120px;overflow:auto">'+(p.lyrics||'').replace(/[<&]/g,c=>c==='<'?'&lt;':'&amp;')+'</pre>'}}
setInterval(pollPlayer,700);pollPlayer();
// keep the rendered song with the emulated player: jump to its position once, then follow its clock smoothly
// (playback rate), so the music stays with the lyrics even when the emulator runs a little slow; stop when the
// player's song stops (no notes from the firmware for 6 s)
async function syncAudio(){const a=document.querySelector('#player audio');if(!a||window._songSrc!=='started by the emulated player')return;
 const s=await api('/api/status');if(s.song_clock===null||s.song_clock===undefined)return;
 const pos=s.emulated_seconds-s.song_clock;
 if(!a.dataset.synced){if(pos>0&&pos<a.duration){a.currentTime=pos;a.dataset.synced='1';a.play().catch(()=>{})}return}
 if(s.last_note_seconds!=null&&s.emulated_seconds-s.last_note_seconds>6&&a.currentTime<a.duration-3){stopAudio();$('player').innerHTML='<span class="mut">The player stopped the song.</span>';return}
 if(a.paused)return;const drift=a.currentTime-pos;
 if(Math.abs(drift)>0.8){a.currentTime=Math.max(0,pos);a.playbackRate=1}
 else a.playbackRate=Math.min(1.2,Math.max(0.8,1-drift*0.8))}
setInterval(syncAudio,500);
async function sendKey(k){const r=await api('/api/key',{key:k});if(r.error)$('keylog').textContent=r.error}
document.querySelectorAll('button.key').forEach(b=>b.onclick=()=>sendKey(+b.dataset.k));

// keyboard -> remote keys (editable in the main page; shared through the settings)
let KEYMAP={};const kmKey=ev=>ev.key.length===1?ev.key.toLowerCase():ev.key;
async function loadKeymap(){try{const r=await fetch('/api/config',{headers:{'X-Token':TOKEN}});const c=await r.json();KEYMAP=c.keymap||{}}catch(e){}}
function kmLookup(ev){if(ev.ctrlKey||ev.altKey||ev.metaKey)return null;const k=kmKey(ev);return (k in KEYMAP)?KEYMAP[k]:null}
loadKeymap();setInterval(loadKeymap,5000);
document.addEventListener('keydown',ev=>{if(kmCapture){kmCaptureKey(ev);return}if(['INPUT','SELECT','TEXTAREA'].includes(ev.target.tagName))return;const c=kmLookup(ev);if(c!==null){ev.preventDefault();sendKey(c)}});
document.addEventListener('keydown_old',ev=>{if(ev.target.tagName==='INPUT')return;if(/^[0-9]$/.test(ev.key))sendKey(+ev.key);else if(ev.key==='Enter'){ev.preventDefault();sendKey(109)}});
$('boot').onclick=async()=>{const r=await api('/api/boot',{fw:$('fw').value.trim(),assist:$('assist').checked,insert:$('insert').checked,quickboot:$('quick').checked,realtime_boot:$('rtboot').checked,speed:$('speed').value});if(r.error)alert(r.error);osdShown=0;liveSet=false;poll()};
$('speed').onchange=async()=>{const r=await api('/api/speed',{speed:$('speed').value});if(r.error)alert(r.error)};
let PFS=false;
function NATIVE(){return !!(window.pywebview&&window.pywebview.api&&window.pywebview.api.fullscreen)}
// the Android app's window: already full screen, no browser fullscreen there - the picture fills the window instead
const APPWV=/ProjectChiakiApp/.test(navigator.userAgent);if(APPWV)document.body.classList.add('appwv');   // (its buttons go away in fullscreen there)
async function pfs(on,keepWin){PFS=on;document.body.classList.toggle('pfs',on);try{if(NATIVE()&&!keepWin)await window.pywebview.api.fullscreen(on)}catch(e){}}
document.addEventListener('keydown',ev=>{if(PFS&&ev.key==='Escape'){pfs(false)}});
$('fsbtn').onclick=()=>{if(NATIVE()||APPWV){pfs(!PFS);return}const el=$('screen');(el.requestFullscreen||el.webkitRequestFullscreen).call(el)};
window.addEventListener('pywebviewready',async()=>{try{const m=await window.pywebview.api.mode();if(m==='fullscreen')pfs(true,true)}catch(e){}});
$('screen').ondblclick=()=>$('fsbtn').click();
$('popremote').onclick=()=>window.open('/remote?t='+TOKEN,'kemuremote','width=360,height=720');
$('popscreen').onclick=()=>{const a=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ar'))||1.5;window.open('/screen?t='+TOKEN,'kemuscreen','width=960,height='+Math.round(960/a))};
async function loadSound(){const r=await api('/api/sound');if(r.error)return;
 $('sound').innerHTML=r.devices.filter(d=>d.id!=='capture').map(d=>'<option value="live:'+d.id+'"'+(r.sound==='live'&&r.device===d.id?' selected':'')+'>MIDI: '+d.name+'</option>').join('')+'<option value="live:capture"'+(r.sound==='live'&&r.device==='capture'?' selected':'')+'>No sound</option>';
 $('soundinfo').textContent=(r.problem?'⚠ '+r.problem+' ':'')+(r.soundfont?'· '+r.soundfont+' ':'')+(r.windows?'':(APPWV?'':'(MIDI devices: Windows only)'));$('soundinfo').style.color=r.problem?'var(--bad)':'';$('soundinfo').title=r.log||''}
$('soundinfo').onclick=()=>{const t=$('soundinfo').title;if(t)alert(t)};   // (a phone has no tooltips)
$('sound').onchange=async()=>{const v=$('sound').value;const r=v==='render'?await api('/api/sound',{sound:'render'}):await api('/api/sound',{sound:'live',device:v.slice(5)});if(r.error)alert(r.error);loadSound()};
loadSound();
let liveSet=false;
function tfSet(sa,zm,bg,osd){const z=1+2*zm/100,o=z*(1-2*sa/100);const b=$(bg),im=$(osd);if(b)b.style.transform=z!==1?'scale('+z+')':'';if(bg==='bgimg')$('bgcv').style.transform=z!==1?'scale('+z+')':'';if(im)im.style.transform=o!==1?'scale('+o+')':''}
function applySafe(r,id){tfSet(0,+(r.headers.get('X-Zoom')||0),'bgimg',id)}
let bannerT=0;function showBanner(r){applySafe(r,'osdimg');const t=r.headers.get('X-Banner');if(!t)return;const b=$('banner');b.textContent=decodeURIComponent(t);b.style.display='block';clearTimeout(bannerT);bannerT=setTimeout(()=>{b.style.display='none'},+(r.headers.get('X-Banner-Ms')||3000))}
async function osdLoop(){while(true){try{const r=await fetch('/api/osd.png?after='+osdShown,{headers:{'X-Token':TOKEN}});
  if(r.ok){const v=+r.headers.get('X-OSD-Version');showBanner(r);if(v&&v!==osdShown){osdShown=v;const im=$('osdimg');const old=im.src;const url=URL.createObjectURL(await r.blob());const pre=new Image();pre.src=url;try{await pre.decode()}catch(e){}im.src=url;if(old)setTimeout(()=>URL.revokeObjectURL(old),500);$('scrmsg').style.display='none';$('osdimg').style.display=$('osdon').checked?'':'none'}else await r.arrayBuffer()}
  else{if(r.status===404&&osdShown){osdShown=0;const im=$('osdimg');im.removeAttribute('src');im.style.display='none'}await new Promise(z=>setTimeout(z,800))}}catch(e){await new Promise(z=>setTimeout(z,1000))}}}
osdLoop();
$('stop').onclick=async()=>{await api('/api/stop',{});poll()};
async function showDir(path){const r=await api('/api/disc/ls?path='+encodeURIComponent(path));if(r.error){alert(r.error);return}
 const up=path!=='/'?'<tr class="f" data-p="'+path.replace(/\/[^\/]*$/,'')+'" data-d="1"><td>↰ ..</td><td></td></tr>':'';
 $('dirs').innerHTML=up+r.entries.map(e=>'<tr class="f" data-p="'+(path==='/'?'':path)+'/'+e.name+'" data-d="'+(e.is_dir?1:0)+'"><td>'+(e.is_dir?'📁 ':'')+e.name+'</td><td class="sz">'+(e.is_dir?'':fmt(e.size))+'</td></tr>').join('');
 $('cwd').textContent=path;
 document.querySelectorAll('#dirs tr.f').forEach(tr=>tr.onclick=()=>{const p=tr.dataset.p||'/';if(tr.dataset.d==='1')showDir(p);else $('xp').value=p})}
function renderDisc(i){
 let kv='<div class="kv"><b>Type</b><span><span class="pill ok">'+i.type+'</span></span><b>Volume</b><span>'+(i.volume||'–')+'</span><b>Filesystem</b><span>'+i.filesystem+'</span><b>Layout</b><span>'+i.sector_layout+'</span><b>File</b><span>'+i.path+' ('+fmt(i.size)+')</span>';
 if(i.titles!==undefined)kv+='<b>Titles</b><span>'+i.titles+' in '+i.title_sets+' title set(s)</span>';
 (i.vts||[]).forEach(v=>{kv+='<b>'+(v.ifo||'').split('/').pop()+'</b><span>'+(v.pgc_play_times||[]).join(', ')+' · '+(v.vob_files||[]).length+' VOB · '+fmt(v.vob_bytes||0)+'</span>'});
 if(i.tracks)kv+='<b>Tracks</b><span>'+i.tracks.length+'</span>';
 if(i.contains)kv+='<b>Contains</b><span>'+i.contains.join(', ')+'</span>';
 kv+='</div>';
 $('disc').innerHTML=kv+'<div class="row"><button class="sec" id="th">Preview frame</button><span class="mut" id="cwd">/</span></div><div class="thumbs" id="thumbs"></div><table id="dirs"></table>'+
  '<div class="row"><input type="text" id="xp" class="grow" placeholder="/path/on/disc (click a file)"><input type="text" id="xd" class="grow" placeholder="save to, e.g. C:\\out\\file.vob"><button id="ex">Extract</button></div><div class="note" id="xmsg"></div>';
 $('th').onclick=async()=>{$('thumbs').innerHTML='decoding…';const r=await fetch('/api/disc/thumb',{headers:{'X-Token':TOKEN}});if(!r.ok){$('thumbs').innerHTML='<span class="bad">'+(await r.text())+'</span>';return}
   $('thumbs').innerHTML='<img src="'+URL.createObjectURL(await r.blob())+'">'};
 $('ex').onclick=async()=>{const r=await api('/api/disc/extract',{path:$('xp').value,dest:$('xd').value});$('xmsg').innerHTML=r.error?'<span class="bad">'+r.error+'</span>':'<span class="ok">wrote '+r.dest+' ('+fmt(r.bytes)+')</span>'};
 showDir('/')}
function mb(n){return n>=1<<30?(n/(1<<30)).toFixed(1)+' GB':(n/(1<<20)).toFixed(1)+' MB'}
function fillSel(sel,items,cur,label,empty){const el=$(sel);el.innerHTML='';const o0=document.createElement('option');o0.value='';o0.textContent=items.length?'— choose ('+items.length+' found) —':empty;el.appendChild(o0);
 for(const it of items){const o=document.createElement('option');o.value=it.path;o.textContent=label(it);if(cur&&it.path.toLowerCase()===cur.toLowerCase())o.selected=true;el.appendChild(o)}}
function showLib(L){LIB=L;const c=L.current||{};
 fillSel('fwsel',L.firmware,c.fw,it=>it.name+'  ·  '+it.player,'no firmware in the BIOS folder');
 fillSel('sfsel',L.soundfonts,c.soundfont,it=>it.name+'  ·  '+mb(it.size),'no .sf2 in the Soundfont folder');
 fillSel('isosel',L.discs,c.iso,it=>it.name+'  ·  '+(it.kind||'')+'  ·  '+mb(it.size),'no disc images in this folder');
 if(!$('discdir').value)$('discdir').placeholder=L.disc_dir}
let LIB=null;
async function loadLib(dir){const r=dir===undefined?await api('/api/library'):await api('/api/library',{disc_dir:dir});if(r.error){alert(r.error);return}showLib(r)}
$('fwsel').onchange=()=>{if($('fwsel').value)$('fw').value=$('fwsel').value};
$('sfsel').onchange=()=>{if($('sfsel').value){$('sfpath').value=$('sfsel').value;$('sfset').click()}};
$('isosel').onchange=()=>{const v=$('isosel').value;if(!v)return;$('iso').value=v;$('open').click();
 // a disc made for one player: pick that player's firmware from the BIOS folder too
 const d=(LIB&&LIB.discs||[]).find(x=>x.path===v);const f=d&&d.player&&(LIB.firmware||[]).find(x=>x.id===d.player);
 if(f&&$('fw').value!==f.path){$('fw').value=f.path;$('fwsel').value=f.path}};
$('discscan').onclick=()=>loadLib($('discdir').value.trim());
$('open').onclick=async()=>{const r=await api('/api/disc/open',{path:$('iso').value});if(r.error){$('disc').innerHTML='<span class="bad">'+r.error+'</span>';return}renderDisc(r)};
(async()=>{const c=await api('/api/config');if(c.fw)$('fw').value=c.fw;if(c.soundfont)$('sfpath').value=c.soundfont;if(c.disc_dir)$('discdir').value=c.disc_dir;loadLib();
 if(c.speed)$('speed').value=c.speed;$('insert').checked=!!c.insert;$('quick').checked=c.quickboot!==false;$('assist').checked=c.assist!==false;$('rtboot').checked=!!c.realtime_boot;$('osdon').checked=c.osd!==false;$('kvoice').checked=c.key_voice!==false;$('kchorus').checked=c.chorus!==false;$('kscore').checked=c.random_score!==false;$('mvdelay').value=c.mv_delay||0;$('lyrdelay').value=(c.lyrics_delay??250);setRes(c.resolution||'720x480');if(c.window_mode)$('winmode').value=c.window_mode;{const z=$('zoom'),v=+c.zoom||0;if(![...z.options].some(o=>+o.value===v)){const o=document.createElement('option');o.value=String(v);o.textContent=v+'%';z.appendChild(o)}z.value=String(v)}tfSet(0,+c.zoom||0,'bgimg','osdimg');fillBgv(c);
 if(c.settings_file)$('setnote').textContent='Settings are remembered in '+c.settings_file;
 if(c.iso){$('iso').value=c.iso;$('open').click()}})();
$('osdon').addEventListener('change',()=>api('/api/settings',{osd:$('osdon').checked}));
$('kvoice').addEventListener('change',()=>api('/api/settings',{key_voice:$('kvoice').checked}));
$('kchorus').addEventListener('change',()=>api('/api/settings',{chorus:$('kchorus').checked}));
$('kscore').addEventListener('change',()=>api('/api/settings',{random_score:$('kscore').checked}));
$('mvdelay').addEventListener('change',()=>api('/api/settings',{mv_delay:+$('mvdelay').value||0}));
function setRes(r){$('resol').value=r;const a={'640x480':4/3,'720x480':3/2,'1920x1080':16/9}[r]||1.5;document.documentElement.style.setProperty('--ar',a)}
function fillBgv(c){if(!c||!c.bgv_files)return;const s=$('bgvov');s.innerHTML='';const add=(v,t)=>{const o=document.createElement('option');o.value=v;o.textContent=t;s.appendChild(o)};add('',"off (the disc's own)");if(c.bgv_files.length)add('*','all videos in the folder ('+c.bgv_files.length+', in turn)');for(const f of c.bgv_files)add(f,f);if(c.bgv_override&&!c.bgv_files.includes(c.bgv_override)&&c.bgv_override!=='*')add(c.bgv_override,c.bgv_override+' (missing)');s.value=c.bgv_override||'';if(document.activeElement!==$('bgvdir'))$('bgvdir').value=c.bgv_dir||''}
$('bgvov').addEventListener('change',async()=>{const c=await api('/api/settings',{bgv_override:$('bgvov').value});fillBgv(c)});
$('bgvdirset').onclick=async()=>{const c=await api('/api/settings',{bgv_dir:$('bgvdir').value});if(c&&c.error){alert(c.error);return}fillBgv(c)};
$('bgvopen').onclick=()=>api('/api/settings',{bgv_open:1});
$('resol').addEventListener('change',()=>{setRes($('resol').value);api('/api/settings',{resolution:$('resol').value})});
$('safearea').addEventListener('change',()=>{const v=+$('safearea').value||0;tfSet(v,+$('zoom').value||0,'bgimg','osdimg');api('/api/settings',{safe_area:v})});
let CAL=null;function calibShow(on){const c=$('calib');if(on){CAL=+$('zoom').value||0;c.classList.add('on');calibSet(CAL);if(!PFS&&typeof pfs==='function')pfs(true)}else{c.classList.remove('on');c.style.transform='';api('/api/settings',{zoom:CAL});const z=$('zoom');if(![...z.options].some(o=>+o.value===CAL)){const o=document.createElement('option');o.value=String(CAL);o.textContent=CAL+'%';z.appendChild(o)}z.value=String(CAL);CAL=null}}
function calibSet(v){CAL=Math.max(-10,Math.min(25,Math.round(v*2)/2));const sc=1+2*CAL/100;$('calib').style.transform=sc!==1?'scale('+sc+')':'';$('calibv').textContent=Math.round(sc*100)+'%';tfSet(+$('safearea').value||0,CAL,'bgimg','osdimg')}
$('calibbtn').onclick=()=>calibShow(true);
$('calminus').onclick=ev=>{ev.stopPropagation();calibSet(CAL-0.5)};$('calplus').onclick=ev=>{ev.stopPropagation();calibSet(CAL+0.5)};$('caldone').onclick=ev=>{ev.stopPropagation();calibShow(false)};
document.addEventListener('keydown',ev=>{if(CAL===null)return;ev.preventDefault();ev.stopImmediatePropagation();if(ev.key==='ArrowUp')calibSet(CAL+0.5);else if(ev.key==='ArrowDown')calibSet(CAL-0.5);else if(ev.key==='Enter'||ev.key==='Escape')calibShow(false)},true);
$('zoom').addEventListener('change',()=>{const v=+$('zoom').value||0;tfSet(+$('safearea').value||0,v,'bgimg','osdimg');api('/api/settings',{zoom:v})});
$('winmode').addEventListener('change',async()=>{const m=$('winmode').value;await api('/api/settings',{window_mode:m});if(m==='fullscreen')pfs(true);else if(PFS)pfs(false,true)});
$('lyrdelay').addEventListener('change',()=>api('/api/settings',{lyrics_delay:+$('lyrdelay').value||0}));
</script></body></html>"""


SONGBOOK_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Project : Chiaki songbook</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>:root{--bg:#10131a;--card:#181d28;--ink:#e8ecf4;--mut:#8b95a9;--acc:#ffb020;--line:#2a3142}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,Segoe UI,sans-serif;padding:12px}
h1{font-size:16px;margin:0 0 10px}input,select{padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit}
button{padding:6px 11px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer;font-weight:600}
.row{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;align-items:center}#q{flex:1;min-width:160px}
table{width:100%;border-collapse:collapse}td{padding:5px 6px;border-bottom:1px solid var(--line)}.n{font-family:monospace;color:var(--acc)}.m{color:var(--mut)}
.pill{border:1px solid var(--line);border-radius:99px;padding:0 7px;font-size:11px;margin-right:4px}</style></head><body>
<h1>📖 Songbook</h1>
<div class="row"><input id="q" placeholder="search a title, singer or number" autofocus><select id="only"><option value="">all</option><option value="songs">songs</option><option value="mv">music videos</option></select>
<button id="prev">◀</button><button id="next">▶</button><span class="m" id="info"></span></div>
<table id="tab"></table>
<script>const TOKEN="__TOKEN__";const $=id=>document.getElementById(id);
const api=async(p,b)=>{const o={headers:{'X-Token':TOKEN}};if(b!==undefined){o.method='POST';o.body=JSON.stringify(b);o.headers['Content-Type']='application/json'}const r=await fetch(p,o);return r.json()};
let st=0,tm=null;const esc=s=>String(s||'').replace(/[<&]/g,c=>c==='<'?'&lt;':'&amp;');
let rev=null;
async function load(){const r=await api('/api/songbook?q='+encodeURIComponent($('q').value)+'&only='+$('only').value+'&start='+st);
 if(r.error){$('info').textContent=r.error;$('tab').innerHTML='';rev='none';return}
 rev=r.rev;
 $('info').textContent=r.total?((st+1)+'–'+Math.min(st+50,r.total)+' of '+r.total):'no songs found';
 $('tab').innerHTML=r.songs.map(s=>'<tr><td class="n">'+s.number+'</td><td>'+(s.mv?'<span class="pill">MV</span>':'')+esc(s.title)+(s.chorus?' <span class="pill" title="this song has a chorus (recorded voices)">🎤</span>':'')+'<div class="m">'+esc(s.artist)+'</div></td><td style="white-space:nowrap"><button data-n="'+s.number+'" class="p">▶ Sing</button> <button data-n="'+s.number+'" class="t" title="type the number only">#</button></td></tr>').join('');
 document.querySelectorAll('.p').forEach(b=>b.onclick=()=>api('/api/songbook/play',{number:+b.dataset.n}).then(r=>{if(r.error)alert(r.error)}));
 document.querySelectorAll('.t').forEach(b=>b.onclick=()=>api('/api/songbook/play',{number:+b.dataset.n,enter:false}).then(r=>{if(r.error)alert(r.error)}))}
$('q').oninput=()=>{clearTimeout(tm);tm=setTimeout(()=>{st=0;load()},300)};$('only').onchange=()=>{st=0;load()};
$('prev').onclick=()=>{st=Math.max(0,st-50);load()};$('next').onclick=()=>{st+=50;load()};load();
setInterval(async()=>{if(rev===null)return;try{const r=await api('/api/songbook?start=0');if(r.rev&&r.rev!==rev)load()}catch(e){}},4000);
</script></body></html>"""


SCREEN_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Project : Chiaki screen</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden;cursor:default}
#wrap{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
#tv{position:relative;width:min(100vw,calc(100vh * __AR__));height:min(100vh,calc(100vw / __AR__))}
#tv img,#tv canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:fill}
#tv{container-type:size;overflow:hidden}
#banner{position:absolute;left:6%;top:7%;display:none;color:#3cf03c;font:bold 6.2cqh/1 'Courier New',monospace;letter-spacing:.06em;text-shadow:0 0 .12em #000,.08em .08em 0 #000,-.06em -.06em 0 #000;pointer-events:none}
#hint{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);color:#ccd;background:rgba(0,0,0,.6);font:13px system-ui,sans-serif;padding:6px 12px;border-radius:8px;transition:opacity .6s}
body.idle{cursor:none}body.idle #hint{opacity:0}</style></head><body>
<div id="wrap"><div id="tv"><canvas id="bg"></canvas><img id="osd" alt=""><div id="banner"></div></div></div>
<div id="hint">double-click or F: fullscreen · your keyboard keys (set in the main page) work here</div>
<script>
const TOKEN="__TOKEN__";const $=id=>document.getElementById(id);
// Live background video: the MJPEG stream read with fetch and drawn on a canvas once per screen refresh. As an
// <img> stream the browser painted the pictures when they arrived (uneven: stutter, worse while OBS captures).
function mkLive(cvId,afterStop){let ctl=null,raf=0;const cv=$(cvId),cx=cv.getContext('2d');
 const q=[];let iv=33.3,lastIn=0,nextAt=0;
 // pictures arrive with a few ms of jitter: shown straight away, a 30 fps video on a 60 Hz screen got 1 / 2 / 3
 // refreshes per picture (judder). They wait in a short queue and are shown at the video's own steady rate.
 function draw(t){raf=0;if(!q.length){nextAt=0;return}
  if(!nextAt||t-nextAt>3*iv)nextAt=t;
  if(t+4>=nextAt){const b=q.shift();if(cv.width!==b.width||cv.height!==b.height){cv.width=b.width;cv.height=b.height}cx.drawImage(b,0,0);b.close();nextAt+=iv;
   if(q.length>3){while(q.length>2)q.shift().close();nextAt=t+iv}}
  if(q.length)raf=requestAnimationFrame(draw)}
 function stop(){if(ctl){ctl.abort();ctl=null}cv.style.display='none';while(q.length)q.shift().close();nextAt=0}
 async function run(c){let buf=new Uint8Array(0);
  try{const r=await fetch('/api/bgv.mjpg?t='+TOKEN,{signal:c.signal});if(!r.ok)throw 0;const rd=r.body.getReader();
   while(true){const {done,value}=await rd.read();if(done)break;const nb=new Uint8Array(buf.length+value.length);nb.set(buf);nb.set(value,buf.length);buf=nb;
    while(true){let h=-1;for(let i=0;i+3<buf.length;i++){if(buf[i]===13&&buf[i+1]===10&&buf[i+2]===13&&buf[i+3]===10){h=i;break}}
     if(h<0)break;const head=new TextDecoder().decode(buf.subarray(0,h));const m=/Content-Length:\s*(\d+)/i.exec(head);if(!m){buf=buf.subarray(h+4);continue}
     const n=+m[1],st=h+4;if(buf.length<st+n)break;const jpg=buf.slice(st,st+n);buf=buf.subarray(st+n);
     try{const bmp=await createImageBitmap(new Blob([jpg],{type:'image/jpeg'}));if(c.signal.aborted){bmp.close();return}const now=performance.now();if(lastIn){const d=now-lastIn;if(d>8&&d<120)iv=iv*0.9+d*0.1}lastIn=now;q.push(bmp);cv.style.display='';if(!raf)raf=requestAnimationFrame(draw)}catch(e){}}}
  }catch(e){}
  if(ctl===c&&!c.signal.aborted)setTimeout(()=>{if(ctl===c){ctl=new AbortController();run(ctl)}},1000)}
 return {start(){stop();ctl=new AbortController();run(ctl)},stop(){stop();if(afterStop)afterStop()},on(){return !!ctl}}}
const LIVE=mkLive('bg');LIVE.start();
let shown=0;
(async function osd(){while(true){try{const r=await fetch('/api/osd.png?after='+shown,{headers:{'X-Token':TOKEN}});
 if(r.ok){const v=+r.headers.get('X-OSD-Version');{const sa=0,zm=+(r.headers.get('X-Zoom')||0),z=1+2*zm/100,o=z*(1-2*sa/100);$('bg').style.transform=z!==1?'scale('+z+')':'';$('osd').style.transform=o!==1?'scale('+o+')':''}{const t=r.headers.get('X-Banner');if(t){const b=$('banner');b.textContent=decodeURIComponent(t);b.style.display='block';clearTimeout(window._bt);window._bt=setTimeout(()=>{b.style.display='none'},+(r.headers.get('X-Banner-Ms')||3000))}}if(v&&v!==shown){shown=v;const url=URL.createObjectURL(await r.blob());const pre=new Image();pre.src=url;try{await pre.decode()}catch(e){}
  const im=$('osd');const old=im.src;im.src=url;if(old)setTimeout(()=>URL.revokeObjectURL(old),500)}else await r.arrayBuffer()}
 else{if(r.status===404){shown=0;$('osd').removeAttribute('src')}await new Promise(z=>setTimeout(z,800))}}catch(e){await new Promise(z=>setTimeout(z,1000))}}})();
function fs(){if(typeof NATIVE==='function'&&NATIVE()){pfs(!PFS);return}if(document.fullscreenElement)document.exitFullscreen();else (document.documentElement.requestFullscreen||document.documentElement.webkitRequestFullscreen).call(document.documentElement)}
document.addEventListener('dblclick',fs);
const send=k=>fetch('/api/key',{method:'POST',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:JSON.stringify({key:k})});


// keyboard -> remote keys (editable in the main page; shared through the settings)
let KEYMAP={};const kmKey=ev=>ev.key.length===1?ev.key.toLowerCase():ev.key;
async function loadKeymap(){try{const r=await fetch('/api/config',{headers:{'X-Token':TOKEN}});const c=await r.json();KEYMAP=c.keymap||{}}catch(e){}}
function kmLookup(ev){if(ev.ctrlKey||ev.altKey||ev.metaKey)return null;const k=kmKey(ev);return (k in KEYMAP)?KEYMAP[k]:null}
loadKeymap();setInterval(loadKeymap,5000);
document.addEventListener('keydown',ev=>{if(ev.key==='f'||ev.key==='F'){fs();return}
 if(ev.key==='Escape'&&document.fullscreenElement)return;const c=kmLookup(ev);if(c!==null){ev.preventDefault();send(c)}});
let t=null;function wake(){document.body.classList.remove('idle');clearTimeout(t);t=setTimeout(()=>document.body.classList.add('idle'),2500)}
document.addEventListener('mousemove',wake);wake();
</script></body></html>"""


REMOTE_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Project : Chiaki remote</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;background:#15171c;color:#e8ecf4;font:14px system-ui,Segoe UI,sans-serif;display:flex;justify-content:center}
.rc{width:min(360px,100vw);box-sizing:border-box;padding:14px 14px 20px;background:linear-gradient(#2a2e38,#1c1f26);border-radius:0 0 28px 28px;box-shadow:0 8px 30px #0008}
h1{font-size:13px;text-align:center;color:#ff9bcb;letter-spacing:.2em;margin:4px 0 10px}
.g{display:grid;gap:7px;margin:9px 0}.g3{grid-template-columns:repeat(3,1fr)}.g4{grid-template-columns:repeat(4,1fr)}
button{height:42px;border-radius:12px;border:1px solid #444b5a;background:#303644;color:#fff;font:600 13px system-ui;cursor:pointer}
button:active{background:#ff4fa3}button.num{font-size:19px;height:48px}button.ok{background:#ff4fa3;border-color:#ff4fa3}
button.red{background:#b23a48}.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:10px 0}.pad .e{visibility:hidden}
#log{font-size:11px;color:#8b95a9;text-align:center;min-height:14px}
.sb{margin-top:12px}#sq{width:100%;box-sizing:border-box;height:40px;border-radius:12px;border:1px solid #444b5a;background:#11141a;color:#fff;padding:0 12px;font:14px system-ui}
#sres{max-height:46vh;overflow:auto;margin-top:6px}.si{display:flex;gap:8px;align-items:center;padding:7px 4px;border-bottom:1px solid #2a2f3a}
.si .n{color:#ff9bcb;font-variant-numeric:tabular-nums;min-width:52px}.si .t{flex:1;min-width:0}.si .t b{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.si .t span{color:#8b95a9;font-size:12px}
.si button{height:34px;padding:0 10px;font-size:12px}button.on{background:#ff4fa3;border-color:#ff4fa3}
</style></head><body><div class="rc"><h1>PROJECT CHIAKI · REMOTE</h1>
<div class="g g3"><button class="red" data-k="39">⏻ Power</button><button data-k="13">🔇 Mute</button><button data-k="12">⏏ Eject</button></div>
<div class="g g3"><button class="num" data-k="1">1</button><button class="num" data-k="2">2</button><button class="num" data-k="3">3</button>
<button class="num" data-k="4">4</button><button class="num" data-k="5">5</button><button class="num" data-k="6">6</button>
<button class="num" data-k="7">7</button><button class="num" data-k="8">8</button><button class="num" data-k="9">9</button>
<button data-k="75" title="opens the player's Setup menu (password 0000)">⚙ Setup</button><button class="num" data-k="0">0</button><button class="ok" data-k="72">Enter ⏎</button></div>
<div class="pad"><span class="e"></span><button data-k="41">▲</button><span class="e"></span><button data-k="43">◀</button><button class="ok" data-k="72">OK</button><button data-k="44">▶</button><span class="e"></span><button data-k="42">▼</button><span class="e"></span></div>
<div class="g g4"><button data-k="29">⏮</button><button data-k="27">⏪</button><button data-k="28">⏩</button><button data-k="30">⏭</button></div>
<div class="g g3"><button data-k="109">⏫ First reserve</button><button data-k="74" title="clear the number / reserved list: press again to delete">✖ Cancel</button><button data-k="29">⏮ Prev</button></div>
<div class="g g3"><button class="red" data-k="25">■ Stop</button><button data-k="34">Play mode</button><button data-k="31">Display</button></div>
<div class="g g4"><button data-k="17">Vol +</button><button data-k="18">Vol −</button><button data-k="14">Key ♯</button><button data-k="16">Key ♭</button></div>
<div class="g g4"><button data-k="66" class="k66">Karaoke</button><button data-k="72">Mix</button><button data-k="73">Book</button><button data-k="15" title="key back to the original">Key ♮</button></div>
<div class="g g3"><button id="rmelody" title="the song's guide melody on / off">🎵 Melody</button><button id="rmf" title="original key → male (5 lower) → female (5 higher)">⚥ M/F</button><button data-k="13">🔇 Mute</button></div>
<div class="g g4"><button data-k="70">Menu</button><button data-k="69">Reading</button><button data-k="52">Intro</button><button data-k="40">Zoom</button></div>
<div class="g g3"><button class="ok" data-k="114" style="grid-column:span 3">🎞 Change background video</button></div>
<div id="log">keys go to the emulated player · number keys and Enter work on the keyboard</div>
<div class="sb"><input id="sq" placeholder="🔎 Search songs: title, artist or number" autocomplete="off"><div id="sres"></div></div></div>
<script>
const TOKEN="__TOKEN__";
async function send(k){const r=await fetch('/api/key',{method:'POST',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:JSON.stringify({key:k})});
 const j=await r.json().catch(()=>({}));document.getElementById('log').textContent=j.error?j.error:'sent key '+k}
document.querySelectorAll('button[data-k]').forEach(b=>b.onclick=()=>send(+b.dataset.k));
const J=(p,b)=>fetch(p,b===undefined?{headers:{'X-Token':TOKEN}}:{method:'POST',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
function synthShow(r){if(!r)return;const m=document.getElementById('rmelody'),f=document.getElementById('rmf');
 m.textContent=r.mel_text||(r.melody===false?'🎵 Melody off':'🎵 Melody');m.classList.toggle('on',r.mel_on??(r.melody===false));
 const t=r.mf??r.transpose;f.textContent=t<0?'♂ M/F':t>0?'♀ M/F':'⚥ M/F';f.classList.toggle('on',!!t)}
document.getElementById('rmelody').onclick=()=>J('/api/synth',{melody_toggle:1}).then(synthShow);
document.getElementById('rmf').onclick=()=>J('/api/synth',{mf_cycle:1}).then(synthShow);
// the player's own names for keys that differ (Megapro: the Karaoke key is Play / Pause)
const KEYNAMES={mp100:{66:'⏯ Play/Pause'},migael:{66:'⏯ Play/Pause'}};
async function look(){try{const s=await J('/api/status');const n=KEYNAMES[s.fw_id]||{};document.querySelectorAll('.k66').forEach(b=>b.textContent=n[66]||'Karaoke');
 const h=document.querySelector('h1');if(h&&s.fw_id)h.textContent='PROJECT CHIAKI · REMOTE'}catch(e){}try{synthShow(await J('/api/synth'))}catch(e){}}
look();setInterval(look,4000);
// songbook: search the disc's songs and reserve one (it is typed on the remote for you)
let sqT=null;document.getElementById('sq').addEventListener('input',()=>{clearTimeout(sqT);sqT=setTimeout(search,250)});
async function search(){const q=document.getElementById('sq').value.trim(),box=document.getElementById('sres');if(!q){box.innerHTML='';return}
 const r=await J('/api/songbook?q='+encodeURIComponent(q));box.innerHTML='';if(r.error){box.textContent=r.error;return}
 (r.songs||[]).slice(0,50).forEach(s=>{const d=document.createElement('div');d.className='si';
  d.innerHTML='<span class="n"></span><span class="t"><b></b><span></span></span><button>▶ Reserve</button>';
  d.querySelector('.n').textContent=s.number;d.querySelector('b').textContent=s.title||'';d.querySelector('.t span').textContent=s.artist||'';
  d.querySelector('button').onclick=async()=>{const j=await J('/api/songbook/play',{number:s.number,enter:true});document.getElementById('log').textContent=j.error||('reserved '+s.number+' · '+(s.title||''))};
  box.appendChild(d)})}

// keyboard -> remote keys (editable in the main page; shared through the settings)
let KEYMAP={};const kmKey=ev=>ev.key.length===1?ev.key.toLowerCase():ev.key;
async function loadKeymap(){try{const r=await fetch('/api/config',{headers:{'X-Token':TOKEN}});const c=await r.json();KEYMAP=c.keymap||{}}catch(e){}}
function kmLookup(ev){if(ev.ctrlKey||ev.altKey||ev.metaKey)return null;const k=kmKey(ev);return (k in KEYMAP)?KEYMAP[k]:null}
loadKeymap();setInterval(loadKeymap,5000);
document.addEventListener('keydown',ev=>{const c=kmLookup(ev);if(c!==null){ev.preventDefault();send(c)}});
document.addEventListener('keydown_old',ev=>{if(/^[0-9]$/.test(ev.key))send(+ev.key);else if(ev.key==='Enter'){ev.preventDefault();send(109)}});
</script></body></html>"""


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = 'ProjectChiaki/' + VERSION

        def log_message(self, *a): pass

        def _ok_origin(self):
            host = self.headers.get('Host', '')
            return host.split(':')[0] in ('127.0.0.1', 'localhost')

        def _send(self, code, body, ctype='application/json'):
            if isinstance(body, (dict, list)): body = json.dumps(body, default=str).encode()
            elif isinstance(body, str): body = body.encode()
            self.send_response(code); self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body)

        def _authed(self, q):
            tok = self.headers.get('X-Token') or (q.get('t') or [''])[0]
            return self._ok_origin() and secrets.compare_digest(tok or '', app.token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
            if u.path == '/':
                if not self._ok_origin(): return self._send(403, 'forbidden', 'text/plain')
                return self._send(200, PAGE.replace('__TOKEN__', app.token).replace('__VERSION__', VERSION), 'text/html; charset=utf-8')
            if u.path == '/favicon.ico':
                ico = _icon_bytes()
                if not ico: return self._send(404, 'no icon', 'text/plain')
                return self._send(200, ico, 'image/x-icon')
            if u.path == '/songbook':
                if not self._authed(q): return self._send(403, 'forbidden', 'text/plain')
                return self._send(200, SONGBOOK_PAGE.replace('__TOKEN__', app.token), 'text/html; charset=utf-8')
            if u.path == '/screen':
                if not self._authed(q): return self._send(403, 'forbidden', 'text/plain')
                return self._send(200, SCREEN_PAGE.replace('__TOKEN__', app.token).replace('__AR__', '%.6f' % resolution(app.settings.get('resolution'))[1]), 'text/html; charset=utf-8')
            if u.path == '/remote':
                if not self._authed(q): return self._send(403, 'forbidden', 'text/plain')
                return self._send(200, REMOTE_PAGE.replace('__TOKEN__', app.token), 'text/html; charset=utf-8')
            if not u.path.startswith('/api/') or not self._authed(q): return self._send(403, {'error': 'forbidden'})
            try:
                if u.path == '/api/status':
                    return self._send(200, app.job.status() if app.job else dict(state='idle', frames=[], irqs={}, ffmpeg=bool(ffmpeg_path()), budget=1))
                if u.path == '/api/config':
                    return self._send(200, app.config())
                if u.path == '/api/library':
                    lib = library(app.settings.get('disc_dir') or None)
                    lib.update(current=dict(fw=disp_path(app.fw), soundfont=disp_path(app.sf_path), iso=disp_path(app.disc_path)))
                    return self._send(200, lib)
                if u.path == '/api/synth':
                    return self._send(200, app.synth_state(full=bool(q.get('full'))))
                if u.path == '/api/smartlink' and type(self).__name__ == 'H':
                    return self._send(200, app.smartlink())
                if u.path == '/api/songbook':
                    g = lambda k, d='': (q.get(k) or [d])[0]
                    return self._send(200, app.songbook(g('q'), g('only') or None, int(g('start', '0') or 0), 50))
                if u.path == '/api/osd.png':
                    j = app.job
                    if not j: return self._send(404, {'error': 'no OSD yet'})
                    after = int((q.get('after') or ['-1'])[0])
                    if after >= 0 and j.osd_version <= after and j.is_alive():      # wait (up to 2 s) for the next change
                        with j.osd_cond: j.osd_cond.wait(2.0)
                    if j.osd_png is None: return self._send(404, {'error': 'no OSD yet'})
                    self.send_response(200); self.send_header('Content-Type', 'image/png'); self.send_header('Content-Length', str(len(j.osd_png)))
                    self.send_header('X-OSD-Version', str(j.osd_version)); self.send_header('Cache-Control', 'no-store')
                    try:
                        self.send_header('X-Safe', str(float(app.settings.get('safe_area', 0) or 0)))
                        self.send_header('X-Zoom', str(float(app.settings.get('zoom', 0) or 0)))
                    except Exception: pass
                    bn = getattr(j, 'banner', None)
                    if bn and bn[1] > time.time():
                        self.send_header('X-Banner', urllib.parse.quote(bn[0])); self.send_header('X-Banner-Ms', str(int((bn[1] - time.time()) * 1000)))
                    self.end_headers()
                    self.wfile.write(j.osd_png); return
                if u.path == '/api/bgv.mjpg':
                    j = app.job
                    if not j or not j.video.available(): return self._send(404, {'error': 'no live video (ffmpeg missing?)'})
                    self.send_response(200); self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=kframe')
                    self.send_header('Cache-Control', 'no-store'); self.end_headers()
                    v = 0                                   # version 0 = no frame yet (waiting on it must block,
                    try:                                    # not spin: a spinning loop starved the emulator thread)
                        while j.is_alive() or v < j.video.version:
                            v2, jpg = j.video.wait(v, 1.0)
                            if jpg is None or v2 == v:
                                if v2 > v: time.sleep(0.05)
                                continue
                            v = v2
                            self.wfile.write(b'--kframe\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(jpg)).encode() + b'\r\n\r\n' + jpg + b'\r\n')
                            self.wfile.flush()
                            vd_ = j.video; t_ = time.perf_counter(); ls_ = getattr(vd_, '_last_sent', None); vd_._last_sent = t_
                            if ls_ is not None: vd_.sent_gap_max = max(getattr(vd_, 'sent_gap_max', 0.0), t_ - ls_); vd_.sent_n = getattr(vd_, 'sent_n', 0) + 1
                            vd_.jpg_kb = len(jpg) // 1024
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return
                if u.path == '/api/sound':
                    return self._send(200, app.sound_status())
                if u.path == '/api/bgv.jpg':
                    if not app.job or app.job.bgv_jpg is None: return self._send(404, {'error': 'no background picture yet'})
                    return self._send(200, app.job.bgv_jpg, 'image/jpeg')
                if u.path == '/api/fw_midi.mid':
                    m = app.job.fw_midi() if app.job else None
                    if not m: return self._send(404, {'error': 'the emulated player has not sent any MIDI yet'})
                    return self._send(200, m, 'audio/midi')
                if u.path.startswith('/api/frame/'):
                    name = u.path.rsplit('/', 1)[1]; idx = int(name.split('.')[0]); fr = app.job.frames[idx]
                    if name.endswith('.m2v'): return self._send(200, fr['m2v'], 'video/mpeg')
                    if fr['png'] is None: return self._send(404, {'error': 'no png (ffmpeg missing)'})
                    return self._send(200, fr['png'], 'image/png')
                if u.path == '/api/songs':
                    return self._send(200, app.list_songs((q.get('file') or [None])[0], int((q.get('start') or ['0'])[0]), int((q.get('n') or ['40'])[0])))
                if u.path == '/api/player':
                    return self._send(200, app.player_status())
                if u.path == '/api/song.wav':
                    if not app.player.get('wav'): return self._send(404, {'error': 'no rendered song'})
                    app.player['owner'] = (q.get('client') or [''])[0]       # only the latest page plays it
                    return self._send(200, app.player['wav'], 'audio/wav')
                if u.path == '/api/song.mid':
                    if not app.player.get('mid'): return self._send(404, {'error': 'no song'})
                    return self._send(200, app.player['mid'], 'audio/midi')
                if u.path == '/api/disc/ls':
                    if not app.disc: return self._send(400, {'error': 'no disc loaded'})
                    return self._send(200, dict(entries=app.disc.listdir((q.get('path') or ['/'])[0])))
                if u.path == '/api/disc/thumb':
                    if not app.disc: return self._send(400, 'no disc loaded', 'text/plain')
                    if app.thumb is None:
                        import tempfile
                        fd, tmp = tempfile.mkstemp(suffix='.png'); os.close(fd); os.unlink(tmp)
                        try:
                            app.disc.thumbnail(tmp); app.thumb = open(tmp, 'rb').read()
                        finally:
                            if os.path.exists(tmp): os.unlink(tmp)
                    return self._send(200, app.thumb, 'image/png')
            except isoread.DiscError as e:
                return self._send(400, str(e) if 'thumb' in u.path else {'error': str(e)}, 'text/plain' if 'thumb' in u.path else 'application/json')
            except Exception as e:
                return self._send(500, {'error': '%s: %s' % (type(e).__name__, e)})
            self._send(404, {'error': 'not found'})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if not self._authed({}): return self._send(403, {'error': 'forbidden'})
            try:
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'{}')
            except Exception:
                return self._send(400, {'error': 'bad json'})
            try:
                if u.path == '/api/open_link':
                    url_ = str(body.get('url', ''))
                    if url_ not in SOCIAL_LINKS: return self._send(400, {'error': 'unknown link'})
                    ok_ = False
                    if not os.environ.get('KE_ANDROID'):
                        try: ok_ = bool(webbrowser.open(url_))
                        except Exception: ok_ = False
                    return self._send(200, {'ok': ok_})
                if u.path == '/api/boot':
                    app.start_job(fw=real_path(body.get('fw')) or None, budget=body.get('budget'), assist=bool(body.get('assist', True)), insert=bool(body.get('insert', False)), quickboot=bool(body.get('quickboot', True)), realtime_boot=bool(body.get('realtime_boot', False)), speed=body.get('speed'))
                    return self._send(200, {'ok': True})
                if u.path == '/api/key':
                    if not app.job or not app.job.is_alive(): return self._send(400, {'error': 'emulator is not running'})
                    k = int(body.get('key')); app.job.press(k)
                    if k == 25 and app.player.get('source') == 'started by the emulated player' and app.player.get('state') in ('ready', 'rendering'):
                        app.player['cancelled'] = True           # stop on the remote: the page stops the rendered audio,
                        app.player['state'] = 'stopped'          # and a song still rendering is never played
                        app.fw_now = None
                    return self._send(200, {'ok': True})
                if u.path == '/api/stop':
                    if app.job: app.job.stop_flag = True
                    return self._send(200, {'ok': True})
                if u.path == '/api/sound':
                    return self._send(200, app.set_sound(body.get('sound'), body.get('device')))
                if u.path == '/api/synth':
                    return self._send(200, app.set_synth(body))
                if u.path == '/api/smartlink':
                    return self._send(200, app.smartlink(bool(body.get('on'))))
                if u.path == '/api/songbook/play':
                    try: return self._send(200, app.songbook_play(body.get('number'), body.get('enter', True)))
                    except Exception as ex: return self._send(400, {'error': str(ex)})
                if u.path == '/api/library':
                    dd = real_path(body.get('disc_dir') or '')
                    if dd and not os.path.isdir(dd): return self._send(400, dict(error='folder not found: ' + disp_path(dd)))
                    app.save_settings(disc_dir=dd)
                    lib = library(dd or None)
                    lib.update(current=dict(fw=disp_path(app.fw), soundfont=disp_path(app.sf_path), iso=disp_path(app.disc_path)))
                    return self._send(200, lib)
                if u.path == '/api/settings':
                    app.save_settings(**{k: bool(body[k]) for k in ('osd', 'key_voice', 'chorus', 'random_score') if k in body})
                    if 'lyrics_delay' in body:
                        ld = min(1000, max(0, int(float(body['lyrics_delay'] or 0)))); app.save_settings(lyrics_delay=ld)
                        if app.job: app.job.LYRICS_DELAY = ld / 1000.0
                    if 'bgv_dir' in body:
                        dd = real_path(body.get('bgv_dir') or '')
                        if dd and not os.path.isdir(dd): return self._send(400, dict(error='folder not found: ' + disp_path(dd)))
                        app.save_settings(bgv_dir=dd if dd and os.path.normcase(dd) != os.path.normcase(app_folder('bgv')) else '')
                        app.apply_bgv_override()
                    if 'bgv_override' in body:
                        app.save_settings(bgv_override=str(body.get('bgv_override') or ''))
                        app.apply_bgv_override()
                        ov_ = app.settings.get('bgv_override') or ''
                        app.note('BGV: ' + ("disc" if not ov_ else 'MY VIDEOS' if ov_ == '*' else os.path.splitext(ov_)[0]))
                    if body.get('bgv_open'):
                        try:
                            d_ = app.bgv_folder()
                            if os.name == 'nt': os.startfile(d_)
                        except Exception: pass
                    fw_bgv = (app.job is not None and not app.job.video.override and int(body.get('bgv_change', 0) or 0) > 0
                              and __import__('fwprofile').extra(soc.FW_ID).get('bgv_call')
                              and getattr(app.job.remote, 'direct', False))
                    if 'bgv_change' in body and fw_bgv:
                        app.job.press(114)                      # the player's own Change video (its own message)
                        return self._send(200, dict(ok=True, bgv_name="player's next"))
                    if 'bgv_change' in body:
                        name = app.job.video.change_bgv(int(body['bgv_change'])) if app.job else None
                        if app.job:
                            v_ = app.job.video
                            if v_.override: app.note('BGV %d/%d' % (v_.override_i + 1, len(v_.override)))
                            elif v_.custom is not None: app.note('BGV %d/%d %s' % (v_.custom % max(1, len(v_.bgv_groups())) + 1, len(v_.bgv_groups()), name or ''))
                            else: app.note('BGV AUTO')
                        return self._send(200, dict(ok=True, bgv_name=name or "player's own"))
                    if body.get('bgv') in ('on', 'paused', 'off'):
                        app.bgv_mode = body['bgv']
                        if app.job: app.job.video.set_mode(body['bgv']); app.note({'on': 'BGV ON', 'paused': 'BGV PAUSE', 'off': 'BGV OFF'}[body['bgv']])
                    if 'safe_area' in body:
                        try: app.save_settings(safe_area=max(0.0, min(20.0, float(body['safe_area']))))
                        except (TypeError, ValueError): pass
                    if 'zoom' in body:
                        try: app.save_settings(zoom=max(-10.0, min(25.0, float(body['zoom']))))
                        except (TypeError, ValueError): pass
                    if body.get('window_mode') in WINDOW_MODES:
                        app.save_settings(window_mode=body['window_mode']); _apply_window_mode(body['window_mode'])
                    if body.get('resolution') in RESOLUTIONS:
                        app.save_settings(resolution=body['resolution'])
                        if app.job:
                            v = app.job.video; v.size = RESOLUTIONS[body['resolution']][0]
                            if v.proc is not None: v._kill(); v.cur_file = None      # restarts at the new size
                    if 'mv_delay' in body:
                        md = min(5.0, max(0.0, float(body['mv_delay'] or 0))); app.save_settings(mv_delay=md)
                        if app.job: app.job.video.MV_DELAY = md
                    if isinstance(body.get('keymap'), dict):
                        km = {str(a)[:20]: int(b) for a, b in body['keymap'].items() if 0 <= int(b) < 154}
                        app.settings['keymap'] = km; app.save_settings()
                    e_ = app.job.emu_ref if app.job and getattr(app.job, 'emu_ref', None) is not None else None
                    if e_ is not None:
                        if 'key_voice' in body: e_.voice.enabled = bool(body['key_voice'])
                        if 'random_score' in body: e_.score.enabled = bool(body['random_score'])
                        if 'chorus' in body:
                            e_.chorus.enabled = bool(body['chorus'])
                            if not body['chorus']: app.chorus_out.stop(0)
                    return self._send(200, app.config())
                if u.path == '/api/speed':
                    m = body.get('speed')
                    if m not in ('auto', 'realtime', 'instant'): return self._send(400, {'error': 'speed must be auto, realtime or instant'})
                    app.speed_mode = m; app.save_settings(speed=m)
                    if app.job: app.job.speed_mode = m; app.job.rt_ref = None
                    return self._send(200, {'speed': m})
                if u.path == '/api/song/stop':
                    if app.mplayer: app.mplayer.stop()
                    return self._send(200, {'ok': True})
                if u.path == '/api/soundfont':
                    return self._send(200, app.set_soundfont(real_path(body.get('path', ''))))
                if u.path == '/api/song/play':
                    return self._send(200, app.play_song(body.get('file'), body.get('offset')))
                if u.path == '/api/disc/open':
                    return self._send(200, app.open_disc(real_path(body.get('path', ''))))
                if u.path == '/api/disc/extract':
                    if not app.disc: return self._send(400, {'error': 'no disc loaded'})
                    dest = os.path.abspath(os.path.expanduser((body.get('dest') or '').strip().strip('"')))
                    if not body.get('dest'): return self._send(400, {'error': 'enter a destination file path'})
                    if os.path.isdir(dest): dest = os.path.join(dest, os.path.basename(body['path'].rstrip('/')))
                    raw = app.disc.src.is_xa and body['path'].upper().endswith('.DAT')
                    app.disc.extract(body['path'], dest, raw_xa=raw)
                    return self._send(200, dict(dest=dest, bytes=os.path.getsize(dest)))
            except (isoread.DiscError, FileNotFoundError, RuntimeError, ValueError) as e:
                return self._send(400, {'error': str(e)})
            except Exception as e:
                return self._send(500, {'error': '%s: %s' % (type(e).__name__, e)})
            self._send(404, {'error': 'not found'})
    return H


PIN_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Project : Chiaki</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#15171c;color:#e8ecf4;font:15px system-ui,sans-serif}
.b{width:min(320px,90vw);text-align:center}h1{font-size:15px;letter-spacing:.2em;color:#ff9bcb}
input{width:100%;box-sizing:border-box;height:52px;font:600 26px system-ui;text-align:center;letter-spacing:.4em;border-radius:12px;border:1px solid #444b5a;background:#11141a;color:#fff}
button{margin-top:12px;width:100%;height:48px;border-radius:12px;border:0;background:#ff4fa3;color:#fff;font:600 16px system-ui}#m{color:#ff8a8a;min-height:20px;margin-top:10px}</style></head>
<body><div class="b"><h1>PROJECT CHIAKI</h1><p>Enter the PIN shown on the computer<br>(Settings → 📶 Smart link)</p>
<input id="p" inputmode="numeric" maxlength="4" autocomplete="off"><button id="go">Connect</button><div id="m"></div></div>
<script>const go=async()=>{const r=await fetch('/pin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:document.getElementById('p').value})});
 const j=await r.json().catch(()=>({}));if(j.ok)location.reload();else document.getElementById('m').textContent=j.error||'wrong PIN'};
document.getElementById('go').onclick=go;document.getElementById('p').addEventListener('keydown',e=>{if(e.key==='Enter')go()});</script></body></html>"""


def make_phone_handler(app):
    """Smart link: phones on the same Wi-Fi open http://<this computer>:<port>/, enter the PIN shown in the
    settings, and get the remote control and the song search. Only those few calls are open to them."""
    Base = make_handler(app)
    GET_OK = ('/api/status', '/api/synth', '/api/songbook', '/api/config')
    POST_OK = ('/api/key', '/api/songbook/play')
    fails = []

    class P(Base):
        server_version = 'ProjectChiaki-SmartLink/' + VERSION

        def _ok_origin(self): return True

        def _cookie_ok(self):
            c = self.headers.get('Cookie', '') or ''
            for part in c.split(';'):
                k, _, v = part.strip().partition('=')
                if k == 'chiaki' and secrets.compare_digest(v, app.phone_token): return True
            return False

        def _authed(self, q):
            tok = self.headers.get('X-Token') or (q.get('t') or [''])[0]
            return secrets.compare_digest(tok or '', app.phone_token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
            if u.path in ('/', '/remote'):
                if not self._cookie_ok(): return self._send(200, PIN_PAGE, 'text/html; charset=utf-8')
                return self._send(200, REMOTE_PAGE.replace('__TOKEN__', app.phone_token), 'text/html; charset=utf-8')
            if u.path == '/favicon.ico' or u.path in GET_OK: return Base.do_GET(self)
            return self._send(403, {'error': 'not available on the phone'})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == '/pin':
                now = time.time(); fails[:] = [t for t in fails if now - t < 60]
                if len(fails) >= 8: return self._send(429, {'error': 'too many tries - wait a minute'})
                try:
                    n = int(self.headers.get('Content-Length') or 0); body = json.loads(self.rfile.read(n) or b'{}')
                except Exception:
                    body = {}
                if secrets.compare_digest(str(body.get('pin', '')), app.phone_pin):
                    b = json.dumps({'ok': True}).encode()
                    self.send_response(200); self.send_header('Content-Type', 'application/json')
                    self.send_header('Set-Cookie', 'chiaki=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000' % app.phone_token)
                    self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b); return
                fails.append(now); return self._send(403, {'error': 'wrong PIN'})
            if not self._authed({}): return self._send(403, {'error': 'forbidden'})
            if u.path == '/api/synth':           # the phone may only switch the melody and the M/F key
                try:
                    n = int(self.headers.get('Content-Length') or 0); body = json.loads(self.rfile.read(n) or b'{}')
                except Exception:
                    return self._send(400, {'error': 'bad json'})
                return self._send(200, app.set_synth({k: body[k] for k in ('melody_toggle', 'mf_cycle') if k in body}))
            if u.path in POST_OK: return Base.do_POST(self)
            return self._send(403, {'error': 'not available on the phone'})
    return P


def lan_address():
    """This computer's address on the local network (no traffic is sent)."""
    import socket
    s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s_.connect(('10.255.255.255', 1)); return s_.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s_.close()


def qr_svg(text):
    """A QR code of `text` as SVG markup, or '' when the qrcode package is not there."""
    try:
        import qrcode, qrcode.image.svg, io as _io
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        b = _io.BytesIO(); img.save(b); return b.getvalue().decode('utf-8', 'replace')
    except Exception:
        return ''


_ICON = None
def _icon_bytes():
    """The app icon (ProjectChiaki.ico: bundled in the exe, or next to this script) for the browser tab."""
    global _ICON
    if _ICON is None:
        _ICON = b''
        for d in (getattr(sys, '_MEIPASS', None), os.path.dirname(os.path.abspath(__file__)), APPDIR):
            try:
                if d and os.path.isfile(os.path.join(d, 'ProjectChiaki.ico')):
                    _ICON = open(os.path.join(d, 'ProjectChiaki.ico'), 'rb').read(); break
            except Exception:
                pass
    return _ICON


def _running_copy(port):
    """The address of this program already running on the usual port (opened twice), else None."""
    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:%d/' % port, timeout=1.5) as r:
            srv_ = r.headers.get('Server', '') or ''
            if srv_.startswith(('ProjectChiaki/', 'KaraokeEmu/')): return 'http://127.0.0.1:%d/' % port
    except Exception:
        pass
    return None


def android_start(home, port=8765):
    """Android: start the emulator's page server in the background and return its address. The app's own window
    (a WebView) then opens that address. home: the folder on the phone with BIOS, Discs, Soundfont ..."""
    os.environ['KE_HOME'] = home; os.environ['KE_ANDROID'] = '1'
    global APPDIR
    APPDIR = home
    make_folders(); _synth_log()
    app = App()
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), make_handler(app)); port = p; break
        except OSError:
            continue
    if srv is None: raise RuntimeError('could not bind a local port')
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if app.settings.get('smart_link'): app.smartlink(True)
    return 'http://127.0.0.1:%d/' % port


# the author's pages, linked at the top right of the app (opened in the default browser)
SOCIAL_LINKS = ('https://linktr.ee/mitsukisato', 'https://www.tiktok.com/@mitsukikaraoke',
                'https://www.facebook.com/profile.php?id=61593864883476')


def serve(app, port=8765, open_browser=True, native=True):
    # opened again while it already runs: show that one instead of starting a second emulator next to it (two
    # running at once share the computer: both ran slow, the music too)
    other = _running_copy(port)
    if other:
        print('%s is already running: %s' % (APP_NAME, other))
        if open_browser and not (native and _native_window(other)):
            try: webbrowser.open(other)
            except Exception: pass
        return
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), make_handler(app)); port = p; break
        except OSError:
            continue
    if srv is None: raise SystemExit('could not bind a local port')
    url = 'http://127.0.0.1:%d/' % port
    if app.settings.get('smart_link'): app.smartlink(True)
    print('%s %s - open %s   (Ctrl+C to quit)' % (APP_NAME, VERSION, url))
    if app.fw: print('firmware:', app.fw)
    else: print('firmware: not found next to the program - enter its path in the page')
    print('ffmpeg  :', ffmpeg_path() or 'not found (pictures cannot be rendered; place ffmpeg next to this program)')
    if open_browser and native and _native_window(url, srv, app):
        return srv
    if open_browser: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print('bye')
    return srv


WINDOW_MODES = ('windowed', 'maximized', 'borderless', 'fullscreen')
_WIN = None; _WIN_FS = [False]


def _set_native_fs(on):
    w = _WIN
    if w is None or bool(on) == _WIN_FS[0]: return
    try: w.toggle_fullscreen(); _WIN_FS[0] = bool(on)
    except Exception: pass


def _apply_window_mode(mode):
    """Settings > window: windowed, maximized (windowed fullscreen), borderless fullscreen (the whole app covers the
    screen) or fullscreen (the picture only; Esc or double-click goes back)."""
    w = _WIN
    if w is None: return
    try:
        if mode in ('borderless', 'fullscreen'): _set_native_fs(True)
        else:
            _set_native_fs(False)
            if mode == 'maximized': w.maximize()
            else: w.restore()
    except Exception: pass


class _JsApi:
    """What the page may ask of the app window (pywebview js_api)."""
    def __init__(self, app=None): self._app = app
    def fullscreen(self, on):
        # the picture's fullscreen button: the window covers the screen too (inside the app window the page's own
        # fullscreen only filled the window); back to the chosen window mode when it ends
        if on: _set_native_fs(True)
        else:
            m = (self._app.settings.get('window_mode') if self._app else None) or 'windowed'
            if m != 'borderless': _apply_window_mode(m)
        return True
    def mode(self):
        return ((self._app.settings.get('window_mode') if self._app else None) or 'windowed')


def _native_window(url, srv=None, app=None):
    """The app in its own window (pywebview: Edge WebView2 on Windows) instead of a browser tab. The page talks to
    the emulator through the same local connection (127.0.0.1 only, nothing is reachable from other computers).
    Returns False when no native window can be shown (pywebview or WebView2 missing): the browser is used then."""
    try:
        import webview
    except Exception:
        return False
    if srv is not None: threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        global _WIN
        mode = ((app.settings.get('window_mode') if app else None) or 'windowed')
        if mode not in WINDOW_MODES: mode = 'windowed'
        fs = mode in ('borderless', 'fullscreen')
        try:
            w = webview.create_window(APP_NAME, url, width=1360, height=900, min_size=(800, 560), background_color='#101018',
                                      js_api=_JsApi(app), fullscreen=fs, maximized=(mode == 'maximized'), text_select=True)
        except TypeError:                                   # an older pywebview without maximized=
            w = webview.create_window(APP_NAME, url, width=1360, height=900, min_size=(800, 560), background_color='#101018',
                                      js_api=_JsApi(app), fullscreen=fs)
        _WIN = w; _WIN_FS[0] = fs
        ico = None
        for d in (getattr(sys, '_MEIPASS', None), os.path.dirname(os.path.abspath(__file__))):
            if d and os.path.isfile(os.path.join(d, 'ProjectChiaki.ico')): ico = os.path.join(d, 'ProjectChiaki.ico'); break
        kw = dict(private_mode=False)                     # keep the page's remembered choices between runs
        try: kw['storage_path'] = os.path.join(os.path.dirname(settings_path()), 'window')
        except Exception: pass
        if ico: kw['icon'] = ico
        try: webview.start(**kw)
        except TypeError: webview.start()
    except Exception as ex:
        print('native window not available (%s): using the browser' % ex)
        if srv is None: return False
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt: return True
        return True
    # the window was closed: quit (the emulator, its sound and video helpers stop with the process)
    try:
        if srv is not None: srv.shutdown()
    except Exception: pass
    os._exit(0)


# ------------------------------------------------------------------------------------ headless
def cmd_boot(args):
    fw = find_firmware(args.fw)
    if not fw: print('firmware not found; use --fw'); return 2
    out = args.out or os.path.join(os.getcwd(), 'display'); os.makedirs(out, exist_ok=True)
    job = EmuJob(fw, budget=args.budget, max_frames=args.frames, assist=not args.no_assist, disc=args.disc); job.start()
    shown = 0; last = 0
    while job.is_alive():
        time.sleep(1.0)
        if len(job.uart) > shown: sys.stdout.write(job.uart[shown:]); sys.stdout.flush(); shown = len(job.uart)
        if time.time() - last > 15:
            last = time.time(); print('  .. %s instrs (%.2f s emulated)' % (format(job.n, ','), job.n / INSTR_PER_SEC), flush=True)
    for f in job.frames:
        base = os.path.join(out, 'display_%02d' % f['index'])
        open(base + '.m2v', 'wb').write(f['m2v'])
        if f['png']: open(base + '.png', 'wb').write(f['png'])
        print('frame %d: %d bytes at %.2f s emulated -> %s' % (f['index'], f['bytes'], f['at_seconds'], base + ('.png' if f['png'] else '.m2v')))
    if job.osd_png:
        p = os.path.join(out, 'osd_last.png'); open(p, 'wb').write(job.osd_png)
        print('OSD layer (transparent PNG, %d region(s)) at %.2f s emulated -> %s' % (len(job.osd_regions), job.osd_at or 0, p))
    if job.error: print('ERROR:', job.error); return 1
    return 0


def main(argv=None):
    # the .exe is a window app (no console): printing goes nowhere instead of failing
    if sys.stdout is None: sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None: sys.stderr = open(os.devnull, 'w')
    import multiprocessing
    multiprocessing.freeze_support()                     # the SoundFont synth process (also in the .exe)
    try:
        import winperf; winperf.process('above')        # not 'efficiency mode' while the browser is in front
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'version': print(APP_NAME, VERSION); return 0
    if argv and argv[0] == 'iso': return isoread.main(['karaoke_emu'] + argv[1:])
    sub = 'gui'
    if argv and argv[0] in ('gui', 'boot'): sub = argv.pop(0)
    ap = argparse.ArgumentParser(prog='"Project Chiaki" ' + sub)
    ap.add_argument('--fw'); ap.add_argument('--iso'); ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true'); ap.add_argument('--browser', action='store_true', help='use the web browser instead of the app window'); ap.add_argument('--no-assist', action='store_true'); ap.add_argument('--out')
    ap.add_argument('--budget', type=int, default=700_000_000); ap.add_argument('--disc'); ap.add_argument('--frames', type=int, default=1)
    a = ap.parse_args(argv)
    if sub == 'boot': return cmd_boot(a)
    make_folders(); _synth_log()
    serve(App(fw=a.fw, iso=a.iso), port=a.port, open_browser=not a.no_browser, native=not a.browser)
    return 0


if __name__ == '__main__':
    sys.exit(main())

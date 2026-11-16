"""Real-time SoundFont synthesizer: a MIDI output device built into the programs.

It plays MIDI messages as they come, with a SoundFont (for example dreamsam_2695.sf2, the sound of the real player's
SAM2695 chip), straight to the computer's speakers. There is no rendering step and no extra program to install.

The synthesizer runs in its own process, so it never competes with the emulator for Python's lock. Audio goes out
through winmm waveOut on Windows: four 16 ms buffers, about 60 ms latency. Elsewhere it can only write to a WAV
file (KE_SYNTH_WAV) or nowhere, which is what the tests use.

Voices: SoundFont regions by bank/program/key/velocity, sample loops, root key and tuning, the volume envelope,
attenuation and pan. Channel volume, expression, pan and pitch bend act live on sounding notes; the sustain pedal,
all-notes-off and reset-controllers work. At most 64 voices: the oldest released voice is taken first.
"""
import os, sys, time, math, struct

RATE = 32000
DRUM_SUB = {27: 37, 28: 39, 29: 75, 30: 75, 31: 37, 32: 37, 33: 76, 34: 53, 82: 70, 83: 54, 84: 81, 85: 75,
            86: 64, 87: 64, 88: 49}
STATS = dict(steals=0, steal_on=0, peak=0, silent={})          # voice use (tests)
BLOCK = 512                   # 16 ms
NBUF = 4
LOG_PATH = None                # the app's log file: a crash of the synth process is written there
MAX_VOICES = 256              # (layered presets use 2+ voices per note; 64 dropped notes in dense songs)


def _load_biquad():
    """The block filter from the emulator's C library (kecpu.dll / libkecpu.so), or None."""
    import ctypes
    names = ('kecpu.dll',) if os.name == 'nt' else ('libkecpu.so',)
    dirs = [os.path.dirname(os.path.abspath(__file__))]
    if getattr(sys, '_MEIPASS', None): dirs.insert(0, sys._MEIPASS)
    if getattr(sys, 'frozen', False): dirs.append(os.path.dirname(sys.executable))
    if os.environ.get('KE_LIBDIR'): dirs.insert(0, os.environ['KE_LIBDIR'])      # Android: the app's library folder
    for d in dirs:
        for n in names:
            try:
                lib = ctypes.CDLL(os.path.join(d, n)); f = lib.synth_biquad
                f.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p]; f.restype = None
                return f
            except (OSError, AttributeError):
                continue
    return None


def _load_voice():
    """The C voice renderer (synth_voice in kecpu.dll / libkecpu.so), or None (then numpy renders)."""
    import ctypes
    names = ('kecpu.dll',) if os.name == 'nt' else ('libkecpu.so',)
    dirs = [os.path.dirname(os.path.abspath(__file__))]
    if getattr(sys, '_MEIPASS', None): dirs.insert(0, sys._MEIPASS)
    if getattr(sys, 'frozen', False): dirs.append(os.path.dirname(sys.executable))
    if os.environ.get('KE_LIBDIR'): dirs.insert(0, os.environ['KE_LIBDIR'])
    for d in dirs:
        for n in names:
            try:
                lib = ctypes.CDLL(os.path.join(d, n)); f = lib.synth_voice
                f.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p]
                f.restype = ctypes.c_int32
                return f
            except (OSError, AttributeError):
                continue
    return None


def _synth_process(conn, sf_path, wav_path=None, log_path=None):
    global LOG_PATH
    if log_path: LOG_PATH = log_path                 # (a new process: the parent's setting is not inherited)
    try:
        _synth_main(conn, sf_path, wav_path)
    except (KeyboardInterrupt, SystemExit, EOFError, BrokenPipeError):
        return                                      # the program is closing: not an error
    except BaseException:
        import traceback, time as _t
        if log_path:
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write('%s SYNTH ERROR (SoundFont %s)\n%s\n' % (_t.strftime('%H:%M:%S'), os.path.basename(sf_path), traceback.format_exc()))
            except Exception: pass
        raise


def _synth_main(conn, sf_path, wav_path=None):
    try:
        import winperf; winperf.process('high'); winperf.thread('highest')    # audio: never 'efficiency mode'
    except Exception:
        pass
    import numpy as np
    import sf2synth
    from sf2synth import GEN, _default, _tc
    sf = sf2synth.SoundFont(sf_path)
    smp = sf.samples; nsmp = len(smp)
    ar_full = np.arange(BLOCK, dtype=np.float64)
    import ctypes
    biquad = None if os.environ.get('KE_NOFILTER') else _load_biquad()
    cvoice = None if os.environ.get('KE_PYSYNTH') else _load_voice()     # whole voices in C: ~20x faster
    out_dev = (_WaveOut() if sys.platform == 'win32' else _AndroidOut() if ANDROID else None) if not wav_path else None
    wav = open(wav_path, 'wb') if wav_path else None
    if wav: wav.write(b'\0' * 44)
    chans = None
    def reset_channels():
        return [dict(prog=0, bank=128 if c == 9 else 0, vol=100, expr=127, pan=64, bend=0, sustain=False, brange=2,
                     drum=(c == 9), rpn=None, mod=0)
                for c in range(16)]
    chans = reset_channels()
    lim = [MAX_VOICES]; light = [False]; load = [0.0]  # voice limit / filters off, lowered on a slow computer
    voices = []                                      # dicts
    clock = 0                                        # samples rendered

    def start_note(c, key, vel):
        C = chans[c]
        pk = key if C['drum'] else min(127, max(0, key + int(ctl['transpose']) + int(ctl['fw_key']))) # the key that sounds
        # (fw_key: the player's own Key setting where its DSP is the synthesizer - KS5)
        ov = ctl['prog'][c] if c < len(ctl['prog']) else -1                           # instrument chosen in the panel
        bank, prog = (ov // 128, ov % 128) if ov is not None and ov >= 0 else (C['bank'], C['prog'])
        nv0 = len(voices)
        # the same key again on this channel: the earlier note goes to its release (as on the chip) instead of
        # piling up voices - dense songs otherwise ran out of voices and long notes were cut off
        for v in voices:
            if v['ch'] == c and v['key'] == key and v['rel_t'] is None:
                v['on'] = False; v['held'] = False; v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']
        regs = [r for r in sf.regions(bank, prog) if r['vr'][0] <= vel <= r['vr'][1]]
        hit = [r for r in regs if r['kr'][0] <= pk <= r['kr'][1]]
        if not hit and C['drum']:
            # GS drum notes this SoundFont's kits lack (27-34, 82-88: high Q, slap, scratch, sticks, metronome,
            # shaker, jingle bell, bell tree, castanets, surdo): the closest sound it has, so the part is not
            # left with holes. Then the standard kit if the chosen kit has no sample for the key.
            for k2 in (pk, DRUM_SUB.get(pk, pk)):
                hit = [r for r in regs if r['kr'][0] <= k2 <= r['kr'][1]]
                if not hit and prog != 0:
                    hit = [r for r in sf.regions(128, 0) if r['vr'][0] <= vel <= r['vr'][1] and r['kr'][0] <= k2 <= r['kr'][1]]
                if hit: pk = k2; break
        if not hit and regs and not C['drum']:
            # a key above / below every sample of the instrument: the nearest one, transposed (the chip plays these;
            # before, such notes were silent)
            dist = lambda r: r['kr'][0] - pk if pk < r['kr'][0] else pk - r['kr'][1]
            best = min(dist(r) for r in regs)
            if best <= 24: hit = [r for r in regs if dist(r) == best]
        for r in hit:
            s = r['s']; g = r['g']
            gv = lambda name: g.get(GEN[name], _default(GEN[name]))
            start = s['start'] + gv('startAddrsOffset') + 32768 * gv('startAddrsCoarseOffset')
            end = s['end'] + gv('endAddrsOffset') + 32768 * gv('endAddrsCoarseOffset')
            ls = s['ls'] + gv('startloopAddrsOffset') + 32768 * gv('startloopAddrsCoarseOffset')
            le = s['le'] + gv('endloopAddrsOffset') + 32768 * gv('endloopAddrsCoarseOffset')
            if end <= start + 1 or end > nsmp: continue
            mode = gv('sampleModes') & 3
            root = gv('overridingRootKey'); root = s['key'] if root < 0 else root
            cents = (pk - root) * gv('scaleTuning') + gv('coarseTune') * 100 + gv('fineTune') + s['corr']
            excl = gv('exclusiveClass')
            if excl:
                for v in voices:
                    if v['ch'] == c and v['excl'] == excl and v['rel_t'] is None: v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']; v['rel'] = 0.005
            v = dict(ch=c, key=key, vel=vel, pos=float(start), start=start, end=end, ls=ls, le=le,
                     looped=mode in (1, 3) and le > ls + 2, base_step=s['rate'] / RATE * 2.0 ** (cents / 1200.0),
                     dly=_tc(gv('delayVolEnv')), att=_tc(gv('attackVolEnv')), hold=_tc(gv('holdVolEnv')),
                     dec=_tc(gv('decayVolEnv')), sus=10 ** (-min(max(gv('sustainVolEnv'), 0), 1440) / 200.0),
                     rel=max(_tc(gv('releaseVolEnv')), 0.01), atten=10 ** (-(gv('initialAttenuation') * 0.4) / 200.0),
                     pan_off=gv('pan') / 500.0, t=0.0, rel_t=None, rel_lvl=0.0, lvl=0.0, held=False, excl=excl,
                     on=True)
            # low-pass filter (most of this SoundFont's instruments are filtered; unfiltered they sound harsh and
            # shrill) with its modulation envelope, and the vibrato / modulation LFOs on pitch
            # (no velocity-to-cutoff modulator: with it, soft notes of the darker instruments of this SoundFont were
            # filtered almost to silence - heard as missing / muted instruments)
            fc = gv('initialFilterFc')
            v.update(fc=fc, fq=gv('initialFilterQ'), me_fc=gv('modEnvToFilterFc'), me_p=gv('modEnvToPitch'),
                     mdly=_tc(gv('delayModEnv')), matt=_tc(gv('attackModEnv')), mhold=_tc(gv('holdModEnv')),
                     mdec=_tc(gv('decayModEnv')), msus=1.0 - min(max(gv('sustainModEnv'), 0), 1000) / 1000.0,
                     mrel=max(_tc(gv('releaseModEnv')), 0.01),
                     vib=gv('vibLfoToPitch'), vf=8.176 * 2.0 ** (gv('freqVibLFO') / 1200.0), vd=_tc(gv('delayVibLFO')),
                     mlp=gv('modLfoToPitch'), mlf=gv('modLfoToFilterFc'), mf=8.176 * 2.0 ** (gv('freqModLFO') / 1200.0),
                     md=_tc(gv('delayModLFO')), z=(ctypes.c_double * 2)(), filt=None)
            v['filt'] = biquad is not None and (fc < 13400 or v['me_fc'] > 0 or v['mlf'] != 0)
            voices.append(v)
        if len(voices) == nv0:
            k = (c, bank, prog, pk, vel); STATS.setdefault('silent', {})[k] = STATS['silent'].get(k, 0) + 1
        STATS['peak'] = max(STATS['peak'], len(voices))
        while len(voices) > lim[0]:                # drop the oldest released voice, else the oldest
            rel = [v for v in voices if v['rel_t'] is not None]
            x = min(rel, key=lambda v: v['lvl']) if rel else voices[0]      # the quietest fading note first
            STATS['steals'] += 1; STATS['steal_on'] += 0 if rel else 1
            if not rel: STATS.setdefault('stolen_ch', []).append(x['ch'])
            voices.remove(x)

    def note_off(c, key):
        drum = chans[c]['drum']
        for v in voices:
            if v['ch'] == c and v['key'] == key and v['on']:
                # drum parts ignore note-off (GS Rx.NoteOff off, as on the SAM2695): a hit plays its whole sample.
                # Released at once, drums with short notes were nearly silent ("muted" kick / snare in some songs)
                if drum and not v.get('looped'): continue
                v['on'] = False
                if chans[c]['sustain']: v['held'] = True
                elif v['rel_t'] is None: v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']

    def release_channel(c, kill=False):
        for v in list(voices):
            if v['ch'] != c: continue
            if kill: voices.remove(v)
            elif v['rel_t'] is None: v['on'] = False; v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']

    master = [1.0]; vol_seen = [False]
    # the synthesizer controller (the page's Synth panel): master volume, per-channel volume / mute, transpose,
    # filter on/off
    ctl = dict(master=1.0, mute=[False] * 16, chvol=[1.0] * 16, transpose=0, filter=True, prog=[-1] * 16, pcm=1.0,
               reverb=0.0, eq=[0.0, 0.0, 0.0], melody=True, fw_key=0, fw_melody=True,
               mic=False, mic_dev=-1, mic_vol=1.0, echo=0.3, echo_ms=120)
    melody = _Melody()
    fx = _MusicFX(biquad)
    mic = [None, None]               # the open microphone, and the (device) it was opened for

    def sysex(m):
        nonlocal chans
        if m[:6] == b'\xf0\x7e\x7f\x09\x01\xf7' or m[:6] == b'\xf0\x7e\x7f\x09\x03\xf7':      # GM on
            chans = reset_channels(); return
        if len(m) >= 10 and m[1] == 0x41 and m[3] == 0x42 and m[4] == 0x12:                       # Roland GS DT1
            addr = m[5:8]; val = m[8]
            if addr == b'\x40\x00\x7f':                                                      # GS reset
                chans = reset_channels(); return
            if addr[0] == 0x40 and (addr[1] & 0xF0) == 0x10 and addr[2] == 0x15:              # use for rhythm part
                part = addr[1] & 0x0F
                c = 9 if part == 0 else (part - 1 if part <= 9 else part)
                C = chans[c]; C['drum'] = val != 0; C['bank'] = 128 if val else 0

    def handle(m):
        nonlocal chans
        if not m: return
        st = m[0]
        if st == 0xFF:                                                     # our own "reset" message
            chans = reset_channels(); voices.clear(); return
        if st == 0xF0:
            sysex(m); return
        if st < 0x80 or st > 0xF0: return
        kind = st & 0xF0; c = st & 15; a = m[1] if len(m) > 1 else 0; b = m[2] if len(m) > 2 else 0
        C = chans[c]
        if kind == 0x90 and b: start_note(c, a, b); melody.note(c, a, clock, C['drum'])
        elif kind == 0x80 or kind == 0x90: note_off(c, a); melody.off(c, a)
        elif kind == 0xB0:
            if a == 0: C['bank'] = 128 if C['drum'] else b
            elif a == 1: C['mod'] = b
            elif a in (101, 100):                                       # RPN select
                r = C['rpn'] if isinstance(C['rpn'], list) else [127, 127]
                r[0 if a == 101 else 1] = b; C['rpn'] = r
            elif a in (99, 98):                                         # NRPN select
                r = C['rpn'] if isinstance(C['rpn'], tuple) else (127, 127)
                C['rpn'] = (b, r[1]) if a == 99 else (r[0], b)
            elif a == 6:                                                # data entry
                r = C['rpn']
                if isinstance(r, list) and r == [0, 0]: C['brange'] = min(b, 24)        # pitch bend sensitivity
                # NRPN 37xx: the SAM2695's master settings. 37 07 is the music volume the player's Vol keys set
                # (MP100: 12 steps, 0x00, 0x09, 0x13 ... 0x6D). 0 is also what a fresh player sends before anyone
                # touched the volume, so 0 counts only once a real level has been seen (else: full volume).
                elif isinstance(r, tuple) and r == (0x37, 0x07):
                    if b or vol_seen[0]:
                        master[0] = min(1.0, b / 109.0) ** 1.5; vol_seen[0] = True
            elif a == 7: C['vol'] = b
            elif a == 11: C['expr'] = b
            elif a == 10: C['pan'] = b
            elif a == 64:
                C['sustain'] = b >= 64
                if not C['sustain']:
                    for v in voices:
                        if v['ch'] == c and v['held'] and v['rel_t'] is None:
                            v['held'] = False; v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']
            elif a == 120: release_channel(c, kill=True)
            elif a == 123: release_channel(c)
            elif a == 121: C.update(expr=127, bend=0, sustain=False, rpn=None, mod=0)
        elif kind == 0xC0: C['prog'] = a
        elif kind == 0xE0: C['bend'] = ((b << 7) | a) - 8192

    D2 = ctypes.c_double
    smp_ptr = None
    def render_part_c(N):
        nonlocal smp_ptr
        if smp_ptr is None: smp_ptr = smp.ctypes.data
        buf = np.zeros(N * 2, np.float32); bp = buf.ctypes.data
        dead = []
        for v in voices:
            C = chans[v['ch']]
            tb = v['t']
            if v['rel_t'] is None:
                a0 = v['mdly']; a1 = a0 + v['matt']; h1 = a1 + v['mhold']; d1 = h1 + v['mdec']
                me = 0.0 if tb < a0 else (tb - a0) / max(v['matt'], 1e-9) if tb < a1 else 1.0 if tb < h1 else \
                     1 - (1 - v['msus']) * (tb - h1) / max(v['mdec'], 1e-9) if tb < d1 else v['msus']
                v['mlvl'] = me
            else:
                me = v.get('mlvl', 0.0) * max(0.0, 1 - (tb - v['rel_t']) / v['mrel'])
            lfo_v = math.sin(2 * math.pi * v['vf'] * (tb - v['vd'])) if tb > v['vd'] else 0.0
            lfo_m = math.sin(2 * math.pi * v['mf'] * (tb - v['md'])) if tb > v['md'] else 0.0
            cents = (v['vib'] + C['mod'] * 50 / 127.0) * lfo_v + v['mlp'] * lfo_m + v['me_p'] * me
            step = v['base_step'] * 2.0 ** (C['bend'] / 8192 * C['brange'] / 12.0 + cents / 1200.0)
            amp = v['atten'] * (v['vel'] / 127.0) ** 2 * (C['vol'] / 127.0) ** 2 * (C['expr'] / 127.0) ** 2
            amp *= 0.0 if (ctl['mute'][v['ch']] or (not (ctl['melody'] and ctl['fw_melody']) and v['ch'] in melody.chans)) else ctl['chvol'][v['ch']]
            pan = min(max((C['pan'] - 64) / 64.0 + v['pan_off'], -1), 1)
            f_on = 0.0; cf = (0.0, 0.0, 0.0, 0.0, 0.0)
            if v['filt'] and ctl['filter'] and not light[0]:
                fcc = min(max(v['fc'] + v['me_fc'] * me + v['mlf'] * lfo_m, 1500.0), 13500.0)
                f = min(8.176 * 2.0 ** (fcc / 1200.0), 0.45 * RATE)
                q = max(0.5, 0.7071 * 10 ** (v['fq'] / 200.0))
                w0 = 2 * math.pi * f / RATE; cs = math.cos(w0); al = math.sin(w0) / (2 * q); a0n = 1 + al
                cf = ((1 - cs) / 2 / a0n, (1 - cs) / a0n, (1 - cs) / 2 / a0n, -2 * cs / a0n, (1 - al) / a0n); f_on = 1.0
            st_ = (D2 * 3)(v['pos'], v['t'], v['lvl'])
            prm = (D2 * 23)(step, v['end'], v['ls'], v['le'], 1.0 if v['looped'] else 0.0, v['dly'], v['att'], v['hold'],
                             v['dec'], v['sus'], v['rel'], -1.0 if v['rel_t'] is None else v['rel_t'], v['rel_lvl'], amp,
                             math.cos((pan + 1) * math.pi / 4), math.sin((pan + 1) * math.pi / 4), float(RATE), f_on, *cf)
            got = cvoice(smp_ptr, nsmp, bp, N, ctypes.addressof(st_), ctypes.addressof(prm), ctypes.addressof(v['z']))
            v['pos'], v['t'], v['lvl'] = st_[0], st_[1], st_[2]
            if got < N: dead.append(v)
        for v in dead:
            if v in voices: voices.remove(v)
        mix = buf.reshape(N, 2)
        mix *= 0.5 * master[0] * ctl['master']
        return mix

    def render_part(N):
        if cvoice is not None: return render_part_c(N)
        ar = ar_full[:N]
        mix = np.zeros((N, 2), np.float32)
        dead = []
        for v in voices:
            C = chans[v['ch']]
            tb = v['t']                                                     # this block's start (voice time)
            # modulation envelope (0..1) and LFOs, taken once per block
            if v['rel_t'] is None:
                a0 = v['mdly']; a1 = a0 + v['matt']; h1 = a1 + v['mhold']; d1 = h1 + v['mdec']
                me = 0.0 if tb < a0 else (tb - a0) / max(v['matt'], 1e-9) if tb < a1 else 1.0 if tb < h1 else \
                     1 - (1 - v['msus']) * (tb - h1) / max(v['mdec'], 1e-9) if tb < d1 else v['msus']
                v['mlvl'] = me
            else:
                me = v.get('mlvl', 0.0) * max(0.0, 1 - (tb - v['rel_t']) / v['mrel'])
            lfo_v = math.sin(2 * math.pi * v['vf'] * (tb - v['vd'])) if tb > v['vd'] else 0.0
            lfo_m = math.sin(2 * math.pi * v['mf'] * (tb - v['md'])) if tb > v['md'] else 0.0
            cents = (v['vib'] + C['mod'] * 50 / 127.0) * lfo_v + v['mlp'] * lfo_m + v['me_p'] * me
            step = v['base_step'] * 2.0 ** (C['bend'] / 8192 * C['brange'] / 12.0 + cents / 1200.0)
            pos = v['pos'] + ar * step
            n = N
            if v['looped']:
                over = pos >= v['le']
                if over.any(): pos[over] = v['ls'] + np.mod(pos[over] - v['ls'], v['le'] - v['ls'])
            else:
                lim = v['end'] - 1
                if pos[0] >= lim: dead.append(v); continue
                if pos[-1] >= lim: n = int(np.searchsorted(pos, lim)); pos = pos[:n]
            i0 = pos.astype(np.int64); fr = (pos - i0).astype(np.float32)
            wave = smp[i0] * (1 - fr) + smp[np.minimum(i0 + 1, nsmp - 1)] * fr
            t = v['t'] + ar[:n] / RATE
            # volume envelope
            a0 = v['dly']; a1 = a0 + v['att']; h1 = a1 + v['hold']; d1 = h1 + v['dec']; sus = v['sus']
            env = np.where(t < a0, 0.0, np.where(t < a1, (t - a0) / max(v['att'], 1e-9),
                           np.where(t < h1, 1.0, np.where(t < d1, 1 - (1 - sus) * (t - h1) / max(v['dec'], 1e-9), sus))))
            if v['rel_t'] is not None:
                env = v['rel_lvl'] * np.maximum(0.0, 1 - (t - v['rel_t']) / v['rel'])
                if env[-1] <= 0: dead.append(v)
            env = env.astype(np.float32)
            amp = v['atten'] * (v['vel'] / 127.0) ** 2 * (C['vol'] / 127.0) ** 2 * (C['expr'] / 127.0) ** 2
            amp *= 0.0 if (ctl['mute'][v['ch']] or (not (ctl['melody'] and ctl['fw_melody']) and v['ch'] in melody.chans)) else ctl['chvol'][v['ch']]
            pan = min(max((C['pan'] - 64) / 64.0 + v['pan_off'], -1), 1)
            sig = wave * env * amp
            if v['filt'] and ctl['filter'] and not light[0] and n:
                fcc = min(max(v['fc'] + v['me_fc'] * me + v['mlf'] * lfo_m, 1500.0), 13500.0)
                f = min(8.176 * 2.0 ** (fcc / 1200.0), 0.45 * RATE)
                q = max(0.5, 0.7071 * 10 ** (v['fq'] / 200.0))             # initialFilterQ: cB of resonance
                w0 = 2 * math.pi * f / RATE; cs = math.cos(w0); al = math.sin(w0) / (2 * q); a0n = 1 + al
                coef = np.array([(1 - cs) / 2 / a0n, (1 - cs) / a0n, (1 - cs) / 2 / a0n, -2 * cs / a0n, (1 - al) / a0n])
                x = np.ascontiguousarray(sig, dtype=np.float32)
                biquad(x.ctypes.data, n, coef.ctypes.data, ctypes.addressof(v['z']))
                sig = x
            mix[:n, 0] += sig * math.cos((pan + 1) * math.pi / 4)
            mix[:n, 1] += sig * math.sin((pan + 1) * math.pi / 4)
            v['lvl'] = float(env[-1]) if n else 0.0
            v['pos'] = float(pos[-1] + step) if n else v['pos']; v['t'] += N / RATE
            if n < N and v not in dead: dead.append(v)
        for v in dead:
            if v in voices: voices.remove(v)
        mix *= 0.5 * master[0] * ctl['master']
        return mix

    # Timing: a message stamped with the sender's clock (the emulated player's seconds) is played exactly that far
    # from the previous ones, LATENCY after its first arrival; unstamped messages play at the next sample.
    LATENCY = int(0.03 * RATE)
    SLACK_LATE = int(0.06 * RATE); SLACK_EARLY = int(0.06 * RATE)
    sched = []                                       # (due sample, order, message)
    offset = [None]; order = [0]
    def schedule(t, m):
        if t is None: due = clock
        else:
            due = int(t * RATE) + offset[0] if offset[0] is not None else None
            # (Re)lock when a message would play clearly late, or clearly later than it needs to. The upper bound
            # was 1 s: after the emulator fell behind for a moment the clocks re-locked late, and when it caught up
            # again the music stayed up to a second behind the lyrics for the rest of the song.
            if due is None or due < clock - SLACK_LATE or due > clock + LATENCY + SLACK_EARLY:
                offset[0] = clock + LATENCY - int(t * RATE); due = int(t * RATE) + offset[0]
            due = max(due, clock)
        order[0] += 1; sched.append((due, order[0], m))

    streams = []                                     # recorded audio (song chorus clips) being mixed in
    shift = _PitchShift(RATE)
    def mix_streams(out, clk0):
        sbuf = np.zeros_like(out) if shift.active(ctl['fw_key']) else None
        g = 0.8 * ctl['pcm'] * ctl['master']
        done = []
        for k, st_ in enumerate(streams):
            start = max(clk0, st_['due']); a = st_['a']
            if start >= clk0 + BLOCK: continue
            src = start - st_['due']
            if src >= len(a):
                # a stream's piece may still come (late): kept 2 s so it goes on from there, not skipped
                if st_.get('sid') is None or clk0 - (st_['due'] + len(a)) > 2 * RATE: done.append(k)
                continue
            n = min(clk0 + BLOCK - start, len(a) - src)
            dst = sbuf if shift.active(ctl['fw_key']) else out
            dst[start - clk0:start - clk0 + n] += a[src:src + n] * g
        # (by position: list.remove compares the dicts, and so their sample arrays, which raised an error and
        # stopped the whole synth as soon as two clips of different lengths were playing)
        for k in reversed(done): del streams[k]
        if sbuf is not None: out += shift.run(sbuf, ctl['fw_key'])

    def render_block():
        nonlocal clock
        clk0 = clock
        sched.sort()
        out = np.zeros((BLOCK, 2), np.float32); done = 0
        while True:
            nxt = sched[0][0] - clock if sched else BLOCK
            nxt = min(max(nxt, 0), BLOCK - done)
            if nxt: out[done:done + nxt] = render_part(nxt); done += nxt; clock += nxt
            while sched and sched[0][0] <= clock: handle(sched.pop(0)[2])
            if done >= BLOCK: break
        fx.run(out, ctl)                                      # reverb / equalizer on the music
        if streams: mix_streams(out, clk0)
        if ctl['mic'] and out_dev is not None:
            if mic[1] != ctl['mic_dev'] or mic[0] is None:
                if mic[0] is not None: mic[0].close()
                mic[0] = _open_mic(ctl['mic_dev']); mic[1] = ctl['mic_dev']
            if mic[0] is not None: mic[0].mix(out, ctl)
        elif mic[0] is not None:
            mic[0].close(); mic[0] = None; mic[1] = None
        return (np.clip(out, -1, 1) * 32767).astype('<i2').tobytes()

    conn.send(('ready', sf.name))
    t_next = time.perf_counter()
    while True:
        while conn.poll():
            try: msg = conn.recv()
            except EOFError: msg = None
            if msg is None:
                if out_dev: out_dev.close()
                if wav: _finish_wav(wav)
                return
            t, m = msg
            if t == 'pcm':                                  # recorded audio to mix in: (seconds, 16-bit stereo bytes)
                ts, raw = m[0], m[1]; sid = m[2] if len(m) > 2 else None
                a = np.frombuffer(raw, dtype='<i2').astype(np.float32).reshape(-1, 2) / 32768.0
                due = (int(ts * RATE) + offset[0]) if (ts is not None and offset[0] is not None) else clock + LATENCY
                if ts is not None and offset[0] is not None and abs(due - (clock + LATENCY)) > int(0.5 * RATE) and not any(
                        x.get('sid') == sid for x in streams):
                    # the clocks were last locked by MIDI long ago (at power-on, when the player ran at another
                    # speed): a new recorded sound would wait seconds or never come - or, the player having fallen
                    # behind since, come "too late" and be skipped whole (the KS5's spoken numbers before the first
                    # song: heard only after a song had played and its MIDI re-locked the clocks). It re-locks the
                    # clocks, as a late or early MIDI message does
                    offset[0] = clock + LATENCY - int(ts * RATE); due = clock + LATENCY
                if sid is not None:
                    # a piece of a stream already known: straight after its previous piece, whatever its time
                    # stamp says (timing jitter left sample gaps / overlaps there: crackle). A stream that ran dry
                    # (the next piece came late) goes on from now, faded in, instead of skipping into the piece
                    st_ = next((x for x in streams if x.get('sid') == sid), None)
                    if st_ is not None:
                        end = st_['due'] + len(st_['a'])
                        if end > clock:
                            played = clock - st_['due']
                            if played > 0: st_['a'] = st_['a'][played:]; st_['due'] += played
                            st_['a'] = np.concatenate((st_['a'], a))
                            continue
                        del streams[next(i for i, x in enumerate(streams) if x is st_)]; due = max(due, clock + 64)
                        n = min(len(a), 96); a = a.copy(); a[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
                    elif due < clock - int(0.4 * RATE):
                        cut = clock - due                               # first piece clearly late: skip what is past
                        if cut >= len(a): continue
                        a = a[cut:]; due = clock
                    elif due < clock:
                        due = clock          # a little late (a decoder starting): play it all from now, not cut short
                    streams.append(dict(due=due, a=a, sid=sid))
                    continue
                # a piece that continues a stream still playing (streamed sound comes in 20 ms pieces) is joined to
                # it: no click from a sample gap / overlap where two pieces meet, and nothing is skipped when a piece
                # arrives a little late
                for st_ in streams:
                    end = st_['due'] + len(st_['a'])
                    if abs(due - end) <= 64 and end > clock:
                        played = clock - st_['due']
                        if played > 0: st_['a'] = st_['a'][played:]; st_['due'] += played
                        st_['a'] = np.concatenate((st_['a'], a)); break
                else:
                    streams.append(dict(due=due, a=a))
                continue
            if t == 'pcmstop':
                streams.clear(); continue
            if t == 'ctl':
                for k, val in m.items():
                    if k in ctl: ctl[k] = list(val)[:16] if isinstance(ctl[k], list) else val
                continue
            schedule(t, m)
        t_r = time.perf_counter()
        data = render_block()
        load[0] = 0.95 * load[0] + 0.05 * (time.perf_counter() - t_r) / (BLOCK / RATE)
        # a slow computer: when rendering takes most of the real time, play fewer voices (oldest first) and skip
        # the filters, so the sound stays in time instead of falling behind; back up when there is room again
        if load[0] > 0.6 and lim[0] > 24:
            lim[0] = max(24, int(lim[0] * 0.75)); light[0] = True; load[0] = 0.45
        elif load[0] < 0.25 and lim[0] < MAX_VOICES:
            lim[0] = min(MAX_VOICES, lim[0] + 8)
            if lim[0] >= MAX_VOICES: light[0] = False
        if out_dev: out_dev.write(data)                 # waits while all buffers are playing
        else:
            if wav: wav.write(data)
            t_next += BLOCK / RATE                      # no device: keep real time
            d = t_next - time.perf_counter()
            if d > 0: time.sleep(d)



class _PitchShift:
    """The player's Key setting on recorded music (KS5 MP3 songs: its DSP shifts the song's pitch, keeping the
    tempo). Two read heads sweep a short delay line at the new speed, each faded in and out (sin^2) half a window
    apart, the way karaoke DSPs do it."""
    W = 1600                                            # window: 50 ms at 32 kHz
    N = 8192
    def __init__(self, rate):
        import numpy as np
        self.buf = np.zeros((self.N, 2), np.float32); self.w = 0; self.ph = 0.0; self.key = 0
    def active(self, key):
        return bool(key) or bool(self.key)
    def run(self, x, key):
        import numpy as np
        if key != self.key:
            self.key = key
            if not key: self.buf[:] = 0; self.ph = 0.0; return x       # back to the song's own key: straight through
        n = len(x); N = self.N; W = self.W
        idx = (self.w + np.arange(n)) % N
        self.buf[idx] = x
        r = 2.0 ** (key / 12.0)
        i = np.arange(n, dtype=np.float64)
        d1 = np.mod(self.ph - (r - 1.0) * i, W); d2 = np.mod(d1 + W / 2, W)
        out = np.zeros_like(x)
        for d in (d1, d2):
            pos = (self.w + i - d - 2.0) % N
            p0 = np.floor(pos).astype(np.int64); f = (pos - p0).astype(np.float32)[:, None]
            s_ = self.buf[p0 % N] * (1 - f) + self.buf[(p0 + 1) % N] * f
            out += s_ * (np.sin(np.pi * d / W) ** 2).astype(np.float32)[:, None]
        self.ph = float(np.mod(self.ph - (r - 1.0) * n, W)); self.w = (self.w + n) % N
        return out

class _Melody:
    """Which channels carry the song's melody (the guide tune), for the Melody key: of the instruments that play
    one note at a time in the singing range, the busiest one, and any other that plays the same notes at the same
    moments (melodies are often doubled). Counted over the last ~200 notes of each channel."""
    def __init__(self):
        import collections
        self.on = [set() for _ in range(16)]; self.n = [0] * 16; self.poly = [0] * 16; self.pitch = [0.0] * 16
        self.recent = [collections.deque(maxlen=200) for _ in range(16)]; self.chans = set(); self.k = 0
    def note(self, c, key, t, drum):
        if drum: return
        self.on[c].add(key); self.n[c] += 1
        if len(self.on[c]) > 1: self.poly[c] += 1
        self.pitch[c] = 0.98 * self.pitch[c] + 0.02 * key if self.pitch[c] else key
        self.recent[c].append((t, key)); self.k += 1
        if self.k % 32 == 0: self._pick()
    def off(self, c, key): self.on[c].discard(key)
    def _pick(self):
        cand = [c for c in range(16) if self.n[c] >= 24 and self.poly[c] <= 0.1 * self.n[c] and 52 <= self.pitch[c] <= 86]
        if not cand: self.chans = set(); return
        best = max(cand, key=lambda c: len(self.recent[c]))
        ref = {(t // 800, k % 12) for t, k in self.recent[best]}                # 25 ms slots at 32 kHz
        same = {best}
        for c in cand:
            if c != best and self.recent[c]:
                hit = sum(1 for t, k in self.recent[c] if (t // 800, k % 12) in ref)
                if hit >= 0.6 * len(self.recent[c]): same.add(c)
        self.chans = same


class _MusicFX:
    """Reverb and a three-band equalizer on the music, set from the Synth panel (ctl reverb 0..1, eq [bass, mid,
    treble] in dB, -12..+12). The reverb: four comb filters and two all-passes (Schroeder), all at least a block
    long so each block is a few numpy operations."""
    def __init__(self, biquad):
        import ctypes
        self.bq = biquad; self.eq = None; self.coef = []; self.z = []; self.ct = ctypes; self.rv = None
    def _eq_setup(self, eq):
        import math
        self.eq = list(eq); self.coef = []; self.z = []
        for kind, f, g in (('ls', 200.0, eq[0]), ('pk', 1000.0, eq[1]), ('hs', 4000.0, eq[2])):
            if abs(g) < 0.1: continue
            A = 10 ** (g / 40.0); w = 2 * math.pi * f / RATE; cs = math.cos(w); sn = math.sin(w)
            if kind == 'pk':
                al = sn / (2 * 0.9)
                b = (1 + al * A, -2 * cs, 1 - al * A); a = (1 + al / A, -2 * cs, 1 - al / A)
            else:
                al = sn / 2 * math.sqrt(2); sq = 2 * math.sqrt(A) * al; sg = 1 if kind == 'ls' else -1
                b = (A * ((A + 1) - sg * (A - 1) * cs + sq), sg * 2 * A * ((A - 1) - sg * (A + 1) * cs), A * ((A + 1) - sg * (A - 1) * cs - sq))
                a = ((A + 1) + sg * (A - 1) * cs + sq, -sg * 2 * ((A - 1) + sg * (A + 1) * cs), (A + 1) + sg * (A - 1) * cs - sq)
            c = (self.ct.c_double * 5)(b[0] / a[0], b[1] / a[0], b[2] / a[0], a[1] / a[0], a[2] / a[0])
            self.coef.append(c); self.z.append([(self.ct.c_double * 2)(), (self.ct.c_double * 2)()])
    def run(self, out, ctl):
        import numpy as np
        eq = ctl.get('eq') or [0, 0, 0]
        if self.bq is not None and any(abs(x) >= 0.1 for x in eq):
            if self.eq != list(eq): self._eq_setup(eq)
            for ch in (0, 1):
                x = np.ascontiguousarray(out[:, ch])
                for c, z in zip(self.coef, self.z): self.bq(x.ctypes.data, len(x), self.ct.addressof(c), self.ct.addressof(z[ch]))
                out[:, ch] = x
        r = float(ctl.get('reverb') or 0.0)
        if r > 0.005:
            if self.rv is None:
                k = RATE / 44100.0
                self.rv = dict(comb=[[np.zeros(max(BLOCK, int(d * k)), np.float32), 0.0] for d in (1557, 1617, 1491, 1422)],
                               ap=[np.zeros(max(BLOCK, int(d * k)), np.float32) for d in (761, 829)],
                               ap2=[np.zeros(max(BLOCK, int(d * k)), np.float32) for d in (787, 853)])
            x = out.mean(axis=1) * 0.25; acc = np.zeros(BLOCK, np.float32); fb = 0.80
            for cb in self.rv['comb']:
                buf = cb[0]; old = buf[:BLOCK].copy()
                lp = np.empty_like(old); st = cb[1]                      # damping: a gentle one-pole low-pass
                lp[0] = 0.75 * old[0] + 0.25 * st
                lp[1:] = 0.75 * old[1:] + 0.25 * old[:-1]; cb[1] = float(old[-1])
                y = x + fb * lp
                buf[:-BLOCK] = buf[BLOCK:]; buf[-BLOCK:] = y; acc += old
            wet = []
            for chain in (self.rv['ap'], self.rv['ap2']):
                v = acc
                for buf in chain:
                    d = buf[:BLOCK].copy(); w_ = v + 0.5 * d; y = -0.5 * w_ + d
                    buf[:-BLOCK] = buf[BLOCK:]; buf[-BLOCK:] = w_; v = y
                wet.append(v)
            out[:, 0] += wet[0] * r * 0.9; out[:, 1] += wet[1] * r * 0.9
        elif self.rv is not None and r <= 0.005:
            self.rv = None


def mic_devices():
    """[(id, name)] of the computer's microphones (Windows), -1 = the default one."""
    out = [(-1, 'Default microphone')]
    if sys.platform != 'win32': return out
    try:
        import ctypes
        class CAPS(ctypes.Structure):
            _fields_ = [('wMid', ctypes.c_ushort), ('wPid', ctypes.c_ushort), ('vDriverVersion', ctypes.c_uint),
                        ('szPname', ctypes.c_wchar * 32), ('dwFormats', ctypes.c_uint), ('wChannels', ctypes.c_ushort),
                        ('wReserved1', ctypes.c_ushort)]
        w = ctypes.windll.winmm
        for i in range(w.waveInGetNumDevs()):
            c = CAPS()
            if w.waveInGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c)) == 0: out.append((i, c.szPname))
    except Exception:
        pass
    return out


def _open_mic(dev):
    try:
        if sys.platform == 'win32': return _WinMic(dev)
    except Exception as ex:
        if LOG_PATH:
            try:
                with open(LOG_PATH, 'a', encoding='utf-8') as f: f.write('%s mic: could not open (%r)\n' % (time.strftime('%H:%M:%S'), ex))
            except Exception: pass
    return None


class _WinMic:
    """The microphone through winmm waveIn (mono 16-bit, 8 ms buffers), mixed into the output with an echo
    (delay ctl echo_ms, feedback ctl echo 0..0.9) and a level (ctl mic_vol 0..2): you hear yourself in the speakers
    like on the real player's microphone input."""
    NB = 8; N = 256
    def __init__(self, dev):
        import ctypes, numpy as np
        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [('wFormatTag', ctypes.c_ushort), ('nChannels', ctypes.c_ushort), ('nSamplesPerSec', ctypes.c_uint),
                        ('nAvgBytesPerSec', ctypes.c_uint), ('nBlockAlign', ctypes.c_ushort), ('wBitsPerSample', ctypes.c_ushort),
                        ('cbSize', ctypes.c_ushort)]
        class WAVEHDR(ctypes.Structure):
            _fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', ctypes.c_uint), ('dwBytesRecorded', ctypes.c_uint),
                        ('dwUser', ctypes.c_void_p), ('dwFlags', ctypes.c_uint), ('dwLoops', ctypes.c_uint),
                        ('lpNext', ctypes.c_void_p), ('reserved', ctypes.c_void_p)]
        self.ct = ctypes; self.np = np; self.w = ctypes.windll.winmm
        fmt = WAVEFORMATEX(1, 1, RATE, RATE * 2, 2, 16, 0)
        self.h = ctypes.c_void_p()
        dev_id = 0xFFFFFFFF if dev is None or dev < 0 else int(dev)          # WAVE_MAPPER
        r = self.w.waveInOpen(ctypes.byref(self.h), ctypes.c_uint(dev_id), ctypes.byref(fmt), 0, 0, 0)
        if r != 0: raise OSError('waveInOpen %d' % r)
        self.bufs = []; self.hdrs = []
        for _ in range(self.NB):
            b = ctypes.create_string_buffer(self.N * 2); hd = WAVEHDR(); hd.lpData = ctypes.cast(b, ctypes.c_void_p); hd.dwBufferLength = self.N * 2
            self.w.waveInPrepareHeader(self.h, ctypes.byref(hd), ctypes.sizeof(hd))
            self.w.waveInAddBuffer(self.h, ctypes.byref(hd), ctypes.sizeof(hd))
            self.bufs.append(b); self.hdrs.append(hd)
        self.i = 0; self.fifo = np.zeros(0, np.float32); self.echo = np.zeros(RATE, np.float32); self.ep = 0
        self.w.waveInStart(self.h)
    def _collect(self):
        np = self.np; got = []
        for _ in range(self.NB):
            hd = self.hdrs[self.i]
            if not (hd.dwFlags & 1): break                                   # WHDR_DONE
            n = hd.dwBytesRecorded // 2
            got.append(np.frombuffer(self.ct.string_at(hd.lpData, n * 2), '<i2').astype(np.float32) / 32768.0)
            hd.dwFlags &= ~1
            self.w.waveInAddBuffer(self.h, self.ct.byref(hd), self.ct.sizeof(hd))
            self.i = (self.i + 1) % self.NB
        if got: self.fifo = np.concatenate([self.fifo] + got)
        if len(self.fifo) > 3 * BLOCK: self.fifo = self.fifo[-2 * BLOCK:]   # never let the voice fall behind
    def mix(self, out, ctl):
        np = self.np
        self._collect()
        n = min(len(self.fifo), BLOCK)
        x = np.zeros(BLOCK, np.float32); x[BLOCK - n:] = self.fifo[:n]; self.fifo = self.fifo[n:]
        x *= float(ctl.get('mic_vol', 1.0)) * 1.5
        fbk = min(0.9, max(0.0, float(ctl.get('echo', 0.3)))); d = int(min(0.9, max(0.03, ctl.get('echo_ms', 120) / 1000.0)) * RATE)
        if fbk > 0.01:
            e = self.echo; L = len(e); idx = (self.ep - d + np.arange(BLOCK)) % L
            y = x + fbk * e[idx]
            e[(self.ep + np.arange(BLOCK)) % L] = y; self.ep = (self.ep + BLOCK) % L
            x = y
        out[:, 0] += x; out[:, 1] += x
    def close(self):
        try:
            self.w.waveInStop(self.h); self.w.waveInReset(self.h)
            for hd in self.hdrs: self.w.waveInUnprepareHeader(self.h, self.ct.byref(hd), self.ct.sizeof(hd))
            self.w.waveInClose(self.h)
        except Exception:
            pass


def _finish_wav(f):
    n = f.tell() - 44
    f.seek(0)
    f.write(b'RIFF' + struct.pack('<I', 36 + n) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 2, RATE, RATE * 4, 4, 16) +
            b'data' + struct.pack('<I', n))
    f.close()


ANDROID = hasattr(sys, 'getandroidapilevel') or 'ANDROID_ROOT' in os.environ


class _AndroidOut:
    """Android: the phone's sound through AudioTrack (Java, reached through Chaquopy). write() blocks while the
    track's buffer is full, which paces the synth like waveOut does on Windows."""
    def __init__(self):
        from java import jclass
        AT = jclass('android.media.AudioTrack'); AF = jclass('android.media.AudioFormat'); AM = jclass('android.media.AudioManager')
        mn = AT.getMinBufferSize(RATE, AF.CHANNEL_OUT_STEREO, AF.ENCODING_PCM_16BIT)
        self.t = AT(AM.STREAM_MUSIC, RATE, AF.CHANNEL_OUT_STEREO, AF.ENCODING_PCM_16BIT, max(mn, BLOCK * 4 * 6), AT.MODE_STREAM)
        self.t.play()
        from java import jarray, jbyte
        self.jbytes = jarray(jbyte)
    def write(self, data):
        # as a Java byte[]: Python bytes fit both write(byte[], ...) and write(short[], ...), which Chaquopy refuses
        # as ambiguous - the synth stopped at its first block and the app had no sound at all
        arr = self.jbytes(data); n = 0; total = len(data)
        while n < total:
            k = self.t.write(arr, n, total - n)
            if k <= 0: break
            n += k
    def close(self):
        try: self.t.stop(); self.t.release()
        except Exception: pass


class _QConn:
    """One end of an in-process pipe (Android has no second process for the synth: it runs in a thread)."""
    def __init__(self, inq, outq): self.inq = inq; self.outq = outq; self._peek = []
    def send(self, obj): self.outq.put(obj)
    def recv(self):
        if self._peek: return self._peek.pop(0)
        obj = self.inq.get()
        if obj is _CLOSED: raise EOFError
        return obj
    def poll(self, timeout=0.0):
        if self._peek: return True
        import queue
        try: obj = self.inq.get(timeout=timeout) if timeout else self.inq.get_nowait()
        except queue.Empty: return False
        if obj is _CLOSED: self._peek.append(None); return True     # recv() then gives None: the loop ends
        self._peek.append(obj); return True
    def close(self): self.outq.put(_CLOSED)
_CLOSED = object()


class _ThreadCtx:
    """multiprocessing's Pipe / Process, with a thread instead of a process (Android)."""
    def Pipe(self):
        import queue
        a, b = queue.Queue(), queue.Queue()
        return _QConn(a, b), _QConn(b, a)
    def Process(self, target, args=(), daemon=True):
        import threading
        return threading.Thread(target=target, args=args, daemon=daemon)


class _WaveOut:
    """winmm waveOut with NBUF reusable buffers."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes; self.w = ctypes.windll.winmm
        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [('wFormatTag', wintypes.WORD), ('nChannels', wintypes.WORD), ('nSamplesPerSec', wintypes.DWORD),
                        ('nAvgBytesPerSec', wintypes.DWORD), ('nBlockAlign', wintypes.WORD), ('wBitsPerSample', wintypes.WORD),
                        ('cbSize', wintypes.WORD)]
        class WAVEHDR(ctypes.Structure):
            _fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', wintypes.DWORD), ('dwBytesRecorded', wintypes.DWORD),
                        ('dwUser', ctypes.c_void_p), ('dwFlags', wintypes.DWORD), ('dwLoops', wintypes.DWORD),
                        ('lpNext', ctypes.c_void_p), ('reserved', ctypes.c_void_p)]
        fmt = WAVEFORMATEX(1, 2, RATE, RATE * 4, 4, 16, 0)
        self.h = ctypes.c_void_p()
        r = self.w.waveOutOpen(ctypes.byref(self.h), 0xFFFFFFFF, ctypes.byref(fmt), None, None, 0)   # WAVE_MAPPER
        if r: raise RuntimeError('waveOutOpen failed (%d)' % r)
        self.WAVEHDR = WAVEHDR
        self.bufs = []; self.hdrs = []
        for _ in range(NBUF): self._add(len(self.hdrs))
        self.i = 0; self.underruns = 0; self.started = False

    MAX_BUF = 16                                         # 16 x 16 ms: at most ~0.25 s of sound queued
    def _add(self, at):
        ctypes = self.ct
        b = ctypes.create_string_buffer(BLOCK * 4)
        h = self.WAVEHDR(); h.lpData = ctypes.cast(b, ctypes.c_void_p); h.dwBufferLength = BLOCK * 4
        self.w.waveOutPrepareHeader(self.h, ctypes.byref(h), ctypes.sizeof(h)); h.dwFlags |= 1   # mark free (DONE)
        self.bufs.insert(at, b); self.hdrs.insert(at, h)

    def write(self, data):
        h = self.hdrs[self.i]
        if self.started and all(x.dwFlags & 1 for x in self.hdrs):
            # every buffer already played: the sound card ran dry while this process waited for the CPU (a busy
            # PC: OBS, a game, an Android emulator). Heard as the music dragging and stuttering. Keep more sound
            # queued from now on (a little more delay, no more gaps).
            self.underruns += 1
            if len(self.hdrs) < self.MAX_BUF:
                for _ in range(2): self._add(self.i)
                h = self.hdrs[self.i]
                if LOG_PATH:
                    try:
                        with open(LOG_PATH, 'a', encoding='utf-8') as f:
                            f.write('%s synth: sound ran dry (busy PC) - now %d buffers (%d ms)\n' % (
                                time.strftime('%H:%M:%S'), len(self.hdrs), len(self.hdrs) * BLOCK * 1000 // RATE))
                    except Exception: pass
        while not (h.dwFlags & 1): time.sleep(0.002)    # WHDR_DONE: free again
        self.ct.memmove(self.bufs[self.i], data, len(data))
        h.dwFlags &= ~1
        self.w.waveOutWrite(self.h, self.ct.byref(h), self.ct.sizeof(h))
        self.i = (self.i + 1) % len(self.hdrs); self.started = True

    def close(self):
        try:
            self.w.waveOutReset(self.h)
            for h in self.hdrs: self.w.waveOutUnprepareHeader(self.h, self.ct.byref(h), self.ct.sizeof(h))
            self.w.waveOutClose(self.h)
        except Exception:
            pass


class LiveSynth:
    """The parent side: send(msg) forwards MIDI messages to the synth process."""
    def __init__(self, sf_path, wav_path=None):
        if ANDROID: ctx = _ThreadCtx()
        else:
            import multiprocessing
            ctx = multiprocessing.get_context('spawn')
        self.conn, child = ctx.Pipe()
        import threading
        self._lock = threading.Lock()                 # the pipe is written from several threads (MIDI, chorus, panel)
        self.sf_path = sf_path; self.wav_path = wav_path; self._ctx = ctx; self._restarted = 0.0
        self.proc = ctx.Process(target=_synth_process, args=(child, sf_path, wav_path or os.environ.get('KE_SYNTH_WAV'), LOG_PATH), daemon=True)
        self.proc.start()
        if not self.conn.poll(60): raise RuntimeError('the SoundFont synthesizer did not start')
        tag, self.sf_name = self.conn.recv()

    def send(self, msg, t=None):
        try:
            with self._lock: self.conn.send((t, bytes(msg)))
        except Exception:
            self._revive()

    def _revive(self):
        """The synth process has gone (its error is in the log): start a new one, at most every 5 s."""
        import time as _t, threading as _th
        if self.proc.is_alive() or _t.time() - self._restarted < 5: return
        self._restarted = _t.time()
        def go():
            try:
                with self._lock:
                    self.conn, child = self._ctx.Pipe()
                    self.proc = self._ctx.Process(target=_synth_process, args=(child, self.sf_path, self.wav_path, LOG_PATH), daemon=True)
                    self.proc.start()
                if self.conn.poll(60): self.conn.recv()
            except Exception: pass
        _th.Thread(target=go, daemon=True).start()

    def reset(self):
        self.send(b'\xff')

    def pcm(self, t, raw, sid=None):
        """Mix recorded audio (16-bit stereo at RATE) in, starting at sender time t. Pieces with the same sid are
        one stream: played back to back, sample-continuous."""
        try:
            with self._lock: self.conn.send(('pcm', (t, bytes(raw), sid)))
        except Exception: pass

    def pcm_stop(self):
        try:
            with self._lock: self.conn.send(('pcmstop', None))
        except Exception: pass

    def control(self, **kw):
        """Synthesizer controller settings: master (0-2), mute (16 bools), chvol (16 x 0-2), transpose
        (semitones), filter (bool), prog (16 x bank*128+program, -1 = the song's own)."""
        try:
            with self._lock: self.conn.send(('ctl', kw))
        except Exception: pass

    def close(self):
        try:
            with self._lock: self.conn.send(None)
        except Exception: pass
        self.proc.join(3)
        if self.proc.is_alive() and hasattr(self.proc, 'terminate'): self.proc.terminate()   # (a thread on Android ends itself)

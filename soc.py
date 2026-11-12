"""Sunplus SoC peripheral models (reverse-engineered from firmware behaviour).
Peripheral block base: phys 0x1FFE8000 (firmware uses uncached 0xBFFE8000 in $s6)."""
PB = 0x1FFE8000

BUSY_BITS = {
}

FORCED_BITS = {
    0x6710: (0x3, 0),     # polled until bits 0 and 1 clear (busy flags)
    0x1A0:  (0xFFFFFFFF, 0),  # video 'latch pending' word, polled to 0 by the vsync-wait at 0x080364B0 (self-clears at next field)
}

class UART:
    def __init__(self, emu, echo=True):
        self.out = bytearray(); self.echo = echo
        emu.write_hooks[PB + 0x900] = self.tx
        # status: bit6 = TX ready (polled by the debug printf), bit0 = transmitter idle (polled by the MIDI byte
        # sender 0x080B008C; without it the player hangs the moment a song starts)
        emu.read_hooks[PB + 0x904] = lambda e: 0x41
    def tx(self, emu, p, v, size):
        self.out.append(v & 0xFF)
        if self.echo:
            import sys; sys.stdout.write(chr(v & 0x7F) if v & 0x7F else ''); sys.stdout.flush()

class SPIFlash:
    """SPI NOR controller at PB+0x1700..0x1718, device memory-mapped at phys 0x08000000.
    +0x1708 write: (opcode<<8 | control); busy bit7 clears immediately.
    +0x170C/+0x1710: address lo16 / bits23:16;  +0x1714/+0x1718: data lo16 / hi16.
    KS5: the player keeps its Setup settings (and other saved state) in a log of records at flash 0x100000, read
    with opcode 0x03 four bytes at a time and programmed with 0x02. That area is not part of the firmware image (the
    image holds code and fonts there), so for the KS5 the device is its own store ('nv': 4 KB sectors, erased =
    0xFF), kept between runs by the app (on_write -> dump()/load())."""
    SECTOR = 0x1000
    def __init__(self, emu, log=None):
        self.emu = emu; self.wel = 0; self.status = 0; self.rd = {}; self.log = log if log is not None else []
        self.nv = {} if FW_ID == 'ks5' else None      # sector index -> bytearray
        self.on_write = None
        emu.write_hooks[PB + 0x1708] = self.cmd
        emu.read_hooks[PB + 0x1714] = lambda e: self.rd.get(0x1714, e.mmio_regs.get(PB + 0x1714, 0))
        emu.read_hooks[PB + 0x1718] = lambda e: self.rd.get(0x1718, e.mmio_regs.get(PB + 0x1718, 0))
    def addr(self):
        r = self.emu.mmio_regs
        return ((r.get(PB + 0x1710, 0) & 0xFF) << 16) | (r.get(PB + 0x170C, 0) & 0xFFFF)
    def _sec(self, a, make=False):
        k = a // self.SECTOR; s = self.nv.get(k)
        if s is None and make: s = self.nv[k] = bytearray(b'\xff' * self.SECTOR)
        return s
    def nv_read(self, a, n=4):
        out = bytearray()
        for i in range(n):
            s = self._sec(a + i); out.append(s[(a + i) % self.SECTOR] if s is not None else 0xFF)
        return bytes(out)
    def _changed(self):
        if self.on_write is not None:
            try: self.on_write()
            except Exception: pass
    def dump(self):
        """{sector start address: bytes} of every sector that is not blank."""
        return {k * self.SECTOR: bytes(v) for k, v in sorted((self.nv or {}).items()) if v.count(0xFF) != self.SECTOR}
    def load(self, sectors):
        if self.nv is None: return
        for a, b in sectors.items():
            a = int(a)
            if a % self.SECTOR or len(b) != self.SECTOR: continue
            self.nv[a // self.SECTOR] = bytearray(b)
    def cmd(self, e, p, v, size):
        e.mmio_regs[PB + 0x1708] = v & ~0x80          # operation completes immediately
        op, ctl = (v >> 8) & 0xFF, v & 0xFF
        r = e.mmio_regs; a = self.addr(); self.rd = {}
        data = (r.get(PB + 0x1714, 0) & 0xFFFF) | ((r.get(PB + 0x1718, 0) & 0xFFFF) << 16)
        nv = self.nv is not None
        if op == 0x06: self.wel = 1; self.log.append(('WREN',))
        elif op == 0x04: self.wel = 0; self.log.append(('WRDI',))
        elif op == 0x05: self.rd[0x1714] = (self.status & ~1) | (self.wel << 1); self.log.append(('RDSR',))
        elif op == 0x01: self.status = data & 0xFF; self.log.append(('WRSR', data & 0xFF))
        elif op == 0x9F: self.rd[0x1714] = 0x40EF; self.rd[0x1718] = 0x15; self.log.append(('RDID',))   # Winbond-like 16Mbit
        elif op in (0x03, 0x0B) and nv:
            w = int.from_bytes(self.nv_read(a), 'little')
            self.rd[0x1714] = w & 0xFFFF; self.rd[0x1718] = w >> 16
            if len(self.log) < 20000: self.log.append(('READ', hex(ctl), hex(a), hex(w)))
        elif op in (0x20, 0x23, 0x27, 0xD8, 0x52):
            n = 0x1000 if op == 0x20 else 0x10000 if op == 0xD8 else 0x8000 if op == 0x52 else 0x2000
            base = a & ~(n - 1)
            if self.wel:
                if nv:
                    for k in range(base // self.SECTOR, (base + n) // self.SECTOR): self.nv.pop(k, None)
                    self._changed()
                else: e.ram[base:base + n] = b'\xff' * n
            self.wel = 0; self.log.append(('ERASE', hex(op), hex(a), hex(base), n))
        elif op in (0x02, 0x26):
            nb = {0xB3: 4}.get(ctl, 4)
            if self.wel:
                if nv:
                    for i in range(nb):
                        s = self._sec(a + i, True); s[(a + i) % self.SECTOR] &= (data >> (8 * i)) & 0xFF
                    self._changed()
                else:
                    for i in range(nb): e.ram[a + i] &= (data >> (8 * i)) & 0xFF
            self.wel = 0; self.log.append(('PROG', hex(ctl), hex(a), hex(data)))
        else: self.log.append(('UNKNOWN', hex(v), hex(a), hex(data)))

FW_GP = 0x800045F4          # the firmware's global pointer (fixed once its startup code sets it)
# Which firmware build is loaded (fwprofile.py). The hooks below are written with MP100NSP addresses; T() turns
# one into the loaded build's address (None = that code is not in this build, the hook is not installed).
FW_ID = 'mp100'; FWMAP = None; FWGP = None; FWDATA = None
FW_GPS = {'mp100': 0x800045F4, 'migael': 0x800045F4, 'ks5': 0x80002C70}


def set_firmware(fid):
    global FW_ID, FWMAP, FW_GP, FWGP, FWDATA
    import fwprofile
    FW_ID = fid; FWMAP = fwprofile.table(fid); FWGP = fwprofile.gpoff(fid); FWDATA = fwprofile.data(fid); FW_GP = FW_GPS.get(fid, 0x800045F4)


def T(a):
    if FWMAP is None or a is None: return a
    low = 0x08000000 <= a < 0x10000000
    v = FWMAP.get(a | 0x80000000 if low else a)
    if v is None: return None
    return v & 0x7FFFFFFF if low else v


def G(off):
    """gp offset of an MP100NSP global in the loaded build (None if not known)."""
    return off if FWGP is None else FWGP.get(off)


def D(a):
    """Address of an MP100NSP SDRAM variable in the loaded build (None if not known)."""
    return a if FWDATA is None else FWDATA.get(a)


def TS(pcs):
    return tuple(x for x in (T(p) for p in pcs) if x is not None)


def fw_gp(e):
    """gp for reading firmware globals from outside the CPU. Register r28 is only gp while the firmware's own code
    keeps it; some library routines borrow it (seen in the Setup > Greetings editor), so a raw r28 read could point
    anywhere and an index into SDRAM then failed (IndexError). 0 until startup has set gp."""
    return FW_GP if e.r[28] else 0

NEWSONG_LINE = 3.0               # KS5 new-song list: seconds per line (the real player's pace)
DSP_LOG = []                     # KS5 audio DSP commands (n, command, parameter), for tests
DSP_STACKS = False


def attach(emu, echo=True):
    import fwprofile
    set_firmware(fwprofile.detect(bytes(emu.rd8(0x88000000 + i) for i in range(0x40))))
    emu.fw_id = FW_ID
    uart = UART(emu, echo)
    emu.flash = SPIFlash(emu)
    emu.timers = Timers(emu)
    emu.servo = ServoBus(emu)
    emu.dbg = DebugPrint(emu)
    emu.tray = TraySwitches(emu)
    emu.usb = USBPort(emu)
    emu.i2c = I2CBus(emu)
    emu.keychip = KeyChip(emu, emu.i2c)
    emu.midi = MidiOut(emu)
    emu.voice = KeyVoice(emu)
    emu.score = RandomScore(emu)
    emu.chorus = Chorus(emu)
    emu.setup_redraw = SetupRedraw(emu)
    emu.secchip = SecurityChip(emu)
    if hasattr(emu, 'fast_sink'): emu.fast_sink(PB + 0x4D8)   # written ~200,000x/s by the idle loop, never read back
    emu.inflate = InflateHLE(emu)
    # Display timing status polled by 0x08034DEC (waits, with a 100k-200k loop timeout, for PB+0x4C0 bit15 or for
    # PB+0x4D4 to equal 0, 1 or 2). With nothing modelled every wait ran into its timeout (~1-2 M instructions each).
    # Modelled (INFERRED): 0x4D4 = phase of the current video field (0, 1, 2 in thirds of the 1 ms... field period),
    # 0x4C0 bit15 = set (display engine ready).
    emu.read_hooks[PB + 0x4D4] = lambda e: ((e.n % 108000) * 3) // 108000
    emu.read_hooks[PB + 0x4C0] = lambda e: e.mmio_regs.get(PB + 0x4C0, 0) | 0x8000
    import os
    cid = os.environ.get('KE_CHIPID')                 # experimental: value returned by register 0 (chip ID/variant)
    if cid: emu.read_hooks[PB + 0x0] = (lambda v: lambda e: v)(int(cid, 0))
    # 0x204 bit0 = video field parity (odd/even). Toggles once per field (~108k instrs, matching the field IRQ period).
    emu.read_hooks[PB + 0x204] = lambda e: (e.n // 108000) & 1
    # 0x50C bit15 = field indicator polled by 'wait N fields' (0x807818D0); toggles once per field
    emu.read_hooks[PB + 0x50C] = lambda e: (((e.n // 108000) & 1) << 15) | (e.mmio_regs.get(PB + 0x50C, 0) & 0x7FFF)
    # 0x070: control reg; bit11 = busy, self-clears (PLL/SDRAM init handshake)
    emu.read_hooks[PB + 0x070] = lambda e: e.mmio_regs.get(PB + 0x070, 0) & ~0x800
    def flush_w(e, p, v, size):          # 0x130: write-buffer flush; bit1 = request, bit0 cleared when done
        e.mmio_regs[PB + 0x130] = v & ~1 if v & 2 else v
    emu.write_hooks[PB + 0x130] = flush_w
    def cmd_w(e, p, v, size):             # 0x720: command/serial controller; bits15:12 = busy, completes instantly
        e.mmio_regs[PB + 0x720] = v & ~0xF000
    emu.write_hooks[PB + 0x720] = cmd_w
    if FW_ID == 'ks5':
        # Audio DSP command interface (the KS5 plays its songs through it; reverse-engineered from 0x880D266C,
        # 0x8800FBF4 and their callers):
        #   PB+0x4C0 command: (type << 8) | argument, bit15 = acknowledged by the DSP
        #   PB+0x4C4 parameter; PB+0x4D4 DSP state: 0 stopped, 1 playing, 2 paused; the firmware writes -1 before
        #   loading new DSP code and waits for 0.
        #   type 2 (0x200 | sub): sub 1 / 2 play / resume -> 1, 3 stop -> 0, 4 pause -> 2
        # The MP100NSP reads these as display timing instead (below), so this is only for the KS5.
        #   type 0x27 (0x2700 | sub): the second player (the song: 0x880D2E20 / 2E94 / 2F08), state in PB+0x4D0,
        #   same sub-commands
        dsp = dict(state=0, state2=0, midi=False)
        # The song: the firmware decrypts the song's MIDI file and merges it into one track at 0x80068C00
        # (0x8807C6D4), then starts the DSP's MIDI player (type 3, sub 1). PB+0x4F0 is the player's clock (16 bits,
        # wraps; see SongPlayer): the firmware drives the lyrics from it, ends the song when it reaches the song
        # length and stops the song after a few seconds when it stops moving (0x8807DF0C). HLE: SongPlayer plays the file on the emulated clock into the MIDI output (the SoundFont synth).
        # PB+0x4EC: lyric events played so far (the lyric page).
        player = SongPlayer(emu)
        emu.song_player = player
        emu.write_hooks[PB + 0x4EC] = lambda e, p, v, size: (player.set_lyrics(e, v), e.ks5_ring.set_clock(e, v))
        emu.read_hooks[PB + 0x4EC] = lambda e: player.lyrics(e)
        emu.write_hooks[PB + 0x4F0] = lambda e, p, v, size: player.seek(e, v)
        emu.read_hooks[PB + 0x4F0] = lambda e: player.position(e) & 0xFFFF
        # the firmware reads these ~100,000-300,000 times per second while a song plays: in the C core they are
        # plain registers that the run loop updates (SongPlayer.pump every ~10 ms) instead of Python calls
        if hasattr(emu, 'fast_computed') and emu.fast_computed(PB + 0x4F0, 3) and emu.fast_computed(PB + 0x4EC, 3):
            emu.read_hooks.pop(PB + 0x4F0, None); emu.read_hooks.pop(PB + 0x4EC, None); player.fast = True
        def dsp_cmd(e, p, v, size):
            e.mmio_regs[PB + 0x4C0] = v
            t = (v >> 8) & 0x7F
            if t == 2:
                sub = v & 0xFF
                if sub == 1: e.ks5_ring.play(e, resume=False)
                elif sub == 2: e.ks5_ring.play(e, resume=True)
                elif sub == 3: e.ks5_ring.stop(e)
                elif sub == 4: e.ks5_ring.pause(e)
            if t in (2, 0x27):
                sub = v & 0xFF; k = 'state' if t == 2 else 'state2'
                if sub in (1, 2): dsp[k] = 1
                elif sub == 3: dsp[k] = 0
                elif sub == 4: dsp[k] = 2
            elif t == 5 and (v & 0xFF) == 8:   # 0x508: stop the song (sent by Stop and before each new song)
                player.stop(e); dsp['midi'] = False
            elif t == 3:                    # the song (MIDI) player: 1 = play, 2 = stop
                sub = v & 0xFF
                if sub == 1: player.play(e); dsp['midi'] = True
                elif sub == 2: player.stop(e); dsp['midi'] = False
            DSP_LOG.append((e.n, v, e.mmio_regs.get(PB + 0x4C4, 0)) + ((tuple(hex(e.rd32(e.r[29] + k)) for k in range(0, 400, 4) if 0x88000000 <= e.rd32(e.r[29] + k) < 0x88200000)[:10],) if DSP_STACKS else ()))
            if len(DSP_LOG) > 4000: del DSP_LOG[:1000]
        def dsp_state_w(e, p, v, size):
            dsp['state'] = 0 if (v & 0xFFFFFFFF) == 0xFFFFFFFF else v       # -1: new code loaded, the DSP restarts
        emu.write_hooks[PB + 0x4C0] = dsp_cmd
        emu.write_hooks[PB + 0x4D4] = dsp_state_w
        emu.read_hooks[PB + 0x4C0] = lambda e: e.mmio_regs.get(PB + 0x4C0, 0) | 0x8000
        emu.read_hooks[PB + 0x4D4] = lambda e: dsp['state']
        emu.read_hooks[PB + 0x4D0] = lambda e: dsp['state2']
        emu.dsp = dsp
        # A/V (audio) ring: MP3 for the type-2 player (spoken digits, voice clips, and the music of "MP3 songs"
        # such as 122, which the firmware reads from /IDX/Apvol and decrypts). The firmware writes its write
        # position to PB+0x4C8 and waits for room behind the decoder's read position PB+0x4CC. KS5Ring plays the
        # stream on the emulated clock: it takes MP3 frames as their playing time comes, moves the read position,
        # reports the play clock and hands the bytes to the app's audio output.
        # The firmware compares an audio-mode word the DSP keeps in its shared memory (0x8800EDB0(15)) with the
        # mode it set (gp+2544) every few ticks, and reloads the DSP program and restarts what is playing when they
        # differ (0x880D3E78 -> 0x880D3988). The DSP is not emulated, so the word never matches: the reload ran
        # every 0.3 s (clips cut off, MP3 songs never got going, slow keys). HLE: the check finds them equal.
        import fwprofile as _fp
        mc = _fp.extra(FW_ID).get('dsp_mode_check')
        if mc:
            def mode_ok(e):
                e.pc = e.r[31]; e.npc = e.pc + 4
            emu.hle[mc] = mode_ok; emu.bp.add(mc)
        wd = _fp.extra(FW_ID).get('dsp_watchdog')
        if wd:
            at, to = wd
            def no_reload(e, to=to):
                e.pc = to; e.npc = to + 4
            emu.hle[at] = no_reload; emu.bp.add(at)
        # New-song list: one line is drawn each time a counter (gp+2424) reaches 9; the counter is stepped from the
        # main loop, which runs far more often here than on the player (~0.2 s a line instead of ~3 s). The step
        # is paced on the emulated clock instead: a line every NEWSONG_LINE seconds.
        ns = _fp.extra(FW_ID).get('newsong_step')
        if ns:
            at, skip, cnt = ns; last = [None]
            def ns_step(e, skip=skip, cnt=cnt):
                if e.rd32(FW_GP + cnt) < 9: return                     # not due by the firmware's own count
                if last[0] is not None and 0 <= e.n - last[0] < NEWSONG_LINE * 108_000_000:
                    e.pc = skip; e.npc = skip + 4; return              # wait: leave the counter running
                last[0] = e.n
            emu.hle[at] = ns_step; emu.bp.add(at)
        ring = KS5Ring(emu, dsp)
        emu.ks5_ring = ring
        emu.write_hooks[PB + 0x4C8] = ring.write
        emu.write_hooks[PB + 0x4CC] = ring.reset
        emu.read_hooks[PB + 0x4CC] = lambda e: ring.pump(e) or e.mmio_regs.get(PB + 0x4CC, 0)
        if hasattr(emu, 'fast_computed') and emu.fast_computed(PB + 0x4CC, 3):
            emu.read_hooks.pop(PB + 0x4CC, None)          # read in the C core, updated by pump()
        player.tick = ring.pump
    # handshakes discovered by the auto-hook analyser and made permanent: offset -> (bits forced 0, bits forced 1)
    for off, (cm, sm) in FORCED_BITS.items():
        emu.read_hooks[PB + off] = (lambda off, cm, sm: lambda e: (e.mmio_regs.get(PB + off, 0) & ~cm) | sm)(off, cm, sm)
    if hasattr(emu, 'fast_computed'):        # the same functions, computed in the C core (busy-waited registers)
        if FW_ID != 'ks5':
            emu.fast_computed(PB + 0x4D4, 1, 108000, 3)
            emu.fast_computed(PB + 0x4C0, 3, keep=0xFFFFFFFF, set_=0x8000)
        emu.fast_computed(PB + 0x204, 2, 108000, 0, keep=0)
        emu.fast_computed(PB + 0x50C, 2, 108000, 15, keep=0x7FFF)
        emu.fast_computed(PB + 0x070, 3, keep=~0x800 & 0xFFFFFFFF)
        for off, (cm, sm) in FORCED_BITS.items(): emu.fast_computed(PB + off, 3, keep=~cm & 0xFFFFFFFF, set_=sm)
        # also answered in C (each Python call from the core costs several microseconds; these were ~2000/s):
        # UART/MIDI status (always ready) and the 0x720 command register (busy bits 15:12 read back as 0)
        if emu.fast_computed(PB + 0x904, 3, keep=0, set_=0x41): emu.read_hooks.pop(PB + 0x904, None)
        if emu.fast_computed(PB + 0x720, 3, keep=~0xF000 & 0xFFFFFFFF): emu.write_hooks.pop(PB + 0x720, None)
    # generic table: register offset -> bits that read back as 0 immediately (operation completes instantly)
    for off, mask in BUSY_BITS.items():
        def mk(off, mask):
            def w(e, p, v, size): e.mmio_regs[PB + off] = v & ~mask
            return w
        emu.write_hooks[PB + off] = mk(off, mask)
    return uart


# Primary IRQ bit 13 (handler 0x080355C0 -> 0x08097A54) is the karaoke clock: each interrupt adds 898 to a counter
# and every 1000 advances the 10 ms karaoke tick gp+1832 (song sequencer, number-entry timeouts, "Select Song").
# Nothing raised it before, so after the background video started the karaoke task never ran again. Period
# INFERRED as 8.98 ms (= 10 ms * 898/1000); checked against a song: the firmware's MIDI drum hits come 0.565-0.575 s
# apart, the song file says 0.571 s.
KARAOKE_TICK = 969_840        # instructions (8.98 ms at 108 MHz)
DEFAULT_SOURCES = {9: 108000, 10: 108000, 13: KARAOKE_TICK}


class IRQC:
    """Primary interrupt controller (PB+0x100 mask, +0x104 ack W1C, +0x108 status) -> CPU IP2.
    Sources are periodic pulses {bit: period_in_instructions} (model of unknown hardware timers)."""
    VECTOR = 0x88000080
    # Group 2 (found in the dispatcher at 0x08002408): status PB+0x120, mask PB+0x118, ack (W1C) PB+0x11C -> CPU IP3.
    # Bit 0 = periodic tick that runs the drive module's timers (0x8802CA30 -> 0x8079E9A8). Period is INFERRED (1 ms fits
    # the firmware's 40 ms brake / 1.25 s motor / 2.5 s timeout constants); bits 4, 6, 8 are enabled but not modelled.
    DEFAULT_SOURCES2 = {0: 108000}

    def __init__(self, emu, sources, sources2=None):
        self.emu = emu; self.pending = 0; self.sources = dict(sources)
        self.next = {b: emu.n + p for b, p in sources.items()}
        self.delivered = {}
        self.sources2 = dict(self.DEFAULT_SOURCES2 if sources2 is None else sources2)
        self.pending2 = 0; self.next2 = {b: emu.n + p for b, p in self.sources2.items()}; self.delivered2 = {}
        emu.write_hooks[PB + 0x104] = self.ack
        emu.read_hooks[PB + 0x108] = lambda e: self.pending & e.mmio_regs.get(PB + 0x100, 0)
        emu.write_hooks[PB + 0x11C] = self.ack2
        emu.read_hooks[PB + 0x120] = lambda e: self.pending2 & e.mmio_regs.get(PB + 0x118, 0)
        emu.irq_hook = self.poll
        emu.irq_next_fn = self.next_check
        for off in (0x100, 0x118):                 # mask writes can release a pending interrupt: check right away
            emu.write_hooks[PB + off] = lambda e, p, v, size: setattr(e, 'irq_next', e.n)
    def next_check(self, e):
        """For the C core: the instruction count at which poll() must run next (the next timer deadline, or soon if
        an unmasked interrupt is waiting for the CPU to enable interrupts)."""
        if (self.pending & e.mmio_regs.get(PB + 0x100, 0)) or (self.pending2 & e.mmio_regs.get(PB + 0x118, 0)):
            if e.cp0[12] & 1: return e.n + 1            # deliver as soon as the CPU is outside a delay slot
            # interrupts disabled: the C core checks again when the status register changes (mtc0 / rfe)
        nx = [v for v in self.next.values()] + [v for v in self.next2.values()]
        return min(nx) if nx else e.n + 100000
    def ack(self, e, p, v, size): self.pending &= ~v; e.irq_next = e.n
    def ack2(self, e, p, v, size): self.pending2 &= ~v; e.irq_next = e.n
    def poll(self, e):
        for b, per in self.sources.items():
            if e.n >= self.next[b]:
                self.pending |= 1 << b; self.next[b] = e.n + per
        for b, per in self.sources2.items():
            if e.n >= self.next2[b]:
                self.pending2 |= 1 << b; self.next2[b] = e.n + per
        mask = e.mmio_regs.get(PB + 0x100, 0)
        if self.pending & mask: e.cp0[13] |= 0x400
        else: e.cp0[13] &= ~0x400
        mask2 = e.mmio_regs.get(PB + 0x118, 0)
        if self.pending2 & mask2: e.cp0[13] |= 0x800
        else: e.cp0[13] &= ~0x800
        sr = e.cp0[12]
        if (sr & 1) and (sr & e.cp0[13] & 0xFF00):
            e.cp0[14] = e.pc
            e.cp0[13] &= ~0x7C
            e.cp0[12] = (sr & ~0x3F) | ((sr << 2) & 0x3C)
            e.pc = self.VECTOR; e.npc = self.VECTOR + 4
            st = self.pending & mask
            for b in range(16):
                if st >> b & 1: self.delivered[b] = self.delivered.get(b, 0) + 1
            st2 = self.pending2 & mask2
            for b in range(16):
                if st2 >> b & 1: self.delivered2[b] = self.delivered2.get(b, 0) + 1


class Timers:
    """Free-running counters driven by executed-instruction count (~108 instr/us, from the firmware's own 1 ms delay loop)."""
    IPUS = 108
    def __init__(self, emu):
        self.emu = emu; self.latched = 0
        self.IPUS = int(__import__('os').environ.get('KE_IPUS', 0)) or Timers.IPUS
        def us(e): return (e.n // self.IPUS) & 0xFFFFFFFF
        emu.write_hooks[PB + 0xE8] = lambda e, p, v, s: setattr(self, 'latched', us(e))
        emu.read_hooks[PB + 0xE0] = lambda e: self.latched & 0xFFFF
        emu.read_hooks[PB + 0xE4] = lambda e: (self.latched >> 16) & 0xFFFF
        emu.read_hooks[PB + 0x80] = lambda e: (us(e) // 10) & 0xFFFF     # 10 us ticks (unit assumed)
        if FW_ID == 'ks5':
            # KS5: PB+0x90/0x94 is a millisecond clock. Its main loop only runs when the count changes and runs its
            # 50-count tick (OSD message timeouts: the 'Prog : 12345' box = 20 ticks = 1 s; the new-song list
            # typing, ~3 s a line) from it; counted in microseconds, all of these ran ~18x too fast.
            self.IPMS = self.IPUS * 1000
            def ms(e): return (e.n // self.IPMS) & 0xFFFFFFFF
            emu.read_hooks[PB + 0x90] = lambda e: ms(e) & 0xFFFF
            emu.read_hooks[PB + 0x94] = lambda e: (ms(e) >> 16) & 0xFFFF
            if hasattr(emu, 'fast_computed'):       # the hot 10 us counter in C (the drive code polls it)
                emu.fast_computed(PB + 0x80, 1, p=10 * self.IPUS * 65536, k=65536)
            return
        emu.read_hooks[PB + 0x90] = lambda e: us(e) & 0xFFFF
        if hasattr(emu, 'fast_timers'): emu.fast_timers(self.IPUS)   # same counters, computed in the C core
        emu.read_hooks[PB + 0x94] = lambda e: (us(e) >> 16) & 0xFFFF


class VideoFIFO:
    """1 KB data window at phys 0x1FFF0000: the firmware pushes the MPEG-2 elementary stream (word writes) here.
    We capture the byte stream and split it into complete sequences (seq header .. seq end)."""
    LO, HI = 0x1FFF0000, 0x1FFF0400
    def __init__(self, emu):
        self.buf = bytearray(); self.frames = []; self.pos = 0
        self._mem = bytearray(self.HI - self.LO)          # the window is real SRAM: the firmware reads it back
        if FW_ID == 'ks5':
            # 0xBFFF0080 = 0x1818: 'power key pressed' left by the firmware before it restarts itself. A cold start
            # without it only arms the auto power-off (the real KS5 starts in standby until its power key is
            # pressed), so the emulator starts as if the power key had been pressed.
            self._mem[0x80:0x84] = (0x1818).to_bytes(4, 'little')
            # The KS5 shows its power-on picture (the Platinum KS-Series logo, an MPEG-2 still in its flash) through
            # a decoder path that is not modelled; the picture is taken from the image so the screen shows it.
            try:
                img = bytes(emu.ram[:0x400000]); best = b''; i = -1
                while True:
                    i = img.find(b'\x00\x00\x01\xb3', i + 1)
                    if i < 0: break
                    j = img.find(b'\x00\x00\x01\xb7', i)
                    if 0 < j - i < 1 << 20 and j + 4 - i > len(best): best = img[i:j + 4]
                if len(best) > 10000: self.frames.append((0, best))
            except Exception:
                pass
        self.emu = emu
        if hasattr(emu, 'fast_window'):                   # C core: window handled natively, writes captured in bulk
            emu.fast_window(self.LO, self.HI, self._mem, self._captured)
        else:
            emu.win_w.append((self.LO, self.HI, self.w)); emu.win_r.append((self.LO, self.HI, self.r))
        self.install_feeder_hle(emu)
    @property
    def mem(self): return self._mem
    @mem.setter
    def mem(self, v): self._mem[:] = bytes(v)[:len(self._mem)].ljust(len(self._mem), b'\0')
    total_in = 0
    def since(self, mark):
        """(bytes appended to the stream after byte count `mark`, new mark) - for a live decoder."""
        n = self.total_in - mark
        if n <= 0: return b'', self.total_in
        n = min(n, len(self.buf))
        return bytes(self.buf[-n:]), self.total_in
    def _captured(self, data):
        self.total_in += len(data)
        for i in range(0, len(data), 1024):
            piece = data[i:i + 1024]; self.buf += piece; self.scan(self.emu, len(piece))
        self._trim()
    def r(self, e, p, size):
        o = p - self.LO
        return int.from_bytes(self.mem[o:o + size], 'little')
    KEEP = 4 << 20                                        # bytes of stream history kept (older data is dropped)
    def w(self, e, p, v, size):
        b = (v & ((1 << (8 * size)) - 1)).to_bytes(size, 'little'); self.total_in += size
        o = p - self.LO; self.mem[o:o + size] = b
        self.buf += b
        self.scan(e)
    def bulk(self, e, data):
        """A whole 1 KB window fill at once (used by the feeder HLE below)."""
        if hasattr(e, 'drain'): e.drain()               # bytes the CPU wrote before this block come first
        self.total_in += len(data)
        self.mem[:len(data)] = data
        self.buf += data
        self.scan(e, len(data))
        self._trim()
    def _trim(self):
        if len(self.buf) > 2 * self.KEEP:                 # trim, keeping indices consistent
            cut = len(self.buf) - self.KEEP
            if getattr(self, 'open_i', None) is not None: cut = min(cut, self.open_i)
            if cut > 0:
                del self.buf[:cut]; self.pos = max(0, self.pos - cut)
                if getattr(self, 'open_i', None) is not None: self.open_i -= cut
    def install_feeder_hle(self, e):
        """The stream feeder 0x0807BC34 copies 1 KB from SDRAM into this window with 256 word stores; that loop was
        ~60 % of all emulated time while a disc loads. Done in one Python copy instead (same bytes, same registers)."""
        import emu as emu_mod
        def hook(em):
            src = em.r[11] & 0x1FFFFFFF                     # t3 = source (gp+2508)
            if src + 1024 <= em.low_end: data = bytes(em.low[src:src + 1024])
            elif emu_mod.BASE <= src and src + 1024 <= em.ram_end: data = bytes(em.ram[src - emu_mod.BASE:src - emu_mod.BASE + 1024])
            else: data = bytes(1024)                        # past the end of memory (feeder run-off): reads as 0
            self.bulk(em, data)
            r = em.r; base = em.r[11]
            r[9] = 1024; r[10] = base; r[6] = (base + 1024) & 0xFFFFFFFF; r[7] = 0xBFFF0400; r[8] = 0
            r[3] = (base + 1024) & 0xFFFFFFFF; r[2] = 0
            em.pc = (em.pc & 0xF0000000) | T(0x0807BC98); em.npc = em.pc + 4
            em.n += 720                                     # keep the emulated clock: the loop is ~720 instructions
        if T(0x0807BC98) is not None:
            for pc in TS((0x8807BC44, 0x0807BC44)):
                e.hle[pc] = hook; e.bp.add(pc)
        # Start-code scanner in the RAM module (0x8076F750..0x8076F790): walks the window byte by byte looking for
        # 00 00 01. ~70 % of the remaining time during disc load. Same algorithm in Python, same exit registers.
        def scan(em):
            r = em.r; M = 0xFFFFFFFF; sp = r[29]
            rd = em.rd32
            base = rd(sp + 24); limit = rd(sp + 16); flag = rd(sp + 20); fp = r[30]; s3 = r[19]
            lo = base & 0x1FFFFFFF
            win = self.mem if self.LO <= lo and lo + limit <= self.HI else None
            n = 0
            while True:
                if flag:                                    # found: take the branch to 0x8076F7D4
                    r[2] = (base + s3) & M; r[4] = flag; r[3] = (fp << 8) & M
                    r[19] = s3; r[30] = fp; em.wr32(sp + 20, flag)
                    em.pc = 0x8076F7D4; em.npc = em.pc + 4; em.n += n * 17; return
                b = win[lo - self.LO + s3] if win is not None else em.rd8(base + s3)
                fp = ((fp << 8) | b) & M; s3 += 1; n += 1
                v0 = (fp << 8) & M
                flag = 1 if (v0 ^ 0x100) == 0 else 0
                if s3 >= limit:                             # loop ends: fall through to 0x8076F794
                    r[2] = flag; r[3] = 0; r[4] = limit; r[17] = b; r[19] = s3; r[30] = fp
                    em.wr32(sp + 20, flag)
                    em.pc = 0x8076F794; em.npc = em.pc + 4; em.n += n * 17; return
        self._scan_hook = scan
        if FW_ID in ('mp100', 'migael'):
            e.hle[0x8076F750] = self._scan_guard; e.bp.add(0x8076F750)
    def _scan_guard(self, em):
        # the RAM module is loaded at run time: only take over if the expected code is really there
        if em.rd32(0x8076F76C) == 0x90510000 and em.rd32(0x8076F780) == 0x38420100:
            self._scan_hook(em)
    def scan(self, e, new=4):
        # incremental: only look at the newest bytes (a start code can straddle two writes, hence the overlap)
        b = self.buf; tail = max(len(b) - new - 12, 0)
        if getattr(self, 'open_i', None) is None:
            i = b.find(b'\x00\x00\x01\xb3', max(self.pos, tail))
            if i >= 0: self.open_i = i
        if getattr(self, 'open_i', None) is not None:
            j = b.find(b'\x00\x00\x01\xb7', max(self.open_i, tail))
            if j >= 0:
                self.frames.append((e.n, bytes(b[self.open_i:j + 4]))); self.pos = j + 4; self.open_i = None


class ServoBus:
    """Serial register interface to the drive/servo chip: PB+0xE80 = command/address word, PB+0xE84 = write data,
    PB+0xE88 = read data (low byte).  Modelled as a plain register file (writes read back, unwritten = default).
    Everything is logged so we can see which registers the firmware queries.  Chip behaviour itself is UNKNOWN."""
    # 0x4538 bit3: tested right after servo init (0x807A41B0); clear -> the drive module re-inits forever, set -> it
    # proceeds to the tray-close command. Meaning INFERRED ("servo ready / locked"), value needed to get past init.
    DEFAULTS = {0x4538: 0x08}
    def __init__(self, emu, defaults=None, maxlog=50000):
        self.emu = emu; self.regs = dict(self.DEFAULTS); self.regs.update(defaults or {}); self.cmd = 0; self.log = []; self.maxlog = maxlog
        self.read_addrs = {}          # address -> [count, first_n, last_value]
        self.dyn = {}                 # address -> fn(emu)->byte : dynamic (time-varying) status registers
        emu.write_hooks[PB + 0xE80] = self.w_cmd
        emu.write_hooks[PB + 0xE84] = self.w_data
        emu.read_hooks[PB + 0xE88] = self.r_data
    def _log(self, *x):
        if len(self.log) < self.maxlog: self.log.append(x)
    def w_cmd(self, e, p, v, size):
        self.cmd = v & 0xFFFF
    def w_data(self, e, p, v, size):
        self.regs[self.cmd] = v & 0xFF
        self._log(e.n, e.cur_pc, 'W', self.cmd, v & 0xFF)
    def r_data(self, e):
        v = (self.dyn[self.cmd](e) if self.cmd in self.dyn else self.regs.get(self.cmd, 0)) & 0xFF
        r = self.read_addrs.setdefault(self.cmd, [0, e.n, v]); r[0] += 1; r[2] = v
        self._log(e.n, e.cur_pc, 'R', self.cmd, v)
        return v


class DebugPrint:
    """The firmware's debug printf (0x0803212C) is compiled to a bare 'jr $ra'. Hook it and format the message
    ourselves (MIPS o32: a0 = format, a1..a3 then stack sp+16.. = args) so the firmware's own debug text is visible."""
    PCS = (0x8803212C, 0x0803212C)
    def __init__(self, emu, keep=20000):
        self.emu = emu; self.log = []; self.keep = keep; self.count = 0
        for pc in TS(self.PCS):
            emu.hle[pc] = self.hook; emu.bp.add(pc)
    def _str(self, a, n=200):
        out = bytearray()
        for i in range(n):
            c = self.emu.rd8(a + i)
            if c == 0: break
            out.append(c)
        return out.decode('latin1')
    def hook(self, e):
        try:
            fmt = self._str(e.r[4]); args = [e.r[5], e.r[6], e.r[7]] + [e.rd32(e.r[29] + 16 + 4 * i) for i in range(6)]
            msg = self._format(fmt, args)
        except Exception as ex:
            msg = '<printf error %s>' % ex
        self.count += 1
        if len(self.log) < self.keep: self.log.append((e.n, e.r[31], msg))
    def _format(self, fmt, args):
        import re
        out = []; ai = 0; i = 0
        for m in re.finditer(r'%([-+ 0#]*)(\d*)(?:\.(\d+))?(l|h|hh)?([diuxXcsp%])', fmt):
            out.append(fmt[i:m.start()]); i = m.end()
            flags, width, prec, _, conv = m.groups()
            if conv == '%': out.append('%'); continue
            v = args[ai] if ai < len(args) else 0; ai += 1
            if conv in 'di': v = v - (1 << 32) if v & 0x80000000 else v; s = str(v)
            elif conv == 'u': s = str(v)
            elif conv in 'xp': s = '%x' % v
            elif conv == 'X': s = '%X' % v
            elif conv == 'c': s = chr(v & 0xFF)
            else: s = self._str(v) if v else '(null)'
            w = int(width) if width else 0
            s = s.rjust(w, '0' if '0' in flags and conv not in 'sc' else ' ') if '-' not in flags else s.ljust(w)
            out.append(s)
        out.append(fmt[i:])
        return ''.join(out)


class SecurityChip:
    """Other builds' security chip (see fwprofile.EXTRA): answered as a genuine chip would, so the player starts."""
    def __init__(self, emu):
        import fwprofile
        x = fwprofile.extra(FW_ID); self.checks = 0
        if 'auth_check' in x:
            emu.hle[x['auth_check']] = self._ok; emu.bp.add(x['auth_check'])
        if 'auth_exchange' in x:
            self.flag = x['auth_flag_gp']
            emu.hle[x['auth_exchange']] = self._exchange; emu.bp.add(x['auth_exchange'])
    def _ok(self, e):
        self.checks += 1
        e.r[2] = 1; e.pc = e.r[31]; e.npc = e.pc + 4
    def _exchange(self, e):
        e.wr8(FW_GP + self.flag, 1); e.r[2] = 1; e.pc = e.r[31]; e.npc = e.pc + 4


class TraySwitches:
    """GPIO inputs read by the drive module (reverse-engineered from 0x80786238 / 0x8078647C):
      switch A (pin number at gp+3472, 0x09 on the MP100NSP), switch B (pin at gp+3483, 0x08) -> position = A + 2*B
      tray-in limit switch (pin at gp+3484, 0x60), active low (0 = tray fully in)
    A pin p is bit p%32 of the input register PB+0x9E0 + 8*(p//32) (0x09 -> 0x9E0 bit9, 0x60 -> 0x9F8 bit0).
    The board configuration sets the pins at run time (the KS5 uses 0x4F, 0x4E, 0x42), so they are read from the
    firmware's own variables. The close procedure waits for position 2 and tray-in = 0, so 'closed' = A0 B1
    tray-in 0. Which position codes mean 'open' / 'moving' is NOT confirmed; only 'closed' is used."""
    CLOSED = dict(a=0, b=1, trayin=0)
    PINS = dict(a=(3472, 0x09), b=(3483, 0x08), trayin=(3484, 0x60))
    def __init__(self, emu, state='closed'):
        self.emu = emu; self.set(state)
        for off in (0x9E0, 0x9E8, 0x9F0, 0x9F8):
            emu.read_hooks[PB + off] = (lambda off: lambda e: self.read(e, off))(off)
        # The KS5 polls its buttons ~100,000 times a second: a Python hook per read ran it at 0.3x. With the
        # board's fixed pins (A 0x4F, B 0x4E, tray-in 0x42, all in 0x9F0) and the tray closed, the values are
        # constant, so the C core answers them.
        if FW_ID == 'ks5' and hasattr(emu, 'fast_computed'):
            emu.fast_computed(PB + 0x9E0, 3, keep=0xFFFFFFFF, set_=0x06)
            emu.fast_computed(PB + 0x9F0, 3, keep=~0xC004 & 0xFFFFFFFF, set_=0x4000)
    def set(self, state):
        self.state = state; self.sw = dict(self.CLOSED)
    def _pin(self, e, name):
        off, dflt = self.PINS[name]
        g = G(off)
        if g is None or not e.r[28]: return dflt
        v = e.low[(FW_GP + g) & 0x1FFFFFFF]
        return v if v else dflt
    # buttons wired to inputs, active low (0 = pressed): released by default. KS5: PB+0x9E0 bit2 = OPEN/CLOSE
    # (read at 0x8801CC6C; reading 0 made the player eject the disc right after loading it), bit1 = power.
    BUTTONS = {'ks5': {0x9E0: 0x06}}
    def read(self, e, off):
        v = e.mmio_regs.get(PB + off, 0) | self.BUTTONS.get(FW_ID, {}).get(off, 0)
        for name in ('a', 'b', 'trayin'):
            p = self._pin(e, name)
            if 0x9E0 + 8 * (p // 32) == off:
                v = (v & ~(1 << (p % 32))) | (self.sw[name] << (p % 32))
        return v
    def r9e0(self, e): return self.read(e, 0x9E0)
    def r9f8(self, e): return self.read(e, 0x9F8)


class VirtualDrive:
    """High-level emulation of the drive at the ATAPI packet level (interface reverse-engineered in Phase 11):
      packet  : 12 bytes at SDRAM 0x8000B508, submitted via 0x8079A60C (drive module, RAM)
      status  : gp+3294 bit0 = CHECK CONDITION, bit7 = busy
      data in : 0x0802B8D8(buf, len) -> copies response bytes, returns status bit0
    disc=None answers every media command with sense 02/3A/00 (medium not present).
    A disc object (see isoread.Disc) can be attached later to serve real sectors."""
    PKT = 0x8000B508
    SUBMIT = 0x8079A60C
    BUSY_UNTIL_DONE = not __import__('os').environ.get('KE_NOBUSY')
    DATAIN = (0x8802B8D8, 0x0802B8D8)
    def __init__(self, emu, disc=None):
        self.emu = emu; self.disc = disc; self.log = []; self.pending = b''; self.sense = bytes(18)
        if T(self.SUBMIT) is None or D(self.PKT) is None: return     # this build's drive code is not mapped yet
        self.PKT = D(self.PKT)
        emu.hle[T(self.SUBMIT)] = self.submit; emu.bp.add(T(self.SUBMIT))
        for pc in TS(self.DATAIN): emu.hle[pc] = self.datain; emu.bp.add(pc)
    def _gp(self, off): return (FW_GP + G(off)) & 0x1FFFFFFF
    def _set_status(self, check):
        a = self._gp(3294); v = self.emu.low[a]
        self.emu.low[a] = (v & 0x7E) | (1 if check else 0)
    @staticmethod
    def make_sense(key, asc, ascq=0):
        s = bytearray(18); s[0] = 0x70; s[2] = key; s[7] = 10; s[12] = asc; s[13] = ascq
        return bytes(s)
    def _ret(self, e, v0=0):
        e.r[2] = v0; e.pc = e.r[31]; e.npc = (e.pc + 4) & 0xFFFFFFFF
    def submit(self, e):
        pkt = bytes(e.rd8(self.PKT + i) for i in range(12))
        check, data = self.handle(pkt)
        self.pending = data or b''
        self._set_status(check)
        st = getattr(self, 'stream', None)
        if not check and pkt[0] == 0xBE and st is not None and getattr(self, 'dvd', False) and self.BUSY_UNTIL_DONE:
            # a stream read keeps the drive busy (status bit7) until all its sectors are in the A/V buffer, as on
            # the real drive. Reported idle at once, the firmware's 1 kHz stream service issued the next read
            # after ~1 of its 4 sectors had arrived: 3 of every 4 sectors were skipped (the background video ran
            # 4x fast) and after ~20 s the firmware fell into a scan of thousands of reads per second.
            a = self._gp(3294); self.emu.low[a] |= 0x80; st.st['busy_addr'] = a
        self.log.append((e.n, pkt.hex(' '), 'CHECK' if check else 'ok', self.sense[2], self.sense[12]))
        self.log_count = getattr(self, 'log_count', 0) + 1                    # total ever logged (the list is trimmed)
        if len(self.log) > 6000: del self.log[:1000]
        self._ret(e)
    def datain(self, e):
        buf, n = e.r[4], e.r[5] & 0xFFFFFF          # (was masked to 16 bits: 384 KB reads arrived empty)
        data = bytes(self.pending[:n]) + bytes(max(0, n - len(self.pending)))
        p = buf & 0x1FFFFFFF
        if p + n <= e.low_end: e.low[p:p + n] = data                 # fast path: SDRAM
        else:
            for i in range(n): e.wr8(buf + i, data[i])
        self.datain_lens = (getattr(self, 'datain_lens', []) + [(self.log[-1][1][:2] if self.log else '', n, len(self.pending))])[-200:]
        e.low[self._gp(3224)] = 0                    # (original stores the buffer pointer here; not needed by callers)
        self._set_status(False)
        self._ret(e, 0)
    def handle(self, pkt):
        op = pkt[0]
        no_medium = self.disc is None
        if op == 0x03:                               # REQUEST SENSE
            return False, self.sense
        if op == 0xBD:                               # MECHANISM STATUS: no fault, idle, door closed, no changer
            self.sense = bytes(18); return False, bytes(8)
        if op == 0x12:                               # INQUIRY: CD/DVD device
            self.sense = bytes(18)
            inq = bytearray(36); inq[0] = 0x05; inq[1] = 0x80; inq[3] = 0x21; inq[4] = 31
            inq[8:16] = b'KARAOKE '; inq[16:32] = b'VIRTUAL DVD-ROM '; inq[32:36] = b'0001'
            return False, bytes(inq)
        if op == 0x1B:                               # START STOP UNIT
            self.sense = bytes(18); return False, b''
        if no_medium:
            self.sense = self.make_sense(0x02, 0x3A, 0x00)     # NOT READY, MEDIUM NOT PRESENT
            return True, b''
        return self.disc_cmd(pkt)

    # ---------------------------------------------------------------- disc present (MMC subset)
    def attach_disc(self, path):
        import isoread
        self.src = isoread.SampleSource(path) if open(path, 'rb').read(8) == b'KEMUSPL1' else isoread.SectorSource(path)
        self.disc = path
        self.nsec = self.src.sectors
        self.unknown = {}
        # more data than any CD can hold (80 min = 360000 sectors) -> DVD media
        self.dvd = self.nsec > 360000
        if FW_ID == 'ks5':
            # servo status 0x4079 bit5 = focus found (a disc is there). The KS5's drive module waits for it after
            # spin-up (the MP100NSP build goes on without it).
            self.emu.servo.dyn[0x4079] = lambda e: (self.emu.servo.regs.get(0x4079, 0) | 0x20) & 0xFF
            # 0x407C: signal level read once focus is found; 0 = nothing reflected (INFERRED: any non-zero passes)
            self.emu.servo.dyn[0x407C] = lambda e: 0x40
            # 0x4774: RF amplitude; the gain search accepts 32..208 (INFERRED: a mid value)
            self.emu.servo.dyn[0x4774] = lambda e: 0x80
            import fwprofile
            x = fwprofile.extra(FW_ID)
            if 'detect_step' in x:
                flag = x['detect_flag_gp']
                def found(e):
                    e.low[(FW_GP + flag) & 0x1FFFFFFF] |= 1
                self.emu.hle[x['detect_step']] = found; self.emu.bp.add(x['detect_step'])
    @staticmethod
    def msf(lba):
        l = lba + 150
        if l >= 256 * 4500: return 255, 59, 74          # DVD-sized disc: MSF does not fit, drives clamp it
        return l // 4500, (l // 75) % 60, l % 75
    def cd_sector(self, lba, flags):
        """Build the parts of a 2352-byte Mode-1 sector that READ CD's flag byte asks for (EDC/ECC left zero)."""
        if self.src.ss == 2352 and self.src.off in (16, 24):
            raw = self.src._read_at(lba * 2352, 2352)
        else:
            l = lba + 150; m, s, f = (l // 4500) % 100, (l // 75) % 60, l % 75      # header MSF (minutes wrap on DVD-sized LBAs)
            bcd = lambda x: (x // 10) << 4 | x % 10
            raw = bytes([0] + [0xFF] * 10 + [0]) + bytes([bcd(m), bcd(s), bcd(f), 1]) + self.src.read(lba, 1) + bytes(288)
        part = b''
        if flags & 0x80: part += raw[0:12]                       # sync
        if (flags >> 5) & 3: part += raw[12:16]                  # header (and sub-header for XA)
        if flags & 0x10: part += raw[16:2064] if raw[15] == 1 else raw[24:2072]
        if flags & 0x08: part += raw[2064:2352] if raw[15] == 1 else raw[2072:2352]
        return part
    def _ok(self, data=b''):
        self.sense = bytes(18); return False, data
    def _bad(self, key=0x05, asc=0x20, ascq=0):
        self.sense = self.make_sense(key, asc, ascq); return True, b''
    def disc_cmd(self, pkt):
        import struct
        op = pkt[0]
        be32 = lambda b, o: struct.unpack_from('>I', b, o)[0]
        be16 = lambda b, o: struct.unpack_from('>H', b, o)[0]
        last = self.nsec - 1
        if op == 0x00: return self._ok()                                          # TEST UNIT READY
        if op == 0x25: return self._ok(struct.pack('>II', last, 2048))            # READ CAPACITY
        if op in (0x28, 0xA8):                                                     # READ(10) / READ(12)
            lba = be32(pkt, 2); n = be16(pkt, 7) if op == 0x28 else be32(pkt, 6)
            if lba + n > self.nsec: return self._bad(0x05, 0x21, 0x00)            # LBA out of range
            self.reads = getattr(self, 'reads', 0) + n
            return self._ok(self.src.read(lba, n))
        if op == 0x43:                                                             # READ TOC formats 0/1/2, single data track
            msf = bool(pkt[1] & 2); fmt = pkt[2] & 0x0F
            if fmt == 0 and (pkt[9] >> 6): fmt = pkt[9] >> 6                       # old SFF-8020 format field
            alloc = be16(pkt, 7)
            def addr(l):
                if not msf: return struct.pack('>I', l)
                m, s, f = self.msf(l); return bytes([0, m, s, f])
            if fmt == 0:
                # This firmware's TOC parser (0x0802C4C4) reads 4-byte entries [point, M, S, F] (absolute MSF,
                # point 0xA2 = lead-out), not the 8-byte MMC descriptors.  Answer in the format it parses.
                def ent(point, l):
                    m, s, f = self.msf(l); return bytes([point, m, s, f])
                body = ent(1, 0) + ent(0xA2, self.nsec)
                out = struct.pack('>H', len(body) + 2) + bytes([1, 1]) + body
            elif fmt == 1:
                body = bytes([0, 0x14, 1, 0]) + addr(0)
                out = struct.pack('>H', len(body) + 2) + bytes([1, 1]) + body
            elif fmt == 2:                                                         # full TOC (raw Q-subchannel points)
                def d(point, pm, ps, pf): return bytes([1, 0x14, 0, point, 0, 0, 0, 0, pm, ps, pf])
                lm, ls, lf = self.msf(self.nsec); tm, ts, tf = self.msf(0)
                body = d(0xA0, 1, 0x00, 0) + d(0xA1, 1, 0, 0) + d(0xA2, lm, ls, lf) + d(1, tm, ts, tf)
                out = struct.pack('>H', len(body) + 2) + bytes([1, 1]) + body
            else:
                return self._bad(0x05, 0x24, 0x00)
            return self._ok(out[:alloc] if alloc else out)
        if op == 0xBE and self.dvd:
            # This firmware streams video/BGV with READ CD (flags F8) even on DVD media; its own drive module
            # answers with the 2048-byte user data of each sector (verified by the transfer length it asks for).
            lba = be32(pkt, 2); n = (pkt[6] << 16) | (pkt[7] << 8) | pkt[8]
            if lba + n > self.nsec: return self._bad(0x05, 0x21, 0x00)
            self.reads = getattr(self, 'reads', 0) + n
            st = getattr(self, 'stream', None)
            if st is not None:                       # streaming: the data goes to the A/V bitstream buffer (StreamDMA)
                # a READ of 0 sectors transfers nothing and does not cancel the read in progress (SCSI). The
                # music-video player sends them while it waits for the rest of a read (e.g. the AVI index):
                # taken as a new read, each one threw the undelivered data away and the video never started.
                if n: st.start(lba, n)
                return self._ok(b'')
            return self._ok(self.src.read(lba, n))
        if op == 0xBE:                                                             # READ CD
            lba = be32(pkt, 2); n = (pkt[6] << 16) | (pkt[7] << 8) | pkt[8]; flags = pkt[9]
            if lba + n > self.nsec: return self._bad(0x05, 0x21, 0x00)
            out = bytearray()
            for k in range(n):
                out += self.cd_sector(lba + k, flags)
            self.reads = getattr(self, 'reads', 0) + n
            return self._ok(bytes(out))
        if op == 0xAD and not self.dvd:                                            # READ DVD STRUCTURE on a CD
            return self._bad(0x05, 0x30, 0x02)                                     # cannot read medium: incompatible format
        if op == 0xAD:                                                             # READ DVD STRUCTURE
            fmt = pkt[7]
            if fmt == 0x00:                                                        # physical format: DVD-ROM, single layer
                pf = bytearray(17); pf[0] = 0x01; pf[1] = 0x02; pf[2] = 0x01
                pf[4:8] = struct.pack('>I', 0x030000); pf[8:12] = struct.pack('>I', 0x030000 + last)
                return self._ok(struct.pack('>H', len(pf) + 2) + b'\0\0' + bytes(pf))
            if fmt == 0x01:                                                        # copyright: no protection, all regions
                return self._ok(struct.pack('>H', 6) + b'\0\0' + bytes([0, 0, 0, 0]))
            return self._bad(0x05, 0x24, 0x00)
        if op == 0x46:                                                             # GET CONFIGURATION: current profile
            return self._ok(struct.pack('>IHH', 4, 0, 0x0010 if self.dvd else 0x0008))   # DVD-ROM / CD-ROM
        if op == 0x51:                                                             # READ DISC INFORMATION
            di = bytearray(34); di[1] = 32; di[2] = 0x0E; di[3] = 1; di[4] = 1; di[5] = 1; di[6] = 1
            return self._ok(bytes(di))
        if op == 0x5A:                                                             # MODE SENSE(10): empty page list
            return self._ok(struct.pack('>H', 6) + bytes(6))
        if op == 0x4A:                                                             # GET EVENT STATUS: media present
            return self._ok(bytes([0, 6, 4, 0x10, 0, 2, 0, 0]))
        self.unknown[op] = self.unknown.get(op, 0) + 1
        return self._bad(0x05, 0x20, 0x00)                                         # ILLEGAL REQUEST, INVALID COMMAND


class USBPort:
    """USB host port status at phys 0x1C0202A0 (read by 0x08069F20): bit4/bit5 are taken as 'device connected / port
    enabled'. With no model the register echoed the firmware's own write (0x30), which looked like a plugged-in device
    and made the firmware reset the port and wait 200 ms forever. Modelled as 'nothing plugged in'."""
    STATUS = 0x1C0202A0
    def __init__(self, emu, connected=False):
        self.connected = connected
        emu.read_hooks[self.STATUS] = lambda e: 0x30 if self.connected else 0


class I2CBus:
    """The firmware bit-bangs I2C on GPIO (SCL = PB+0x9C0 bit12, SDA = bit13, SDA dir PB+0x9A0 bit13, SDA in
    PB+0x9E0 bit13).  With nothing modelled every bit read 0 (all ACK, all data 0) and each 2-byte EEPROM read cost
    ~175k instructions.  HLE of the two transfer functions:
        0x080A02D8 write(dev, reg, buf, len)   0x080A03C8 read(dev, reg, buf, len)   (return 0 = success)
    Devices seen: 0xA8..0xAE = 24Cxx-style EEPROM pages (settings), read as blank (0x00, the old behaviour);
    0x66 = karaoke key chip (challenge/response, see KeyChip)."""
    WRITE = (0x080A02D8, 0x880A02D8); READ = (0x080A03C8, 0x880A03C8)
    def __init__(self, emu, fill=0x00):
        self.emu = emu; self.mem = {}; self.fill = fill; self.log = []; self.devices = {}
        self.on_write = None                  # fn(): an EEPROM byte changed (the app saves the settings)
        for pc in TS(self.WRITE): emu.hle[pc] = self._w; emu.bp.add(pc)
        for pc in TS(self.READ): emu.hle[pc] = self._r; emu.bp.add(pc)
    def _ret(self, e, v):
        e.r[2] = v; e.pc = e.r[31]; e.npc = (e.pc + 4) & 0xFFFFFFFF
    def _w(self, e):
        dev, reg, buf, n = e.r[4] & 0xFF, e.r[5] & 0xFF, e.r[6], e.r[7]
        data = bytes(e.rd8(buf + i) for i in range(n))
        if len(self.log) < 5000: self.log.append((e.n, 'W', dev, reg, data.hex()))
        d = self.devices.get(dev)
        if d: d.write(reg, data)
        else:
            changed = False
            for i, b in enumerate(data):
                k = (dev, (reg + i) & 0xFF)
                if self.mem.get(k, self.fill) != b: changed = True
                self.mem[k] = b
            if changed and self.on_write is not None:
                try: self.on_write()
                except Exception: pass
        self._ret(e, 0)
    def dump(self):
        """The EEPROM contents: {device address: 256 bytes} (the key chip is not memory)."""
        devs = sorted({d for d, _ in self.mem if d not in self.devices})
        return {d: bytes(self.mem.get((d, r), self.fill) for r in range(256)) for d in devs}
    def load(self, pages):
        for d, b in pages.items():
            for r in range(min(256, len(b))): self.mem[(int(d), r)] = b[r]
    def _r(self, e):
        dev, reg, buf, n = e.r[4] & 0xFF, e.r[5] & 0xFF, e.r[6], e.r[7]
        d = self.devices.get(dev)
        data = d.read(reg, n) if d else bytes(self.mem.get((dev, (reg + i) & 0xFF), self.fill) for i in range(n))
        for i, b in enumerate(data): e.wr8(buf + i, b)
        if len(self.log) < 5000: self.log.append((e.n, 'R', dev, reg, data.hex()))
        self._ret(e, 0)


class KeyChip:
    """Karaoke key chip on I2C address 0x66.  The firmware writes a random 8-byte challenge, reads back 10 bytes
    (8-byte answer + 2 chip nonce bytes) and compares the answer with its own computation
    F(0x0F, 0xE002, nonce, challenge) done by 0x080BCA6C (variant at 0x080BC7C0 expects challenge^1).  The chip's
    secret is not in the firmware image, so the chip is modelled by answering with exactly what the firmware
    computes: after the firmware's own computation (return points below) the expected bytes are copied into the
    receive buffer, which is what a genuine chip would have put there."""
    RX = 0x80012C48; GP_EXPECT = 7260
    AFTER_F = (0x880BCA2C, 0x080BCA2C, 0x880BC8B0, 0x080BC8B0)
    def __init__(self, emu, bus):
        self.emu = emu; self.challenges = 0; self.auth = 0
        bus.devices[0x66] = self
        for pc in TS(self.AFTER_F): emu.hle[pc] = self._answer; emu.bp.add(pc)
    def write(self, reg, data):
        if len(data) == 8: self.challenges += 1
    def read(self, reg, n):
        return bytes(n)                       # nonce bytes 8,9 = 0; answer bytes filled in by _answer
    def _answer(self, e):
        gp = fw_gp(e)
        for i in range(8): e.wr8(self.RX + i, e.rd8(gp + self.GP_EXPECT + i))
        self.auth += 1


# ---------------------------------------------------------------------------------------------------------------
# OSD (on-screen display) layer
# ---------------------------------------------------------------------------------------------------------------
# The firmware's OSD library (0x0802CF20..0x08031F00) keeps a region table at 0x80006C20 (20-byte entries:
# +0 header offset, +2 alternate header offset, +4 pitch in bytes, +6 width, +8 height, +18 in-use, +19 format) and
# builds a 32-byte *hardware* header per region in SDRAM at  base + offset*32,  base = PB+0x524 << 10:
#   +0  byte: 0x80 | format (0 = 2 bpp / 4 colours, 1 = 4 bpp / 16, 3 = 1 bpp / 2, 2 = 8 bpp)
#   +4  height (lines)        +6  pitch (32-bit words)      +8  y (frame lines)     +10 x (pixels)
#   +12 bitmap offset (32-byte units from base)             +14 next region header (0xFFFF = end of list)
#   +16 palette: 32-bit entries, bytes  G, B, R, alpha (alpha 0 = transparent, 0xFF = opaque).  Checked against
#       photos of a real player: number entry c800c8 -> yellow digit, ffffff -> white digits, 202020 -> dark edge,
#       screensaver logo 88a5b6 -> pink "DVD VIDEO".
# The display list starts at PB+0x514 (header offset of the first region, sorted top to bottom).
# All of this is reconstructed from the firmware's code (0x0802DAB0 builds headers, 0x0802D208 sorts and links
# them, 0x0802D3F0 sets palette entries, 0x0802E830 draws text into bitmaps) and checked by rendering: in the
# idle karaoke state region 0 holds the "DVD VIDEO" logo.

OSD_W, OSD_H = 720, 480
OSD_BPP = {0: 2, 1: 4, 3: 1, 2: 8}


def _ycc_rgb(y, cb, cr):
    c = y - 16; d = cb - 128; e_ = cr - 128
    r = (298 * c + 409 * e_ + 128) >> 8
    g = (298 * c - 100 * d - 208 * e_ + 128) >> 8
    b = (298 * c + 516 * d + 128) >> 8
    cl = lambda v: 0 if v < 0 else 255 if v > 255 else v
    return cl(r), cl(g), cl(b)


def osd_regions(emu):
    """Walk the OSD display list. Returns a list of dicts describing each region (no pixels)."""
    base = (emu.mmio_regs.get(PB + 0x524, 0) & 0xFFFFF) << 10
    mem = emu.low
    if not base: return []
    be16 = lambda a: (mem[a] << 8) | mem[a + 1]
    out = []; seen = set()
    off = emu.mmio_regs.get(PB + 0x514, 0) & 0xFFFF
    while off != 0xFFFF and off not in seen and len(out) < 32:
        seen.add(off)
        h = base + off * 32
        if h + 32 > len(mem): break
        b0 = mem[h]; fmt = b0 & 7
        # byte 0 bit7 = in use, bit6 = hidden (the OSD library's show/hide only toggles this bit, e.g. when a timed
        # message such as the KS5's 'KARAOKE' runs out)
        reg = dict(header=0x80000000 + h, enabled=bool(b0 & 0x80) and not (b0 & 0x40), format=fmt, bpp=OSD_BPP.get(fmt, 0),
                   height=be16(h + 4), pitch=be16(h + 6) * 4, y=be16(h + 8), x=be16(h + 10),
                   bitmap=0x80000000 + base + be16(h + 12) * 32, next=be16(h + 14))
        n = {2: 4, 4: 16, 1: 2, 8: 256}.get(reg['bpp'], 0)      # 8 bpp: 256 entries, bitmap after them (KS5 menus)
        reg['palette'] = [tuple(mem[h + 16 + 4 * i: h + 20 + 4 * i]) for i in range(n)]
        reg['width'] = reg['pitch'] * 8 // reg['bpp'] if reg['bpp'] else 0
        out.append(reg)
        off = reg['next']
    return out


_EXPAND = {}
def osd_render(emu, w=OSD_W, h=OSD_H):
    """Compose the OSD regions into a w x h RGBA image (bytes, row-major). Returns (rgba, regions).
    Integer numpy: palette lookups through a byte->pixels table, plain copies where the palette is only opaque or
    clear (the usual case), integer blending otherwise (~4x faster than the earlier float version)."""
    try:
        import numpy as np
    except ImportError:
        return _osd_render_py(emu, w, h)
    img = np.zeros((h, w, 4), np.uint8)
    regs = osd_regions(emu); mem = emu.low
    for r in regs:
        bpp = r['bpp']
        if not r['enabled'] or bpp not in (1, 2, 4, 8) or r['height'] == 0: continue
        if r['pitch'] > 2048 or r['height'] > 1024 or r['x'] >= w or r['y'] >= h: continue
        pal = [(c[2], c[0], c[1], c[3]) for c in r['palette']]
        while len(pal) < (1 << bpp): pal.append((0, 0, 0, 0))
        pal = np.array(pal[:1 << bpp], np.uint8)
        src = r['bitmap'] & 0x1FFFFFFF; pitch = r['pitch']
        rows = min(r['height'], h - r['y']); width = min(r['width'], w - r['x'])
        raw = np.frombuffer(bytes(mem[src:src + pitch * rows]), np.uint8)
        if len(raw) < pitch * rows: continue
        per = 8 // bpp
        ex = _EXPAND.get(bpp)
        if ex is None:
            mask = (1 << bpp) - 1
            ex = _EXPAND[bpp] = np.array([[(b >> ((per - 1 - k) * bpp)) & mask for k in range(per)] for b in range(256)], np.uint8)
        idx = ex[raw.reshape(rows, pitch)].reshape(rows, pitch * per)[:, :width]
        col = pal[idx]                                     # rows x width x 4
        dst = img[r['y']:r['y'] + rows, r['x']:r['x'] + width]
        al = pal[:, 3]
        if np.all((al == 0) | (al == 255)):
            m = col[..., 3] == 255
            dst[m] = col[m]
        else:
            a = col[..., 3:4].astype(np.uint16)
            dst[..., :3] = ((col[..., :3] * a + dst[..., :3] * (255 - a)) // 255).astype(np.uint8)
            np.maximum(dst[..., 3], col[..., 3], out=dst[..., 3])
    return img.tobytes(), regs


def _osd_render_py(emu, w=OSD_W, h=OSD_H):
    img = bytearray(w * h * 4)
    regs = osd_regions(emu); mem = emu.low
    for r in regs:
        bpp = r['bpp']
        if not r['enabled'] or bpp not in (1, 2, 4, 8) or r['height'] == 0: continue
        if r['pitch'] > 2048 or r['height'] > 1024 or r['x'] >= w or r['y'] >= h: continue   # header mid-update
        pal = [(c[2], c[0], c[1], c[3]) for c in r['palette']]   # entries are stored G, B, R, alpha
        while len(pal) < (1 << bpp): pal.append((0, 0, 0, 0))
        per = 8 // bpp; mask = (1 << bpp) - 1
        lut = [[pal[(b >> ((per - 1 - k) * bpp)) & mask] for k in range(per)] for b in range(256)]
        src = r['bitmap'] & 0x1FFFFFFF; pitch = r['pitch']
        width = min(r['width'], (w - r['x']))
        for yy in range(r['height']):
            sy = r['y'] + yy
            if sy >= h: break
            row = mem[src + yy * pitch: src + (yy + 1) * pitch]
            p = (sy * w + r['x']) * 4; xx = 0
            for b in row:
                for c in lut[b]:
                    if xx >= width: break
                    a = c[3]
                    if a == 255: img[p:p + 4] = bytes(c)
                    elif a:
                        ia = 255 - a
                        img[p] = (c[0] * a + img[p] * ia) // 255; img[p + 1] = (c[1] * a + img[p + 1] * ia) // 255
                        img[p + 2] = (c[2] * a + img[p + 2] * ia) // 255; img[p + 3] = max(img[p + 3], a)
                    p += 4; xx += 1
    return bytes(img), regs


def png_rgba(rgba, w, h):
    import struct
    """Minimal PNG encoder (no PIL needed, so it works inside the packaged exe)."""
    import zlib
    raw = b''.join(b'\x00' + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    def chunk(t, d):
        c = struct.pack('>I', len(d)) + t + d
        return c + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 1)) + chunk(b'IEND', b''))


def video_plane_on(emu):
    """PB+0x388 (and 0xBF0) hold the video plane width (720) while video is shown; the firmware sets them to 0
    when it blanks the video, e.g. for the DVD-logo screensaver (which then sits on black)."""
    return emu.mmio_regs.get(PB + 0x388, 0) != 0


def osd_signature(emu):
    """Cheap change detector for the OSD: hash of the display list headers and their bitmaps."""
    import hashlib
    h = hashlib.md5(); mem = emu.low
    for r in osd_regions(emu):
        a = r['header'] & 0x1FFFFFFF; h.update(mem[a:a + 32])
        if r['pitch'] <= 2048 and r['height'] <= 1024:
            b = r['bitmap'] & 0x1FFFFFFF; h.update(mem[b:b + r['pitch'] * r['height']])
    return h.hexdigest()


def install_delay_hle(emu):
    """mdelay(ms) at 0x0807C9F8 busy-waits 26 998 x 3 instructions per millisecond (~56 % of the boot). Each pass of
    the outer loop is replaced by advancing the instruction clock by the same amount, so interrupts and timers
    still see the same elapsed time (they are delivered between the 1 ms steps exactly as before)."""
    def hook(em):
        if em.r[4] == 0: return
        em.r[4] = (em.r[4] - 1) & 0xFFFFFFFF; em.r[2] = 0
        em.n += 26998 * 3
        em.pc = (em.pc & 0xF0000000) | T(0x0807CA18); em.npc = em.pc + 4
    for pc in TS((0x8807CA00, 0x0807CA00)):
        emu.hle[pc] = hook; emu.bp.add(pc)


class IRRemote:
    """Infra-red remote receiver (NEC-style decoder in the chip).
    Found in 0x080165A0, which the firmware polls from its main loop:
      PB+0x734: bit15 = a new code is waiting, bit14 = repeat (button held), bits 7..0 = command byte.
                The firmware clears bit15 by writing the register back.
      PB+0x738: the remote's custom (address) code; this firmware only accepts 0x205F.
    The command byte is bit-reversed (0x0807B870) and looked up in the table at 0x880DD444, which gives the
    firmware's internal key number; that goes to the key dispatcher 0x080176AC (karaoke keys -> 0x080A05A4).
    Key numbers follow the order of the firmware's ircmd_* name list: 0-9 digits, 22 play, 25 stop, ..."""
    REG, CUSTOM = PB + 0x734, PB + 0x738
    CUSTOM_CODE = 0x205F
    TABLE = 0x880DD444
    GAP = 5_400_000                      # ~50 ms between presses (the decoder needs the firmware to take each one)
    # While a karaoke song plays, the firmware's main loop (which polls this register) is not reached: the song task
    # 0x080979F4 keeps running inside the main loop's task call. A key the register path does not take within
    # STUCK instructions is handed straight to the firmware's key dispatcher 0x080176AC instead, injected at the
    # entry of that song task (a call from there returns through a trampoline and the task then runs as usual).
    STUCK = 27_000_000                   # 0.25 s
    SONG_TASK = (0x880979F4, 0x080979F4)
    DISPATCH = 0x880176AC
    TRAMP = 0x881FFF00                   # unused RAM above the firmware image: return point of an injected call
    def __init__(self, emu):
        self.emu = emu; self.queue = []; self.sent = []; self.next_at = 0
        self.raw_for_key = None
        self.inject_q = []; self.direct_until = 0; self._saved_ra = None; self.injected = 0
        # the song task is entered ~270,000 times a second: its hook is armed only while keys wait for it
        self.SONG_TASK = TS(self.SONG_TASK)
        for pc in self.SONG_TASK: emu.hle[pc] = self._task_entry
        emu.hle[self.TRAMP] = self._tramp; emu.bp.add(self.TRAMP)
        self.armed = False; self.armed_at = 0
        import fwprofile
        x = fwprofile.extra(FW_ID); self.direct = 'key_poll' in x; self._key_side = None
        if self.direct:
            self._dispatch = x['key_dispatch']; self._key_side = x.get('key_side')
            emu.hle[x['key_poll']] = self._poll_entry; emu.bp.add(x['key_poll'])
    def _poll_entry(self, e):
        """(builds with key_poll) the firmware's remote poll: hand it the next key as if decoded."""
        if not self.queue or e.n < self.next_at: return
        key = self.queue.pop(0)
        if FW_ID == 'ks5':
            # KS5: during a song a typed number is reserved with the remote's PROG key (52); Enter is ignored
            # there. Enter (and the app's reserve key, 109) send PROG while a song plays, as on the Megapro.
            playing = e.rd8(FW_GP + 4120) == 2
            if key == 109 or (key == 72 and playing): key = 52
        # what the poll itself records for a decoded key before calling the dispatcher (0x88010DB8): the key count
        # and the time of the last key (the PB+0x90/0x94 clock: milliseconds on the KS5)
        x = self._key_side
        if x:
            gp = FW_GP & 0x1FFFFFFF
            a = gp + x[0]; e.low[a:a + 4] = ((int.from_bytes(e.low[a:a + 4], 'little') + 1) & 0xFFFFFFFF).to_bytes(4, 'little')
            a = gp + x[1]; e.low[a:a + 4] = ((e.n // getattr(e.timers, 'IPMS', 108)) & 0xFFFFFFFF).to_bytes(4, 'little')
        e.r[4] = key; e.pc = self._dispatch; e.npc = e.pc + 4          # returns to the poll's caller
        self.sent.append((e.n, key, 'direct')); self.next_at = e.n + self.GAP
    def _arm(self, e, on):
        if on == self.armed: return
        for pc in self.SONG_TASK:
            if on: e.bp.add(pc)
            else: e.bp.discard(pc)
        self.armed = on
        if on: self.armed_at = e.n
    def _task_entry(self, e):
        self.task_seen = e.n
        if not self.inject_q or self._saved_ra is not None or e.n < self.next_at: return
        key = self.inject_q.pop(0)
        self._saved_ra = (e.r[31], e.pc)
        e.r[4] = key; e.r[31] = self.TRAMP; e.pc = T(self.DISPATCH); e.npc = e.pc + 4
        self.sent.append((e.n, key, 'direct')); self.injected += 1
        self.next_at = e.n + self.GAP; self.direct_until = e.n + 2 * 108_000_000
    def _tramp(self, e):
        ra, pc = self._saved_ra or (e.r[31], self.SONG_TASK[0]); self._saved_ra = None
        e.r[31] = ra; e.pc = pc; e.npc = pc + 4
    def _build_map(self):
        rev = lambda b: int('{:08b}'.format(b)[::-1], 2)
        m = {}
        import fwprofile
        x = fwprofile.extra(FW_ID); nokey = x.get('ir_nokey', 100); n = x.get('ir_len', 108)
        tab = T(self.TABLE)
        for idx in range(n if tab else 0):           # the table's real length (entries beyond are other data)
            k = self.emu.rd8(tab + idx)
            if k != nokey and k not in m: m[k] = rev(idx)   # 100 (KS5: 101) = "no key"
        self.raw_for_key = m
    def press(self, key):
        """Queue one key (firmware key number)."""
        self.queue.append(int(key))
    def keys(self):
        if self.direct: return list(range(112))
        if self.raw_for_key is None: self._build_map()
        return sorted(self.raw_for_key)
    def poll(self, e):
        """Call between run() slices: hands the next queued key to the decoder register once the last one was taken."""
        if self.direct: return
        if self.inject_q and e.n - max(getattr(self, 'task_seen', 0), self.armed_at) > self.STUCK:
            self.queue = self.inject_q + self.queue; self.inject_q = []; self.direct_until = 0   # song over
        self._arm(e, bool(self.inject_q) or e.n < self.direct_until)
        reg = e.mmio_regs.get(self.REG, 0)
        if reg & 0x8000 and self.sent and isinstance(self.sent[-1][2], int) and e.n - self.sent[-1][0] > self.STUCK:
            # not taken (a song is playing): withdraw it and give it to the dispatcher directly
            e.mmio_regs[self.REG] = reg & 0x7FFF
            n, key, raw = self.sent.pop(); self.inject_q.append(key); self.direct_until = e.n + 2 * 108_000_000
            reg &= 0x7FFF
        if not self.queue or e.n < self.next_at: return
        if e.n < self.direct_until and e.n - max(getattr(self, 'task_seen', 0), self.armed_at) < self.STUCK:   # in a song: straight in
            self.inject_q += self.queue; self.queue = []; return
        if reg & 0x8000: return           # previous code not taken yet
        if self.raw_for_key is None: self._build_map()
        key = self.queue.pop(0); raw = self.raw_for_key.get(key)
        if raw is None:
            # no remote button produces this key (front-panel only): use the firmware's own software key slot
            # gp+2968, which the same poller 0x080165A0 dispatches before reading the IR register (0 = empty slot)
            gp = fw_gp(e)
            if G(2968) is None:
                return                                     # (not mapped for this build: the key is dropped)
            if key and gp and e.rd32(gp + 2968) == 0:
                e.wr32(gp + 2968, key); self.sent.append((e.n, key, 'panel')); self.next_at = e.n + self.GAP
            else:
                self.queue.insert(0, key)
            return
        e.mmio_regs[self.CUSTOM] = self.CUSTOM_CODE
        e.mmio_regs[self.REG] = 0x8000 | raw
        self.sent.append((e.n, key, raw)); self.next_at = e.n + self.GAP


# Names for the firmware's key numbers (order of its ircmd_* table; numbers above 85 are not named in the image)
KEY_NAMES = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '+10', 'eject', 'mute', 'key up', 'key reset',
             'key down', 'vol up', 'vol down', 'slow', 'step', 'pause', 'play', 'play/resume', 'return', 'stop',
             'null', 'rewind', 'forward', 'prev', 'next', 'display', 'repeat A-B', 'repeat all', 'play mode',
             'goto', 'shuffle', 'resume', 'remain time', 'power', 'zoom', 'up', 'down', 'left', 'right', 'source',
             'channel', 'channel down', 'preview', '9 frames', 'program', 'display format', 'intro', 'disc', 'usb',
             'disc 1', 'disc 2', 'disc 3', 'play/pause', 'subtitle', 'angle', 'audio', 'select/enter', 'title menu',
             'clear', 'setup', 'karaoke', '3D sound', 'slow back', 'reading', 'root menu', 'play select', 'mix',
             'book', 'score', 'title return', 'echo up', 'echo down', 'mic up', 'mic down', 'P-scan', 'reset ir',
             'ir default', 'HD', 'pause only', 'exit setup']



class KeyVoice:
    """Spoken digits. When a number key is pressed the firmware's digit handler 0x080B26D4 asks the setting check
    0x080B488C (settings byte +132, from the EEPROM; the emulated EEPROM reads blank = off), then looks up the clip
    with 0x0809885C(digit) - ten MP3 files (44.1 kHz mono, 40 kbps, 0.5-0.9 s) inside the firmware image at
    0x88123B78.. - and hands it to the audio DSP's MP3 mixer 0x080AA908(ptr, size, volume), which is not modelled.
    HLE: the check answers 1 while `enabled`, and each play request is recorded as (seq, ptr, size) with the
    clip bytes; the app plays them on the PC."""
    CHECK = (0x880B488C, 0x080B488C)
    PLAY = (0x880AA908, 0x080AA908)
    def __init__(self, emu):
        self.emu = emu; self.enabled = True; self.seq = 0; self.last = None; self.clips = {}
        self.sink = None                       # optional fn(seq, data): called on the emulator thread, keep cheap
        for pc in TS(self.CHECK): emu.hle[pc] = self._check; emu.bp.add(pc)
        for pc in TS(self.PLAY): emu.hle[pc] = self._play; emu.bp.add(pc)
    def _check(self, e):
        e.r[2] = 1 if self.enabled else 0                    # the page's "spoken numbers" switch decides
        e.pc = e.r[31]; e.npc = (e.pc + 4) & 0xFFFFFFFF
    def _play(self, e):
        ptr, size = e.r[4] & 0xFFFFFFFF, e.r[5]
        if not (0 < size < 1 << 20): return
        key = (ptr, size)
        data = self.clips.get(key)
        if data is None:
            try: data = bytes(e.rd8(ptr + i) for i in range(size))
            except Exception: return
            self.clips[key] = data
        self.seq += 1; self.last = (self.seq, ptr, size)
        if self.sink is not None:
            try: self.sink(self.seq, data)
            except Exception: self.sink = None


class RandomScore:
    """Score at the end of a song. The firmware's song-end routine 0x08098CF4 shows the score screen (and plays the
    fanfare for 90/80/70/60) through 0x080AACF8(score) only when its score value (s0 at 0x08098DD0) is 50 or more;
    on the real player that comes from the microphone, here there is none, so it was always 0 and no score was
    shown. HLE: while `enabled`, a random score is put there (like the many players that score at random)."""
    PCS = (0x88098DD0, 0x08098DD0)
    def __init__(self, emu):
        import random
        self.enabled = True; self.rng = random.Random(); self.last = None; self.count = 0
        for pc in TS(self.PCS): emu.hle[pc] = self._hook; emu.bp.add(pc)
    def _hook(self, e):
        if not self.enabled: return
        r = self.rng.random()                     # mostly good scores, now and then a great or a so-so one
        sc = 100 if r < 0.08 else self.rng.randint(90, 99) if r < 0.40 else self.rng.randint(80, 89) if r < 0.80 else self.rng.randint(70, 79)
        e.r[16] = sc; self.last = sc; self.count += 1        # s0; the firmware's own code continues


class Chorus:
    """Recorded voices ("chorus" clips, WMA) that come with some songs. When the song reaches a clip, the firmware
    hands it to the audio DSP's WMA player 0x080AA90C(ptr, size, ...) - the unscrambled ASF file in SDRAM - which is
    not modelled. HLE: while `enabled`, each request is passed to `sink(seq, wma bytes, emulated seconds)`; the
    synth chip reset byte (0xFF, sent when a song ends or is stopped) calls `stop_sink(emulated seconds)`."""
    PCS = (0x880AA90C, 0x080AA90C)
    ASF = bytes.fromhex('3026b2758e66cf11a6d900aa0062ce6c')
    def __init__(self, emu):
        self.enabled = True; self.seq = 0; self.sink = None; self.stop_sink = None; self.playing = False; self.log = []
        for pc in TS(self.PCS): emu.hle[pc] = self._play; emu.bp.add(pc)
    def _play(self, e):
        ptr, size = e.r[4] & 0xFFFFFFFF, e.r[5]
        p = ptr & 0x1FFFFFFF
        if not (0 < size < 16 << 20) or p + size > e.low_end: return
        data = bytes(e.low[p:p + size])
        if data[:16] != self.ASF: return
        self.seq += 1; self.log.append((e.n, ptr, size)); del self.log[:-50]
        if self.enabled and self.sink is not None:
            self.playing = True
            try: self.sink(self.seq, data, e.n / 108e6)
            except Exception: pass
    def stop(self, e):
        if self.playing and self.stop_sink is not None:
            try: self.stop_sink(e.n / 108e6)
            except Exception: pass
        self.playing = False


class SetupRedraw:
    """Setup menu: after the Greetings text editor, the firmware redraws the two menu panels from y = 122 down
    (fills at 0x080B6BDC / 0x080B6C6C via the rectangle fill 0x080B0680) but not the band y = 108..121 between the
    title bar and the panels, where the editor's green text box was - a strip of it (and of the greeting) stayed on
    screen. When the left panel is drawn, that band is filled with the panels' background colour too."""
    FILL = (0x880B0680, 0x080B0680)
    PANEL_RA = 0x880B6BDC
    def __init__(self, emu):
        self.fixes = 0
        for pc in TS(self.FILL): emu.hle[pc] = self._fill; emu.bp.add(pc)
    def _fill(self, e):
        if (e.r[31] | 0x80000000) != T(self.PANEL_RA): return
        x, y, w, h = e.r[4] & 0xFFFF, e.r[5] & 0xFFFF, e.r[6] & 0xFFFF, e.r[7] & 0xFFFF
        if (x, y, w, h) != (54, 122, 306, 310): return
        colour = e.rd32(e.r[29] + 16) & 0x0F
        regs = osd_regions(e)
        if len(regs) != 1 or regs[0]['bpp'] != 4: return
        r = regs[0]; bm = r['bitmap'] & 0x1FFFFFFF; pitch = r['pitch']
        # the whole box width: panel colour inside, the 2-pixel dark frame (index 6, as in the rows below) at the edges
        x0 = 0; x1 = r['width'] & ~1
        fillb = bytes([0x66]) + bytes([colour << 4 | colour]) * ((x1 - x0) // 2 - 2) + bytes([0x66])
        for yy in range(108, 122):
            row = yy - r['y']
            if 0 <= row < r['height']:
                a = bm + row * pitch + x0 // 2
                if a + len(fillb) <= e.low_end: e.low[a:a + len(fillb)] = fillb
        self.fixes += 1

class MidiOut:
    """Serial MIDI to the karaoke synthesizer chip (a Dream SAM2695-class GS synth; the player ships a SAM2695
    SoundFont as the matching sound set). The firmware's sequencer 0x080AC9C4 (10 ms karaoke tick) and senders
    0x080AB1CC / 0x080AB090 end in the byte sender 0x080B008C(byte), which waits for PB+0x904 bit0 and writes
    PB+0x900. HLE: record (instruction count, byte) and return - the bytes are a plain MIDI stream."""
    PCS = (0x880B008C, 0x080B008C)
    KEEP = 400000
    def __init__(self, emu):
        self.bytes = []; self.total = 0
        self.last_note = None; self._st = 0; self._d = []      # instruction count of the latest note-on
        self.sink = None                     # optional fn(byte): live output (midiout.StreamParser(port.send).feed)
        for pc in TS(self.PCS): emu.hle[pc] = self.hook; emu.bp.add(pc)
    def hook(self, e):
        b = e.r[4] & 0xFF
        self.bytes.append((e.n, b)); self.total += 1
        if b == 0xFF:                                  # synth chip reset: the song ended or was stopped
            ch = getattr(e, 'chorus', None)
            if ch is not None: ch.stop(e)
        if b < 0xF8:                                   # cheap running-status parse: remember the latest note-on
            if b & 0x80: self._st = b if b < 0xF0 else 0; self._d = []
            elif self._st:
                self._d.append(b)
                if len(self._d) == (1 if (self._st & 0xE0) == 0xC0 else 2):
                    if (self._st & 0xF0) == 0x90 and self._d[1]: self.last_note = e.n
                    self._d = []
        if self.sink is not None:
            try: self.sink(b, e.n / 108e6)            # with the emulated time, for exact timing in the synth
            except TypeError:
                try: self.sink(b)
                except Exception: self.sink = None
            except Exception: self.sink = None
        if len(self.bytes) > self.KEEP: del self.bytes[:self.KEEP // 2]
        e.pc = e.r[31]; e.npc = e.pc + 4; e.n += 40          # ~ the instructions the real sender runs
    def emit(self, e, msg, n):
        """Bytes of one MIDI message from a high-level player (KS5), stamped with instruction count n."""
        for b in msg:
            self.bytes.append((n, b)); self.total += 1
            if self.sink is not None:
                try: self.sink(b, n / 108e6)
                except TypeError:
                    try: self.sink(b)
                    except Exception: self.sink = None
                except Exception: self.sink = None
        if (msg[0] & 0xF0) == 0x90 and len(msg) > 2 and msg[2]: self.last_note = n
        if len(self.bytes) > self.KEEP: del self.bytes[:self.KEEP // 2]
    def to_midi(self, since=0):
        """The captured stream as a Standard MIDI File (1 tick = 1 ms of emulated time)."""
        import struct
        ev = self.events(since)
        if not ev: return None
        t0 = ev[0][0]; trk = bytearray(b'\x00\xff\x51\x03\x0f\x42\x40')     # tempo 1,000,000 us/beat, 1000 tpb
        last = 0
        for n, msg in ev:
            t = int((n - t0) / 108000); d = max(0, t - last); last = max(last, t)
            v = bytearray([d & 0x7F]); d >>= 7
            while d: v.insert(0, 0x80 | (d & 0x7F)); d >>= 7
            trk += v
            if msg[0] == 0xF0:
                body = msg[1:]; ln = len(body); trk += b'\xf0' + bytes([ln]) + body if ln < 128 else b''
            else: trk += msg
        trk += b'\x00\xff\x2f\x00'
        return b'MThd' + struct.pack('>IHHH', 6, 0, 1, 1000) + b'MTrk' + struct.pack('>I', len(trk)) + bytes(trk)
    def events(self, since=0):
        """Parsed messages [(instr_count, bytes)] after instruction count `since` (running status resolved)."""
        out = []; status = 0; cur = None; need = 0; sysex = None
        for n, b in self.bytes:
            if n < since: continue
            if b == 0xF0: sysex = [b]; continue
            if sysex is not None:
                sysex.append(b)
                if b == 0xF7: out.append((n, bytes(sysex))); sysex = None
                continue
            if b >= 0xF8: continue                          # realtime (0xFF = the chip reset the firmware sends first)
            if b & 0x80:
                status = b; cur = [b]; need = 1 if (b & 0xE0) == 0xC0 else 2
                continue
            if not status: continue
            if cur is None or len(cur) == 0: cur = [status]
            cur.append(b)
            if len(cur) == 1 + need: out.append((n, bytes(cur))); cur = []
        return out


def mp3_seconds(d):
    """Playing time of an MPEG audio stream (sum of its frames)."""
    BR = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
          2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
    SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
    i = 0; sec = 0.0; n = len(d)
    while i + 4 <= n:
        if d[i] != 0xFF or (d[i + 1] & 0xE0) != 0xE0: i += 1; continue
        ver = (d[i + 1] >> 3) & 3; layer = (d[i + 1] >> 1) & 3; bri = d[i + 2] >> 4; sri = (d[i + 2] >> 2) & 3
        if layer != 1 or ver == 1 or bri in (0, 15) or sri == 3: i += 1; continue      # Layer III only
        br = BR[1 if ver == 3 else 2][bri] * 1000; sr = SR[ver][sri]; pad = (d[i + 2] >> 1) & 1
        spf = 1152 if ver == 3 else 576
        size = spf // 8 * br // sr + pad
        if size < 4: i += 1; continue
        sec += spf / sr; i += size
    return sec


def mp3_frame(d, i):
    """(length in bytes, seconds) of the MPEG audio (Layer III) frame starting at d[i], or None."""
    if i + 4 > len(d) or d[i] != 0xFF or (d[i + 1] & 0xE0) != 0xE0: return None
    ver = (d[i + 1] >> 3) & 3; layer = (d[i + 1] >> 1) & 3; bri = d[i + 2] >> 4; sri = (d[i + 2] >> 2) & 3
    if layer != 1 or ver == 1 or bri in (0, 15) or sri == 3: return None
    br = (_BR1 if ver == 3 else _BR2)[bri] * 1000; sr = _SR[ver][sri]; spf = 1152 if ver == 3 else 576
    size = spf // 8 * br // sr + ((d[i + 2] >> 1) & 1)
    return (size, spf / sr) if size >= 4 else None
_BR1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


class KS5Ring:
    """KS5 audio DSP, type-2 player (MP3 from the A/V ring), high-level.
    play(): the clock starts; pump() (every run slice) takes the frames whose time has come (LEAD seconds ahead,
    like the decoder's input buffer), sets the read position PB+0x4CC behind them and passes their bytes to the
    app's audio output (emu.ks5_audio: start(t) / feed(bytes) / stop() / end()). When the firmware stops writing
    and the ring is played out the DSP reports stopped (PB+0x4D4 = 0), which the firmware waits for between clips.
    Songs in MP3 mode (gp+0x101E = 1) read the play clock from PB+0x4EC; the firmware converts it to 90 kHz with
    * gp[0x33C] / 10 (0x880739C8), so one unit is gp[0x33C] / 900000 s."""
    LEAD = 0.4
    def __init__(self, emu, dsp):
        self.dsp = dsp; self.pend = bytearray(); self.wr = 0; self.cons = 0; self.playing = False
        self.t0 = 0; self.sec = 0.0; self.last_w = 0; self.clip = bytearray(); self.started = False; self.no_out = False
        self.off = 0; self.armed = False; self.song = False; self.plays = 0
    def _out(self, e): return getattr(e, 'ks5_audio', None)
    def reset(self, e, p=None, v=0, size=4):
        """The firmware sets both positions (0) before a new clip / song: what was not played is dropped."""
        e.mmio_regs[PB + 0x4CC] = v & 0xFFFF; self.wr = (v * 3); self.cons = v * 3; self.pend = bytearray()
    def write(self, e, p, v, size):
        e.mmio_regs[PB + 0x4C8] = v
        gp = FW_GP; base = e.rd32(gp + 2152) & 0x1FFFFFFF
        sz = e.low[(gp + 2458) & 0x1FFFFFFF] | e.low[(gp + 2459) & 0x1FFFFFFF] << 8
        if not sz or not base: return
        new = (v * 3) % sz; old = self.wr % sz
        if new == old: return
        if new > old: self.pend += e.low[base + old: base + new]
        else: self.pend += e.low[base + old: base + sz]; self.pend += e.low[base: base + new]
        self.wr = new; self.last_w = e.n
        self.pump(e)
    def set_clock(self, e, v):
        """The firmware sets the play clock (0 before a song): the next stream counts from there."""
        self.off = v & 0xFFFF; self.armed = True
        if self.playing and self.song: self.off = (v - self._units(e)) & 0xFFFF
    def _units(self, e):
        div = e.rd32(FW_GP + 0x33C) or 3918
        return int(self.clock(e) * 900000 / div)
    def play(self, e, resume=False):
        if not resume:
            # a new stream: the clock goes on from where it was (it never runs backwards, which the firmware would
            # take as a wrap), unless the firmware has just set it
            # only the song's stream (started while the song's MIDI player runs, 0x301) drives the clock, from 0;
            # voice clips leave it alone
            self.song = bool(self.dsp.get('midi'))
            if self.song: self.off = 0
            self.sec = 0.0; self.clip = bytearray(); self.started = False; self.no_out = False; self.plays += 1
        self.t0 = e.n - int(self.sec * 108e6); self.playing = True; self.last_w = e.n
    def pause(self, e):
        if self.playing: self.pump(e); self.sec = min(self.sec, (e.n - self.t0) / 108e6); self.playing = False
    def stop(self, e):
        self.pause(e); self._finish(e, stopped=True)
    def _finish(self, e, stopped=False):
        self.playing = False; self.dsp['state'] = 0
        out = self._out(e)
        if out is not None and self.started:
            try: out.stop() if stopped else out.end()
            except Exception: pass
        elif self.clip and not stopped:          # no stream output: a short clip goes to the key-voice output
            kv = getattr(e, 'voice', None)
            if kv is not None and kv.enabled and kv.sink is not None and len(self.clip) < 200_000:
                kv.seq += 1
                try: kv.sink(kv.seq, bytes(self.clip))
                except Exception: kv.sink = None
        self.started = False; self.clip = bytearray()
    def clock(self, e):
        if not self.playing: return self.sec
        return min((e.n - self.t0) / 108e6, self.sec)
    def pump(self, e):
        if self.playing:
            now = (e.n - self.t0) / 108e6; pend = self.pend; i = 0; out = self._out(e); took = bytearray()
            while i < len(pend):
                f = mp3_frame(pend, i)
                if f is None:
                    j = pend.find(b'\xff', i + 1)
                    if j < 0: i = len(pend); break
                    i = j; continue
                if i + f[0] > len(pend) or self.sec > now + self.LEAD: break
                took += pend[i:i + f[0]]; i += f[0]; self.sec += f[1]
            if i:
                del pend[:i]; self.cons += i
                if took:
                    if out is not None and not self.no_out:
                        try:
                            if not self.started: out.start(self.t0 / 108e6); self.started = True
                            out.feed(bytes(took))
                        except Exception as ex:
                            self.no_out = True
                            try: out.error = out.error or repr(ex)
                            except Exception: pass
                    if (out is None or self.no_out) and len(self.clip) < 200_000: self.clip += took
            if not pend and now > self.sec:
                if e.n - self.last_w > 21_600_000:
                    self._finish(e)                 # played out and nothing more coming: the DSP stops
                elif now > self.sec + 0.1:
                    self.t0 = e.n - int((self.sec + 0.1) * 108e6)   # waiting for data: the clock waits too
        gp = FW_GP
        sz = (e.low[(gp + 2458) & 0x1FFFFFFF] | e.low[(gp + 2459) & 0x1FFFFFFF] << 8) // 3 or 3072
        e.mmio_regs[PB + 0x4CC] = (self.cons // 3) % sz
        if self.song and e.low[(gp + 0x101E) & 0x1FFFFFFF] == 1:        # MP3 song: the play clock
            div = e.rd32(gp + 0x33C) or 3918                # 90 kHz ticks * 10 per clock unit (3918 at 44.1 kHz, 1959 at 22.05)
            e.mmio_regs[PB + 0x4EC] = (self.off + int(self.clock(e) * 900000 / div)) & 0xFFFF


class SongPlayer:
    """KS5: the audio DSP's MIDI player, high-level. play() reads the one-track MIDI file the firmware built at
    MIDI_BUF and plays its events on the emulated clock (instruction count, 108 MHz): they go to the MIDI output
    (emu.midi) as the clock passes them, each stamped with its own time, so the synth plays them in step.
    lyrics() = how many lyric events (FF 05) have been played, PB+0x4EC: the firmware compares it with its lyric
    table index to turn the lyric pages (0x88076270).
    position() = the DSP's play clock in PB+0x4F0: elapsed song time in units of 1/5512.5 s (44.1 kHz / 8). The
    firmware's lyric table holds 90 kHz times and it converts the clock with * 2613 / 160 (0x88073A68); the song
    length it waits for (gp+0xFB4) is in the same units."""
    MIDI_BUF = 0x80068C00
    MAX = 0x2F000
    RATE = 5512.5
    def __init__(self, emu):
        self.ev = []; self.i = 0; self.t0 = 0; self.playing = False; self.sec = 0.0; self.loaded = False
        self.length = 0.0       # song length the firmware waits for (s)
        self.tick = None        # fn(e) also run by pump (the run loop calls pump every slice)
        self.fast = False       # True: PB+0x4F0 / 0x4EC are kept in the C core's registers by pump()
        self.lyric = 0          # lyric events played (PB+0x4EC: the firmware picks the lyric page from it)
    def _parse(self, e):
        import struct
        lo = e.low; a = self.MIDI_BUF & 0x1FFFFFFF
        if bytes(lo[a:a + 4]) != b'MThd': return False
        div = struct.unpack('>H', bytes(lo[a + 12:a + 14]))[0] or 120
        p = a + 14
        if bytes(lo[p:p + 4]) != b'MTrk': return False
        ln = struct.unpack('>I', bytes(lo[p + 4:p + 8]))[0]
        end = min(p + 8 + ln, a + self.MAX); p += 8
        def vlq(q):
            n = 0
            while q < end:
                c = lo[q]; q += 1; n = (n << 7) | (c & 0x7F)
                if not c & 0x80: break
            return n, q
        tempo = 500000; sec = 0.0; st = 0; ev = []
        while p < end:
            d, p = vlq(p)
            sec += d * tempo / 1e6 / div
            b = lo[p]
            if b == 0xFF:
                typ = lo[p + 1]; n, q = vlq(p + 2)
                if typ == 0x51 and n == 3: tempo = (lo[q] << 16) | (lo[q + 1] << 8) | lo[q + 2]
                if typ == 0x05: ev.append((sec, None))          # a lyric syllable: counted in PB+0x4EC
                p = q + n
                if typ == 0x2F: break
                continue
            if b in (0xF0, 0xF7):
                n, q = vlq(p + 1); msg = bytes(lo[q:q + n]); p = q + n
                if b == 0xF0: ev.append((sec, b'\xf0' + msg))
                continue
            if b & 0x80: st = b; p += 1
            if not st: break
            need = 1 if (st & 0xE0) == 0xC0 else 2
            ev.append((sec, bytes([st]) + bytes(lo[p:p + need]))); p += need
        self.ev = ev
        return True
    def play(self, e):
        if self.playing: return
        if not self.loaded:
            if not self._parse(e): self.ev = []
            self.loaded = True
            self.i = next((k for k, x in enumerate(self.ev) if x[0] >= self.sec - 1e-9), len(self.ev))
        self.t0 = e.n - int(self.sec * 108e6); self.playing = True
        if self.sec == 0:
            ln = e.rd32(FW_GP + 0xFB4)
            self.length = ln / self.RATE if 0 < ln < 0x10000000 else 0.0
        self.pump(e)
    def stop(self, e):
        if not self.playing: return
        self.pump(e); self.sec = self._sec(e); self.playing = False
        for ch in range(16):            # silence at once (sustain off, all notes off, all sound off)
            for cc in (64, 123, 120): self._send(e, bytes([0xB0 | ch, cc, 0]), e.n)
        self.pump(e)
    def seek(self, e, v):
        """The firmware sets the clock (0 before a new song): the file is read again at the next play."""
        if self.playing: self.stop(e)
        self.sec = (v & 0xFFFF) / self.RATE; self.loaded = False; self.i = 0
        self.pump(e)
    def pump(self, e):
        if self.tick is not None: self.tick(e)
        if self.playing:
            now = e.n; ev = self.ev
            while self.i < len(ev):
                n = self.t0 + int(ev[self.i][0] * 108e6)
                if n > now: break
                if ev[self.i][1] is None: self.lyric += 1
                else: self._send(e, ev[self.i][1], n)
                self.i += 1
        if self.fast:        # the C core answers the firmware's (very frequent) reads from these registers
            R = e.mmio_regs
            R[PB + 0x4F0] = int(self._sec(e) * self.RATE) & 0xFFFF
            if e.low[(FW_GP + 0x101E) & 0x1FFFFFFF] != 1: R[PB + 0x4EC] = self.lyric & 0xFFFF    # (MP3 songs: KS5Ring)
    def _sec(self, e):
        """Song time. The DSP's clock stops at the end of the file: the firmware sees it stand still and ends the
        song (0x8807DF0C), as on the real player."""
        sec = (e.n - self.t0) / 108e6 if self.playing else self.sec
        if self.ev and self.i >= len(self.ev):
            # it runs on to the song length the firmware expects (gp+0xFB4 at the start, in clock units; files
            # can end with silence after the last note), then stops
            sec = min(sec, max(self.ev[-1][0] + 0.2, self.length + 0.2))
        return sec
    def position(self, e):
        self.pump(e)
        return int(self._sec(e) * self.RATE)
    def lyrics(self, e):
        self.pump(e); return self.lyric
    def set_lyrics(self, e, v):
        self.pump(e); self.lyric = v & 0xFFFF
    def _send(self, e, msg, n):
        m = getattr(e, 'midi', None)
        if m is not None: m.emit(e, msg, n)


class StreamDMA:
    """Streaming READ CD (opcode BE, flags F8) on the karaoke discs: background videos (VOB = MPEG-2 program stream).
    The real drive module pushes sectors into its ring and DMA engine 0x08085D78 copies them into the A/V bitstream
    buffer; the firmware's own demuxer (0x0807xxxx module, read index gp+2614) takes 1 KB blocks from that buffer.
    Registers (KB units): PB+0x6100 base, +0x6108 size, +0x6110 write index (hardware), +0x6118 = last block the
    consumer has freed (write must stop before it).
    HLE: the BE packet is answered OK and its sectors' user data is written into the buffer whenever the firmware
    looks at the write index. Delivery is paced by the pack SCR (MPEG system clock) so the stream runs at playback
    speed, like a real decoder that only takes data as it plays it - without this the firmware demuxes ~5x faster
    than real time and starves its other tasks (the song sequencer, the OSD)."""
    LEAD = 0.25            # seconds of stream the firmware may run ahead of the clock
    RAW_RATE = 1_300_000   # bytes/s for sectors without a pack header
    MAX_RATE = 1_385_000   # bytes/s: 1x DVD
    NO_SCR = bool(__import__('os').environ.get('KE_NOSCR'))
    SEEK = float(__import__('os').environ.get('KE_SEEK', '0.08'))   # seconds for a jump of the drive's head
    STATE = 'stream_state'
    POLL_N = 54_000        # 0.5 ms
    def __init__(self, emu, vd):
        self.e = emu; self.vd = vd; self.log = []; self.total = 0
        self.tap = bytearray(); self.tap_total = 0       # the sectors in delivery order, for a live decoder
        st = self.st
        if st['lba'] is None:
            # resuming a snapshot taken mid-stream: the last READ CD is still in the packet buffer
            pkt = bytes(emu.rd8(vd.PKT + i) for i in range(12))
            if pkt[0] == 0xBE and getattr(vd, 'dvd', False):
                st['lba'] = int.from_bytes(pkt[2:6], 'big'); st['left'] = (pkt[6] << 16) | (pkt[7] << 8) | pkt[8]
        emu.read_hooks[PB + 0x6110] = self.rd_wr
        vd.stream = self
    @property
    def st(self):
        """Stream state lives on the emulator object, so snapshots (rig / quick boot) carry it along."""
        st = getattr(self.e, self.STATE, None)
        if not isinstance(st, dict):
            st = dict(lba=None, left=0, q=b'', base=None, scr=None); setattr(self.e, self.STATE, st)
        return st
    lba = property(lambda self: self.st['lba'])
    def start(self, lba, n):
        st = self.st
        # A new command restarts delivery at its sector. Keeping the undelivered rest of the previous commands
        # instead (0.6.4) built up a backlog: the firmware, which has no video decoder holding it back here, asks
        # for data several times faster than real time, decided its reads had failed and began scanning the disc
        # (thousands of scattered reads per second, a broken picture and a slow emulator).
        seek = st['lba'] is not None and abs(lba - st['lba']) > 64
        ahead = FW_ID == 'ks5' and st['lba'] is not None and st['lba'] <= lba <= st['lba'] + st['left'] + 16
        if st['lba'] is None or (lba != st['lba'] and not ahead): st['base'] = None     # a jump: restart the clock mapping
        # KS5: it asks for the next 4 sectors before the last ones have all arrived. Such a read continues the
        # stream: the stream clock is kept, so its sectors still come at playing speed (resetting it made the
        # background video run 3-4x fast). The undelivered rest of the previous read is dropped, as before.
        st['lba'] = lba; st['left'] = n; st['q'] = b''
        # a real drive needs time to move its head: data for a far jump comes later (also keeps the firmware's
        # disc scans, a few per second on a real player, from turning into thousands of reads per second here)
        st['wait_n'] = self.e.n + int(self.SEEK * 108e6) if seek else 0
        if hasattr(self.e, 'fast_cache'): self.e.fast_cache(PB + 0x6110, 0, 0)     # new data: stop answering from cache
        self.log.append((self.e.n, lba, n)); del self.log[:-2000]
    @staticmethod
    def scr(sec):
        if sec[:4] != b'\x00\x00\x01\xba' or (sec[4] >> 6) != 1: return None
        b = sec
        return (((b[4] >> 3) & 7) << 30) | ((b[4] & 3) << 28) | (b[5] << 20) | ((b[6] >> 3) << 15) | ((b[6] & 3) << 13) | (b[7] << 5) | (b[8] >> 3)
    MV_LEAD = 1.0          # seconds of a music video the player may read ahead of its playing time
    def _mv_extents(self):
        """[(first sector, end sector, bytes per second)] of the disc's music videos (KARA125/MVV/*.mvv, AVI)."""
        if getattr(self, '_mvx', None) is not None: return self._mvx
        self._mvx = []
        try:
            import isoread, struct as S
            d = isoread.Disc(self.vd.disc)
            kara = [x['name'] for x in d.listdir('/') if x['is_dir'] and x['name'].upper().startswith('KARA')]
            for k in kara:
                mvd = [x['name'] for x in d.listdir('/' + k) if x['is_dir'] and x['name'].upper() == 'MVV']
                for m in mvd:
                    for f in d.listdir('/%s/%s' % (k, m)):
                        if f['is_dir'] or not f['name'].lower().endswith('.mvv'): continue
                        a, n = d.lookup('/%s/%s/%s' % (k, m, f['name'])).extents[0]
                        rate = 200_000
                        try:
                            h = self.vd.src.read(a, 2)
                            i = h.find(b'avih')
                            if i >= 0:
                                usec, _, _, _, frames = S.unpack_from('<5I', h, i + 8)
                                dur = usec * frames / 1e6
                                if dur > 1: rate = max(20_000, n / dur)
                        except Exception:
                            pass
                        self._mvx.append((a, a + (n + 2047) // 2048, rate))
            self._mvx.sort()
        except Exception:
            self._mvx = []
        return self._mvx
    def _mv_of(self, lba):
        for a, b, rate in self._mv_extents():
            if a <= lba < b: return a, b, rate
        return None
    def _due(self, e, sec):
        """May this sector be delivered now?"""
        st = self.st; now = e.n / 108e6
        mv = self._mv_of(st['lba']) if getattr(self.vd, 'disc', None) else None
        if mv is not None:
            # a music video (AVI): the player has no MPEG-4 decoder here to hold it back, so its reads are paced at
            # the file's own playing speed (bytes per second from the AVI header), a little ahead of the clock
            a, b, rate = mv; p = st.get('mvp')
            if p is None or p['start'] != a: p = st['mvp'] = dict(start=a, t0=now)
            want = (st['lba'] - a) * 2048 / rate                 # this sector's place in the video, in seconds
            ahead = want - (now - p['t0'])
            if ahead > 30 or ahead < -5: p['t0'] = now - want; ahead = 0     # a jump (the index at the end, a seek)
            if ahead > self.MV_LEAD:
                st['wait_n'] = e.n + int(min(ahead - self.MV_LEAD, 0.05) * 108e6); return False
            return True
        st.pop('mvp', None)
        s = self.scr(sec)
        t = s / 90000.0 if s is not None else ((st['scr'] or 0.0) + 2048 / self.RAW_RATE)
        base = st['base']
        if base is None or t < base[1] - 0.5 or t > base[1] + (now - base[0]) + 5.0:
            st['base'] = base = (now, t)                   # first sector, or a clock discontinuity
        if t - base[1] > (now - base[0]) + self.LEAD and not self.NO_SCR:
            st['wait_n'] = int((base[0] + (t - base[1]) - self.LEAD) * 108e6); return False
        # and never faster than a 1x DVD drive (some karaoke VOBs count their SCR up by 1 per pack at the start)
        cap = st.get('cap')
        if cap is None or now - cap[0] > 2.0: cap = st['cap'] = [now, 0]
        if cap[1] > (now - cap[0]) * self.MAX_RATE + 65536:
            st['wait_n'] = e.n + 108000; return False
        if now - cap[0] > 1.0: cap[0] += 0.5; cap[1] = max(0, cap[1] - int(0.5 * self.MAX_RATE))
        cap[1] += 2048
        st['scr'] = t
        return True
    def rd_wr(self, e):
        st = self.st; R = e.mmio_regs
        if not (e.n < st.get('wait_n', 0) and len(st['q']) < 1024):
            self.pump(e)
        wr = R.get(PB + 0x6110, 0)
        if hasattr(e, 'fast_cache'):
            size = R.get(PB + 0x6108, 4) or 4; rd = R.get(PB + 0x6118, size - 1) & 0xFFFF
            if wr == rd % size and len(st['q']) >= 1024: e.fast_cache(PB + 0x6110, wr, 1 << 62)   # full: until the next register write
            elif len(st['q']) < 1024 and e.n < st.get('wait_n', 0): e.fast_cache(PB + 0x6110, wr, st['wait_n'])
            elif not st['left'] and len(st['q']) < 1024: e.fast_cache(PB + 0x6110, wr, 1 << 62)    # stream done until the next command
            else: e.fast_cache(PB + 0x6110, wr, e.n + self.POLL_N)   # the main loop polls this thousands of times a
            # second; like a real DMA engine, new data shows up in steps (every POLL_N instructions, or at once when
            # the firmware frees a block or sends a command) instead of a Python call per poll
        return wr
    def since(self, mark):
        """(program-stream bytes delivered after byte count `mark`, new mark): the whole VOB stream as read from the
        disc (video and audio packs), which a live decoder can play directly. The video decoder window only sees
        part of it: the firmware hands most blocks to its decoder another way (not emulated)."""
        n = self.tap_total - mark
        if n <= 0: return b'', self.tap_total
        n = min(n, len(self.tap))
        return bytes(self.tap[-n:]), self.tap_total

    def pump(self, e):
        st = self.st; R = e.mmio_regs
        size = R.get(PB + 0x6108, 4) or 4; base = R.get(PB + 0x6100, 0) << 10
        wr = R.get(PB + 0x6110, 0) % size; rd = R.get(PB + 0x6118, size - 1) & 0xFFFF
        q = st['q']
        while True:
            if len(q) < 1024 and st['left'] and st['lba'] is not None:
                sec = self.vd.src.read(st['lba'], 1)        # the 2048 user-data bytes (the DMA skips the sector header)
                if not self._due(e, sec): break
                lba = st['lba']; q += sec; st['lba'] += 1; st['left'] -= 1
                # the live picture gets each disc sector once, in disc order: a sector the firmware reads again
                # (stepping back a little) is left out, as a repeated stretch confuses the decoder
                last = getattr(self, 'tap_lba', None)
                if last is None or lba > last or lba < last - 4096:
                    self.tap += sec; self.tap_total += len(sec); self.tap_lba = lba
                    if len(self.tap) > 8 << 20: del self.tap[:len(self.tap) - (4 << 20)]
            if len(q) < 1024 or wr == rd: break
            e.low[base + wr * 1024:base + wr * 1024 + 1024] = q[:1024]; q = q[1024:]; self.total += 1024
            wr = (wr + 1) % size
        st['q'] = q
        R[PB + 0x6110] = wr
        if not st['left'] and not q and st.get('busy_addr') is not None:     # transfer complete: drive idle
            e.low[st['busy_addr']] &= 0x7F; st['busy_addr'] = None


class InflateHLE:
    """The firmware's DEFLATE decoder 0x0807B8DC(bitstream, out) (fixed-Huffman blocks; bitstream = {+0 base pointer,
    +4 bit position}). It unpacks graphics kept compressed in flash (e.g. 43,664 bytes from 0x88109780 when a song
    starts) and costs ~70 instructions per output byte. Replaced by zlib: same output bytes, same return value
    (bytes written) and same end bit position (end of the last block rounded up to a byte, plus a 64-bit trailer),
    checked against the native routine. Falls back to the native code for anything unusual."""
    PCS = (0x8807B8DC, 0x0807B8DC)
    COST = 70                                       # emulated instructions charged per output byte
    def __init__(self, emu):
        self.calls = 0
        for pc in TS(self.PCS): emu.hle[pc] = self.hook; emu.bp.add(pc)
    @staticmethod
    def _slice(e, addr, n):
        import emu as emu_mod
        p = addr & 0x1FFFFFFF
        if p < e.low_end: return bytes(e.low[p:min(p + n, e.low_end)])
        if emu_mod.BASE <= p < e.ram_end: return bytes(e.ram[p - emu_mod.BASE:min(p - emu_mod.BASE + n, e.ram_end - emu_mod.BASE)])
        return None
    def hook(self, e):
        import zlib
        bs, out = e.r[4], e.r[5]
        base = e.rd32(bs); bit = e.rd32(bs + 4)
        if bit % 8: return                           # not byte aligned: let the firmware do it
        src = self._slice(e, base + bit // 8, 1 << 20)
        if not src or (src[0] & 6) != 2: return       # first block not fixed-Huffman: native path (returns -1 there)
        d = zlib.decompressobj(-15)
        try: o = d.decompress(src)
        except zlib.error: return
        if not d.eof: return
        used = len(src) - len(d.unused_data)
        p = out & 0x1FFFFFFF
        if p + len(o) <= e.low_end: e.low[p:p + len(o)] = o
        else:
            for i, b in enumerate(o): e.wr8(out + i, b)
        e.wr32(bs + 4, bit + used * 8 + 64)
        e.r[2] = len(o); e.pc = e.r[31]; e.npc = e.pc + 4
        e.n += self.COST * len(o); self.calls += 1

#!/usr/bin/env python3
"""Minimal MIPS I (little-endian) emulator for the Sunplus MP100NSP firmware.
Phase 2: CPU core + RAM + logged MMIO. Peripherals are stubbed incrementally."""
import struct, sys, collections
from mipsdis import dis

M32 = 0xFFFFFFFF
BASE = 0x08000000
def s16(x): return x - 0x10000 if x & 0x8000 else x
def s32(x): return x - (1 << 32) if x & 0x80000000 else x

class Stop(Exception): pass

class PyEmu:
    """Reference core in pure Python (used when the C core is not available, or with KE_PYCPU=1)."""
    def __init__(self, image, ram_mb=32):
        self.ram = bytearray(ram_mb << 20)
        self.ram[:len(image)] = image
        self.ram_end = BASE + len(self.ram)
        self.low = bytearray(32 << 20)           # SDRAM at phys 0 (probed by firmware)
        self.low_end = len(self.low)
        self.r = [0] * 32
        self.hi = self.lo = 0
        self.pc = 0x08000220
        self.npc = self.pc + 4
        self.cp0 = [0] * 32
        self.n = 0
        self.nw = 0; self._nw0 = 0; self.nr = 0; self._nr0 = 0
        self.mmio_log = collections.OrderedDict()   # (phys,'R'/'W') -> [count, firstpc, lastval]
        self.mmio_regs = {}                         # phys -> value written (simple read-back)
        self.read_hooks = {}                        # phys -> fn(emu)->value
        self.write_hooks = {}                       # phys -> fn(emu,phys,val,size)
        self.ring = collections.deque(maxlen=32)
        self.dc = {}
        self.irq_hook = None; self.irq_next = 0
        self.watch = {}                              # phys word addr -> list of (n,pc,val) for RAM writes
        self.bp = set(); self.bp_hits = []; self.hle = {}   # hle: pc -> fn(emu) called on reaching pc (pc must also be in bp)           # breakpoints: record (n, pc) when reached, execution continues
        self.win_r = []                              # (lo,hi,fn(emu,p,size)->value) read windows
        self.win_w = []                              # (lo,hi,fn(emu,p,v,size)) write windows, not logged
        self.stub_ranges = []                       # (lo,hi) of physical ranges treated as plain RAM-ish regs

    # ---- memory ----
    @staticmethod
    def phys(a): return a & 0x1FFFFFFF
    def _mmio_r(self, p, size):
        for lo, hi, fn in self.win_r:
            if lo <= p < hi: return fn(self, p, size)
        self.nr += 1
        k = (p, 'R'); e = self.mmio_log.get(k)
        if e is None: self.mmio_log[k] = [1, self.cur_pc, 0]
        else: e[0] += 1
        h = self.read_hooks.get(p & ~3)
        v = h(self) if h else self.mmio_regs.get(p & ~3, 0)
        if size < 4: v = (v >> (8 * (p & 3))) & ((1 << (8 * size)) - 1)
        return v
    def _mmio_w(self, p, v, size):
        for lo, hi, fn in self.win_w:
            if lo <= p < hi: fn(self, p, v, size); return
        k = (p, 'W'); e = self.mmio_log.get(k)
        if e is None: self.mmio_log[k] = [1, self.cur_pc, v]
        else: e[0] += 1; e[2] = v
        if size == 4: self.mmio_regs[p] = v
        h = self.write_hooks.get(p & ~3)
        if h: h(self, p, v, size)
    def rd32(self, a):
        p = a & 0x1FFFFFFF
        if p < self.low_end:
            return struct.unpack_from('<I', self.low, p)[0]
        if BASE <= p < self.ram_end:
            return struct.unpack_from('<I', self.ram, p - BASE)[0]
        return self._mmio_r(p, 4)
    def rd16(self, a):
        p = a & 0x1FFFFFFF
        if p < self.low_end:
            return struct.unpack_from('<H', self.low, p)[0]
        if BASE <= p < self.ram_end:
            return struct.unpack_from('<H', self.ram, p - BASE)[0]
        return self._mmio_r(p, 2)
    def rd8(self, a):
        p = a & 0x1FFFFFFF
        if p < self.low_end: return self.low[p]
        if BASE <= p < self.ram_end: return self.ram[p - BASE]
        return self._mmio_r(p, 1)
    def wr32(self, a, v):
        self.nw += 1
        p = a & 0x1FFFFFFF
        if p < self.low_end:
            struct.pack_into('<I', self.low, p, v & M32)
            if self.watch and (p & ~3) in self.watch: self.watch[p & ~3].append((self.n, self.cur_pc, v & M32))
            return
        if BASE <= p < self.ram_end:
            struct.pack_into('<I', self.ram, p - BASE, v & M32); self.dc.pop(p & ~3, None); return
        self._mmio_w(p, v & M32, 4)
    def wr16(self, a, v):
        self.nw += 1
        p = a & 0x1FFFFFFF
        if p < self.low_end:
            struct.pack_into('<H', self.low, p, v & 0xFFFF)
            if self.watch and (p & ~3) in self.watch: self.watch[p & ~3].append((self.n, self.cur_pc, ('h', p & 3, v & 0xFFFF)))
            return
        if BASE <= p < self.ram_end:
            struct.pack_into('<H', self.ram, p - BASE, v & 0xFFFF); self.dc.pop(p & ~3, None); return
        self._mmio_w(p, v & 0xFFFF, 2)
    def wr8(self, a, v):
        self.nw += 1
        p = a & 0x1FFFFFFF
        if p < self.low_end:
            self.low[p] = v & 0xFF
            if self.watch and (p & ~3) in self.watch: self.watch[p & ~3].append((self.n, self.cur_pc, ('b', p & 3, v & 0xFF)))
            return
        if BASE <= p < self.ram_end:
            self.ram[p - BASE] = v & 0xFF; self.dc.pop(p & ~3, None); return
        self._mmio_w(p, v & 0xFF, 1)

    # ---- execution ----
    def run(self, max_instr, spin_window=200000):
        r = self.r; ram = self.ram; dc = self.dc
        seen = set(); sigs = set(); end = self.n + max_instr; skip = self.skipcache = getattr(self,'skipcache',{})
        while self.n < end:
            if self.n >= self.irq_next:
                self.irq_next = self.n + 128
                if self.irq_hook is not None and self.npc == ((self.pc + 4) & M32):
                    self.irq_hook(self)
            pc = self.pc
            self.cur_pc = pc
            if self.bp and pc in self.bp:
                h = self.hle.get(pc)
                if h is not None:
                    h(self)
                    if self.pc != pc:          # the hook emulated the function itself (HLE) and redirected pc
                        self.n += 1; r[0] = 0; continue
                else: self.bp_hits.append((self.n, pc))
            p = pc & 0x1FFFFFFF
            if p < self.low_end:
                w = struct.unpack_from('<I', self.low, p)[0]
            elif BASE <= p < self.ram_end:
                w = struct.unpack_from('<I', ram, p - BASE)[0]
            else:
                raise Stop(f'fetch from non-RAM address {pc:08x}')
            self.ring.append((pc, w))
            npc = self.npc
            self.pc = npc
            self.npc = nn = (npc + 4) & M32
            op = w >> 26
            rs = (w >> 21) & 31; rt = (w >> 16) & 31; im = w & 0xFFFF
            self.n += 1
            if op == 9 and im == 0xFFFF and rs == rt and rt:
                ln = skip.get(pc)
                if ln is None:
                    ln = 0
                    def W(o): return struct.unpack_from('<I', ram, (p + o) - BASE)[0] if BASE <= p < self.ram_end else 0
                    bne = 0x14000000 | (rt << 21)
                    if W(4) == (bne | 0xFFFE) and W(8) == 0: ln = 3
                    elif W(4) == 0 and W(8) == (bne | 0xFFFD) and W(12) == 0: ln = 4
                    skip[pc] = ln
                if ln and r[rt]:
                    self.n += r[rt] * ln
                    r[rt] = 0
                    self.pc = (pc + ln * 4) & M32; self.npc = (self.pc + 4) & M32
                    continue
            if self.n & 63 == 0:
                seen.add(pc); sigs.add(hash(tuple(r)))
                if self.n % spin_window < 64:
                    if len(seen) < 40 and len(sigs) < 8 and (self.nw - self._nw0) < 100 and (self.nr - self._nr0) > 100:
                        raise Stop(f'spin: only {len(seen)} distinct pcs in last window')
                    seen.clear(); sigs.clear(); self._nw0 = self.nw; self._nr0 = self.nr
            if op == 0:
                fn = w & 63; rd = (w >> 11) & 31; sh = (w >> 6) & 31
                if fn == 0: v = (r[rt] << sh) & M32
                elif fn == 2: v = r[rt] >> sh
                elif fn == 3: v = (s32(r[rt]) >> sh) & M32
                elif fn == 4: v = (r[rt] << (r[rs] & 31)) & M32
                elif fn == 6: v = r[rt] >> (r[rs] & 31)
                elif fn == 7: v = (s32(r[rt]) >> (r[rs] & 31)) & M32
                elif fn == 8: self.npc = r[rs]; continue
                elif fn == 9: ra = nn if False else (pc + 8) & M32; t = r[rs]; r[rd] = ra; self.npc = t; r[0] = 0; continue
                elif fn == 12: raise Stop('syscall')
                elif fn == 13: raise Stop('break')
                elif fn == 16: v = self.hi
                elif fn == 17: self.hi = r[rs]; continue
                elif fn == 18: v = self.lo
                elif fn == 19: self.lo = r[rs]; continue
                elif fn == 24:
                    # 3-operand form 'mult rd,rs,rt' (10x in this firmware) is this core's 16x16 multiplier:
                    # rd = lo16(rs) * lo16(rt). Proven by the song descrambler, whose compiler-generated
                    # 32-bit multiply is built from three of these (see PHASE16_NOTES.md).
                    if rd:
                        x = s16(r[rs] & 0xFFFF) * s16(r[rt] & 0xFFFF); self.lo = x & M32; self.hi = (x >> 32) & M32
                        r[rd] = self.lo
                    else:
                        x = s32(r[rs]) * s32(r[rt]); self.lo = x & M32; self.hi = (x >> 32) & M32
                    continue
                elif fn == 25:
                    if rd:                     # 'multu rd,rs,rt' (1318x): 16x16 -> 32 unsigned
                        x = (r[rs] & 0xFFFF) * (r[rt] & 0xFFFF); self.lo = x; self.hi = 0
                        r[rd] = x
                    else:
                        x = r[rs] * r[rt]; self.lo = x & M32; self.hi = (x >> 32) & M32
                    continue
                elif fn == 26:
                    a, b = s32(r[rs]), s32(r[rt])
                    if b: q = int(abs(a) // abs(b)) * (1 if (a < 0) == (b < 0) else -1); self.lo = q & M32; self.hi = (a - q * b) & M32
                    continue
                elif fn == 27:
                    if r[rt]: self.lo = r[rs] // r[rt]; self.hi = r[rs] % r[rt]
                    continue
                elif fn == 32 or fn == 33: v = (r[rs] + r[rt]) & M32
                elif fn == 34 or fn == 35: v = (r[rs] - r[rt]) & M32
                elif fn == 36: v = r[rs] & r[rt]
                elif fn == 37: v = r[rs] | r[rt]
                elif fn == 38: v = r[rs] ^ r[rt]
                elif fn == 39: v = ~(r[rs] | r[rt]) & M32
                elif fn == 42: v = 1 if s32(r[rs]) < s32(r[rt]) else 0
                elif fn == 43: v = 1 if r[rs] < r[rt] else 0
                else: raise Stop(f'unimplemented SPECIAL fn={fn} at {pc:08x}')
                if rd: r[rd] = v
                continue
            si = im - 0x10000 if im & 0x8000 else im
            if op == 15:
                if rt: r[rt] = (im << 16) & M32
            elif op == 9 or op == 8:
                if rt: r[rt] = (r[rs] + si) & M32
            elif op == 13:
                if rt: r[rt] = r[rs] | im
            elif op == 12:
                if rt: r[rt] = r[rs] & im
            elif op == 14:
                if rt: r[rt] = r[rs] ^ im
            elif op == 10:
                if rt: r[rt] = 1 if s32(r[rs]) < si else 0
            elif op == 11:
                if rt: r[rt] = 1 if r[rs] < (si & M32) else 0
            elif op == 35:
                v = self.rd32((r[rs] + si) & M32)
                if rt: r[rt] = v
            elif op == 43: self.wr32((r[rs] + si) & M32, r[rt])
            elif op == 36:
                v = self.rd8((r[rs] + si) & M32)
                if rt: r[rt] = v
            elif op == 32:
                v = self.rd8((r[rs] + si) & M32)
                if rt: r[rt] = (v - 256 if v & 0x80 else v) & M32
            elif op == 37:
                v = self.rd16((r[rs] + si) & M32)
                if rt: r[rt] = v
            elif op == 33:
                v = self.rd16((r[rs] + si) & M32)
                if rt: r[rt] = (v - 65536 if v & 0x8000 else v) & M32
            elif op == 40: self.wr8((r[rs] + si) & M32, r[rt])
            elif op == 41: self.wr16((r[rs] + si) & M32, r[rt])
            elif op == 4:
                if r[rs] == r[rt]: self.npc = (npc + (si << 2)) & M32
            elif op == 5:
                if r[rs] != r[rt]: self.npc = (npc + (si << 2)) & M32
            elif op == 6:
                if s32(r[rs]) <= 0: self.npc = (npc + (si << 2)) & M32
            elif op == 7:
                if s32(r[rs]) > 0: self.npc = (npc + (si << 2)) & M32
            elif op == 1:
                x = s32(r[rs])
                if rt == 0: c = x < 0
                elif rt == 1: c = x >= 0
                elif rt == 16: c = x < 0; r[31] = (pc + 8) & M32
                elif rt == 17: c = x >= 0; r[31] = (pc + 8) & M32
                else: raise Stop(f'bad REGIMM {rt} at {pc:08x}')
                if c: self.npc = (npc + (si << 2)) & M32
            elif op == 2: self.npc = (npc & 0xF0000000) | ((w & 0x3FFFFFF) << 2)
            elif op == 3:
                r[31] = (pc + 8) & M32
                self.npc = (npc & 0xF0000000) | ((w & 0x3FFFFFF) << 2)
            elif op == 34:  # lwl
                a = (r[rs] + si) & M32; al = a & ~3; k = a & 3
                m = self.rd32(al); o = r[rt]
                v = ((o & ((1 << (8 * (3 - k))) - 1)) | ((m << (8 * (3 - k))) & M32)) if True else 0
                if rt: r[rt] = v & M32
            elif op == 38:  # lwr
                a = (r[rs] + si) & M32; al = a & ~3; k = a & 3
                m = self.rd32(al); o = r[rt]
                v = (m >> (8 * k)) | (o & ((M32 << (8 * (4 - k))) & M32) if k else 0)
                if k == 0: v = m
                if rt: r[rt] = v & M32
            elif op == 42:  # swl
                a = (r[rs] + si) & M32; al = a & ~3; k = a & 3
                m = self.rd32(al); v = r[rt]
                mask = (1 << (8 * (k + 1))) - 1
                self.wr32(al, (m & ~mask) | (v >> (8 * (3 - k))))
            elif op == 46:  # swr
                a = (r[rs] + si) & M32; al = a & ~3; k = a & 3
                m = self.rd32(al); v = r[rt]
                mask = (M32 << (8 * k)) & M32
                self.wr32(al, (m & ~mask) | ((v << (8 * k)) & M32))
            elif op == 16:
                rd = (w >> 11) & 31
                if rs == 0:
                    if rt: r[rt] = self.cp0[rd]
                elif rs == 4: self.cp0[rd] = r[rt]
                elif rs == 16 and (w & 63) == 16:      # rfe: pop KU/IE stack
                    sr = self.cp0[12]; self.cp0[12] = (sr & ~0xF) | ((sr >> 2) & 0xF)
                else: raise Stop(f'unimplemented COP0 rs={rs} at {pc:08x}')
            else:
                raise Stop(f'unimplemented opcode {op} (word {w:08x}) at {pc:08x}')
            r[0] = 0

    def report(self, why):
        print(f'== stopped after {self.n:,} instrs: {why}')
        print(f'pc={self.cur_pc:08x}')
        print('regs:', ' '.join(f'{n}={v:08x}' for n, v in zip(
            ['zero','at','v0','v1','a0','a1','a2','a3','t0','t1','t2','t3','t4','t5','t6','t7','s0','s1','s2','s3','s4','s5','s6','s7','t8','t9','k0','k1','gp','sp','fp','ra'], self.r)))
        print('last instructions:')
        for pc, w in list(self.ring)[-16:]:
            print(f'  {pc:08x}: {w:08x}  {dis(w, pc)}')
        print(f'MMIO / non-RAM accesses ({len(self.mmio_log)} unique):')
        for (p, k), (c, fpc, lv) in list(self.mmio_log.items())[:60]:
            print(f'  {k} {p:08x} x{c:<8} first@{fpc:08x} last={lv:#x}')

# ------------------------------------------------------------------------------------------- fast C core
import ctypes, os as _os

_LIB = None
def _load_lib():
    global _LIB
    if _LIB is not None or _os.environ.get('KE_PYCPU'): return _LIB
    here = _os.path.dirname(_os.path.abspath(__file__))
    dirs = [here]
    if getattr(sys, '_MEIPASS', None): dirs.insert(0, sys._MEIPASS)
    if getattr(sys, 'frozen', False): dirs.append(_os.path.dirname(sys.executable))
    if _os.environ.get('KE_LIBDIR'): dirs.insert(0, _os.environ['KE_LIBDIR'])     # Android: the app's native library folder
    names = ('kecpu.dll',) if _os.name == 'nt' else ('libkecpu.so',)
    for d in dirs:
        for nm in names:
            cand = _os.path.join(d, nm)
            if _os.path.isfile(cand):
                try:
                    lib = ctypes.CDLL(cand); lib.cpu_run.restype = ctypes.c_int32
                    lib.cpu_run.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
                    if lib.cpu_struct_size() != ctypes.sizeof(_CPU): continue
                    _LIB = lib; return lib
                except OSError:
                    continue
    return None

_MMIO_R = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32)
_MMIO_W = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32)
_IRQ = ctypes.CFUNCTYPE(ctypes.c_uint64)

class _CPU(ctypes.Structure):
    _fields_ = [('r', ctypes.c_uint32 * 32), ('hi', ctypes.c_uint32), ('lo', ctypes.c_uint32), ('pc', ctypes.c_uint32),
                ('npc', ctypes.c_uint32), ('cur_pc', ctypes.c_uint32), ('cp0', ctypes.c_uint32 * 32),
                ('n', ctypes.c_uint64), ('nw', ctypes.c_uint64), ('nr', ctypes.c_uint64), ('irq_next', ctypes.c_uint64),
                ('low', ctypes.c_void_p), ('low_end', ctypes.c_uint32), ('ram', ctypes.c_void_p), ('ram_base', ctypes.c_uint32),
                ('ram_end', ctypes.c_uint32), ('bpmap', ctypes.c_void_p), ('bpmap_bits', ctypes.c_uint32),
                ('mmio_r', _MMIO_R), ('mmio_w', _MMIO_W), ('irq_cb', _IRQ),
                ('has_irq', ctypes.c_int32), ('skip_bp', ctypes.c_int32), ('abort', ctypes.c_int32), ('stop_word', ctypes.c_uint32),
                ('win_lo', ctypes.c_uint32), ('win_hi', ctypes.c_uint32), ('win_mem', ctypes.c_void_p),
                ('cap', ctypes.c_void_p), ('cap_len', ctypes.c_uint32), ('cap_size', ctypes.c_uint32),
                ('timers_on', ctypes.c_int32), ('ipus', ctypes.c_uint32),
                ('nsink', ctypes.c_uint32), ('sink_addr', ctypes.c_uint32 * 16), ('sink_val', ctypes.c_uint32 * 16),
                ('cache_addr', ctypes.c_uint32), ('cache_val', ctypes.c_uint32), ('cache_until', ctypes.c_uint64),
                ('regs', ctypes.c_void_p), ('rflag', ctypes.c_void_p), ('reg_base', ctypes.c_uint32), ('reg_words', ctypes.c_uint32),
                ('ncomp', ctypes.c_uint32), ('comp_addr', ctypes.c_uint32 * 32), ('comp_kind', ctypes.c_uint32 * 32),
                ('comp_p', ctypes.c_uint32 * 32), ('comp_k', ctypes.c_uint32 * 32), ('comp_keep', ctypes.c_uint32 * 32),
                ('comp_set', ctypes.c_uint32 * 32)]

REG_BASE, REG_WORDS = 0x1FFE8000, 0x8000 // 4        # the peripheral block whose plain registers live in C


import collections.abc as _abc

class _RegFile(_abc.MutableMapping):
    """emu.mmio_regs for the C core: words of the peripheral block are stored in C (so plain register writes and
    read-backs never leave C); other addresses in a normal dict. Same behaviour as the dict it replaces
    (get(addr, default) returns the default for a word never written)."""
    def __init__(self, regs, flags):
        self._r = regs; self._f = flags; self._d = {}
    def _idx(self, k):
        i = (k - REG_BASE) >> 2
        return i if 0 <= i < REG_WORDS and not (k & 3) else None
    def __getitem__(self, k):
        i = self._idx(k)
        if i is None: return self._d[k]
        if not self._f[i] & 1: raise KeyError(k)
        return self._r[i]
    def get(self, k, default=None):
        i = self._idx(k)
        if i is None: return self._d.get(k, default)
        return self._r[i] if self._f[i] & 1 else default
    def __setitem__(self, k, v):
        i = self._idx(k)
        if i is None: self._d[k] = v
        else: self._r[i] = v & 0xFFFFFFFF; self._f[i] |= 1
    def __delitem__(self, k):
        i = self._idx(k)
        if i is None: del self._d[k]
        elif self._f[i] & 1: self._f[i] &= ~1 & 0xFF; self._r[i] = 0
        else: raise KeyError(k)
    def __contains__(self, k):
        i = self._idx(k)
        return (k in self._d) if i is None else bool(self._f[i] & 1)
    def __iter__(self):
        for i in range(REG_WORDS):
            if self._f[i] & 1: yield REG_BASE + 4 * i
        yield from list(self._d)
    def __len__(self): return sum(1 for i in range(REG_WORDS) if self._f[i] & 1) + len(self._d)


class _HookDict(dict):
    """read_hooks / write_hooks for the C core: marks the words Python must see (flag bit)."""
    def __init__(self, emu, bit, items=()):
        super().__init__(); self._emu = emu; self._bit = bit
        for k, v in dict(items).items(): self[k] = v
    def _mark(self, k, on):
        i = (k - REG_BASE) >> 2
        if 0 <= i < REG_WORDS:
            f = self._emu._rflag
            if on: f[i] |= self._bit
            else: f[i] &= ~self._bit & 0xFF
    def __setitem__(self, k, v): super().__setitem__(k, v); self._mark(k, True)
    def __delitem__(self, k): super().__delitem__(k); self._mark(k, False)
    def pop(self, k, *d):
        had = k in self; r = super().pop(k, *d)
        if had: self._mark(k, False)
        return r
    def setdefault(self, k, v=None):
        if k not in self: self[k] = v
        return self[k]
    def update(self, *a, **kw):
        for k, v in dict(*a, **kw).items(): self[k] = v
    def clear(self):
        for k in list(self): self._mark(k, False)
        super().clear()

_REASONS = {2: 'syscall', 3: 'break', 5: 'fetch from non-RAM address', 7: 'bad REGIMM', 8: 'unimplemented COP0',
            9: 'unimplemented SPECIAL', 4: 'unimplemented opcode'}


class _BPSet(set):
    """The breakpoint set, mirrored into the C core's bitmap (by physical word address)."""
    def __init__(self, emu, items=()):
        super().__init__(); self._emu = emu; self.update(items)
    def _mark(self, pc, on):
        p = (pc & 0x1FFFFFFF) >> 2; m = self._emu._bpmap
        if p >= len(m) * 8: return
        if on: m[p >> 3] |= 1 << (p & 7)
        elif not any(((q & 0x1FFFFFFF) >> 2) == p for q in self if q != pc): m[p >> 3] &= ~(1 << (p & 7)) & 0xFF
    def add(self, pc): super().add(pc); self._mark(pc, True)
    def discard(self, pc):
        if pc in self: super().discard(pc); self._mark(pc, False)
    def remove(self, pc): super().remove(pc); self._mark(pc, False)
    def update(self, *its):
        for it in its:
            for pc in it: self.add(pc)
    def __ior__(self, other): self.update(other); return self
    def clear(self):
        for pc in list(self): self.discard(pc)


class FastEmu(PyEmu):
    """Same interface as PyEmu; the instruction loop runs in C (fastcpu/cpu.c, libkecpu.so / kecpu.dll)."""
    def __init__(self, image, ram_mb=32):
        self._c = _CPU(); self._exc = None; self._irq_hook = None; self.irq_next_fn = None
        self._bpmap = bytearray((0x0A000000 >> 2) // 8)
        self._bpbuf = (ctypes.c_char * len(self._bpmap)).from_buffer(self._bpmap)
        c = self._c; c.bpmap = ctypes.addressof(self._bpbuf); c.bpmap_bits = len(self._bpmap) * 8
        self._cb_r = _MMIO_R(self._cb_mmio_r); self._cb_w = _MMIO_W(self._cb_mmio_w); self._cb_i = _IRQ(self._cb_irq)
        c.mmio_r = self._cb_r; c.mmio_w = self._cb_w; c.irq_cb = self._cb_i
        self._ram = self._low = None
        self._regs = (ctypes.c_uint32 * REG_WORDS)(); self._rflag = (ctypes.c_uint8 * REG_WORDS)()
        c.regs = ctypes.addressof(self._regs); c.rflag = ctypes.addressof(self._rflag); c.reg_base = REG_BASE; c.reg_words = REG_WORDS
        self._mmio = _RegFile(self._regs, self._rflag); self._rh = _HookDict(self, 2); self._wh = _HookDict(self, 4)
        super().__init__(image, ram_mb)

    @property
    def mmio_regs(self): return self._mmio
    @mmio_regs.setter
    def mmio_regs(self, v):
        if v is self._mmio: return
        for i in range(REG_WORDS): self._rflag[i] &= ~1 & 0xFF; self._regs[i] = 0
        self._mmio._d.clear()
        for k, x in dict(v).items(): self._mmio[k] = x
    @property
    def read_hooks(self): return self._rh
    @read_hooks.setter
    def read_hooks(self, v):
        if v is not self._rh: self._rh.clear(); self._rh.update(v)
    @property
    def write_hooks(self): return self._wh
    @write_hooks.setter
    def write_hooks(self, v):
        if v is not self._wh: self._wh.clear(); self._wh.update(v)

    # ---- state lives in the C struct
    def _arr_prop(name):
        def g(self): return getattr(self._c, name)
        def s(self, v): getattr(self._c, name)[:] = [x & 0xFFFFFFFF for x in v]
        return property(g, s)
    def _int_prop(name):
        def g(self): return getattr(self._c, name)
        def s(self, v): setattr(self._c, name, v & ((1 << 64) - 1) if name in ('n', 'nw', 'nr') else v & 0xFFFFFFFF)
        return property(g, s)
    r = _arr_prop('r'); cp0 = _arr_prop('cp0')
    hi = _int_prop('hi'); lo = _int_prop('lo'); pc = _int_prop('pc'); npc = _int_prop('npc'); cur_pc = _int_prop('cur_pc')
    n = _int_prop('n'); nw = _int_prop('nw'); nr = _int_prop('nr')
    del _arr_prop, _int_prop

    def _mem_prop(attr, field, endfield):
        def g(self): return getattr(self, attr)
        def s(self, v):
            cur = getattr(self, attr)
            if cur is None:
                buf = v if isinstance(v, bytearray) else bytearray(v)
                setattr(self, attr, buf)
                cb = (ctypes.c_char * len(buf)).from_buffer(buf); setattr(self, attr + '_cb', cb)
                setattr(self._c, field, ctypes.addressof(cb))
            else:
                if len(v) != len(cur): raise ValueError('memory size mismatch')
                cur[:] = v
        return property(g, s)
    ram = _mem_prop('_ram', 'ram', 'ram_end'); low = _mem_prop('_low', 'low', 'low_end')
    del _mem_prop

    @property
    def ram_end(self): return self._c.ram_end
    @ram_end.setter
    def ram_end(self, v): self._c.ram_end = v; self._c.ram_base = BASE
    @property
    def low_end(self): return self._c.low_end
    @low_end.setter
    def low_end(self, v): self._c.low_end = v

    @property
    def bp(self): return self._bp
    @bp.setter
    def bp(self, v):
        if getattr(self, '_bp', None) is None or not isinstance(v, _BPSet):
            old = getattr(self, '_bp', None)
            if old is not None: old.clear()
            self._bp = _BPSet(self, v)

    @property
    def irq_hook(self): return self._irq_hook
    @irq_hook.setter
    def irq_hook(self, f):
        self._irq_hook = f; self._c.has_irq = 1 if f is not None else 0; self._c.irq_next = self._c.n

    @property
    def irq_next(self): return self._c.irq_next
    @irq_next.setter
    def irq_next(self, v): self._c.irq_next = v

    # ---- fast peripherals (soc uses these when present)
    def fast_window(self, lo, hi, mem, on_capture):
        """A plain-memory window (mem: bytearray of hi-lo bytes) whose writes are also captured in order and handed
        to on_capture(bytes) in bulk."""
        c = self._c; self._win_mem = mem
        self._win_cb = (ctypes.c_char * len(mem)).from_buffer(mem)
        self._cap = bytearray(1 << 16); self._cap_cb = (ctypes.c_char * len(self._cap)).from_buffer(self._cap)
        c.win_lo, c.win_hi, c.win_mem = lo, hi, ctypes.addressof(self._win_cb)
        c.cap = ctypes.addressof(self._cap_cb); c.cap_len = 0; c.cap_size = len(self._cap)
        self._on_capture = on_capture
    def drain(self):
        c = self._c
        if c.cap and c.cap_len:
            data = bytes(self._cap[:c.cap_len]); c.cap_len = 0
            self._on_capture(data)
    def fast_timers(self, ipus):
        self._c.ipus = ipus; self._c.timers_on = 1
    def fast_cache(self, phys, value, until):
        """Answer 32-bit reads of phys with value until instruction count `until` (or the next MMIO write)."""
        c = self._c; c.cache_addr = phys; c.cache_val = value & 0xFFFFFFFF; c.cache_until = min(until, (1 << 64) - 1)
    def fast_computed(self, phys, kind, p=1, k=0, keep=0xFFFFFFFF, set_=0):
        """A status register computed in C: kind 1 = ((n % p) * k) // p, kind 2 = ((n // p) & 1) << k | (reg & keep),
        kind 3 = (reg & keep) | set_ (reg = the last value written, 0 if none). Replaces the Python read hook."""
        c = self._c; i = (phys - REG_BASE) >> 2
        if not (0 <= i < REG_WORDS) or c.ncomp >= 32: return False
        j = c.ncomp; c.comp_addr[j] = phys; c.comp_kind[j] = kind; c.comp_p[j] = max(1, p); c.comp_k[j] = k
        c.comp_keep[j] = keep & 0xFFFFFFFF; c.comp_set[j] = set_ & 0xFFFFFFFF; c.ncomp += 1
        self._rflag[i] |= 8
        return True
    def fast_sink(self, phys):
        c = self._c
        if c.nsink < 16: c.sink_addr[c.nsink] = phys; c.nsink += 1

    # ---- callbacks from C
    def _cb_mmio_r(self, p, size):
        try: return self._mmio_r(p, size) & 0xFFFFFFFF
        except BaseException as ex: self._exc = ex; self._c.abort = 1; self._c.irq_next = 0; return 0
    def _cb_mmio_w(self, p, v, size):
        try: self._mmio_w(p, v, size)
        except BaseException as ex: self._exc = ex; self._c.abort = 1; self._c.irq_next = 0
    def _cb_irq(self):
        try:
            h = self._irq_hook
            if h is not None: h(self)
            f = self.irq_next_fn
            return f(self) if f else self._c.n + 128
        except BaseException as ex:
            self._exc = ex; self._c.abort = 1; return self._c.n + 128

    # ---- execution
    def run(self, max_instr, spin_window=None):
        c = self._c; lib = _LIB; end = c.n + max_instr
        while c.n < end:
            rc = lib.cpu_run(ctypes.byref(c), end - c.n)
            if self._exc is not None:
                ex = self._exc; self._exc = None; c.abort = 0; raise ex
            if rc == 0: self.drain(); return
            if rc == 10: self.drain(); continue
            if rc == 1:
                self.drain()
                pc = c.pc
                if pc in self._bp:
                    h = self.hle.get(pc)
                    if h is not None:
                        h(self)
                        if self._exc is not None:
                            ex = self._exc; self._exc = None; c.abort = 0; raise ex
                        if c.pc != pc:
                            c.n += 1; c.r[0] = 0; continue
                    else:
                        self.bp_hits.append((c.n, pc))
                c.skip_bp = 1
                continue
            if rc == 6:
                c.abort = 0; continue
            self.drain()
            why = _REASONS.get(rc, 'stop %d' % rc)
            raise Stop(f'{why} (word {c.stop_word:08x}) at {c.pc:08x}' if rc != 5 else f'{why} {c.pc:08x}')


Emu = FastEmu if _load_lib() else PyEmu


def load(path='MP100NSP.dec.bin'):
    return open(path, 'rb').read()

if __name__ == '__main__':
    e = Emu(load())
    nmax = int(sys.argv[1]) if len(sys.argv) > 1 else 5_000_000
    try:
        e.run(nmax); why = 'instruction budget reached'
    except Stop as s: why = str(s)
    e.report(why)

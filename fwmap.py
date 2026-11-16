"""Find the same code in two builds of the Sunplus karaoke firmware (MP100NSP <-> other players on the same SDK).

The builds share most of their code but everything sits at different addresses, and global-variable offsets
(gp-relative), absolute addresses (lui/addiu pairs) and call targets differ. A code address is mapped by taking
the instructions around it, masking those fields, and searching the other image for a unique match.
"""
import struct, numpy as np

BASE = 0x88000000


def load(path_or_bytes):
    d = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, 'rb').read()
    n = len(d) // 4
    return np.frombuffer(bytes(d[:n * 4]), '<u4').copy()


def mask_words(w):
    """Masked copy of instruction words: what stays the same between builds."""
    w = np.asarray(w, dtype=np.uint32)
    op = w >> 26; rs = (w >> 21) & 31
    m = w.copy()
    jt = (op == 2) | (op == 3)                                   # j / jal: target differs
    m[jt] = w[jt] & 0xFC000000
    lui = op == 0x0F
    m[lui] = w[lui] & 0xFFFF0000
    itype = (op >= 4) & ~jt
    gp = itype & (rs == 28)                                      # gp-relative globals
    m[gp] = w[gp] & 0xFFFF0000
    # immediate of an addiu/ori/load/store that follows a lui of the same register (an absolute address)
    rt_prev = np.empty_like(w); rt_prev[0] = 99; rt_prev[1:] = (w[:-1] >> 16) & 31
    prev_lui = np.zeros(len(w), bool); prev_lui[1:] = lui[:-1]
    prev2_lui = np.zeros(len(w), bool); prev2_lui[2:] = lui[:-2]
    rt_prev2 = np.empty_like(w); rt_prev2[:2] = 99; rt_prev2[2:] = (w[:-2] >> 16) & 31
    absadr = itype & ((prev_lui & (rs == rt_prev)) | (prev2_lui & (rs == rt_prev2)))
    m[absadr] = w[absadr] & 0xFFFF0000
    return m


class Mapper:
    def __init__(self, ref, tgt, ref_base=BASE, tgt_base=BASE):
        self.rw = load(ref); self.tw = load(tgt)
        self.rm = mask_words(self.rw); self.tm = mask_words(self.tw)
        self.rb = ref_base; self.tb = tgt_base

    def find(self, addr, before=8, after=24, need=None):
        """Target address of the code at ref address `addr`, or None. Tries a window around it, widening if the
        match is not unique."""
        i = ((addr | 0x80000000) - (self.rb | 0x80000000)) // 4
        for b, a in ((before, after), (16, 40), (32, 64), (4, 12), (0, 16), (0, 8)):
            lo = max(0, i - b); pat = self.rm[lo:i + a]
            if len(pat) < 4: continue
            hits = self._search(pat)
            if len(hits) == 1:
                return (self.tb | 0x80000000) + (hits[0] + (i - lo)) * 4
            if len(hits) == 0 and b == 0:
                return None
        return None

    def _search(self, pat):
        t = self.tm; k = len(pat)
        cand = np.nonzero(t[:len(t) - k + 1] == pat[0])[0]
        for j in range(1, k):
            if len(cand) == 0: break
            cand = cand[t[cand + j] == pat[j]]
        return cand

    def imm_at(self, addr, image='tgt'):
        w = (self.tw if image == 'tgt' else self.rw)
        b = (self.tb if image == 'tgt' else self.rb) | 0x80000000
        return int(w[((addr | 0x80000000) - b) // 4])


def _abs_at(w, i):
    """Absolute address built by lui at i and an I-type using it within the next 3 words, or None."""
    x = int(w[i])
    if x >> 26 != 0x0F: return None
    rt = (x >> 16) & 31; hi = (x & 0xFFFF) << 16
    for j in range(i + 1, min(i + 4, len(w))):
        y = int(w[j]); op = y >> 26
        if op >= 8 and ((y >> 21) & 31) == rt and op != 0x0F:
            lo = y & 0xFFFF
            if op in (0x0C, 0x0D, 0x0E): return (hi | lo) & 0xFFFFFFFF          # andi/ori/xori: zero-extended
            return (hi + (lo - 0x10000 if lo & 0x8000 else lo)) & 0xFFFFFFFF
    return None


class Multi:
    """Several (ref, tgt) mappers: code in the reference may live in its flash image or a RAM module."""
    def __init__(self, mappers):
        self.ms = mappers

    def find(self, addr):
        for m in self.ms:
            lo = m.rb | 0x80000000; hi = lo + len(m.rw) * 4
            if lo <= (addr | 0x80000000) < hi:
                r = m.find(addr)
                if r: return r
        return None

    def gp_offsets(self, off):
        """Counter {target gp offset: votes} for an MP100NSP gp offset."""
        import collections
        c = collections.Counter()
        for m in self.ms:
            w = m.rw; op = w >> 26; rs = (w >> 21) & 31; imm = w & 0xFFFF
            for i in np.nonzero((rs == 28) & (imm == (off & 0xFFFF)) & (op >= 8) & (op != 0x0F))[0]:
                a = (m.rb | 0x80000000) + int(i) * 4
                r = m.find(a)
                if r:
                    t = m.imm_at(r)
                    if ((t >> 21) & 31) == 28: c[t & 0xFFFF] += 1
        return c

    def abs_addr(self, addr):
        """Counter {target address: votes} for an absolute (lui-built) address used in the reference code."""
        import collections
        c = collections.Counter()
        for m in self.ms:
            w = m.rw
            luis = np.nonzero((w >> 26) == 0x0F)[0]
            for i in luis:
                if (int(w[i]) & 0xFFFF) not in ((addr >> 16) & 0xFFFF, ((addr >> 16) + 1) & 0xFFFF): continue
                if _abs_at(w, int(i)) != addr: continue
                a = (m.rb | 0x80000000) + int(i) * 4
                r = m.find(a)
                if r:
                    j = (r - (m.tb | 0x80000000)) // 4
                    v = _abs_at(m.tw, j)
                    if v is not None: c[v] += 1
        return c

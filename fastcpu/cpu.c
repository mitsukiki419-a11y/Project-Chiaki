/* Fast MIPS I (little-endian) interpreter core for the MP100NSP emulator.
 * Same instruction semantics as emu.py's Python core (including this chip's 3-operand 16x16 mult/multu).
 * Memory: SDRAM at phys 0 (low), flash/RAM image at phys 0x08000000 (ram); everything else is MMIO and goes to
 * Python through callbacks. Breakpoints (HLE hooks) return to Python. Interrupts: Python's controller is called
 * when the instruction count reaches irq_next and returns the next count at which it wants to be called.
 * Build: cc -O2 -shared -fPIC -o libkecpu.so cpu.c   (Windows: zig cc -target x86_64-windows -O2 -shared)
 */
#include <stdint.h>
#include <string.h>
#include <math.h>

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

typedef uint32_t (*mmio_r_fn)(uint32_t p, int32_t size);
typedef void (*mmio_w_fn)(uint32_t p, uint32_t v, int32_t size);
typedef uint64_t (*irq_fn)(void);

typedef struct {
    uint32_t r[32];
    uint32_t hi, lo, pc, npc, cur_pc;
    uint32_t cp0[32];
    uint64_t n, nw, nr;
    uint64_t irq_next;
    uint8_t *low; uint32_t low_end;
    uint8_t *ram; uint32_t ram_base, ram_end;
    uint8_t *bpmap; uint32_t bpmap_bits;      /* bit per word of physical address space below bpmap_bits*4 */
    mmio_r_fn mmio_r; mmio_w_fn mmio_w; irq_fn irq_cb;
    int32_t has_irq;
    int32_t skip_bp;                           /* 1: do not stop at a breakpoint on the current pc once */
    int32_t abort;                             /* set by Python when a callback raised */
    uint32_t stop_word;                        /* instruction word for unimplemented-opcode stops */
    /* fast peripherals (optional) */
    uint32_t win_lo, win_hi; uint8_t *win_mem;  /* decoder data window: plain memory + write capture */
    uint8_t *cap; uint32_t cap_len, cap_size;
    int32_t timers_on; uint32_t ipus;           /* PB+0x80 / 0x90 / 0x94 free-running counters */
    uint32_t nsink; uint32_t sink_addr[16]; uint32_t sink_val[16];
    uint32_t cache_addr, cache_val; uint64_t cache_until;   /* a polled register Python answered ahead of time */
    /* plain register file for the peripheral block: read back what was written, unless Python hooks the word */
    uint32_t *regs; uint8_t *rflag; uint32_t reg_base, reg_words;   /* rflag: 1 written, 2 Python reads, 4 Python writes, 8 computed */
    /* computed status registers (functions of the instruction count and the stored value) */
    uint32_t ncomp; uint32_t comp_addr[32], comp_kind[32], comp_p[32], comp_k[32], comp_keep[32], comp_set[32];
} CPU;

#define PB 0x1FFE8000u
static inline int fast_r(CPU *c, uint32_t p, int size, uint32_t *out) {
    if (c->regs && p - c->reg_base < c->reg_words * 4) {
        uint32_t i = (p - c->reg_base) >> 2; uint8_t f = c->rflag[i];
        if (f & 8) {
            for (uint32_t k = 0; k < c->ncomp; k++) if (c->comp_addr[k] == (p & ~3u)) {
                uint32_t reg = (f & 1) ? c->regs[i] : 0, v;
                switch (c->comp_kind[k]) {
                case 1: v = (uint32_t)(((c->n % c->comp_p[k]) * c->comp_k[k]) / c->comp_p[k]); break;   /* field phase */
                case 2: v = ((uint32_t)((c->n / c->comp_p[k]) & 1) << c->comp_k[k]) | (reg & c->comp_keep[k]); break;
                default: v = (reg & c->comp_keep[k]) | c->comp_set[k]; break;                           /* forced bits */
                }
                if (size < 4) v = (v >> (8 * (p & 3))) & ((1u << (8 * size)) - 1);
                *out = v; return 1;
            }
        }
        if (!(f & 2) && !(c->cache_until > c->n && c->cache_addr == (p & ~3u))) {
            uint32_t v = (f & 1) ? c->regs[i] : 0;
            if (size < 4) v = (v >> (8 * (p & 3))) & ((1u << (8 * size)) - 1);
            *out = v; return 1;
        }
    }
    if (p >= c->win_lo && p + size <= c->win_hi) {
        uint32_t o = p - c->win_lo, v = 0; memcpy(&v, c->win_mem + o, size); *out = v; return 1;
    }
    if (c->timers_on && (p & ~3u) >= PB + 0x80 && (p & ~3u) <= PB + 0x94) {
        uint32_t us = (uint32_t)(c->n / c->ipus), v; uint32_t a = p & ~3u;
        if (a == PB + 0x80) v = (us / 10) & 0xFFFF;
        else if (a == PB + 0x90) v = us & 0xFFFF;
        else if (a == PB + 0x94) v = (us >> 16) & 0xFFFF;
        else return 0;
        if (size < 4) v = (v >> (8 * (p & 3))) & ((1u << (8 * size)) - 1);
        *out = v; return 1;
    }
    if (c->cache_until > c->n && c->cache_addr == p && size == 4) { *out = c->cache_val; return 1; }
    for (uint32_t i = 0; i < c->nsink; i++) if (c->sink_addr[i] == (p & ~3u)) {
        uint32_t v = c->sink_val[i]; if (size < 4) v = (v >> (8 * (p & 3))) & ((1u << (8 * size)) - 1); *out = v; return 1; }
    return 0;
}
static inline int fast_w(CPU *c, uint32_t p, uint32_t v, int size) {
    if (c->regs && p - c->reg_base < c->reg_words * 4) {
        uint32_t i = (p - c->reg_base) >> 2;
        if (!(c->rflag[i] & 4)) {
            if (size == 4) { c->regs[i] = v; c->rflag[i] |= 1; }     /* (sub-word writes are not stored, as in Python) */
            if (((p ^ c->cache_addr) & ~0x3Fu) == 0) c->cache_until = 0;   /* e.g. the stream consumer index */
            return 1;
        }
    }
    if (p >= c->win_lo && p + size <= c->win_hi) {
        memcpy(c->win_mem + (p - c->win_lo), &v, size);
        if (c->cap) { if (c->cap_len + size <= c->cap_size) { memcpy(c->cap + c->cap_len, &v, size); c->cap_len += size; }
                      if (c->cap_len + 64 > c->cap_size) c->irq_next = c->n; }
        return 1;
    }
    if (size == 4) for (uint32_t i = 0; i < c->nsink; i++) if (c->sink_addr[i] == p) { c->sink_val[i] = v; return 1; }
    return 0;
}

enum { R_CAP = 10, R_BUDGET = 0, R_BP = 1, R_SYSCALL = 2, R_BREAK = 3, R_UNIMPL = 4, R_FETCH = 5, R_ABORT = 6, R_REGIMM = 7, R_COP0 = 8, R_SPECIAL = 9 };

static inline uint32_t ld32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static inline uint16_t ld16(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }
static inline void st32(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }
static inline void st16(uint8_t *p, uint16_t v) { memcpy(p, &v, 2); }

static inline uint32_t rd32(CPU *c, uint32_t a) {
    uint32_t p = a & 0x1FFFFFFF;
    if (p + 4 <= c->low_end) return ld32(c->low + p);
    if (p >= c->ram_base && p + 4 <= c->ram_end) return ld32(c->ram + (p - c->ram_base));
    { uint32_t v; if (fast_r(c, p, 4, &v)) return v; }
    c->nr++; return c->mmio_r(p, 4);
}
static inline uint32_t rd16(CPU *c, uint32_t a) {
    uint32_t p = a & 0x1FFFFFFF;
    if (p + 2 <= c->low_end) return ld16(c->low + p);
    if (p >= c->ram_base && p + 2 <= c->ram_end) return ld16(c->ram + (p - c->ram_base));
    { uint32_t v; if (fast_r(c, p, 2, &v)) return v; }
    c->nr++; return c->mmio_r(p, 2) & 0xFFFF;
}
static inline uint32_t rd8(CPU *c, uint32_t a) {
    uint32_t p = a & 0x1FFFFFFF;
    if (p < c->low_end) return c->low[p];
    if (p >= c->ram_base && p < c->ram_end) return c->ram[p - c->ram_base];
    { uint32_t v; if (fast_r(c, p, 1, &v)) return v; }
    c->nr++; return c->mmio_r(p, 1) & 0xFF;
}
static inline void wr32(CPU *c, uint32_t a, uint32_t v) {
    uint32_t p = a & 0x1FFFFFFF; c->nw++;
    if (p + 4 <= c->low_end) { st32(c->low + p, v); return; }
    if (p >= c->ram_base && p + 4 <= c->ram_end) { st32(c->ram + (p - c->ram_base), v); return; }
    if (fast_w(c, p, v, 4)) return;
    c->cache_until = 0;
    c->mmio_w(p, v, 4);
}
static inline void wr16(CPU *c, uint32_t a, uint32_t v) {
    uint32_t p = a & 0x1FFFFFFF; c->nw++;
    if (p + 2 <= c->low_end) { st16(c->low + p, (uint16_t)v); return; }
    if (p >= c->ram_base && p + 2 <= c->ram_end) { st16(c->ram + (p - c->ram_base), (uint16_t)v); return; }
    if (fast_w(c, p, v & 0xFFFF, 2)) return;
    c->cache_until = 0;
    c->mmio_w(p, v & 0xFFFF, 2);
}
static inline void wr8(CPU *c, uint32_t a, uint32_t v) {
    uint32_t p = a & 0x1FFFFFFF; c->nw++;
    if (p < c->low_end) { c->low[p] = (uint8_t)v; return; }
    if (p >= c->ram_base && p < c->ram_end) { c->ram[p - c->ram_base] = (uint8_t)v; return; }
    if (fast_w(c, p, v & 0xFF, 1)) return;
    c->cache_until = 0;
    c->mmio_w(p, v & 0xFF, 1);
}

/* slow paths (MMIO): the caller has stored its local state into c first */
static uint32_t rd_slow(CPU *c, uint32_t p, int size) {
    uint32_t v; if (fast_r(c, p, size, &v)) return v;
    c->nr++; v = c->mmio_r(p, size);
    return size == 4 ? v : size == 2 ? (v & 0xFFFF) : (v & 0xFF);
}
static void wr_slow(CPU *c, uint32_t p, uint32_t v, int size) {
    if (fast_w(c, p, v, size)) return;
    if (((p ^ c->cache_addr) & ~0x3Fu) == 0) c->cache_until = 0;   /* only a write near the cached register */
    c->mmio_w(p, v, size);
}

static inline int32_t S(uint32_t x) { return (int32_t)x; }

EXPORT int32_t cpu_struct_size(void) { return (int32_t)sizeof(CPU); }

EXPORT int32_t cpu_run(CPU *c, uint64_t max_instr) {
    /* The hot state (instruction count, pc, npc) lives in locals and is stored into c only around anything that can
       look at it: MMIO callbacks, the interrupt callback and every return. Same behaviour as the earlier version,
       which kept them in c (memory) on every instruction. */
    uint64_t n = c->n; const uint64_t end = n + max_instr;
    uint32_t pcl = c->pc, npcl = c->npc;
    uint32_t *r = c->r;
    uint8_t *const low = c->low; const uint32_t low_end = c->low_end;
    uint8_t *const ram = c->ram; const uint32_t ram_base = c->ram_base, ram_end = c->ram_end;
    const uint8_t *const bpmap = c->bpmap; const uint32_t bpbits = c->bpmap_bits;
    uint32_t pc = pcl;
#define OUT() (c->n = n, c->pc = pcl, c->npc = npcl, c->cur_pc = pc)
#define IN()  (n = c->n, pcl = c->pc, npcl = c->npc)
#define RET(code) do { OUT(); return (code); } while (0)
#define STOPHERE(code) do { pcl = pc; npcl = npc; n--; c->stop_word = w; RET(code); } while (0)
#define RD(a, sz, ld) ({ uint32_t _p = (a) & 0x1FFFFFFF, _v; \
        if (_p + (sz) <= low_end) _v = ld(low + _p); \
        else if (_p >= ram_base && _p + (sz) <= ram_end) _v = ld(ram + (_p - ram_base)); \
        else { OUT(); _v = rd_slow(c, _p, (sz)); IN(); } _v; })
#define WR(a, val, sz, st) do { uint32_t _p = (a) & 0x1FFFFFFF; c->nw++; \
        if (_p + (sz) <= low_end) st(low + _p, val); \
        else if (_p >= ram_base && _p + (sz) <= ram_end) st(ram + (_p - ram_base), val); \
        else { OUT(); wr_slow(c, _p, (val), (sz)); IN(); } } while (0)
#define LD8(q) (*(q))
#define ST8(q, v) (*(q) = (uint8_t)(v))
#define ST16(q, v) st16((q), (uint16_t)(v))
    while (n < end) {
        if (n >= c->irq_next) {                     /* one compare per instruction: every slow check hangs off it */
            OUT();
            if (c->abort) return R_ABORT;
            if (c->cap && c->cap_len + 64 > c->cap_size) { c->irq_next = c->n; return R_CAP; }
            if (c->has_irq && c->npc == c->pc + 4) {
                c->irq_next = c->irq_cb();
                if (c->abort) { return R_ABORT; }
            } else c->irq_next = c->n + 1;
            if (c->cap && c->cap_len + 64 > c->cap_size) c->irq_next = c->n;
            IN();
        }
        pc = pcl;
        uint32_t p = pc & 0x1FFFFFFF;
        if ((p >> 2) < bpbits && (bpmap[p >> 5] >> ((p >> 2) & 7) & 1)) {
            if (c->skip_bp) c->skip_bp = 0;
            else RET(R_BP);
        }
        uint32_t w;
        if (p + 4 <= low_end) w = ld32(low + p);
        else if (p >= ram_base && p + 4 <= ram_end) w = ld32(ram + (p - ram_base));
        else RET(R_FETCH);
        uint32_t npc = npcl;
        pcl = npc; npcl = npc + 4;
        uint32_t op = w >> 26, rs = (w >> 21) & 31, rt = (w >> 16) & 31, im = w & 0xFFFF;
        n++;
        if (op == 9 && im == 0xFFFF && rs == rt && rt) {               /* busy-wait 'addiu r,r,-1 / bne' loops */
            if (p >= ram_base && p + 16 <= ram_end) {
                const uint8_t *q = ram + (p - ram_base);
                uint32_t bne = 0x14000000u | (rt << 21), ln = 0;
                if (ld32(q + 4) == (bne | 0xFFFE) && ld32(q + 8) == 0) ln = 3;
                else if (ld32(q + 4) == 0 && ld32(q + 8) == (bne | 0xFFFD) && ld32(q + 12) == 0) ln = 4;
                if (ln && r[rt]) {
                    n += (uint64_t)r[rt] * ln; r[rt] = 0;
                    pcl = pc + ln * 4; npcl = pcl + 4; continue;
                }
            }
        }
        int32_t si = (int16_t)im;
        uint32_t v;
        switch (op) {
        case 0: {
            uint32_t fn = w & 63, rd = (w >> 11) & 31, sh = (w >> 6) & 31;
            switch (fn) {
            case 0: v = r[rt] << sh; break;
            case 2: v = r[rt] >> sh; break;
            case 3: v = (uint32_t)(S(r[rt]) >> sh); break;
            case 4: v = r[rt] << (r[rs] & 31); break;
            case 6: v = r[rt] >> (r[rs] & 31); break;
            case 7: v = (uint32_t)(S(r[rt]) >> (r[rs] & 31)); break;
            case 8: npcl = r[rs]; r[0] = 0; continue;
            case 9: { uint32_t t = r[rs]; r[rd] = pc + 8; npcl = t; r[0] = 0; continue; }
            case 12: pcl = pc; npcl = npc; n--; RET(R_SYSCALL);
            case 13: pcl = pc; npcl = npc; n--; RET(R_BREAK);
            case 16: v = c->hi; break;
            case 17: c->hi = r[rs]; continue;
            case 18: v = c->lo; break;
            case 19: c->lo = r[rs]; continue;
            case 24:
                if (rd) { int64_t x = (int64_t)(int16_t)(r[rs] & 0xFFFF) * (int16_t)(r[rt] & 0xFFFF);
                          c->lo = (uint32_t)x; c->hi = (uint32_t)((uint64_t)x >> 32); r[rd] = c->lo; }
                else { int64_t x = (int64_t)S(r[rs]) * S(r[rt]); c->lo = (uint32_t)x; c->hi = (uint32_t)((uint64_t)x >> 32); }
                r[0] = 0; continue;
            case 25:
                if (rd) { uint32_t x = (r[rs] & 0xFFFF) * (r[rt] & 0xFFFF); c->lo = x; c->hi = 0; r[rd] = x; }
                else { uint64_t x = (uint64_t)r[rs] * r[rt]; c->lo = (uint32_t)x; c->hi = (uint32_t)(x >> 32); }
                r[0] = 0; continue;
            case 26: {
                int32_t a = S(r[rs]), b = S(r[rt]);
                if (b) {
                    if (a == INT32_MIN && b == -1) { c->lo = 0x80000000u; c->hi = 0; }
                    else { c->lo = (uint32_t)(a / b); c->hi = (uint32_t)(a - (a / b) * b); }
                }
                continue; }
            case 27: if (r[rt]) { c->lo = r[rs] / r[rt]; c->hi = r[rs] % r[rt]; } continue;
            case 32: case 33: v = r[rs] + r[rt]; break;
            case 34: case 35: v = r[rs] - r[rt]; break;
            case 36: v = r[rs] & r[rt]; break;
            case 37: v = r[rs] | r[rt]; break;
            case 38: v = r[rs] ^ r[rt]; break;
            case 39: v = ~(r[rs] | r[rt]); break;
            case 42: v = S(r[rs]) < S(r[rt]); break;
            case 43: v = r[rs] < r[rt]; break;
            default: STOPHERE(R_SPECIAL);
            }
            if (rd) r[rd] = v;
            continue; }
        case 15: if (rt) r[rt] = im << 16; break;
        case 8: case 9: if (rt) r[rt] = r[rs] + (uint32_t)si; break;
        case 13: if (rt) r[rt] = r[rs] | im; break;
        case 12: if (rt) r[rt] = r[rs] & im; break;
        case 14: if (rt) r[rt] = r[rs] ^ im; break;
        case 10: if (rt) r[rt] = S(r[rs]) < si; break;
        case 11: if (rt) r[rt] = r[rs] < (uint32_t)si; break;
        case 35: v = RD(r[rs] + (uint32_t)si, 4, ld32); if (rt) r[rt] = v; break;
        case 43: WR(r[rs] + (uint32_t)si, r[rt], 4, st32); break;
        case 36: v = RD(r[rs] + (uint32_t)si, 1, LD8); if (rt) r[rt] = v; break;
        case 32: v = RD(r[rs] + (uint32_t)si, 1, LD8); if (rt) r[rt] = (uint32_t)(int32_t)(int8_t)v; break;
        case 37: v = RD(r[rs] + (uint32_t)si, 2, ld16); if (rt) r[rt] = v; break;
        case 33: v = RD(r[rs] + (uint32_t)si, 2, ld16); if (rt) r[rt] = (uint32_t)(int32_t)(int16_t)v; break;
        case 40: WR(r[rs] + (uint32_t)si, r[rt] & 0xFF, 1, ST8); break;
        case 41: WR(r[rs] + (uint32_t)si, r[rt] & 0xFFFF, 2, ST16); break;
        case 4: if (r[rs] == r[rt]) npcl = npc + ((uint32_t)si << 2); break;
        case 5: if (r[rs] != r[rt]) npcl = npc + ((uint32_t)si << 2); break;
        case 6: if (S(r[rs]) <= 0) npcl = npc + ((uint32_t)si << 2); break;
        case 7: if (S(r[rs]) > 0) npcl = npc + ((uint32_t)si << 2); break;
        case 1: {
            int32_t x = S(r[rs]); int cnd;
            if (rt == 0) cnd = x < 0;
            else if (rt == 1) cnd = x >= 0;
            else if (rt == 16) { cnd = x < 0; r[31] = pc + 8; }
            else if (rt == 17) { cnd = x >= 0; r[31] = pc + 8; }
            else STOPHERE(R_REGIMM);
            if (cnd) npcl = npc + ((uint32_t)si << 2);
            break; }
        case 2: npcl = (npc & 0xF0000000u) | ((w & 0x3FFFFFF) << 2); break;
        case 3: r[31] = pc + 8; npcl = (npc & 0xF0000000u) | ((w & 0x3FFFFFF) << 2); break;
        case 34: {  /* lwl */
            uint32_t a = r[rs] + (uint32_t)si, al = a & ~3u, k = a & 3;
            uint32_t m = RD(al, 4, ld32), o = r[rt];
            uint32_t sh = 8 * (3 - k);
            uint32_t lowmask = (uint32_t)(((uint64_t)1 << sh) - 1);
            v = (o & lowmask) | (uint32_t)((uint64_t)m << sh);
            if (rt) r[rt] = v;
            break; }
        case 38: {  /* lwr */
            uint32_t a = r[rs] + (uint32_t)si, al = a & ~3u, k = a & 3;
            uint32_t m = RD(al, 4, ld32), o = r[rt];
            if (k == 0) v = m;
            else v = (m >> (8 * k)) | (o & (uint32_t)(((uint64_t)0xFFFFFFFFu << (8 * (4 - k))) & 0xFFFFFFFFu));
            if (rt) r[rt] = v;
            break; }
        case 42: {  /* swl */
            uint32_t a = r[rs] + (uint32_t)si, al = a & ~3u, k = a & 3;
            uint32_t m = RD(al, 4, ld32), val = r[rt];
            uint32_t mask = (uint32_t)(((uint64_t)1 << (8 * (k + 1))) - 1);
            WR(al, (m & ~mask) | (val >> (8 * (3 - k))), 4, st32);
            break; }
        case 46: {  /* swr */
            uint32_t a = r[rs] + (uint32_t)si, al = a & ~3u, k = a & 3;
            uint32_t m = RD(al, 4, ld32), val = r[rt];
            uint32_t mask = (uint32_t)(((uint64_t)0xFFFFFFFFu << (8 * k)) & 0xFFFFFFFFu);
            WR(al, (m & ~mask) | (uint32_t)(((uint64_t)val << (8 * k)) & 0xFFFFFFFFu), 4, st32);
            break; }
        case 16: {
            uint32_t rd = (w >> 11) & 31;
            if (rs == 0) { if (rt) r[rt] = c->cp0[rd]; }
            else if (rs == 4) { c->cp0[rd] = r[rt]; if (rd == 12 || rd == 13) c->irq_next = n; }
            else if (rs == 16 && (w & 63) == 16) { uint32_t sr = c->cp0[12]; c->cp0[12] = (sr & ~0xFu) | ((sr >> 2) & 0xF); c->irq_next = n; }
            else STOPHERE(R_COP0);
            break; }
        default:
            STOPHERE(R_UNIMPL);
        }
        r[0] = 0;
    }
    RET(R_BUDGET);
}

/* ---- helper for the built-in SoundFont synth (livesynth.py): a biquad filter over one block, in place.
   c = {b0, b1, b2, a1, a2} (normalised, a0 = 1); z = 2 floats of state (direct form II transposed). */
EXPORT void synth_biquad(float *x, int32_t n, const double *c, double *z) {
    double b0 = c[0], b1 = c[1], b2 = c[2], a1 = c[3], a2 = c[4], z1 = z[0], z2 = z[1];
    for (int32_t i = 0; i < n; i++) {
        double in = x[i], out = b0 * in + z1;
        z1 = b1 * in - a1 * out + z2;
        z2 = b2 * in - a2 * out;
        x[i] = (float)out;
    }
    z[0] = z1; z[1] = z2;
}

/* ---- one SoundFont voice for one block, added into a stereo mix (livesynth.py's render, in C).
   smp: all samples (float), out: interleaved stereo float, accumulated. st = {pos, t, lvl} (updated).
   p = {step, end, ls, le, looped, dly, att, hold, dec, sus, rel, rel_t (<0: not released), rel_lvl, amp, gl, gr,
        rate, filt (0/1), b0, b1, b2, a1, a2}; z = 2 doubles of filter state.
   Returns the number of samples produced; less than n means the voice has ended. */
EXPORT int32_t synth_voice(const float *smp, int64_t nsmp, float *out, int32_t n, double *st, const double *p, double *z) {
    double pos = st[0], t = st[1], lvl = st[2];
    const double step = p[0], end = p[1], ls = p[2], le = p[3]; const int looped = p[4] != 0.0;
    const double dly = p[5], att = p[6], hold = p[7], dec = p[8], sus = p[9], rel = p[10], rel_t = p[11], rel_lvl = p[12];
    const double amp = p[13], gl = p[14], gr = p[15], dt = 1.0 / p[16];
    const int filt = p[17] != 0.0; const double b0 = p[18], b1 = p[19], b2 = p[20], a1 = p[21], a2 = p[22];
    double z1 = z[0], z2 = z[1];
    const double a1e = dly + att, h1 = a1e + hold, d1 = h1 + dec, loop_len = le - ls;
    int32_t i = 0;
    for (; i < n; i++) {
        if (looped) { if (pos >= le && loop_len > 0) pos = ls + fmod(pos - ls, loop_len); }
        else if (pos >= end - 1) break;
        int64_t i0 = (int64_t)pos; if (i0 < 0) i0 = 0; if (i0 + 1 >= nsmp) break;
        double fr = pos - (double)i0;
        double w = smp[i0] * (1.0 - fr) + smp[i0 + 1] * fr;
        double env;
        if (rel_t >= 0.0) { env = rel_lvl * (1.0 - (t - rel_t) / rel); if (env <= 0.0) { lvl = 0.0; break; } }
        else if (t < dly) env = 0.0;
        else if (t < a1e) env = (t - dly) / (att > 1e-9 ? att : 1e-9);
        else if (t < h1) env = 1.0;
        else if (t < d1) env = 1.0 - (1.0 - sus) * (t - h1) / (dec > 1e-9 ? dec : 1e-9);
        else env = sus;
        lvl = env;
        double s = w * env * amp;
        if (filt) { double o = b0 * s + z1; z1 = b1 * s - a1 * o + z2; z2 = b2 * s - a2 * o; s = o; }
        out[2 * i] += (float)(s * gl); out[2 * i + 1] += (float)(s * gr);
        pos += step; t += dt;
    }
    st[0] = pos; st[1] = t; st[2] = lvl; z[0] = z1; z[1] = z2;
    return i;
}

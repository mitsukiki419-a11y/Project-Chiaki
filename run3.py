import sys, json, struct
from emu import Emu, Stop, load
import soc
from mipsdis import dis

def analyze_poll(e):
    """Decode the polling loop from the instruction ring -> (phys_addr, mask, want_clear) or None."""
    ring = list(e.ring)[-14:]
    ws = [w for _, w in ring]
    for i, w in enumerate(ws):
        op = w >> 26
        if op in (35, 36, 37):               # lw/lbu/lhu
            rs, rt, off = (w >> 21) & 31, (w >> 16) & 31, w & 0xFFFF
            off = off - 0x10000 if off & 0x8000 else off
            addr = (e.r[rs] + off) & 0xFFFFFFFF; p = addr & 0x1FFFFFFF
            if BASE_RAM(e, p): continue
            reg, mask, shift = rt, 0xFFFFFFFF, 0
            for w2 in ws[i + 1:]:
                if w2 >> 26 == 0 and (w2 & 63) in (2, 3) and (w2 >> 16) & 31 == reg:   # srl/sra
                    shift += (w2 >> 6) & 31; reg = (w2 >> 11) & 31
                elif w2 >> 26 == 12 and (w2 >> 21) & 31 == reg:      # andi
                    mask = (w2 & 0xFFFF) << shift; reg = (w2 >> 16) & 31
                elif w2 >> 26 in (4, 5) and ((w2 >> 21) & 31 == reg or (w2 >> 16) & 31 == reg):
                    other = (w2 >> 16) & 31 if (w2 >> 21) & 31 == reg else (w2 >> 21) & 31
                    if other != 0: return None
                    wants_clear = (w2 >> 26 == 5)      # bne reg,zero,loop -> loop while nonzero -> need zero
                    return p, mask, wants_clear
    return None
def BASE_RAM(e, p): return p < e.low_end or 0x08000000 <= p < e.ram_end

def forced_hook(p, cm, sm):
    def h(e):
        v = e.mmio_regs.get(p & ~3, 0)
        return (v & ~cm) | sm
    return h

def boot(max_total, patches, verbose=True):
    e = Emu(load()); u = soc.attach(e, echo=False)
    for k, (cm, sm) in patches.items():
        e.read_hooks[int(k)] = forced_hook(int(k), cm, sm)
    return e, u

if __name__ == '__main__':
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 300_000_000
    max_patches = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    try: patches = {int(k): tuple(v) for k, v in json.load(open('autopatch.json')).items()}
    except Exception: patches = {}
    e, u = boot(budget, patches)
    why = None
    while True:
        try:
            while e.n < budget:
                e.run(min(25_000_000, budget - e.n))
                print(f'  .. {e.n:,} instrs  pc={e.cur_pc:08x}  uart_bytes={len(u.out)}  mmio_regs={len(e.mmio_log)}', flush=True)
            why = 'budget reached'; break
        except Stop as s:
            why = str(s)
            if not why.startswith('spin'): break
            r = analyze_poll(e)
            if not r: break
            p, mask, clear = r
            cm, sm = patches.get(p & ~3, (0, 0))
            if (clear and mask & cm == mask) or (not clear and mask & sm == mask):
                why = f'spin persists even with hook on {p:08x} (mask {mask:#x}, clear={clear})'; break
            if clear: cm |= mask; sm &= ~mask
            else: sm |= mask; cm &= ~mask
            if len(patches) >= max_patches and (p & ~3) not in patches: break
            patches[p & ~3] = (cm, sm)
            e.read_hooks[p & ~3] = forced_hook(p & ~3, cm, sm)
            print(f'  auto-hook: reg {p & ~3:08x} mask {mask:#06x} -> loop wants bits {"CLEAR" if clear else "SET"}   (pc {e.cur_pc:08x}, instr #{e.n:,})')
            e.ring.clear()
    json.dump({str(k): list(v) for k, v in patches.items()}, open('autopatch.json', 'w'))
    txt = bytes(u.out).decode('latin1').replace('\r', '')
    open('uart.txt', 'w').write(txt)
    print(f'[{e.n:,} instrs] stop: {why}   pc={e.cur_pc:08x}')
    lines = [l for l in txt.split('\n') if l.strip()]
    print('UART lines:', len(lines), ' last 10:', lines[-10:])
    print('last instrs:'); [print(f'   {pc:08x}: {dis(w,pc)}') for pc, w in list(e.ring)[-8:]]
    rc = sorted(((c, p) for (p, k), (c, f, l) in e.mmio_log.items() if k == 'R'), reverse=True)[:5]
    print('hot reads:', [(hex(p), c) for c, p in rc], ' unique MMIO regs:', len(e.mmio_log))
    if len(sys.argv) > 3:
        lo = int(sys.argv[3], 0); n = int(sys.argv[4], 0) if len(sys.argv) > 4 else 40
        print(f'--- live disassembly @ {lo:08x}')
        for i in range(n):
            a = lo + 4*i; w = e.rd32(a); print(f'   {a:08x}: {w:08x}  {dis(w, a)}')

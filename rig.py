"""Full emulation rig + snapshot support (state of CPU, RAM, peripherals)."""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emu import Emu, Stop, load
import soc
EF = ['ram','low','r','hi','lo','pc','npc','cp0','n','nw','nr','_nw0','_nr0','mmio_regs','skipcache','stream_state']
def build(fw='MP100NSP.dec.bin', sources=None):
    e = Emu(load(fw)); u = soc.attach(e, echo=False)
    irq = soc.IRQC(e, sources or soc.DEFAULT_SOURCES); fifo = soc.VideoFIFO(e)
    return e, u, irq, fifo
def save(e, u, irq, fifo, path):
    d = {f: (list(getattr(e, f)) if f in ('r', 'cp0') else dict(getattr(e, f)) if f == 'mmio_regs' else getattr(e, f, None)) for f in EF}
    d['_irq'] = (irq.pending, irq.next, irq.delivered)
    d['_irq2'] = (irq.pending2, irq.next2, irq.delivered2)
    d['_fifo'] = (bytes(fifo.buf), fifo.pos, list(fifo.frames), bytes(fifo.mem), getattr(fifo, 'open_i', None))
    d['_tim'] = e.timers.latched; d['_uart'] = bytes(u.out)
    d['_flash'] = (e.flash.wel, e.flash.status, list(e.flash.log))
    d['_servo'] = (dict(e.servo.regs), e.servo.cmd)
    pickle.dump(d, open(path, 'wb'))
def restore(path, fw='MP100NSP.dec.bin', sources=None):
    e, u, irq, fifo = build(fw, sources)
    d = pickle.load(open(path, 'rb'))
    for f in EF:
        if d.get(f) is not None: setattr(e, f, d[f])
    irq.pending, irq.next, irq.delivered = d['_irq']
    for b, p in irq.sources.items(): irq.next.setdefault(b, e.n + p)
    if '_irq2' in d: irq.pending2, irq.next2, irq.delivered2 = d['_irq2']
    else: irq.next2 = {b: e.n + p for b, p in irq.sources2.items()}
    b, p, fr, mem, oi = d['_fifo']; fifo.buf = bytearray(b); fifo.pos = p; fifo.frames = fr; fifo.mem = bytearray(mem); fifo.open_i = oi
    e.timers.latched = d['_tim']; u.out = bytearray(d['_uart'])
    e.flash.wel, e.flash.status, e.flash.log = d['_flash']
    e.servo.regs, e.servo.cmd = dict(d['_servo'][0]), d['_servo'][1]
    e.cur_pc = e.pc; e.dc.clear()
    return e, u, irq, fifo

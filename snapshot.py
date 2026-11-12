"""Quick boot: save / restore the complete emulator state (CPU, RAM, peripherals) to a compressed file.

The state is taken the moment the firmware reaches its main loop. Up to that point it has not polled the disc drive
yet, so one snapshot serves every disc (the virtual drive is attached after restoring). The file is keyed by a hash
of the firmware image and STATE_VERSION, so a different firmware or a changed emulator never loads a stale state."""
import hashlib, pickle, zlib, os

STATE_VERSION = 'ke-state-5'
EF = ['ram', 'low', 'r', 'hi', 'lo', 'pc', 'npc', 'cp0', 'n', 'nw', 'nr', '_nw0', '_nr0', 'mmio_regs', 'skipcache', 'stream_state']


def key_for(image):
    return hashlib.sha1(STATE_VERSION.encode() + bytes(image)).hexdigest()[:16]


def save(path, key, e, u, irq, fifo, extra=None):
    d = {f: (list(getattr(e, f)) if f in ('r', 'cp0') else dict(getattr(e, f)) if f == 'mmio_regs' else getattr(e, f, None)) for f in EF}
    d['ram'] = bytes(e.ram); d['low'] = bytes(e.low)
    d['_irq'] = (irq.pending, irq.next, irq.delivered, irq.pending2, irq.next2, irq.delivered2)
    d['_fifo'] = (bytes(fifo.buf), fifo.pos, list(fifo.frames), bytes(fifo.mem), getattr(fifo, 'open_i', None))
    d['_tim'] = e.timers.latched; d['_uart'] = bytes(u.out)
    d['_flash'] = (e.flash.wel, e.flash.status)
    d['_servo'] = (dict(e.servo.regs), e.servo.cmd)
    d['_i2c'] = dict(e.i2c.mem)
    d['_dbg'] = list(e.dbg.log[-200:]), e.dbg.count
    d['_extra'] = extra or {}
    blob = zlib.compress(pickle.dumps(d, protocol=4), 3)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f: f.write(b'KESTATE1' + key.encode() + b'\n' + blob)
    os.replace(tmp, path)


def load(path, key, e, u, irq, fifo):
    """Restore into freshly built objects. Returns the 'extra' dict, or None if the file does not match."""
    try:
        raw = open(path, 'rb').read()
    except OSError:
        return None
    if not raw.startswith(b'KESTATE1'): return None
    hdr, _, blob = raw[8:].partition(b'\n')
    if hdr.decode(errors='replace') != key: return None
    d = pickle.loads(zlib.decompress(blob))
    for f in EF:
        v = d.get(f)
        if v is None: continue
        if f in ('ram', 'low'): getattr(e, f)[:] = v
        else: setattr(e, f, v)
    (irq.pending, irq.next, irq.delivered, irq.pending2, irq.next2, irq.delivered2) = d['_irq']
    for b, p in irq.sources.items(): irq.next.setdefault(b, e.n + p)
    b, p, fr, mem, oi = d['_fifo']; fifo.buf = bytearray(b); fifo.pos = p; fifo.frames = fr; fifo.mem = bytearray(mem); fifo.open_i = oi
    e.timers.latched = d['_tim']; u.out = bytearray(d['_uart'])
    e.flash.wel, e.flash.status = d['_flash']
    e.servo.regs, e.servo.cmd = dict(d['_servo'][0]), d['_servo'][1]
    e.i2c.mem = dict(d['_i2c'])
    e.dbg.log, e.dbg.count = list(d['_dbg'][0]), d['_dbg'][1]
    e.cur_pc = e.pc; e.dc.clear(); e.ring.clear(); e.bp_hits = []
    return d['_extra']

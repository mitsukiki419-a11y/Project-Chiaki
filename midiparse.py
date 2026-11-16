def parse_track(b, limit=None):
    """Lenient SMF track event parser. Returns (events, stop_pos, reason)."""
    i=0; run=None; ev=[]; t=0
    def vlq():
        nonlocal i
        v=0
        for _ in range(4):
            c=b[i]; i+=1; v=(v<<7)|(c&0x7f)
            if not c&0x80: return v
        raise ValueError('vlq')
    try:
        while i<len(b):
            st=i; dt=vlq(); t+=dt
            s=b[i]
            if s&0x80: i+=1; 
            else:
                if run is None: return ev,st,'data without status'
                s=run
            if s==0xff:
                typ=b[i]; i+=1; ln=vlq(); data=b[i:i+ln]; i+=ln; ev.append((t,'meta',typ,data))
                if typ==0x2f: return ev,i,'EOT'
            elif s in (0xf0,0xf7):
                ln=vlq(); data=b[i:i+ln]; i+=ln; ev.append((t,'sysex',data))
            elif 0x80<=s<0xf0:
                run=s; n=2 if (s&0xf0) not in (0xc0,0xd0) else 1
                data=b[i:i+n]; i+=n
                if any(x&0x80 for x in data): return ev,st,'bad data byte'
                ev.append((t,s,bytes(data)))
            else: return ev,st,'bad status %02x'%s
    except (IndexError,ValueError) as ex: return ev,i,'eof '+str(ex)
    return ev,i,'end'


def parse_track_resync(b, max_skip=64):
    """Like parse_track, but on an undecodable byte skips ahead and resynchronises. Returns (events, skipped)."""
    pos = 0; out = []; skipped = 0; t_off = 0
    while pos < len(b):
        ev, stop, why = parse_track(b[pos:])
        out += [(t + t_off,) + tuple(x[1:]) for (t, *rest) in [(e[0], *e[1:]) for e in ev] for x in [(t, *rest)]]
        if ev: t_off = out[-1][0]
        if why == 'EOT' or why.startswith('eof') or why == 'end': break
        pos += stop + 1; skipped += 1
        if skipped > 4000: break
    return out, skipped

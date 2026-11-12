#!/usr/bin/env python3
"""Descramble / rescramble Sunplus-style MP100NSP.BIN firmware.

Layout: first 0x40 bytes are a plain header. From 0x40 on, every byte is
XORed with a key that depends only on address bits 4, 6 and 8:
    key = 0xA1 ^ (a4 ? 0x04 : 0) ^ ... see key() below.
XOR is symmetric, so the same function scrambles and descrambles.
Decoded image is little-endian MIPS I, load base 0x08000000 (0x88000000 uncached alias).
"""
import sys
def key(a):
    return 0xA1 | ((1 ^ ((a >> 4) & 1)) << 2) | (((a >> 6) & 1) << 4) | (((a >> 8) & 1) << 6)
def xform(data, hdr=0x40):
    out = bytearray(data)
    for a in range(hdr, len(out)):
        out[a] ^= key(a)
    return bytes(out)
if __name__ == '__main__':
    src, dst = sys.argv[1], sys.argv[2]
    open(dst, 'wb').write(xform(open(src, 'rb').read()))
    print('wrote', dst)

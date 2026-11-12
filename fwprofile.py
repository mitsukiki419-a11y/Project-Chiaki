"""Which player firmware is loaded, and where its code is.

The emulator's hooks were written for the Megapro MP100NSP firmware and use its addresses. Other players built on
the same Sunplus SDK share most of the system code (drive, video, OSD library, delay, inflate, ...) at different
addresses. For those, ADDR maps an MP100NSP address to the same code in the other build (found with fwmap.py by
matching the instructions around it, gp offsets / absolute addresses / call targets masked). A hook whose code is
not in the other build maps to None and is not installed.
"""

MP100 = 'mp100'
KS5 = 'ks5'
MIGAEL = 'migael'

NAMES = {MP100: 'Megapro MP100NSP', KS5: 'Platinum KS5', MIGAEL: 'Megapro (MIGAEL_R build, 2019)'}

# text in the image header: KS5 version text at 0x18; the Megapro builds share '03R-000' there and differ in the
# checksum word at 0x20
_SIGS = [(b'VER.2309', KS5), (bytes.fromhex('4f5dce5a'), MIGAEL)]

# builds with the MP100NSP's memory layout (same gp, same globals and buffers; only the code moved)
SAME_DATA = {MP100, MIGAEL}

# MP100NSP address (kseg0 form) -> this build's address. Only entries checked by hand.
ADDR = {
    KS5: {
        # stream feeder (1 KB copies into the A/V bitstream window) and its exit
        0x8807BC44: 0x880B66A0, 0x8807BC98: 0x880B66F4,
        # mdelay inner loop and its exit
        0x8807CA00: 0x880B7334, 0x8807CA18: 0x880B734C,
        # debug printf (bare 'jr $ra')
        0x8803212C: 0x88035688,
        # DEFLATE decoder
        0x8807B8DC: 0x880B6338,
        # IR command bit reversal
        0x8807B870: 0x880B62CC,
        # main-loop entry of the startup code
        0x8802BFE8: 0x8802D870,
        # A/V DMA engine
        0x88085D78: 0x880C8238,
        # the stream play loop (splash / background plays)
        0x88084774: 0x880C6B24,
        # remote: IR command -> key table (bit-reversed command byte as index)
        0x880DD444: 0x880FECA4,
        # drive: ATAPI packet submit (MP100NSP: RAM drive module; KS5: in flash) and data-in copy
        0x8079A60C: 0x8804690C, 0x8802B8D8: 0x8802D160,
    },
    # MIGAEL_R.BIN: a later Megapro build (1,820,672 bytes). Every hook found by fwmap.py; the IR key table and the
    # spoken-digit clips found by their bytes; the two Setup return addresses checked by hand (they follow the
    # rectangle-fill call).
    MIGAEL: {
        # its RAM drive module loads at the MP100NSP's addresses
        0x8076F750: 0x8076F750, 0x8076F794: 0x8076F794, 0x8076F7D4: 0x8076F7D4, 0x8079A60C: 0x8079A60C,
        0x88002408: 0x88002408, 0x880165A0: 0x880165A0, 0x880176AC: 0x880176AC, 0x8802B8D8: 0x8802B8D0,
        0x8802BFE8: 0x8802BFE0, 0x8802C4C4: 0x8802C4BC, 0x8802CA30: 0x8802CA28, 0x8802CF20: 0x8802CF18,
        0x8802D208: 0x8802D200, 0x8802D3F0: 0x8802D3E8, 0x8802DAB0: 0x8802DAA8, 0x8802E830: 0x8802E828,
        0x8803212C: 0x88032124, 0x88034DEC: 0x88034DE4, 0x880355C0: 0x880355B8, 0x880364B0: 0x880364A8,
        0x88069F20: 0x88069F18, 0x8807B870: 0x8807B868, 0x8807B8DC: 0x8807B8D4, 0x8807BC34: 0x8807BC2C,
        0x8807BC44: 0x8807BC3C, 0x8807BC98: 0x8807BC90, 0x8807C9F8: 0x8807C9F0, 0x8807CA00: 0x8807C9F8,
        0x8807CA18: 0x8807CA10, 0x88084774: 0x8808476C, 0x88085D78: 0x88085D70, 0x880979F4: 0x880979EC,
        0x88097A54: 0x88097A4C, 0x8809885C: 0x88098854, 0x88098CF4: 0x88098CC0, 0x88098DD0: 0x88098D9C,
        0x880A02D8: 0x880A0284, 0x880A03C8: 0x880A0374, 0x880A05A4: 0x880A0550, 0x880AA908: 0x880AA7B4,
        0x880AA90C: 0x880AA7B8, 0x880AACF8: 0x880AABA4, 0x880AB090: 0x880AAF24, 0x880AB1CC: 0x880AB060,
        0x880AC9C4: 0x880AC858, 0x880B008C: 0x880AFF20, 0x880B0680: 0x880B0514, 0x880B26D4: 0x880B2568,
        0x880B488C: 0x880B4720, 0x880B6BDC: 0x880B6A70, 0x880B6C6C: 0x880B6B00, 0x880BC7C0: 0x880BC654,
        0x880BC8B0: 0x880BC744, 0x880BCA2C: 0x880BC8C0, 0x880BCA6C: 0x880BC900, 0x880DD444: 0x880E4D10,
        0x88123B78: 0x8812B428,
    },
}

# absolute data addresses (SDRAM): MP100NSP -> this build (from the lui/addiu pairs that build them)
DATA = {
    KS5: {
        0x8000B508: 0x8000A3A0,    # ATAPI packet buffer
        0x80006C20: 0x80005BC0,    # OSD region table
    },
}

# gp-relative globals: MP100NSP offset -> this build's offset (found from the instructions that use them)
GPOFF = {
    KS5: {
        2716: 2573,        # play-control flags (bit0 = stop requested, bit1 = stopping)
        3294: 3171,        # drive status (bit0 check condition, bit7 busy)
        3224: 3104,        # drive data buffer pointer
        2734: 2593,        # player state
        3333: 3212, 429: 457, 397: 423, 3498: 3362,     # drive state / step / command / result
        3472: 3336, 3483: 3347, 3484: 3348,             # tray switch GPIO pins (A, B, tray-in)
        430: 458, 431: 459, 514: 540, 3476: 3340, 418: 444,   # drive sub-step, sub-sub-step, timer, disc flags, error
    },
}


# Code that only the other builds have.
# KS5: security chip on a bit-banged serial bus (device 0x38). The firmware sends it a 16-byte challenge encrypted
# with a key from its own tables and expects the chip to return the plain challenge; until that works it retries
# for ever and never starts the disc. 0x8801BF34 = "chip authentic?" (1 = yes), 0x88023BE0 = one exchange
# (sets gp+287 = 1 on success and returns it). Both are answered as a genuine chip would.
EXTRA = {
    KS5: dict(auth_check=0x8801BF34, auth_exchange=0x88023BE0, auth_flag_gp=287,
              # drive module, disc detection after spin-up (state 1 step 5, sub-step 4 at 0x8804F9B8): it measures
              # focus / RF levels from the servo and sets gp+3340 bit0 'disc found'. The servo signals are not
              # modelled, so with a disc in the virtual drive the flag is set when that sub-step runs.
              detect_step=0x8804F9B8, detect_flag_gp=3340,
              # IR table: 101 = no key, 96 entries for the 0x205F remote (other remotes' tables follow)
              ir_nokey=101, ir_len=96,
              # keys go straight to the key dispatcher 0x88011B1C(key) from the IR poll 0x88010CC4 (its caller
              # ignores the result). The IR path cannot give every key: with the 0x205F remote the firmware
              # turns table entry 12 ('8') into 19 ('9').
              key_poll=0x88010CC4, key_dispatch=0x88011B1C,
              key_side=(76, 2788),          # gp offsets the poll sets for a key: key count, time of last key (µs)
              # periodic check of the DSP's audio-mode word (see soc: answered as matching)
              dsp_mode_check=0x880D3E78,
              # DSP watchdog: reloads the DSP when the ring read position has not moved for 11 checks while playing
              # (0x880D4974). Skipped (jal at reload -> its exit): the emulated decoder moves in whole frames
              dsp_watchdog=(0x880D49A0, 0x880D4948),
              # new-song list: 'draw the next line' check (0x88089C1C: gp+2424 >= 9), its 'not yet' exit, the counter
              newsong_step=(0x88089C1C, 0x88089BF0, 2424)),
}


def extra(fid):
    return EXTRA.get(fid, {})


def detect(image):
    hdr = bytes(image[:0x40])
    for sig, fid in _SIGS:
        if sig in hdr: return fid
    return MP100


def data(fid):
    return None if fid in SAME_DATA else DATA.get(fid, {})


def gpoff(fid):
    return None if fid in SAME_DATA else GPOFF.get(fid, {})


def table(fid):
    """None for the MP100NSP (addresses used as written), else the address map."""
    return None if fid == MP100 else ADDR.get(fid, {})

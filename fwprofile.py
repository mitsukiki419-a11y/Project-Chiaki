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

# Newer Sunplus builds ('026.' at 0x30 of the header, the Megapro has '124.'): DVD/USB MIDI karaoke players
K55 = 'k55'; K901 = 'k901'; K8005 = 'k8005'; D02 = '02rd'
U7000 = 'u7000'; U7035 = 'u7035'; WL8202 = 'wl8202'
NAMES = {MP100: 'Megapro MP100NSP', KS5: 'Platinum KS5', MIGAEL: 'Megapro (MIGAEL_R build, 2019)',
         K55: 'Ace MIDI DVD K55 (DVD124)', K901: 'K16 / Pensonic 25KV16 (K901)', K8005: 'Remix K-8005 / Kvision K7001',
         D02: 'Unikon DMD-7000 / LX-7035, WOWE WL8202RD (02R-D-02)'}
NEW_GEN = {K55, K901, K8005, D02, U7000, U7035, WL8202}
NAMES.update({U7000: 'Unikon DMD-7000', U7035: 'Unikon LX-7035', WL8202: 'WOWE WL8202RD'})
# the '02R-D-02' builds share one header name: told apart by their image checksum (header bytes 0x20..0x23)
D02_IDS = {bytes.fromhex('7bf5251b'): U7000, bytes.fromhex('5baa0498'): U7035, bytes.fromhex('e7b72f60'): WL8202}

# text in the image header: KS5 version text at 0x18; the Megapro builds share '03R-000' there and differ in the
# checksum word at 0x20
_SIGS = [(b'VER.2309', KS5), (bytes.fromhex('4f5dce5a'), MIGAEL)]

# builds with the MP100NSP's memory layout (same gp, same globals and buffers; only the code moved)
SAME_DATA = {MP100, MIGAEL}

# MP100NSP address (kseg0 form) -> this build's address. Only entries checked by hand.
ADDR = {
    # Ace MIDI DVD K55 (DVD124): found with fwmap.py against its flash image and a RAM dump (code it unpacks to
    # 0x8004xxxx-0x8006xxxx at start-up). Work in progress - see NEWGEN_NOTES.md.
    K55: {
        0x8802B8D8: 0x880076FC, 0x8802BFE8: 0x88007E0C,          # startup / main-loop entry
        0x8807B870: 0x8803E294, 0x8807B8DC: 0x8803E300,          # inflate
        0x8807BC44: 0x8004269C, 0x8807BC98: 0x800426F0,          # stream feeder (RAM) and its exit
        0x8807CA00: 0x8803EAC4, 0x8807CA18: 0x8803EADC,          # mdelay loop and exit
        0x88085D78: 0x8006CC14,                                  # A/V DMA (RAM)
        0x88084774: 0x802ECE90,                                  # stream play loop (RAM; ends on gp+2001 bit1)
        0x8079A60C: 0x803184C0,                                  # drive module: submit ATAPI packet (RAM)
    },
    # K16 / Pensonic 25KV16 (K901): the K55's map carried over with fwmap.py (K55 -> K16, flash and RAM images)
    K901: {
        0x8802B8D8: 0x880076CC, 0x8802BFE8: 0x88007DDC,          # startup / main-loop entry
        0x8807B870: 0x8803F174, 0x8807B8DC: 0x8803F1E0,          # inflate
        0x8807BC44: 0x800425EC, 0x8807BC98: 0x80042640,          # stream feeder (RAM) and its exit
        0x8807CA00: 0x8803F9AC, 0x8807CA18: 0x8803F9C4,          # mdelay loop and exit
        0x88085D78: 0x8006C99C,                                  # A/V DMA (RAM)
        0x88084774: 0x802E76A4,                                  # stream play loop (RAM)
        0x8079A60C: 0x80311798,                                  # drive module: submit ATAPI packet (RAM)
    },
    # Remix K-8005 / Kvision K7001: the K55's map carried over with fwmap.py (K55 -> K-8005)
    K8005: {
        0x8802B8D8: 0x880073B0, 0x8802BFE8: 0x88007B30,          # startup / main-loop entry
        0x8807B870: 0x88042E04, 0x8807B8DC: 0x88042E70,          # inflate
        0x8807BC44: 0x80047504, 0x8807BC98: 0x80047558,          # stream feeder (RAM) and its exit
        0x8807CA00: 0x88043690, 0x8807CA18: 0x880436A8,          # mdelay loop and exit
        0x88085D78: 0x80072B28,                                  # A/V DMA (RAM)
        0x88084774: 0x802F4EA8,                                  # stream play loop (RAM)
        0x8079A60C: 0x8031FEE8,                                  # drive module: submit ATAPI packet (RAM)
    },
    # Unikon DMD-7000: the K-8005's map carried over with fwmap.py (K-8005 -> DMD-7000; its program runs from RAM)
    U7000: {
        0x8802B8D8: 0x806AE0DC, 0x8802BFE8: 0x806AE7EC,          # startup / main-loop entry
        0x8807B870: 0x88001C8C, 0x8807B8DC: 0x88001CF8,          # inflate
        0x8807BC44: 0x8074B8DC, 0x8807BC98: 0x8074B930,          # stream feeder and its exit
        0x88085D78: 0x806E00B8,                                  # A/V DMA
        0x88084774: 0x80666CC8,                                  # stream play loop
        0x8079A60C: 0x8068D0C4,                                  # drive module: submit ATAPI packet
    },
    # Unikon LX-7035: the DMD-7000's map carried over the same way (the same program, ~10 KB lower)
    U7035: {
        0x8802B8D8: 0x806AC16C, 0x8802BFE8: 0x806AC87C,          # startup / main-loop entry
        0x8807BC44: 0x80746968, 0x8807BC98: 0x807469BC,          # stream feeder and its exit
        0x88085D78: 0x806DD8F4,                                  # A/V DMA
        0x88084774: 0x806644C8,                                  # stream play loop
        0x8079A60C: 0x8068B120,                                  # drive module: submit ATAPI packet
    },
    # WOWE WL8202RD: same SDK, its own build (the song / key code differs more)
    WL8202: {
        0x8802B8D8: 0x806929AC, 0x8802BFE8: 0x806930BC,          # startup / main-loop entry
        0x8807BC44: 0x806F1A30, 0x8807BC98: 0x806F1A84,          # stream feeder and its exit
        0x88085D78: 0x806CBAF4,                                  # A/V DMA
        0x88084774: 0x8065A700,                                  # stream play loop
        0x8079A60C: 0x8066FAE8,                                  # drive module: submit ATAPI packet
    },
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
        0x880AA90C: 0x880AA7B8, 0x880AA75C: 0x880AA608, 0x880AACF8: 0x880AABA4, 0x880B1060: 0x880B0EF4, 0x88035614: 0x8803560C, 0x880AB090: 0x880AAF24, 0x880AB1CC: 0x880AB060,
        0x880AC9C4: 0x880AC858, 0x880B008C: 0x880AFF20, 0x880B0680: 0x880B0514, 0x880B26D4: 0x880B2568,
        0x880B488C: 0x880B4720, 0x880B6BDC: 0x880B6A70, 0x880B6C6C: 0x880B6B00, 0x880BC7C0: 0x880BC654,
        0x880BC8B0: 0x880BC744, 0x880BCA2C: 0x880BC8C0, 0x880BCA6C: 0x880BC900, 0x880DD444: 0x880E4D10,
        0x88123B78: 0x8812B428,
    },
}

# absolute data addresses (SDRAM): MP100NSP -> this build (from the lui/addiu pairs that build them)
DATA = {
    K55: {
        0x8000B508: 0x8000D2F8,    # ATAPI packet buffer (1 vote: check)
        0x80006C20: 0x800087D0,    # OSD region table
    },
    K901: {
        0x8000B508: 0x8000D350,    # ATAPI packet buffer (1 vote)
        0x80006C20: 0x800085B0,    # OSD region table
    },
    K8005: {
        0x8000B508: 0x8000E738,    # ATAPI packet buffer
        0x80006C20: 0x80009BE0,    # OSD region table
    },
    U7000: {
        0x8000B508: 0x8000DEC0,    # ATAPI packet buffer
        0x80006C20: 0x80006E50,    # OSD region table
    },
    U7035: {
        0x8000B508: 0x8000DC50,    # ATAPI packet buffer
        0x80006C20: 0x80006D10,    # OSD region table
    },
    WL8202: {
        0x8000B508: 0x8000A208,    # ATAPI packet buffer
        0x80006C20: 0x80005A30,    # OSD region table
    },
    KS5: {
        0x8000B508: 0x8000A3A0,    # ATAPI packet buffer
        0x80006C20: 0x80005BC0,    # OSD region table
    },
}

# gp-relative globals: MP100NSP offset -> this build's offset (found from the instructions that use them)
GPOFF = {
    K55: {
        2716: 2001,        # play-control flags (bit0 = stop requested, bit1 = stopping)
        3294: 2546, 3224: 2476, 2734: 2021,
        429: 440, 397: 408, 3498: 2738, 430: 441, 431: 442, 418: 429,
    },
    K901: {
        2716: 1990, 3294: 2507, 3224: 2440, 2734: 2010,
        429: 416, 397: 383, 3498: 2687, 430: 417, 431: 418, 418: 404,
    },
    K8005: {
        2716: 2223, 3294: 2778, 3224: 2708, 2734: 2243,
        429: 452, 397: 423, 3498: 2964, 430: 453, 431: 454, 418: 441,
    },
    U7000: {
        2716: 2297, 3294: 2871, 3224: 2804, 2734: 2319,
        429: 384, 397: 351, 3498: 3051, 430: 385, 431: 386, 418: 372,
    },
    U7035: {
        2716: 2199, 3294: 2787, 3224: 2720, 2734: 2221,
        429: 369, 397: 335, 3498: 2979, 430: 370, 431: 371, 418: 356,
    },
    WL8202: {
        2716: 2328, 3294: 2963, 3224: 2896, 2734: 2350,
        429: 408, 397: 374, 3498: 3138, 430: 409, 431: 410, 418: 395,
    },
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
              # the firmware's own next-background-video routine (key 35 in its key table 0x880FEE2C): steps
              # gp+2585 through the movie.idx groups (table 0x8000A5E0), starts that video and shows "BGV: %d"
              bgv_call=0x88018D84,
              # the firmware's own M/F (0x8801A128: gp+944 0 ORIGINAL / 1 MALE / 2 FEMALE, key gp+2562) and Melody
              # (0x8801A210: MELODY ON / OFF, gp+945 1 = off) - both shown with its own message, taken while a song
              # plays (gp+4120 = 2); melody only for MIDI songs (gp+4126 = 0) and when gp+4151 = 0
              mf_call=0x8801A128, melody_call=0x8801A210,
              key_side=(76, 2788),          # gp offsets the poll sets for a key: key count, time of last key (µs)
              # periodic check of the DSP's audio-mode word (see soc: answered as matching)
              dsp_mode_check=0x880D3E78,
              # DSP watchdog: reloads the DSP when the ring read position has not moved for 11 checks while playing
              # (0x880D4974). Skipped (jal at reload -> its exit): the emulated decoder moves in whole frames
              dsp_watchdog=(0x880D49A0, 0x880D4948),
              # idle-screen timers (new-song list pace, typed-number result, screen timeouts)
              idle_tick=0x8808E6AC,
              # OSD message timeout setter (a0 = ticks)
              msg_timeout=0x88088D68,
              # per-region message countdown 0x88034840(elapsed clock units): messages ask for n x 200 units
              # (the "BGV: 3" message: 200) and went in ~0.15 s here. Elapsed time is slowed by this factor.
              region_timer=(0x88034840, 6)),
    # K55: the karaoke program (MIDI.PLF, loaded to RAM) polls the remote at 0x802F905C (decoder 0x802F8E38, custom
    # codes 0xF807 / 0x718E, tables 0x8807BA88 / 0x8807BAE8, 0x83 = no key) and hands the key to 0x802F8AA8(key).
    # The front panel (bit-banged VFD chip, 0x80303A24) goes through a table at 0x8030293C to the same dispatcher.
    K55: dict(key_poll=0x802F905C, key_dispatch=0x802F8AA8,
              # the song: MEGMID holds raw-deflated MIDI files; the program merges one into a single track at
              # 0x8029B800 and drives the audio DSP's MIDI player like the KS5 (PB+0x4C0: 0x301 play, 0x302/0x303)
              midi_buf=0x8029B800,
              # PB+0x4F0 (the MIDI player's clock): 1/5512.5 s like the KS5's (0x880153F4 turns it into 90 kHz
              # lyric time with * 2613 / 160); the song length (gp+3260) is in these units too
              # at the end of the file the DSP's MIDI player stops and the clock reads 0 (the firmware's end check)
              midi_clock_zero_at_end=True,
              # the DSP audio-mode check (KS5 0x880D3E78): word 15 of the DSP's memory vs gp+1972, else reload
              dsp_mode_check=0x802FB9F4,
              # DSP watchdog (KS5 0x880D49A0): reload after 11 checks without progress, skipped like the KS5's
              dsp_watchdog=(0x802FC250, 0x802FC258),
              # no MIDI command of its own: the song starts with the A/V player (0x201) and stops with 0x203
              midi_on_av=True,
              # return point of the song loader 0x8801BF54 (v0 = 2: merged into MIDI_BUF)
              song_loaded=0x8801D1BC,
              # OSD message timeout (KS5 0x88088D68, same code; gp+3400 here): scaled like the KS5's (KS5_MSG_SCALE)
              msg_timeout=0x88026C68,
              # the typed number asks for 9 ticks (0.45 s): too short to type a song number; x6 (~2.7 s)
              msg_scale=6,
              # the A/V ring (MP3 clips such as the score jingle): base address at gp+1604, size (bytes) at gp+1882
              ring_gp=(1604, 1882)),
    # K16 / Pensonic 25KV16: the K55's hooks mapped with fwmap.py (key poll found by its IR register read)
    K901: dict(key_poll=0x802F2DF4, key_dispatch=0x802F27C4,
               midi_buf=0x80295800,            # 1 vote: check
               midi_clock_zero_at_end=True,
               dsp_mode_check=0x802F5468,
               dsp_watchdog=(0x802F5CB4, 0x802F5CBC),
               midi_on_av=True,
               song_loaded=0x8801D8E4,
               msg_timeout=0x88027BF4, msg_scale=6,
               ring_gp=(1600, 1874)),
    # Remix K-8005 / Kvision K7001: the K55's hooks mapped with fwmap.py (key poll by its IR register read,
    # message timeout by its instructions)
    K8005: dict(      # no boot keys any more: key 33 pressed during a song skips to a random one; with slow_counters the
                      # player stays idle by itself (checked: 400 s idle, and after a song ends, nothing else starts)
                # idle counters gp+792 / gp+796 (+1 per main-loop tick; random play starts at 600): they counted ~20 a
                # second here, so random play began after 30 s without a key. Counted 1 in 20 (~10 minutes).
                slow_counters=((0x88021BD0, 0x88021BD4, 20), (0x88021F80, 0x88021F84, 20)),
                key_poll=0x803019DC, key_dispatch=0x803013B4,    # dispatcher found from the poll (tables 0x88081CAC..)
                midi_buf=0x802A3800,            # 2 votes: check
                midi_clock_zero_at_end=True,
                dsp_mode_check=0x803041E4,
                dsp_watchdog=(0x80304AE4, 0x80304AEC),
                midi_on_av=True,
                song_loaded=0x8801F438,
                msg_timeout=0x8802B640, msg_scale=6,
                ring_gp=(1828, 2102),
                # its disc check chip (I2C 0x52/0x53), read by 0x8805F2E4(dev, reg)
                auth_chip=dict(read=0x8805F2E4, dev=0x53, regs={0x40: 0xDD, 0x00: 0xEE})),
    U7000: dict(key_poll=0x80673820, key_dispatch=0x806731C4,     # found by its PB+0x734 read, like the K-8005's
                midi_buf=(0x80055000, 0x80F72000),   # the merged song: one or the other by gp+6767 (0x8075DF8C)
                song_loaded=0x80765C48,
                msg_timeout=0x8075785C,
                midi_clock_zero_at_end=True,
                dsp_mode_check=0x806F21C0,
                dsp_watchdog=(0x806F2C90, 0x806F2C98),
                midi_on_av=True,
                msg_scale=6,
                ring_gp=(1872, 2174)),
    U7035: dict(key_poll=0x80670C08, key_dispatch=0x80670504,
                midi_buf=(0x80055000, 0x80F72000),
                song_loaded=0x80763260,
                msg_timeout=0x80754954,
                midi_clock_zero_at_end=True,
                dsp_mode_check=0x806EFA10,
                dsp_watchdog=(0x806F04CC, 0x806F04D4),
                midi_on_av=True,
                msg_scale=6,
                ring_gp=(1780, 2078)),
    WL8202: dict(key_poll=0x80754F28, key_dispatch=0x8075629C,   # its own key code: poll reads PB+0x734, table
                                                                 # 0x806E11C0 turns the code into the key number
                midi_buf=(0x80049000, 0x80529C20),   # the song's loader 0x80774BBC: one or the other by gp+3877
                song_loaded=0x80775D50,
                midi_clock_zero_at_end=True,
                dsp_mode_check=0x806DA494,
                dsp_watchdog=(0x806DB01C, 0x806DB024),
                midi_on_av=True,
                msg_scale=6,
                ring_gp=(1900, 2206)),
}


def extra(fid):
    return EXTRA.get(fid, {})


def detect(image):
    hdr = bytes(image[:0x40])
    for sig, fid in _SIGS:
        if sig in hdr: return fid
    if hdr[0x30:0x34] == b'026.':
        name = hdr[0x18:0x20].rstrip(b'\0')
        if name.startswith(b'02R-D'): return D02_IDS.get(hdr[0x20:0x24], D02)
        return {b'DVD124': K55, b'K901': K901, b'K8005': K8005}.get(name, K55)
    return MP100


def data(fid):
    return None if fid in SAME_DATA else DATA.get(fid, {})


def gpoff(fid):
    return None if fid in SAME_DATA else GPOFF.get(fid, {})


def table(fid):
    """None for the MP100NSP (addresses used as written), else the address map."""
    return None if fid == MP100 else ADDR.get(fid, {})

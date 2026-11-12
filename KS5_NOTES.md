# Platinum KS5 (aprom_4m.bin, VER.2309) - porting notes

Same Sunplus SDK as the MP100NSP; scrambled the same way. fwprofile.py detects it (header text VER.2309) and maps
the emulator's MP100NSP hook addresses to it (found with fwmap.py: instructions around each hook matched with gp
offsets, absolute addresses and call targets masked). gp = 0x80002C70.

Working so far
- boot to the main loop (~1.8 s emulated): stream feeder, mdelay, inflate, debug print, play loop + stop flag
  (gp+2573) mapped.
- security chip: bit-banged serial device 0x38, 16-byte encrypted challenge; answered at 0x8801BF34 / 0x88023BE0.
- tray: switch pins come from the board config (gp+3336/3347/3348 = 0x4F/0x4E/0x42), TraySwitches reads them.
- drive: ATAPI submit 0x8804690C (packet buffer 0x8000A3A0, status gp+3171), data-in 0x8802D160.
  Disc detection (state 1 step 5) needs servo 0x4079 bit5, 0x407C != 0, 0x4774 in 32..208, then gp+3340 bit0.
  With these the drive state reaches 255 (idle, disc ready).

- buttons: PB+0x9E0 bit2 = OPEN/CLOSE, bit1 = power (active low; released). Answered in the C core (0.3x -> 1x).
- power: a cold start goes to standby (auto power-off armed) unless 0xBFFF0080 = 0x1818 ('power key pressed',
  left by the firmware before it restarts); the emulator presets it.
- disc: READ TOC, font, directories, Newsong.lst, movie.idx, 5pidx, button_*.plf, then the background video
  MOVIES/MIDI*.PLF (MPEG-2 PS) streams. OSD: 8 bpp regions (256-entry palette at header+16) now rendered: the
  "PLATINUM NEW SONG" list shows.

- remote: accepts custom codes 0x0C73 / 0x205F / 0xA05F (and 0x02FD); table 0x880FECA4 (101 = no key). Keys go
  straight to the dispatcher 0x88011B1C from the IR poll 0x88010CC4 (fwprofile key_poll / key_dispatch).
- number entry works: digits show "18354 / Questions" (8 bpp OSD), Enter reads the song from IDX/Apmid.
- A/V audio ring: write pointer PB+0x4C8, read pointer PB+0x4CC (units of 3 bytes); the read pointer follows
  the write pointer (decoder taken to keep up), otherwise the firmware waits for ever after the first digit.

Next
- after Enter the firmware draws into the video frame buffer (0x8802FC20 fills rectangles at PB+0x524-based
  addresses): lyrics are probably drawn into the picture, not the OSD -> render the display frame buffer.
- no MIDI leaves the chip: strings "YXMidiSrc" / "SJMidiSrc" and _FONT/midi.plf (4.4 MB, a wavetable?) suggest
  the music is synthesized by the audio DSP. Find the sequencer's note events and feed the SoundFont synth.
- songs: IDX/Apmid (MIDI bank), IDX/Apvol* (vocal audio?), MIDI sender (UART), lyrics.
- Mtv/*.avi music videos.

## Songs (with the full IDX/Apmid)
- Enter reads 5 sectors of IDX/Apmid (encrypted song record, ~10 KB = a MIDI song), the firmware parses it
  (0x88073B80 / 0x880754E0: MIDI event scans; it checks text events "YXMidiSrc-SM Karaoke" / "SJMidiSrc-SJM
  Karaoke" to pick the lyric format), then ~3.7 s later goes back to the new-song list.
- No MIDI leaves the chip (no writes to the UART at PB+0x900). The sound is made by the audio DSP: data goes into
  the A/V ring (base gp+2152, size gp+2458 = 9216, write pointer PB+0x4C8, read pointer PB+0x4CC); captured data
  holds MP3 frames (LAME 3.93, 22 kHz: probably the spoken number/title). The song itself most likely is played
  by the DSP from MIDI with the MIDI.PLF wavetable, which the emulator does not have.
- Because the read pointer is answered as "consumed at once", the firmware probably sees the song as finished
  right away. Next: model the DSP's consumption at real rate, find the DSP command/status registers, and capture
  the MIDI the firmware hands to the DSP to play it with the SoundFont.

## Song playback (works)

* After Enter the firmware decrypts the song from /IDX/Apmid, keeps the original format-1 MIDI in RAM and merges it
  into one track at 0x80068C00 (0x8807C6D4). It then starts the audio DSP's MIDI player: command 0x301
  (type 3 = MIDI player, sub 1 = play; 0x302 = stop).
* The DSP reports two counters, which the emulator now models (`SongPlayer` in soc.py):
  * PB+0x4F0 = play clock, elapsed song time in 1/5512.5 s, 16 bits, wraps. The lyric table (gp+0xFB8, 16-byte
    entries, times in 90 kHz) is compared with clock * 2613 / 160 (0x88073A68). gp+0xFB4 = song length in clock
    units; the song ends when the clock reaches it. If the clock stops moving the firmware stops the song after
    ~9 checks (0x8807DF0C, reason 4) - that was the "song aborts" bug.
  * PB+0x4EC = number of lyric events (FF 05) played; the firmware turns the lyric pages with it (0x88076270).
* The music: SongPlayer parses the merged MIDI file and sends its events to the emulator's MIDI output (the
  SoundFont synth), stamped with the emulated time. Lyrics and wipe are in step (checked: song 18354 "In a dream"
  at 25.4 s).
* Not done yet: key change / tempo / melody-guide commands (types 5, 0xD ...), pause, the KS5 songbook.

## Speed, background video, Stop, spoken digits (0.9.6)

* The firmware reads PB+0x4F0 / 0x4EC / 0x4CC hundreds of thousands of times a second during a song; they are now
  plain C-core registers that the run loop updates (SongPlayer.pump), not Python calls.
* Background video: the KS5 asks for the next 4 sectors (READ CD) every millisecond without waiting. Each command
  used to restart the stream (sectors thrown away, stream clock reset): the BGV ran ~10x fast and the stream cost
  most of the CPU. A read continuing the one in progress is now joined to it, so delivery follows the MPEG clock
  (~217 sectors/s for MIDIxx.PLF).
* Stop: 0x508 stops the MIDI player (sent by Stop and before each new song).
* Spoken digits: the MP3 clips the firmware writes into the A/V ring are collected and played through the key-voice
  output when the writes pause.

## 0.9.6b fixes

* Stream: KS5 reads that continue the one in progress keep the stream clock (no join: a joined read kept the drive
  busy for a minute -> black screen after typing a number).
* Voice clips: the DSP reports stopped after the clip's playing time, and a play with no data stops at once
  (the firmware's stop path waits 180 ms when the DSP still says playing: slow keys).
* Song end: the DSP clock stops at the end of the MIDI file; the firmware sees it stand still and ends the song.
* Songs in "MP3 mode" (gp+0x101E = 1, e.g. 122 Laklak): the music is MP3 from /IDX/Apvol fed through the A/V ring
  (type-2 player); PB+0x4EC is then the play clock (192 samples at 44.1 kHz per unit, time = clock * gp[0x33C] / 10
  in 90 kHz). Not modelled yet: no music and no lyrics for these songs.
* Song file format (Apmid): raw DEFLATE of the MIDI, XOR layer 1 (table 0x8836FC00, 0x7BC long, start =
  song number % 0x7FF; skipped when bytes 1-3 are 0D C9 48) and layer 2 from byte 0x40 (table 0x88370420, 0x800,
  start = sum of bytes 0..0x39 & 0x7FF). Decoder 0x8807EE08.

## MP3 songs and the DSP checks (0.9.6c)

* MP3 songs (gp+0x101E = 1, e.g. 122): the firmware loads the song's MP3 from /IDX/Apvol (first ~32 KB encrypted, it
  decrypts them) and feeds it through the A/V ring with the type-2 player. KS5Ring (soc.py) plays the ring at MP3
  speed, moves the read position and reports the play clock in PB+0x4EC (unit = gp[0x33C] / 900000 s). The app
  decodes the stream with ffmpeg and mixes it into the built-in synth (StreamPCM).
* Two firmware checks restarted the DSP every ~0.3 s because the DSP is not emulated:
  0x880D3E78 (DSP audio-mode word in its shared memory != gp+2544) and the watchdog at 0x880D4974 (ring read position
  unchanged for 11 checks). Both are answered as a healthy DSP would (fwprofile EXTRA dsp_mode_check / dsp_watchdog).
* The MIDI of an MP3 song is only the lyric track (channel 16, velocity-1 notes).

## 0.9.7

* Timers: PB+0x90/0x94 is a millisecond clock on the KS5 (its main loop runs once per count and its 50-count tick
  runs OSD message timeouts). Counted in microseconds the 'Prog : 12345' box was cleared ~0.1 s after each digit,
  so a number could not be typed (or reserved) during a song. Now 20 ticks = 1 s, as on the player. PB+0x80 (10 us)
  stays in the C core (fast_computed kind 1).
* New-song list: a line is drawn when gp+2424 >= 9 (0x88089C1C); that counter follows the main loop, which runs far
  more often here. HLE paces the step on the emulated clock: one line every 3 s (soc.NEWSONG_LINE).
* Setup settings: kept in the SPI flash, a log of records around 0x0FF000/0x100000 (read with opcode 0x03). The KS5
  flash device is now its own store (erased = 0xFF), saved in Saved Settings\KaraokeEmu.ks5settings.json.
* Megapro score: the SCORE background (an MPEG still in the firmware, resource 0x13 via 0x88098934, played by
  0x880B1060) is shown in the live picture until the firmware streams from the disc again; the digits sit in its
  cards (tens x=342, ones x=452).
* Settings > Resolution: 640x480, 720x480 (normal), 1920x1080.

# Chiaki Karaoke Emu

An emulator for **Sunplus karaoke players**: the **Megapro MP100NSP** (and the later Megapro MIGAEL_R build) and
the **Platinum KS5** (work in progress). It runs the player's own firmware, so songs, lyrics, scoring, the setup
menu and greetings behave like the real machine. You use it in your web browser.

Want to edit Megapro discs (add songs, music videos, a chorus)? That is the **Megapro Song Manager**, a separate
program with its own download and its own README.

Made by **ChiakiMK** · built with Claude (Anthropic)

---

## What you need

| File | What it is | Where to get it |
|---|---|---|
| `KaraokeEmu.exe` | the emulator | this release |
| the player's firmware (`MP100NSP.BIN`, `MIGAEL_R.BIN` or the KS5's `aprom_4m.bin`) | the player's own program | **your own player / disc** (not included) |
| a karaoke disc image (`.iso`) | the songs | **your own disc** (not included) |
| a SoundFont (`.sf2`) | instrument sounds | see below (not included) |
| `ffmpeg.exe` | video decoder | free download, see below (not included) |

The firmware, the songs and the player's sounds belong to their owners, so they are not part of this release.
Use files from a player and discs you own.

---

## Setup (Windows)

### 1. Make a folder
Create a folder, for example `C:\KaraokeEmu`, put `KaraokeEmu.exe` in it and start it once. It creates these
folders next to itself:

| Folder | What goes in it |
|---|---|
| `BIOS` | the player's firmware |
| `Discs` | your disc images (`.iso`) |
| `Soundfont` | your SoundFonts (`.sf2`) |
| `Saved Settings` | your settings, the player's setup and the quick boot (made by the program) |
| `Logs` | error logs (made by the program) |

Whatever it finds in `BIOS`, `Discs` and `Soundfont` shows up as a choice in ⚙ Settings. You can also point it
at another folder that holds your discs.

### 2. Add the firmware
Copy your firmware file (for example `MP100NSP.BIN`) into `BIOS`. Use the original file as it is; the program
descrambles it itself and recognises which player it is from.

### 3. Add ffmpeg (for the background video and music videos)
1. Go to **https://www.gyan.dev/ffmpeg/builds/**
2. Download **ffmpeg-release-essentials.zip**.
3. Open the zip, go into the `bin` folder and copy **`ffmpeg.exe`** next to `KaraokeEmu.exe`.

Without ffmpeg the player still works and plays songs, but there is no moving picture and music videos have no
sound. ffmpeg is a separate free program by the FFmpeg project (https://ffmpeg.org).

### 4. Add a SoundFont (for the music)
Put a General MIDI SoundFont (`.sf2`) in the `Soundfont` folder. If you have `dreamsam_2695.sf2` (the sound set of
the player's synth chip) it sounds closest to the real player; any GM SoundFont works, for example
GeneralUser GS or FluidR3_GM. You can also pick a different `.sf2` in the program later.

Your folder should now look like this:

```
C:\KaraokeEmu\
    KaraokeEmu.exe
    ffmpeg.exe
    BIOS\MP100NSP.BIN
    Discs\your disc.iso
    Soundfont\dreamsam_2695.sf2
```

### 5. Start it
1. Double-click **KaraokeEmu.exe**. A black console window opens (leave it open; closing it quits the program)
   and your browser opens **http://127.0.0.1:8765**.
2. Click the **⚙ settings** icon and choose your disc and firmware (choosing a disc picks the matching
   firmware).
3. Press **Start**. The first start takes about half a minute; after that the program remembers the
   started player (quick boot) and starts in seconds.
4. When the screen shows **Select Song**, type a song number and press **Enter**.

If Windows SmartScreen says "Windows protected your PC", click **More info → Run anyway** (the exe is not signed).

---

## Using the player

### The screen
* The player's picture fills the page. **Double-click** it or use the fullscreen button for fullscreen.
* **⛶ / screen icon:** opens the picture in its own window (put it on a second monitor or a TV).
* **🎮 Remote** and **📖 Songbook** icons open floating panels; each can be detached into its own window.
* **⚙ Settings:** disc, firmware, sound, synth, player options, keyboard keys, theme (dark / light).

### Remote buttons
| Button | What it does |
|---|---|
| 0–9 | type a song number |
| **Enter** | reserve the song (it plays when the current one ends, or right away) |
| **First reserve** | put the song at the front of the queue (plays next) |
| **✖ Cancel** | clear the number you typed. With nothing typed it opens the reserve list; press again to delete the highlighted song |
| **Stop** | stop the song |
| **Setup** | the player's setup menu (greetings, scoring, background, …). Password: **0000** |
| Key ♯ / ♭ / reset | change the song's key |
| Vol + / − , Mute | volume |
| Karaoke / vocal | melody / vocal guide |
| Change video | next background video |

### Keyboard (default, change it under ⚙ Settings → Keys)
| Key | Remote button |
|---|---|
| `0`–`9` | digits |
| `Enter` or `Space` | Enter (reserve) |
| `R` | First reserve |
| `Backspace` | Cancel |
| `Esc` | Stop |
| `S` | Setup |
| Arrow keys | Up / Down / Left / Right |
| `+` / `-` | Volume |
| `M` | Mute |
| `Page Up` / `Page Down` / `Home` | Key ♯ / Key ♭ / Key reset |
| `P` / `N` | Previous / Next |
| `D` | Display |
| `K` | Karaoke / vocal |
| `B` | Book |
| `V` | Change video |

### Songbook
The 📖 Songbook lists every song on the disc. Search by number, title or artist and click a song to play it.

### Sound and the synth panel
* **SoundFont synth (built in)** plays the firmware's music with your `.sf2`, in step with the lyrics.
* The **synth panel** has a master volume, per-channel volume and mute, transpose, and lets you change the
  instrument of each channel.
* You can also choose a Windows MIDI device instead (for example *Microsoft GS Wavetable Synth*).

### Player options (⚙ Settings → Player)
* **Voice on number keys:** the player's voice announces the digits you type.
* **Chorus:** plays the recorded chorus / backing vocals that some songs have.
* **Random scoring:** a score at the end of every song.
* **Music video delay:** if lyrics on music videos come too early or too late, adjust this.

Your settings, the player's setup (greetings, password, …) and the quick boot are saved and come back next time.
To go back to factory settings, delete `KaraokeEmu.eeprom.json` in `Saved Settings`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No moving picture / music video has no sound | `ffmpeg.exe` is missing next to the exe (step 3) |
| No music | no `.sf2` found: put one in `Soundfont` or choose it under ⚙ Settings → Sound |
| Black screen | the firmware and the disc are from different players (a KS5 firmware needs a KS5 disc) |
| Music slow or stuttering | close other heavy programs; keep the console window open (minimised is fine) |
| Lyrics on music videos early or late | ⚙ Settings → Player → Music video delay |
| Setup asks for a password | **0000** |
| Weird settings | delete `Saved Settings\KaraokeEmu.eeprom.json` for factory settings |
| A song or instrument sounds wrong | open an issue with the song number and what you hear |

---

## Build from source (Windows)

1. Install **Python 3.9+** from https://www.python.org (tick *Add python.exe to PATH*).
2. Keep `kecpu.dll` (the fast CPU core) in the source folder.
3. Double-click **`build_exe.bat`** → `dist\KaraokeEmu.exe`.

If `ffmpeg.exe` is in the source folder when you build, it is packed **inside** the exe. For a public release
leave it out, and let people download ffmpeg themselves (step 3), so you don't have to ship ffmpeg's license and
source with your release.

Run without building: `python karaoke_emu.py` (opens http://127.0.0.1:8765). Linux / macOS: `build_exe.sh`;
the fast core there is `libkecpu.so`, built from `fastcpu/cpu.c`.

---

## Notes

* Not affiliated with Megapro, Platinum or Sunplus. These names belong to their owners.
* Platinum KS5: boots, reads its disc, takes song numbers and plays songs with lyrics. Not yet: key change,
  tempo, melody guide and the songbook.
* Don't share firmware, disc images, songs or SoundFonts you don't have the rights to.
* Not emulated: the microphone (echo and real scoring of your voice), and the video decoder chip itself (pictures
  come from ffmpeg). Some hardware details are inferred; they are marked INFERRED in `soc.py`.

**Credits:** ChiakiMK · built with Claude (Anthropic)

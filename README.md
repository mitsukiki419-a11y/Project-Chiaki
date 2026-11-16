# Project : Chiaki

An emulator for **Sunplus-based karaoke players**. It runs the player's own firmware, so songs, lyrics, scoring,
the setup menu and greetings behave like the real machine. Runs on **Windows** and **Android**.

Supported players: **Megapro MP100NSP** and **MIGAEL**, **Platinum KS5**, **DMD-7000**, **LX-7035**, **WOWE**,
and the Remix / K-series.

Want to edit Megapro discs (add songs, music videos, a chorus)? That is the **Megapro Song Manager**, a separate
program (see `SONGMANAGER_README.md`).

Made by **ChiakiMK** · built with Claude (Anthropic) ·
[Linktree](https://linktr.ee/mitsukisato) · [TikTok](https://www.tiktok.com/@mitsukikaraoke) ·
[Facebook](https://www.facebook.com/profile.php?id=61593864883476)

> ⚠️ **Only see a black screen?** Message me with your `Logs\KaraokeEmu.log`: https://linktr.ee/mitsukisato

---

## Legal disclaimer

- **Only for people who own the players and discs.** Project Chiaki is meant only for people who own the karaoke
  player they emulate, and who own or hold a license to the song discs they use. Use only firmware you dumped
  from your own player and disc images you made from discs you own or are licensed to use.
- **No copyrighted content is included.** This project does not include or distribute any firmware, disc
  images, songs, lyrics, music videos or SoundFonts. Please don't ask for them, and don't share them in this
  project's issues or discussions.
- **Not affiliated with anyone.** Project Chiaki is an independent, fan-made project. It is not affiliated with,
  endorsed by or sponsored by Sunplus, Megapro, Platinum, WOWE, or any other manufacturer or song publisher. All
  trademarks and product names belong to their owners and are used only to say which hardware is supported.
- **Your own responsibility.** You are responsible for following the copyright and other laws where you live.
- **No warranty.** This software is provided "as is", without any warranty. Use it at your own risk.

---

## What you need

| File | What it is | Where to get it |
|---|---|---|
| `Project Chiaki.exe` / `Project Chiaki.apk` | the emulator | the Releases page |
| the player's firmware (`MP100NSP.BIN`, `aprom_4m.bin`, …) | the player's own program | **your own player** (not included) |
| a disc image (`.iso`) | the songs | **your own disc** (not included) |
| a SoundFont (`.sf2`) | instrument sounds for the music | see below (not included) |
| ffmpeg | video decoder (pictures, background and music videos) | free download, see below (not included) |

---

## Windows

### 1. Make a folder
Create a folder, for example `C:\Project Chiaki`, put `Project Chiaki.exe` in it and start it once. It opens in
its own window and creates these folders next to itself:

| Folder | What goes in it |
|---|---|
| `BIOS` | the player's firmware |
| `Discs` | your disc images (`.iso`) |
| `Soundfont` | your SoundFonts (`.sf2`) |
| `BGV` | background videos (optional) |
| `Saved Settings` | your settings, the player's setup and the quick boot (made by the program) |
| `Logs` | logs (made by the program) |

If Windows SmartScreen says "Windows protected your PC", click **More info → Run anyway** (the exe is not signed).

### 2. Add the firmware
Copy your firmware file into `BIOS`. Use the original file as it is: the program recognises which player it is
from.

### 3. Add ffmpeg
1. Go to **https://www.gyan.dev/ffmpeg/builds/** and download **ffmpeg-release-essentials.zip**.
2. Open the zip, go into the `bin` folder and copy **`ffmpeg.exe`** next to `Project Chiaki.exe`.

Without ffmpeg the player still plays songs, but there is no moving picture and music videos have no sound.
ffmpeg is a separate free program by the FFmpeg project (https://ffmpeg.org).

### 4. Add a SoundFont
Put a General MIDI SoundFont (`.sf2`) in `Soundfont`. `dreamsam_2695.sf2` (the sound set of the player's synth
chip) sounds closest to the real player; any GM SoundFont works, for example GeneralUser GS.

Your folder should now look like this:

```
C:\Project Chiaki\
    Project Chiaki.exe
    ffmpeg.exe
    BIOS\MP100NSP.BIN
    Discs\your disc.iso
    Soundfont\dreamsam_2695.sf2
```

### 5. Start it
1. Open **Project Chiaki.exe** and click **⚙ Settings**. Choose your disc and firmware (choosing a disc picks the
   matching firmware).
2. Press **⏻** to switch the player on. The first start takes up to about half a minute; after that the
   program remembers the started player (quick boot) and starts in seconds.
3. When the screen shows the song menu, type a song number and press **Enter**.

---

## Android

Needs Android 7 or newer (64-bit or 32-bit). The emulator is demanding: use a **mid-range phone
from about 2021 or newer** (Snapdragon 7/8-series, Dimensity 900+, Galaxy A54 or better). On entry-level phones
(for example the Galaxy A03) the music plays slowly; use **Smart link** with your PC instead (below).
It also runs on PC Android emulators such as **LDPlayer 9** (64-bit).

### 1. Install
Copy `Project Chiaki.apk` to the phone and open it. If Android asks, allow installing apps from this source.
If it warns that the app was made for an older Android, choose **Install anyway**.

### 2. Make the folders
1. Open Project Chiaki. Under **▶ Play on this phone** it shows where its files go: a folder
   **Project Chiaki** at the top of the phone's storage. Tap **📁 Use SD card instead** to keep them on the SD
   card.
2. Tap **▶ Play on this phone**. Android asks for file access (**All files access** on Android 11 and later):
   allow it, then go back to the app. The folders are made: `BIOS`, `Discs`, `Soundfont`, `BGV`,
   `Saved Settings`, `Logs`.

### 3. Copy your files in
With the phone's file manager, or from a PC over USB (choose **File transfer** on the phone):

| Put this | Into |
|---|---|
| firmware | `Project Chiaki/BIOS` |
| disc images (`.iso`) | `Project Chiaki/Discs` |
| one **small** SoundFont (for example a 10–20 MB one; big ones of hundreds of MB don't fit in a phone's memory) | `Project Chiaki/Soundfont` |
| ffmpeg (see below) | `Project Chiaki` itself, next to the `BIOS` folder |

**ffmpeg for Android:** use the Android ffmpeg from this project's **Releases** page
(`ffmpeg-android-arm64` for phones and tablets, `ffmpeg-android-x86_64` for LDPlayer / PC Android emulators) and
put it in the `Project Chiaki` folder (any name starting with `ffmpeg` works). Ordinary Linux builds (for example
from johnvansickle.com) and the Windows `ffmpeg.exe` do **not** run on Android: Android blocks them. Without ffmpeg
the app plays songs, but shows no pictures or video; ⚙ Settings says why if it can't use the file.

These Android builds are FFmpeg 6.1.1 (LGPL), built statically with musl; source:
https://ffmpeg.org/releases/ffmpeg-6.1.1.tar.xz (the build options are in `android/FFMPEG_BUILD.md`).

### 4. Play
Tap **▶ Play on this phone**, open **⚙ Settings**, choose the firmware and disc and press **⏻**.
- **⛶** fills the screen with the picture; the buttons stay on top. **Back** leaves fullscreen.
- **Back** also closes an open panel. Press it twice to close the app.

---

## Smart link (your phone as the remote for the PC)

Pick songs and use the remote from your phone while the PC runs the emulator. Nothing needs to be copied to the
phone for this.

1. On the PC: **⚙ Settings → 📶 Smart link** → tick the box. It shows a PIN and a QR code.
2. On the phone (same Wi-Fi):
   - **with the app:** open Project Chiaki → **📶 Smart link to a computer**. Your PC shows up in the list: tap it
     and type the PIN once. If it isn't listed, type the address shown on the PC (for example
     `192.168.1.20:8770`).
   - **without the app:** scan the QR code with the phone's camera and type the PIN.
3. You get the remote (keys, volume, key ♯/♭, Melody, M/F) and song search with Reserve.

If Windows asks whether Project Chiaki may use the network, allow **Private networks**: the phone needs it to find
and reach the PC.

---

## Using the player

### The screen
- The player's picture fills the window. **Double-click** it or press **⛶** for fullscreen.
- **🎮 Remote**, **📖 Songbook** and **🎛 Synth** open floating panels (on the PC each can be detached into its own
  window).
- **⚙ Settings:** disc, firmware, sound, display, mic, Smart link, keyboard keys, theme.

### Remote buttons
| Button | What it does |
|---|---|
| 0–9 | type a song number |
| **Enter** | reserve the song (it plays when the current one ends, or right away) |
| **First reserve** | put the song at the front of the queue |
| **✖ Cancel** | clear the number you typed; with nothing typed it opens the reserve list |
| **Stop** | stop the song |
| **Setup** | the player's setup menu. Password: **0000** |
| Key ♯ / ♭ / ♮ | change the song's key |
| **🎵 Melody** | the guide melody: the firmware's own setting where the player has one (Megapro: OFF / SMALL / NORMAL / BIG, KS5: ON / OFF) |
| **M/F** | male / female key (Megapro: 5 keys lower / higher; KS5: the firmware's MALE / FEMALE / ORIGINAL) |
| Vol + / −, Mute | volume |
| Megapro **Karaoke** key | play / pause |

Melody, M/F and tempo go back to the song's own after every song.

### Keyboard (default; change under ⚙ Settings → Keys)
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
| `V` | Change video |

### Settings tabs
- **Player & sound:** SoundFont synth (built in) or a Windows MIDI device; voice on number keys, chorus, random
  scoring.
- **🎛 Synth panel:** volume, per-channel volume and mute, transpose, instruments, **reverb** and a
  **bass / mid / treble** equalizer.
- **Display:** zoom and **🎯 Adjust display area** (PS5 style): arrow keys or the **− / +** buttons until the
  marks just touch the screen edges, then **✔ Done**.
- **Mic (PC):** hear your PC microphone through the speakers, with echo and mic volume.
- **📶 Smart link:** see above.

Your settings, the player's setup and the quick boot are saved and come back next time. For factory settings,
delete `KaraokeEmu.eeprom.json` in `Saved Settings`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **Black screen** | check the firmware and the disc are from the same player (a KS5 firmware needs a KS5 disc). Still black? **Message me** with `Logs\KaraokeEmu.log`: https://linktr.ee/mitsukisato |
| No moving picture / music video has no sound | ffmpeg is missing (Windows: `ffmpeg.exe` next to the exe; Android: see step 3) |
| No music | no SoundFont: put an `.sf2` in `Soundfont`. On Android, ⚙ Settings → Sound shows the problem in red; tap it for details |
| Music slow | close other heavy programs. On Android: the phone is too slow (see the Android section) |
| Lyrics on music videos early or late | ⚙ Settings → Player → Music video delay |
| Setup asks for a password | **0000** |
| Phone can't find the PC (Smart link) | same Wi-Fi? Windows firewall: allow Project Chiaki on Private networks; or type the address |

---

## Build from source

**Windows program:** install **Python 3.9+** from https://www.python.org (tick *Add python.exe to PATH*), keep
`kecpu.dll` (the fast CPU core, built from `fastcpu/cpu.c`) in the source folder and run **`build_exe.bat`** →
`dist\Project Chiaki.exe`. Leave `ffmpeg.exe` out of the source folder for a public build (otherwise it is packed
inside the exe). Run without building: `python karaoke_emu.py`.

**Android app:** install Android Studio once (start it and finish its setup), then run
**`android\build_apk.bat`** → `android\Project Chiaki.apk`. It sets up Java 21 and Python 3.11 by itself if they
are missing. See `android/README_ANDROID.md`.

---

## Third-party software: ffmpeg

ffmpeg is **not** part of Project Chiaki. It is FFmpeg (https://ffmpeg.org), a separate program licensed under the
LGPL / GPL. The ffmpeg files offered on this project's Releases page are unmodified FFmpeg:

- **Android** (`ffmpeg-android-arm64`, `ffmpeg-android-x86_64`): FFmpeg 6.1.1, LGPL 2.1, built statically with
  musl. Source: https://ffmpeg.org/releases/ffmpeg-6.1.1.tar.xz. Build options: `android/FFMPEG_BUILD.md`.
- **Windows** (`ffmpeg.exe`): the "release essentials" build from https://www.gyan.dev/ffmpeg/builds/ (GPL);
  its source is linked on that page and at https://ffmpeg.org/download.html.

---

**Credits:** ChiakiMK · built with Claude (Anthropic)

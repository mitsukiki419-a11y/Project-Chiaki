# What is not in this repository

Project Chiaki is only the emulator. To use it you need your own:

- **Firmware** (e.g. `MP100NSP.BIN`, `aprom_4m.bin`): dumped from your own player.
- **Discs** (`.iso`): images of your own song discs.
- **A SoundFont** (`.sf2`) for MIDI songs.
- **ffmpeg**: `ffmpeg.exe` for Windows (https://ffmpeg.org/download.html). For Android, a static Linux build:
  `arm64` for phones, `amd64` (rename it `ffmpeg-x86_64`) for PC Android emulators
  (https://johnvansickle.com/ffmpeg/).

None of these are included or distributed with this project.

## Building

- Windows program: run `build_exe.bat` (needs Python 3.9+ from python.org). `kecpu.dll` is the CPU core
  built from `fastcpu/cpu.c`.
- Android app: see `android/README_ANDROID.md` and run `android/build_apk.bat`. The `libkecpu.so` files in
  `android/app/src/main/jniLibs/` are the same CPU core built for Android.

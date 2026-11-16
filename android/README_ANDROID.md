# Project Chiaki for Android

The same emulator as the Windows program, in an Android app. You build it on your PC.

## Build it (once per update)
1. Install **Android Studio** from https://developer.android.com/studio (all default options).
   Start it once and finish its first-start setup: it downloads the Android SDK. Then you can close it.
2. Install **Python 3.11** from https://www.python.org/downloads/release/python-3119/ ("Windows installer (64-bit)").
   It sits next to any other Python you have; the build uses it to prepare the app's Python (it must be 3.11).
3. Double-click **build_apk.bat** in this folder. The first build downloads about 1-2 GB (Python for Android,
   numpy, build tools) and takes several minutes; later builds are quick.
4. The app is **Project Chiaki.apk** in this folder.

Each build takes the emulator's Python files from the Claude folder above, so the phone always gets the same
version as the Windows program.

## Put it on the phone
1. Copy **Project Chiaki.apk** to the phone and open it. Allow installing apps from this source when asked.
   The phone may say the app was built for an older Android version: that is on purpose (see below) - install anyway.
2. Open Project Chiaki. Under **Play on this phone** it says where its files go: the folder **Project Chiaki** at the
   top of the phone's storage. Tap **📁 Use SD card instead** to keep them on the SD card (tap again to go back).
3. Tap **▶ Play on this phone** once. Android asks for file access ("All files access" on Android 11 and later):
   allow it, then go back to the app. The folders are made: BIOS, Discs, Soundfont, BGV, Saved Settings, Logs.
   (Without the permission, or if the SD card does not let apps write at its top, the app uses its own folder
   `Android\data\com.projectchiaki.app\files` instead - the starting screen shows which.)
4. Copy in, with the phone's file manager or from the PC over USB (**File transfer**):
   - firmware files into **BIOS**
   - disc images (.iso) into **Discs**
   - one SoundFont into **Soundfont** - a small one (the 9 MB Roland SC-55 or the 20 MB Dreamsam). The 460 MB and
     1.3 GB ones are too big for a phone's memory.
   - background videos into **BGV** (optional)
   - ffmpeg from `android\phone-files\` straight into **Project Chiaki** (next to the BIOS folder): the file
     **ffmpeg** for a phone or tablet, **ffmpeg-x86_64** for an Android emulator on a PC (LDPlayer, BlueStacks ...).
     Without it the app runs, but shows no pictures or video.
5. Tap **▶ Play on this phone** and choose the firmware and disc as on the PC. ⛶ fills the screen with the picture;
   Back leaves it.

## Notes
- Needs a 64-bit phone or tablet (nearly all from 2017 on), or a 64-bit Android emulator on a PC (LDPlayer 9 ...).
- Back twice closes the app when the emulator runs (it stops with it). In Smart link, Back returns to the start screen.
- The app targets Android 9 on purpose: newer targets may not run ffmpeg from the phone's storage, and ffmpeg is
  not packed into the app.

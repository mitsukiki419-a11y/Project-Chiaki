@echo off
REM Builds KaraokeEmu.exe (single file) on Windows. Requires Python 3.9+ from python.org (tick "Add to PATH").
REM Optional: put ffmpeg.exe in this folder first and it will be bundled inside the exe.
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
python -m pip install --upgrade pyinstaller numpy || (echo pip failed & pause & exit /b 1)
set EXTRA=
if exist ffmpeg.exe (echo Bundling ffmpeg.exe & set EXTRA=--add-binary "ffmpeg.exe;.")
if not exist kecpu.dll (echo kecpu.dll missing - it is the fast CPU core, keep it next to this script & pause & exit /b 1)
set EXTRA=%EXTRA% --add-binary "kecpu.dll;."
python -m PyInstaller --onefile --clean --name KaraokeEmu --hidden-import livesynth --hidden-import sf2synth --hidden-import winperf --hidden-import songdisc --hidden-import mvlyrics --hidden-import fwprofile %EXTRA% karaoke_emu.py || (echo build failed & pause & exit /b 1)
echo.
echo Built: dist\KaraokeEmu.exe
echo Put your MP100NSP.BIN next to it (or enter its path in the page).
echo Put dreamsam_2695.sf2 (or any .sf2) next to it for song playback.
if not exist ffmpeg.exe echo Note: ffmpeg.exe not bundled - put ffmpeg.exe next to KaraokeEmu.exe to see the firmware's pictures.
pause

@echo off
REM Builds "Project Chiaki.exe" (Project : Chiaki, formerly KaraokeEmu; single file) on Windows. Requires Python 3.9+ from python.org (tick "Add to PATH").
REM The program opens in its own window (pywebview + Microsoft Edge WebView2, part of Windows 10/11).
REM Optional: put ffmpeg.exe in this folder first and it will be bundled inside the exe.
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
python -m pip install --upgrade pyinstaller numpy pywebview qrcode || (echo pip failed & pause & exit /b 1)
set EXTRA=
if exist ffmpeg.exe (echo Bundling ffmpeg.exe & set EXTRA=--add-binary "ffmpeg.exe;.")
if not exist kecpu.dll (echo kecpu.dll missing - it is the fast CPU core, keep it next to this script & pause & exit /b 1)
set EXTRA=%EXTRA% --add-binary "kecpu.dll;."
if not exist ProjectChiaki.ico (echo ProjectChiaki.ico missing - keep it next to this script & pause & exit /b 1)
python -m PyInstaller --onefile --windowed --clean --name "Project Chiaki" --icon "ProjectChiaki.ico" --add-data "ProjectChiaki.ico;." --hidden-import livesynth --hidden-import sf2synth --hidden-import winperf --hidden-import songdisc --hidden-import mvlyrics --hidden-import fwprofile --hidden-import songlists --hidden-import webview --collect-all webview --hidden-import qrcode --collect-submodules qrcode %EXTRA% karaoke_emu.py || (echo build failed & pause & exit /b 1)
echo.
echo Built: "dist\Project Chiaki.exe"
echo Put your MP100NSP.BIN next to it (or enter its path in the page).
echo Put dreamsam_2695.sf2 (or any .sf2) next to it for song playback.
if not exist ffmpeg.exe echo Note: ffmpeg.exe not bundled - put ffmpeg.exe next to "Project Chiaki.exe" to see the firmware's pictures.
pause

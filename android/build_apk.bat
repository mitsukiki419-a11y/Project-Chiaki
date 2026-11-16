@echo off
setlocal EnableDelayedExpansion
REM Builds the Android app: android\Project Chiaki.apk
REM Needs Android Studio installed once (its first-start setup downloads the Android SDK).
cd /d "%~dp0"

REM The Android build tools (Gradle 8.7) run on Java 17 or 21 only: a newer Java (e.g. 25) fails with
REM "Unsupported class file major version". Use the first Java 17 / 21 found; if there is none, a Java 21 is
REM downloaded once into android\.jdk (about 190 MB).
set "JDK="
for %%J in ("%ProgramFiles%\Android\Android Studio\jbr" "%LOCALAPPDATA%\Programs\Android Studio\jbr" "%JAVA_HOME%") do (
  if not defined JDK call :tryjdk "%%~J"
)
if not defined JDK if exist ".jdk" for /d %%J in (".jdk\*") do if not defined JDK call :tryjdk "%%~fJ"
if not defined JDK if exist "%ProgramFiles%\Eclipse Adoptium" for /d %%J in ("%ProgramFiles%\Eclipse Adoptium\jdk-21*" "%ProgramFiles%\Eclipse Adoptium\jdk-17*") do if not defined JDK call :tryjdk "%%~fJ"
if not defined JDK if exist "%ProgramFiles%\Java" for /d %%J in ("%ProgramFiles%\Java\jdk-21*" "%ProgramFiles%\Java\jdk-17*") do if not defined JDK call :tryjdk "%%~fJ"
if not defined JDK (
  echo No Java 17 / 21 found - downloading Java 21 ^(Eclipse Temurin^) once into android\.jdk ...
  if not exist ".jdk" mkdir ".jdk"
  curl -L --fail -o ".jdk\jdk21.zip" "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jdk/hotspot/normal/eclipse"
  if errorlevel 1 (
    echo The download failed. Install Java 21 from https://adoptium.net ^(Temurin 21, Windows x64 .msi^), then run this again.
    pause
    exit /b 1
  )
  tar -xf ".jdk\jdk21.zip" -C ".jdk"
  del ".jdk\jdk21.zip"
  for /d %%J in (".jdk\*") do if not defined JDK call :tryjdk "%%~fJ"
)
if not defined JDK (
  echo Java 17 / 21 could not be set up. Install Java 21 from https://adoptium.net, then run this again.
  pause
  exit /b 1
)
set "JAVA_HOME=%JDK%"
set "PATH=%JDK%\bin;%PATH%"
echo Using Java: %JDK%

if not defined ANDROID_HOME set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
if not exist "%ANDROID_HOME%" (
  echo The Android SDK was not found in %ANDROID_HOME%.
  echo Install Android Studio, start it once and finish its setup, then run this again.
  pause
  exit /b 1
)
set "PY="
for /f "delims=" %%P in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%ProgramFiles%\Python311\python.exe" set "PY=%ProgramFiles%\Python311\python.exe"
if not defined PY (
  REM the phone's Python is 3.11, and the build needs the same version on the PC (other versions stay as they are)
  echo Python 3.11 not found - downloading and installing it once ^(for this user only, about 25 MB^) ...
  curl -L --fail -o "%TEMP%\python-3.11.9-amd64.exe" "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
  if errorlevel 1 (
    echo The download failed. Install Python 3.11 from https://www.python.org/downloads/release/python-3119/ then run this again.
    pause
    exit /b 1
  )
  "%TEMP%\python-3.11.9-amd64.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Shortcuts=0 AssociateFiles=0
  del "%TEMP%\python-3.11.9-amd64.exe"
  if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
)
if not defined PY (
  echo Python 3.11 could not be installed. Install it from https://www.python.org/downloads/release/python-3119/ then run this again.
  pause
  exit /b 1
)
echo Using Python: %PY%
REM the build uses its pip to fetch the phone's packages
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 "%PY%" -m ensurepip --upgrade
REM a Gradle daemon left running from the failed try still uses the other Java
call gradlew.bat --stop >nul 2>nul
echo Building - the first time downloads about 1-2 GB and takes several minutes...
set TRIES=0
:build
set /a TRIES+=1
call gradlew.bat assembleDebug "-PchiakiPython=%PY%"
if errorlevel 1 if %TRIES% LSS 3 (
  echo.
  echo The build stopped ^(a download may have timed out^) - trying again, %TRIES% of 3 ...
  goto build
)
if errorlevel 1 (
  echo.
  echo The build failed - send Claude the messages above.
  pause
  exit /b 1
)
copy /y "app\build\outputs\apk\debug\app-debug.apk" "Project Chiaki.apk" >nul
echo.
echo Done: android\Project Chiaki.apk - copy it to your phone and open it to install.
pause
exit /b 0

:tryjdk
REM sets JDK to %1 when it holds Java 17 or 21
if "%~1"=="" exit /b 0
if not exist "%~1\bin\java.exe" exit /b 0
set "VER="
for /f "tokens=3" %%V in ('"%~1\bin\java.exe" -version 2^>^&1 ^| findstr /i version') do if not defined VER set "VER=%%~V"
if not defined VER exit /b 0
for /f "tokens=1 delims=." %%M in ("!VER!") do set "MAJ=%%M"
if "!MAJ!"=="17" set "JDK=%~1"
if "!MAJ!"=="21" set "JDK=%~1"
exit /b 0

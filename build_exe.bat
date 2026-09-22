@echo off
rem ============================================================
rem  Build both editions of EthernetSwitch
rem    fast\EthernetSwitch\EthernetSwitch.exe  - onedir, starts in ~0.25s  (daily use)
rem    dist\EthernetSwitch.exe                 - onefile, ~11MB portable   (starts in ~2.5s)
rem
rem  A onefile build must unpack ~25MB into the temp folder on
rem  every launch, so it is inherently slower - keep it only for
rem  handing the tool to someone else.
rem
rem  Requires: Python 3.8+ with tkinter. PyInstaller is installed
rem  into a local build venv (.build-venv), so your system Python
rem  stays clean.
rem ============================================================
setlocal enabledelayedexpansion
set "DIR=%~dp0"
set "VENV=%DIR%.build-venv"
set "ICON=%DIR%assets\icon.ico"
set "PNG=%DIR%assets\icon.png"

rem ---- locate a Python interpreter
set "BASEPY="
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
) do if not defined BASEPY if exist "%%~P" set "BASEPY=%%~P"
if not defined BASEPY where python.exe >nul 2>nul && set "BASEPY=python.exe"

if not defined BASEPY (
  echo.
  echo Python 3 was not found on this computer.
  echo Please install it from https://www.python.org/downloads/
  echo ^(keep the "tcl/tk" option enabled^)
  echo.
  pause
  exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
  echo [0/3] Creating build venv with !BASEPY! ...
  "!BASEPY!" -m venv "%VENV%"
  if errorlevel 1 goto :failed
  "%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q pyinstaller
  if errorlevel 1 goto :failed
)

set "PYI=%VENV%\Scripts\pyinstaller.exe"

echo [1/3] Building fast onedir edition ...
"%PYI%" --noconfirm --clean --onedir --windowed --name EthernetSwitch ^
  --icon "%ICON%" --add-data "%ICON%;assets" --add-data "%PNG%;assets" ^
  --distpath "%DIR%fast" --workpath "%DIR%build_fast" --specpath "%DIR%build_fast" ^
  "%DIR%app.py"
if errorlevel 1 goto :failed

echo [2/3] Building portable onefile edition ...
"%PYI%" --noconfirm --clean --onefile --windowed --name EthernetSwitch ^
  --icon "%ICON%" --add-data "%ICON%;assets" --add-data "%PNG%;assets" ^
  --distpath "%DIR%dist" --workpath "%DIR%build" --specpath "%DIR%build" ^
  "%DIR%app.py"
if errorlevel 1 goto :failed

echo [3/3] Done.
if exist "%DIR%fast\EthernetSwitch\EthernetSwitch.exe" echo   fast     : %DIR%fast\EthernetSwitch\EthernetSwitch.exe
if exist "%DIR%dist\EthernetSwitch.exe"               echo   portable : %DIR%dist\EthernetSwitch.exe
pause
exit /b 0

:failed
echo.
echo Build FAILED - please check the messages above.
pause
exit /b 1

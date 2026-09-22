@echo off
rem ============================================================
rem  Ethernet Switch launcher
rem  Picks a Python interpreter that has tkinter, then starts
rem  the app windowless via pythonw.exe.
rem  Requires: Python 3.8+ with tkinter (included in the
rem  official installer - keep the "tcl/tk" option checked).
rem ============================================================
setlocal
set "DIR=%~dp0"
set "PYW="

rem 1) per-user installs and the common default locations
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
  "C:\Python313\pythonw.exe"
  "C:\Python312\pythonw.exe"
) do if not defined PYW if exist "%%~P" set "PYW=%%~P"

rem 2) fall back to PATH
if not defined PYW where pythonw.exe >nul 2>nul && set "PYW=pythonw.exe"
if not defined PYW where pyw.exe >nul 2>nul && set "PYW=pyw.exe"
if not defined PYW where python.exe >nul 2>nul && set "PYW=python.exe"

if not defined PYW (
  echo.
  echo Python 3 was not found on this computer.
  echo Please install it from https://www.python.org/downloads/
  echo ^(keep the "tcl/tk" option enabled^)
  echo.
  pause
  exit /b 1
)

start "" "%PYW%" "%DIR%app.py" %*
exit /b 0

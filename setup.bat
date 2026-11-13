@echo off
rem ===========================================================================
rem  Remote Mic - Windows client (RC003)
rem  Rapid source setup: creates the Python 3.12 environment, installs the
rem  pinned dependencies, verifies the runtime, and opens the settings window.
rem
rem  Usage:
rem    setup.bat            Set up, verify, restart dev session, and launch.
rem    setup.bat /no-run    Set up and verify only.
rem    setup.bat /rebuild   Recreate .venv from scratch, then install.
rem    setup.bat /test      Also run the unit test suite.
rem    setup.bat /?         Show this help.
rem
rem  The script is idempotent. It reuses an existing Python 3.12 environment
rem  and skips dependency installation while the pinned requirements remain
rem  unchanged. "uv" is used when available because it resolves and installs
rem  the pinned set substantially faster than pip; otherwise pip is used.
rem ===========================================================================
setlocal EnableExtensions DisableDelayedExpansion
title Remote Mic - RC003 setup

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem ---- Locate apps\windows\rc003 -------------------------------------------
set "PROJECT="
if exist "%ROOT%\apps\windows\rc003\src\ovb_rc003\__main__.py" set "PROJECT=%ROOT%\apps\windows\rc003"
if not defined PROJECT if exist "%ROOT%\src\ovb_rc003\__main__.py" set "PROJECT=%ROOT%"
if not defined PROJECT goto :NoProject

rem ---- Interpret the command line ------------------------------------------
set "LAUNCH=1"
set "REBUILD="
set "RUN_TESTS="

:ParseArgs
if "%~1"=="" goto :ArgsDone
set "MATCHED="
if /i "%~1"=="/no-run" (
    set "MATCHED=1"
    set "LAUNCH="
)
if /i "%~1"=="/setup" (
    set "MATCHED=1"
    set "LAUNCH="
)
if /i "%~1"=="/rebuild" (
    set "MATCHED=1"
    set "REBUILD=1"
)
if /i "%~1"=="/test" (
    set "MATCHED=1"
    set "RUN_TESTS=1"
)
if /i "%~1"=="/?" (
    set "MATCHED=1"
    goto :Usage
)
if /i "%~1"=="/help" (
    set "MATCHED=1"
    goto :Usage
)
if /i "%~1"=="-h" (
    set "MATCHED=1"
    goto :Usage
)
if not defined MATCHED goto :BadArgument
shift
goto :ParseArgs

:ArgsDone
echo [setup] project: "%PROJECT%"
call :WarnIfElevated

set "VENV=%PROJECT%\.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "VENV_PYW=%VENV%\Scripts\pythonw.exe"

rem ---- Reuse, rebuild, or create the environment ---------------------------
if not defined REBUILD goto :CheckVenv
call :RemoveVenv
if errorlevel 1 goto :VenvBusy

:CheckVenv
if not exist "%VENV_PY%" goto :CreateVenv
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto :CreateVenv
echo [setup] environment: reusing "%VENV%"
goto :Dependencies

:CreateVenv
call :RemoveVenv
if errorlevel 1 goto :VenvBusy
call :FindUv
if not defined HAVE_UV goto :CreateWithVenvModule
echo [setup] environment: creating "%VENV%" with uv, Python 3.12
uv venv --python 3.12 --seed "%VENV%"
if exist "%VENV_PY%" goto :Dependencies
echo [setup] uv did not produce a usable environment; falling back to the venv module.
:CreateWithVenvModule
call :FindBootstrapPython
if errorlevel 1 goto :NoPython
echo [setup] environment: creating "%VENV%" with %BOOTSTRAP% -m venv
%BOOTSTRAP% -m venv "%VENV%"
if not exist "%VENV_PY%" goto :NoPython

rem ---- Install the pinned dependencies, unless already current -------------
:Dependencies
set "REQUIREMENTS=%PROJECT%\requirements-dev.txt"
set "STAMP=%VENV%\.setup-stamp"
set "HASH_FILE=%TEMP%\rc003-requirements.hash"
if exist "%HASH_FILE%" del /q "%HASH_FILE%" >nul 2>&1
"%VENV_PY%" -c "import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], 'rb').read() + open(sys.argv[2], 'rb').read()).hexdigest())" "%PROJECT%\requirements.txt" "%PROJECT%\requirements-dev.txt" > "%HASH_FILE%" 2>nul
set "REQ_HASH="
if not exist "%HASH_FILE%" goto :InstallDependencies
for /f "usebackq delims=" %%H in ("%HASH_FILE%") do set "REQ_HASH=%%H"
del /q "%HASH_FILE%" >nul 2>&1
if not defined REQ_HASH goto :InstallDependencies
set "INSTALLED_HASH="
if exist "%STAMP%" set /p INSTALLED_HASH=<"%STAMP%"
if /i "%REQ_HASH%"=="%INSTALLED_HASH%" goto :DependenciesCurrent
goto :InstallDependencies

:DependenciesCurrent
echo [setup] dependencies: already current
goto :Verify

:InstallDependencies
echo [setup] dependencies: installing the pinned set (first run downloads)
call :FindUv
if not defined HAVE_UV goto :InstallWithPip
uv pip install --python "%VENV_PY%" -r "%REQUIREMENTS%"
if errorlevel 1 goto :InstallFailed
goto :DependenciesInstalled

:InstallWithPip
"%VENV_PY%" -m pip install --disable-pip-version-check -r "%REQUIREMENTS%"
if errorlevel 1 goto :InstallFailed

:DependenciesInstalled
>"%STAMP%" echo %REQ_HASH%
echo [setup] dependencies: installed

rem ---- Verify the runtime without touching GUI, BLE, HID, or audio ---------
:Verify
set "PYTHONPATH=%PROJECT%\src"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
echo [setup] verifying: importing every module and loading the Qt runtime
pushd "%PROJECT%"
"%VENV_PY%" -m ovb_rc003 --dry-run
if errorlevel 1 goto :VerifyFailed
"%VENV_PY%" -m ovb_rc003 --qt-runtime-check
if errorlevel 1 goto :VerifyFailed
if defined RUN_TESTS goto :RunTests
popd
goto :Launch

:RunTests
echo [setup] testing: running the unit test suite
"%VENV_PY%" -m unittest discover -s tests -t . -p "test_*.py"
if errorlevel 1 goto :TestsFailed
popd
goto :Launch

rem ---- Launch the settings window ------------------------------------------
:Launch
if not defined LAUNCH goto :Summary
if not exist "%VENV_PYW%" set "VENV_PYW=%VENV_PY%"
rem The development-session marker lets build\stop-dev.ps1 stop only this copy.
set "REMOTE_MIC_DEV_SESSION=1"
echo [setup] permissions: setup itself needs no Administrator.
echo [setup]             Windows may ask later when you start the Google remote
echo [setup]             service (elevated receiver), fix Xiaomi custom keys,
echo [setup]             or install VB-CABLE from the Voice page.
set "STOP_DEV=%PROJECT%\build\stop-dev.ps1"
if not exist "%STOP_DEV%" goto :LaunchStart
echo [setup] restarting: stopping any marked development session
powershell -NoProfile -ExecutionPolicy Bypass -File "%STOP_DEV%"
if errorlevel 1 goto :StopDevFailed
:LaunchStart
echo [setup] starting: settings window
start "" "%VENV_PYW%" -m ovb_rc003 --settings --remote-mic-dev-session

:Summary
echo.
echo [setup] ready.
echo [setup] environment : "%VENV%"
echo [setup] launch      : build\run-dev.ps1
echo [setup] stop        : build\stop-dev.ps1
echo [setup] elevation trace:
echo [setup]   setup.bat does not show UAC. If Windows asks later, open:
echo [setup]   %LOCALAPPDATA%\RemoteMic\RC003\logs\app.log
echo [setup]   Search for: elevation_prompt
echo [setup]   kind=chromecast_worker  - Google remote service start
echo [setup]   kind=hid_helper_setup  - Xiaomi custom key helper
echo [setup]   kind=vb_cable_setup    - Voice page VB-CABLE install
echo [setup]   kind=voice_program     - optional elevated IME launch
set "QUICK_START=%PROJECT%\build\install-dev-quick-start.ps1"
if exist "%QUICK_START%" (
    echo [setup] quick start: refreshing Start menu shortcut and Ctrl+Alt+M hotkey
    powershell -NoProfile -ExecutionPolicy Bypass -File "%QUICK_START%" >nul 2>&1
)
exit /b 0

rem ===========================================================================
rem  Subroutines
rem ===========================================================================

:WarnIfElevated
rem Detect an elevated setup console (optional "Run as administrator").
fltmc >nul 2>&1
if errorlevel 1 exit /b 0
echo [setup] note: This window is running as Administrator.
echo [setup]       You do not need elevated rights for setup or the venv.
echo [setup]       Close this window, double-click setup.bat normally, and
echo [setup]       run the app as a standard user. Elevation is requested
echo [setup]       later only when the app starts the Google remote receiver.
exit /b 0

:FindUv
set "HAVE_UV="
where uv >nul 2>&1
if not errorlevel 1 set "HAVE_UV=1"
exit /b 0

:FindBootstrapPython
set "BOOTSTRAP="
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "BOOTSTRAP=py -3.12"
if defined BOOTSTRAP exit /b 0
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 set "BOOTSTRAP=python"
if not defined BOOTSTRAP exit /b 1
exit /b 0

:RemoveVenv
if not exist "%VENV%" exit /b 0
echo [setup] environment: removing "%VENV%"
rmdir /s /q "%VENV%" >nul 2>&1
if exist "%VENV%" exit /b 1
exit /b 0

rem ===========================================================================
rem  Diagnostics
rem ===========================================================================

:BadArgument
echo [setup] error: unknown option "%~1".
echo.
goto :Usage

:Usage
echo.
echo Remote Mic - RC003 rapid source setup
echo.
echo   setup.bat            Set up, verify, restart dev session, and launch.
echo   setup.bat /no-run    Set up and verify only.
echo   setup.bat /rebuild   Recreate .venv from scratch, then install.
echo   setup.bat /test      Also run the unit test suite.
echo   setup.bat /?         Show this help.
echo.
echo Requirements: 64-bit Windows 10 1809 or later and Python 3.12.
echo "uv" is used when available and accelerates dependency installation.
echo.
echo Permissions: run this script with a normal double-click. Do not use
echo "Run as administrator" for setup. UAC appears only inside the app for
echo specific actions (see messages above).
exit /b 2

:NoProject
echo [setup] error: this script must be placed in the repository root or in
echo         apps\windows\rc003. Directory inspected: "%ROOT%"
exit /b 1

:NoPython
echo [setup] error: Python 3.12 was not found.
echo         Install it from https://www.python.org/downloads/ or install uv
echo         from https://docs.astral.sh/uv/ and run this script again.
exit /b 1

:VenvBusy
echo [setup] error: "%VENV%" could not be removed.
echo         Close any running copy of the application and run this script again.
exit /b 1

:InstallFailed
echo [setup] error: dependency installation failed.
echo         Check the network connection and run this script again.
exit /b 1

:VerifyFailed
popd
echo [setup] error: the runtime verification failed.
echo         Review the message above before starting the application.
exit /b 1

:TestsFailed
popd
echo [setup] error: the unit test suite reported failures.
exit /b 1

:StopDevFailed
echo [setup] error: could not stop the running development session.
echo         If a copy is stuck, fully quit from the tray or run:
echo         %STOP_DEV%
echo         Close any packaged RemoteMicRC003.exe before retrying setup.
exit /b 1

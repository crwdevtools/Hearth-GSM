@echo off
rem Start Hearth on Windows (Docker Desktop).
rem Checks Docker, creates .env on the first run, builds and starts the containers,
rem waits for the panel to answer, and opens it in your browser. Safe to run again any time.
rem Set HEARTH_NONINTERACTIVE=1 to take every default without asking (used by CI).
setlocal EnableExtensions
cd /d "%~dp0"
title Hearth

rem ----- Docker -----------------------------------------------------------------------------------
where docker >nul 2>nul
if errorlevel 1 (
  echo Docker isn't installed. Get Docker Desktop: https://www.docker.com/products/docker-desktop/
  goto fail
)

docker info >nul 2>nul
if not errorlevel 1 goto docker_ok
echo Starting Docker Desktop...
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
for /l %%i in (1,1,60) do (
  docker info >nul 2>nul && goto docker_ok
  timeout /t 2 /nobreak >nul
)
echo Docker didn't start. Open Docker Desktop, wait until it says it's running, then run this again.
goto fail

:docker_ok
rem Hearth's images are Linux images; Docker Desktop must be in Linux-containers mode.
set "OSTYPE="
for /f "usebackq delims=" %%o in (`docker info --format "{{.OSType}}" 2^>nul`) do set "OSTYPE=%%o"
if /i "%OSTYPE%"=="windows" (
  echo Docker Desktop is using Windows containers. Hearth needs Linux containers:
  echo right-click the Docker icon by the clock and choose "Switch to Linux containers...", then run this again.
  goto fail
)
docker compose version >nul 2>nul
if errorlevel 1 (
  echo Docker Compose is missing. Update Docker Desktop.
  goto fail
)

rem ----- First run: .env --------------------------------------------------------------------------
if exist .env goto have_env
echo First run: setting up .env
call :new_token || goto fail
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "$a = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()).Where({ $_.AddressFamily -eq 'InterNetwork' -and -not $_.ToString().StartsWith('169.254') }); if ($a.Count) { $a[0].ToString() }"`) do set "DETECTED=%%a"
if not defined DETECTED set "DETECTED=localhost"

:ask_drive
echo.
echo Which drive should game servers be installed on? Local drives:
powershell -NoProfile -Command "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { '   {0}   {1,6:N0} GB free of {2,6:N0} GB   {3}' -f $_.DeviceID, ($_.FreeSpace/1GB), ($_.Size/1GB), $_.VolumeName }"
set "DRIVE="
if not defined HEARTH_NONINTERACTIVE set /p "DRIVE=Drive letter [C]: "
if not defined DRIVE set "DRIVE=C"
powershell -NoProfile -Command "if ($env:DRIVE -notmatch '^[A-Za-z]:?\\?$') { exit 1 }"
if errorlevel 1 (echo Type just the drive letter, like D. & goto ask_drive)
set "DRIVE=%DRIVE:~0,1%"
if not exist "%DRIVE%:\" (echo Drive %DRIVE%: isn't available. Pick one from the list. & goto ask_drive)
set "DATA_DIR=%DRIVE%:\Hearth"
mkdir "%DATA_DIR%" 2>nul
if not exist "%DATA_DIR%\" (echo Couldn't create %DATA_DIR%. Pick another drive. & goto ask_drive)
echo Game servers will be installed in %DATA_DIR%

:ask_host
echo.
echo Friends join your servers at this machine's address: a domain, public IP, or LAN IP.
set "PUBLIC_HOST="
if not defined HEARTH_NONINTERACTIVE set /p "PUBLIC_HOST=Address to show players [%DETECTED%]: "
if not defined PUBLIC_HOST set "PUBLIC_HOST=%DETECTED%"
rem Checked through an environment variable, so quotes or symbols typed here can't break anything.
powershell -NoProfile -Command "if ($env:PUBLIC_HOST -notmatch '^[A-Za-z0-9.:-]+$') { exit 1 }"
if errorlevel 1 (echo That doesn't look like a hostname or IP address. Type it without http:// or a port. & goto ask_host)

rem Values reach PowerShell as environment variables, never pasted into the command itself.
powershell -NoProfile -Command "(Get-Content '.env.example') | ForEach-Object { if ($_ -like 'HEARTH_AGENT_TOKEN=*') { 'HEARTH_AGENT_TOKEN=' + $env:TOKEN } elseif ($_ -like 'HEARTH_PUBLIC_HOST=*') { 'HEARTH_PUBLIC_HOST=' + $env:PUBLIC_HOST } elseif ($_ -like 'HEARTH_DATA_DIR=*') { 'HEARTH_DATA_DIR=' + $env:DATA_DIR } else { $_ } } | Set-Content -Encoding ascii '.env'"
if errorlevel 1 (
  echo Couldn't write .env.
  goto fail
)
echo Saved settings to .env. Edit it later to change them.

:have_env
rem A .env copied by hand from .env.example has an empty token; Compose would refuse to start.
powershell -NoProfile -Command "if (Select-String -Path '.env' -Pattern '^HEARTH_AGENT_TOKEN=\S' -Quiet) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto token_ok
echo Generating the agent token in .env
call :new_token || goto fail
powershell -NoProfile -Command "$lines = @(Get-Content '.env' | Where-Object { $_ -notlike 'HEARTH_AGENT_TOKEN=*' }) + ('HEARTH_AGENT_TOKEN=' + $env:TOKEN); $lines | Set-Content -Encoding ascii '.env'"
if errorlevel 1 (
  echo Couldn't write .env.
  goto fail
)
:token_ok

set "PORT=8000"
for /f "tokens=2 delims==" %%p in ('findstr /b "HEARTH_PANEL_PORT=" .env') do set "PORT=%%p"
set "PORT=%PORT: =%"

rem ----- Start ------------------------------------------------------------------------------------
echo.
echo Building and starting Hearth. The first run takes a few minutes...
rem --pull fetches the latest Python base image, so its security fixes reach the panel and agent.
rem Offline, fall back to the copy already on this machine.
docker compose build --pull
if not errorlevel 1 goto built
echo Couldn't check for a newer base image; using the one on this machine.
docker compose build
if errorlevel 1 (
  echo.
  echo Building Hearth failed. The messages above say why.
  goto fail
)
:built
docker compose up -d
if errorlevel 1 (
  echo.
  echo Starting the containers failed. The messages above say why.
  goto fail
)

echo Waiting for the panel...
set "TRIES=0"
set "NEEDS_SETUP="
:wait
set /a TRIES+=1
rem exit 0 = up, 3 = up and waiting for its first admin account, 1 = not answering yet
powershell -NoProfile -Command "try { $r = Invoke-RestMethod -UseBasicParsing -TimeoutSec 3 ('http://localhost:' + $env:PORT + '/api/auth/state'); if ($r.needs_setup) { exit 3 } else { exit 0 } } catch { exit 1 }"
if errorlevel 3 (
  set "NEEDS_SETUP=1"
  goto ready
)
if not errorlevel 1 goto ready
if %TRIES% geq 90 (
  echo The panel didn't answer within 3 minutes. See what happened with:  docker compose logs panel
  goto fail
)
timeout /t 2 /nobreak >nul
goto wait

:ready
set "SHOW_HOST="
for /f "tokens=2 delims==" %%h in ('findstr /b "HEARTH_PUBLIC_HOST=" .env') do set "SHOW_HOST=%%h"
echo.
echo Hearth is running at http://localhost:%PORT%
if defined SHOW_HOST if /i not "%SHOW_HOST%"=="localhost" echo From other devices:  http://%SHOW_HOST%:%PORT%
for /f "tokens=2 delims==" %%d in ('findstr /b "HEARTH_DATA_DIR=" .env') do echo Game servers are stored in %%d
echo Open game ports in Windows Firewall and your router: 25565-25575 TCP for Minecraft Java,
echo 19132 UDP for Bedrock, 2456-2457 UDP for Valheim, 7777 TCP for Terraria, 7777-7778 for
echo Satisfactory, 34197 UDP for Factorio, 8211 UDP for Palworld, 28015-28017 for Rust.
echo Only open the ones for games you run.
echo Stop it with stop.bat
if defined NEEDS_SETUP (
  echo.
  echo Next: create your admin account at http://localhost:%PORT%
  echo Do it from this computer or your local network. Hearth only allows the first account from there.
)
if defined HEARTH_NONINTERACTIVE exit /b 0
start "" "http://localhost:%PORT%"
echo.
pause
exit /b 0

rem ----- helpers ----------------------------------------------------------------------------------
:new_token
rem 32 bytes from the system's cryptographic random generator, as 64 hex characters.
set "TOKEN="
for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b.ForEach({ $_.ToString('x2') }))"`) do set "TOKEN=%%t"
if not defined TOKEN (
  echo Couldn't generate a token with PowerShell.
  exit /b 1
)
exit /b 0

:fail
echo.
if not defined HEARTH_NONINTERACTIVE pause
exit /b 1

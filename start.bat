@echo off
rem Start Hearth on Windows (Docker Desktop).
rem Checks Docker, creates .env on the first run, builds and starts the containers,
rem waits for the panel to answer, and opens it in your browser. Safe to run again any time.
setlocal EnableExtensions
cd /d "%~dp0"
title Hearth

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
docker compose version >nul 2>nul
if errorlevel 1 (
  echo Docker Compose is missing. Update Docker Desktop.
  goto fail
)

if exist .env goto have_env
echo First run: setting up .env
for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')"`) do set "TOKEN=%%t"
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "$a = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()).Where({ $_.AddressFamily -eq 'InterNetwork' -and -not $_.ToString().StartsWith('169.254') }); if ($a.Count) { $a[0].ToString() }"`) do set "DETECTED=%%a"
if not defined DETECTED set "DETECTED=localhost"
if not defined TOKEN (
  echo Couldn't generate a token with PowerShell.
  goto fail
)

:ask_drive
echo.
echo Which drive should game servers be installed on? Local drives:
powershell -NoProfile -Command "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { '   {0}   {1,6:N0} GB free of {2,6:N0} GB   {3}' -f $_.DeviceID, ($_.FreeSpace/1GB), ($_.Size/1GB), $_.VolumeName }"
set "DRIVE="
set /p "DRIVE=Drive letter [C]: "
if not defined DRIVE set "DRIVE=C"
set "DRIVE=%DRIVE::=%"
set "DRIVE=%DRIVE:\=%"
echo %DRIVE%| findstr /r /i /x "[a-z]" >nul || (echo Type just the drive letter, like D. & goto ask_drive)
if not exist "%DRIVE%:\" (echo Drive %DRIVE%: isn't available. Pick one from the list. & goto ask_drive)
set "DATA_DIR=%DRIVE%:\Hearth"
mkdir "%DATA_DIR%" 2>nul
if not exist "%DATA_DIR%\" (echo Couldn't create %DATA_DIR%. Pick another drive. & goto ask_drive)
echo Game servers will be installed in %DATA_DIR%
echo.

echo Friends join your servers at this machine's address: a domain, public IP, or LAN IP.
set "PUBLIC_HOST="
set /p "PUBLIC_HOST=Address to show players [%DETECTED%]: "
if not defined PUBLIC_HOST set "PUBLIC_HOST=%DETECTED%"
powershell -NoProfile -Command "(Get-Content '.env.example') -replace '^HEARTH_AGENT_TOKEN=.*', 'HEARTH_AGENT_TOKEN=%TOKEN%' -replace '^HEARTH_PUBLIC_HOST=.*', 'HEARTH_PUBLIC_HOST=%PUBLIC_HOST%' -replace '^HEARTH_DATA_DIR=.*', 'HEARTH_DATA_DIR=%DATA_DIR%' | Set-Content -Encoding ascii '.env'"
if errorlevel 1 (
  echo Couldn't write .env.
  goto fail
)
echo Saved settings to .env. Edit it later to change them.

:have_env
set "PORT=8000"
for /f "tokens=2 delims==" %%p in ('findstr /b "HEARTH_PANEL_PORT=" .env') do set "PORT=%%p"

echo.
echo Building and starting Hearth. The first run takes a few minutes...
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo Starting the containers failed. The messages above say why.
  goto fail
)

echo Waiting for the panel...
for /l %%i in (1,1,90) do (
  curl.exe -fs -o nul "http://localhost:%PORT%/api/auth/state" && goto ready
  timeout /t 2 /nobreak >nul
)
echo The panel didn't answer within 3 minutes. See what happened with:  docker compose logs panel
goto fail

:ready
echo.
echo Hearth is running at http://localhost:%PORT%
for /f "tokens=2 delims==" %%d in ('findstr /b "HEARTH_DATA_DIR=" .env') do echo Game servers are stored in %%d
echo Open game ports in Windows Firewall and your router: 25565-25575 TCP for Minecraft Java,
echo 19132 UDP for Bedrock, 2456-2457 UDP for Valheim.
echo Stop it with stop.bat
start "" "http://localhost:%PORT%"
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1

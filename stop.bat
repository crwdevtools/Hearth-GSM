@echo off
rem Stop the Hearth panel and the local agent. Game server files and settings are kept.
rem Game servers that were running stay up; stop them in the panel first if you want them off.
cd /d "%~dp0"
docker compose down
echo Hearth stopped. Start it again with start.bat
pause

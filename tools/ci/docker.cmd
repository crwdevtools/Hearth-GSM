@echo off
rem Stand-in for docker.exe when testing start.bat in CI: records each call, says "linux".
echo %*>> "%DOCKER_CALLS%"
if "%1"=="info" if "%2"=="--format" echo linux
exit /b 0

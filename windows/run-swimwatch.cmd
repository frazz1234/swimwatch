@echo off
rem Runs SwimWatch from this folder (config.yaml and .env here). Restarts it if it stops.
cd /d %~dp0
set PYTHONUTF8=1
if not exist data mkdir data
:loop
.venv\Scripts\python.exe -m swimwatch --config config.yaml >> data\console.log 2>&1
timeout /t 15 /nobreak > nul
goto loop

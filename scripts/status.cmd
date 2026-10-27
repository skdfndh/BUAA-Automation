@echo off
set PYTHONUTF8=1
if not "%~1"=="" set "AUTOBOYA_HOME=%~1"
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m autoboya status --interactive
if errorlevel 1 pause

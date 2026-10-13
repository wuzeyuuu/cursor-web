@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
echo Open http://127.0.0.1:8786/login
echo Keep this window open while using the page.
python server.py
if errorlevel 1 pause

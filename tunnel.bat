@echo off
setlocal
cd /d "%~dp0"
set "CLOUDFLARED=C:\Program Files (x86)\cloudflared\cloudflared.exe"
if not exist "%CLOUDFLARED%" set "CLOUDFLARED=cloudflared"
echo Mapping local http://127.0.0.1:8786 through Cloudflare Tunnel.
echo Keep this window AND start.bat running. The public URL is printed below.
echo.
"%CLOUDFLARED%" tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8786
if errorlevel 1 pause

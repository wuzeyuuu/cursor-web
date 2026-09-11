@echo off
setlocal
cd /d "%~dp0"
echo Named Cloudflare Tunnel "cursor" already runs as the Windows service "cloudflared".
echo Do NOT start a Quick Tunnel here — that is the trycloudflare.com URL that changes every time.
echo.
echo Public hostname to add in Cloudflare Zero Trust (once):
echo   cursor.klinedance.kdns.fr  -^>  http://127.0.0.1:8786
echo Tunnel: 27a9add3-53d2-45bf-8cfd-b2ba1a3b6853
echo Dashboard:
echo   https://one.dash.cloudflare.com/0ad4d8f15263c3e9f42099e2a930d5b9/networks/tunnels
echo.
sc query cloudflared | findstr /I "STATE"
curl.exe -s -o nul -w "local 8786: HTTP %%{http_code}\n" --max-time 5 http://127.0.0.1:8786/login
echo.
echo After the public hostname exists, keep start.bat running and open:
echo   https://cursor.klinedance.kdns.fr/login
echo.
pause

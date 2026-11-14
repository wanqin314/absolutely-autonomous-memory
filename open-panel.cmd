@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 21600 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto openbrowser
start "" pythonw -X utf8 "%~dp0webui.py" --port 21600
ping -n 4 127.0.0.1 >nul
:openbrowser
start "" chrome --app=http://127.0.0.1:21600/ --window-size=1400,900

@echo off
cd /d "%~dp0"
start "" "%SystemRoot%\System32\wscript.exe" //nologo "%~dp0start_console.vbs"
exit /b 0

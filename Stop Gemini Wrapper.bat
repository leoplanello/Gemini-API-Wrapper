@echo off
REM Stops the Gemini proxy gracefully (falls back to force-kill after 20s).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop Gemini Wrapper.ps1"
timeout /t 2 > nul

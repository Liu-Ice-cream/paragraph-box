@echo off
setlocal
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 "%~dp0scripts\open_settings.py"
) else (
  start "" pythonw "%~dp0scripts\open_settings.py"
)
endlocal

@echo off
setlocal
set "FLOW_EXE=%~dp0.runtime\FlowLauncher-2.1.4\FlowLauncher\app-2.1.4\Flow.Launcher.exe"
if not exist "%FLOW_EXE%" (
  echo Flow Launcher portable files are missing. Please read README.md.
  pause
  exit /b 1
)
start "" "%FLOW_EXE%"
endlocal

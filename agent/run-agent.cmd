@echo off
rem GS-LPR gate agent: restarts the agent if it ever exits.
cd /d "%~dp0"
:loop
"%~dp0venv\Scripts\python.exe" "%~dp0gate_agent.py" -c "%~dp0agent.yaml" --log-file "%~dp0agent.log"
timeout /t 5 /nobreak >nul
goto loop

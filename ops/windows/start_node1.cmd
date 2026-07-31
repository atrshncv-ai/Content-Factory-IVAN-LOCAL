@echo off
setlocal
if "%NODE1_COMFYUI_DIR%"=="" exit /b 2
if "%NODE1_PYTHON_EXE%"=="" exit /b 2
cd /d "%NODE1_COMFYUI_DIR%"
start "ComfyUI" "%NODE1_PYTHON_EXE%" -s ComfyUI\main.py --listen 0.0.0.0
if "%NODE1_WATCHDOG_PY%"=="" exit /b 2
start "Hermes Node1 Watchdog" "%NODE1_PYTHON_EXE%" "%NODE1_WATCHDOG_PY%"

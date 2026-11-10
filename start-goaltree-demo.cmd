@echo off
cd /d "%~dp0"
python -X utf8 scripts/run_goaltree_demo.py --port 8774 --shell-backend appcontainer --open
pause

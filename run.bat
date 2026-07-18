@echo off
chcp 65001 > nul
python "%~dp0main.py"
if errorlevel 1 pause

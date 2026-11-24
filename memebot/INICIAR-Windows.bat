@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "deploy\start.ps1"

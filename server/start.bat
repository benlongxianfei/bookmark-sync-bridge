@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Bookmark Sync Bridge

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3 not found in PATH. Install from https://www.python.org/
  pause
  exit /b 1
)

echo.
echo   Bookmark Sync Bridge  http://127.0.0.1:8765
echo   Press Ctrl+C to stop.
echo.
python bookmark_sync_server.py
pause

@echo off
chcp 65001 >nul
title SkySheep
echo ==============================================
echo   SkySheep Desktop - starting...
echo   Keep this window OPEN while using the app.
echo ==============================================
cd /d "D:\AsusDownload\Desktop\Agent\SkySheep\engine"
where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found in PATH. Screenshot this window.
    pause
    exit /b 1
)
uv run skysheep app
echo.
echo === SkySheep exited, exit code %errorlevel%. If it failed, screenshot this window. ===
pause

@echo off
chcp 65001 >nul
title SkySheep
echo ==============================================
echo   SkySheep Desktop - starting...
echo   Keep this window OPEN while using the app.
echo ==============================================

rem Locate the engine folder relative to this script (%~dp0 is the script dir).
rem Do NOT hardcode an absolute path: it breaks other people's clones and leaks
rem the author's local directory layout into the public repo.
set "ENGINE_DIR=%~dp0engine"

if not exist "%ENGINE_DIR%\pyproject.toml" (
    echo [ERROR] engine folder not found next to this script:
    echo         %ENGINE_DIR%
    echo         Keep this .bat inside the SkySheep repo root.
    pause
    exit /b 1
)
cd /d "%ENGINE_DIR%"

where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found in PATH. Screenshot this window.
    echo         Install guide: https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

uv run skysheep app
echo.
echo === SkySheep exited, exit code %errorlevel%. If it failed, screenshot this window. ===
pause

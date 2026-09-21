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

rem Source-tree runs default to the "dev" instance so their data lands in
rem ~/.skysheep-dev and never mixes with an installed copy's ~/.skysheep.
rem This script is for editing/verifying code; an installed build is for daily use.
rem Do not override an explicit SKYSHEEP_HOME / SKYSHEEP_INSTANCE.
rem NOTE: keep every comment in this file ASCII-only. cmd.exe parses .bat bytes
rem in the OEM code page, so UTF-8 multi-byte characters split into stray commands.
if not defined SKYSHEEP_HOME if not defined SKYSHEEP_INSTANCE set "SKYSHEEP_INSTANCE=dev"

rem Logically: data dir = SKYSHEEP_HOME, else ~/.skysheep[-<instance>].
if defined SKYSHEEP_HOME (
    set "SKY_DATA=%SKYSHEEP_HOME%"
    set "SKY_LABEL=custom"
) else if defined SKYSHEEP_INSTANCE (
    set "SKY_DATA=%USERPROFILE%\.skysheep-%SKYSHEEP_INSTANCE%"
    set "SKY_LABEL=%SKYSHEEP_INSTANCE%"
) else (
    set "SKY_DATA=%USERPROFILE%\.skysheep"
    set "SKY_LABEL=default"
)
title SkySheep [%SKY_LABEL%]
echo   Instance: %SKY_LABEL%   Data: %SKY_DATA%
echo.

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

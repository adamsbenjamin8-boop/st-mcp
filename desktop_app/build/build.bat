@echo off
REM ============================================================
REM  ST MCP Desktop App — Full Build Script
REM  Run from the desktop_app\ directory:  build\build.bat
REM ============================================================
setlocal

echo.
echo === ST MCP Desktop App Builder ===
echo.

REM ---- Step 1: Install Python dependencies ----
echo [1/3] Installing Python dependencies...
pip install customtkinter pystray pillow requests packaging pyinstaller httpx ^
    --quiet --disable-pip-version-check
if %errorlevel% neq 0 (
    echo ERROR: pip install failed.
    exit /b 1
)
echo       Done.

REM ---- Step 2: Build .exe with PyInstaller ----
echo [2/3] Building executable with PyInstaller...
cd /d "%~dp0.."
python -m PyInstaller build\st_mcp.spec --clean --noconfirm
if %errorlevel% neq 0 (
    echo ERROR: PyInstaller build failed.
    exit /b 1
)
echo       Output: desktop_app\dist\ST_MCP_Launcher.exe

REM ---- Step 3: Build installer with Inno Setup ----
echo [3/3] Building installer with Inno Setup...
cd /d "%~dp0"

REM Try iscc directly (works when installed for all users and added to PATH)
where iscc >nul 2>&1
if %errorlevel% equ 0 (
    iscc installer.iss
    goto iscc_done
)

REM Fall back to known install paths
if exist "C:\Program Files (x86)\Inno Setup 6\iscc.exe" (
    "C:\Program Files (x86)\Inno Setup 6\iscc.exe" installer.iss
    goto iscc_done
)
if exist "C:\Program Files\Inno Setup 6\iscc.exe" (
    "C:\Program Files\Inno Setup 6\iscc.exe" installer.iss
    goto iscc_done
)

echo WARNING: Inno Setup not found. Skipping installer build.
echo          Install from https://www.jrsoftware.org/isdl.php
echo          Then re-run this script or run: iscc build\installer.iss
goto done

:iscc_done
if %errorlevel% neq 0 (
    echo ERROR: Inno Setup build failed.
    exit /b 1
)
echo       Output: desktop_app\build\Output\ST_MCP_Setup.exe

:done
echo.
echo === Build complete ===
echo.
echo Deliverables:
echo   Executable : desktop_app\dist\ST_MCP_Launcher.exe
echo   Installer  : desktop_app\build\Output\ST_MCP_Setup.exe  (if Inno Setup found)
echo.
endlocal

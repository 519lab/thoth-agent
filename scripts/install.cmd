@echo off
REM ============================================================================
REM Thoth Agent Installer for Windows (CMD wrapper)
REM ============================================================================
REM This batch file launches the PowerShell installer for users running CMD.
REM
REM Usage:
REM   curl -fsSL https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.cmd -o install.cmd && install.cmd && del install.cmd
REM
REM Or if you're already in PowerShell, use the direct command instead:
REM   iex (irm https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.ps1)
REM ============================================================================

setlocal

echo.
echo  Thoth Agent Installer
echo  Launching PowerShell installer...
echo.

set "PS_URL=https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.ps1"
set "PS_TMP=%TEMP%\thoth-install-%RANDOM%%RANDOM%.ps1"

REM Download the installer to a temp .ps1 so we can run it with -File. Unlike
REM `iex (irm ...)`, this forwards CLI args (%*) to the installer and lets the
REM script's real exit code propagate back through %ERRORLEVEL% instead of being
REM swallowed by Invoke-Expression.
powershell -ExecutionPolicy ByPass -NoProfile -Command "irm '%PS_URL%' -OutFile '%PS_TMP%'"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  Download failed. Please try running PowerShell directly:
    echo    powershell -ExecutionPolicy ByPass -c "iex (irm %PS_URL%)"
    echo.
    pause
    exit /b 1
)

powershell -ExecutionPolicy ByPass -NoProfile -File "%PS_TMP%" %*
set "PS_EXIT=%ERRORLEVEL%"

del "%PS_TMP%" >nul 2>&1

if not "%PS_EXIT%"=="0" (
    echo.
    echo  Installation failed ^(exit code %PS_EXIT%^). Please try running PowerShell directly:
    echo    powershell -ExecutionPolicy ByPass -c "iex (irm %PS_URL%)"
    echo.
    pause
)

exit /b %PS_EXIT%

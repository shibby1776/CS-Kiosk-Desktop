@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "COMPILE_ONLY=0"
if /I "%~1"=="--compile-only" set "COMPILE_ONLY=1"

if "%COMPILE_ONLY%"=="0" (
  echo [INSTALLER] Building Windows application...
  call build_windows.bat --no-pause
  if errorlevel 1 goto :build_failed
) else (
  echo [INSTALLER] Reusing the existing Windows application build...
  if not exist "dist\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe" goto :existing_build_missing
)

set "APP_VERSION="
for /f "tokens=2 delims==" %%V in ('findstr /B /C:"DESKTOP_VERSION=" "RELEASE.env"') do set "APP_VERSION=%%V"
if not defined APP_VERSION goto :version_missing
set "PUBLIC_VERSION="
for /f "tokens=2 delims==" %%V in ('findstr /B /C:"KIOSK_VERSION=" "RELEASE.env"') do set "PUBLIC_VERSION=%%V"
if not defined PUBLIC_VERSION goto :version_missing

set "ISCC_EXE="
where ISCC.exe >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%I in ('where ISCC.exe') do if not defined ISCC_EXE set "ISCC_EXE=%%I"
)
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC_EXE (
  echo.
  echo Inno Setup 6 was not found.
  echo Install Inno Setup 6, then run build_installer.bat again.
  echo The Windows application build remains available under dist.
  echo.
  pause
  exit /b 1
)

echo [INSTALLER] Compiling upgrade-safe installer...
call :create_physical_stage
if errorlevel 1 goto :short_path_failed

echo [INSTALLER] Using physical short-path staging at %STAGE_ROOT%.
"%ISCC_EXE%" "%STAGE_ROOT%\ShibbyPrintsCaseSorterInstaller.iss"
set "ISCC_RESULT=%ERRORLEVEL%"

set "SETUP_EXE=%CD%\installer_output\ShibbyPrints-Kiosk-%PUBLIC_VERSION%-Public-Setup.exe"
set "STAGED_SETUP=%STAGE_ROOT%\installer_output\ShibbyPrints-Kiosk-%PUBLIC_VERSION%-Public-Setup.exe"
if not "%ISCC_RESULT%"=="0" goto :staged_installer_failed
if not exist "%STAGED_SETUP%" goto :staged_installer_missing
if not exist "%CD%\installer_output" mkdir "%CD%\installer_output" >nul 2>&1
copy /Y "%STAGED_SETUP%" "%SETUP_EXE%" >nul
if errorlevel 1 goto :staged_installer_copy_failed
call :remove_physical_stage
if not exist "%SETUP_EXE%" goto :installer_missing

echo.
echo INSTALLER BUILD COMPLETED SUCCESSFULLY.
echo Official installer:
echo   %SETUP_EXE%
echo.
start "" explorer.exe "%CD%\installer_output"
pause
exit /b 0

:build_failed
echo.
echo The application build failed. Review build_report\errors.log and build.log.
pause
exit /b 1

:existing_build_missing
echo.
echo The existing application build was not found under dist\ShibbyPrintsCaseSorter.
echo Run build_installer.bat without --compile-only to build everything.
pause
exit /b 1

:installer_failed
echo.
echo Inno Setup could not compile the installer.
pause
exit /b 1

:staged_installer_failed
call :remove_physical_stage
goto :installer_failed

:staged_installer_missing
call :remove_physical_stage
goto :installer_missing

:staged_installer_copy_failed
call :remove_physical_stage
echo.
echo The installer compiled, but it could not be copied to installer_output.
pause
exit /b 1

:installer_missing
echo.
echo Inno Setup reported success, but the expected installer was not found:
echo   %SETUP_EXE%
pause
exit /b 1

:version_missing
echo.
echo DESKTOP_VERSION or KIOSK_VERSION could not be read from RELEASE.env.
pause
exit /b 1

:short_path_failed
echo.
echo A physical short-path staging folder could not be prepared for Inno Setup.
echo Confirm the Windows temporary drive has enough free space for a copy of dist.
pause
exit /b 1

:create_physical_stage
set "STAGE_ROOT=%TEMP%\SPI-%RANDOM%-%RANDOM%"
mkdir "%STAGE_ROOT%\dist\ShibbyPrintsCaseSorter" >nul 2>&1
if errorlevel 1 goto :physical_stage_creation_failed

echo [INSTALLER] Copying the existing runtime to the short staging path...
robocopy "%CD%\dist\ShibbyPrintsCaseSorter" "%STAGE_ROOT%\dist\ShibbyPrintsCaseSorter" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :physical_stage_creation_failed

copy /Y "%CD%\ShibbyPrintsCaseSorterInstaller.iss" "%STAGE_ROOT%\" >nul
if errorlevel 1 goto :physical_stage_creation_failed
copy /Y "%CD%\installer_version.iss" "%STAGE_ROOT%\" >nul
if errorlevel 1 goto :physical_stage_creation_failed
copy /Y "%CD%\LICENSE" "%STAGE_ROOT%\" >nul
if errorlevel 1 goto :physical_stage_creation_failed
copy /Y "%CD%\NOTICE" "%STAGE_ROOT%\" >nul
if errorlevel 1 goto :physical_stage_creation_failed
copy /Y "%CD%\README.md" "%STAGE_ROOT%\" >nul
if errorlevel 1 goto :physical_stage_creation_failed
exit /b 0

:physical_stage_creation_failed
call :remove_physical_stage
exit /b 1

:remove_physical_stage
if defined STAGE_ROOT if exist "%STAGE_ROOT%\" rmdir /S /Q "%STAGE_ROOT%" >nul 2>&1
set "STAGE_ROOT="
set "STAGED_SETUP="
exit /b 0

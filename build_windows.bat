@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "NO_PAUSE=0"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

set "PY_EXE="
set "PY_ARGS="
set "REPORT=build_report"
if not exist "%REPORT%" mkdir "%REPORT%"
set "LOG=%REPORT%\build.log"
set "ERR=%REPORT%\errors.log"
set "ENV=%REPORT%\environment.txt"
set "MANIFEST=%REPORT%\package_manifest.txt"
set "CHECKSUMS=%REPORT%\checksums.txt"
set "FAILED_STEP=Unknown build step"
set "FAILURE_DETAIL="

>"%LOG%" echo ShibbyPrints Windows build started %DATE% %TIME%
>"%ERR%" echo No errors detected yet.
>"%ENV%" echo Build environment captured %DATE% %TIME%

set "FAILED_STEP=Locate Python 3.10 or newer"
echo [BUILD] !FAILED_STEP!
call :find_python
if not defined PY_EXE (
  echo.
  echo Python 3.10 or newer is required to build the Windows application.
  choice /C YN /N /M "Install Python 3.12 for the current user now? [Y/N] "
  if errorlevel 2 (
    set "FAILURE_DETAIL=Python installation was declined. Install Python 3.10 or newer, then run build_windows.bat again."
    goto :failed
  )

  where winget >nul 2>&1
  if errorlevel 1 (
    set "FAILURE_DETAIL=Windows Package Manager ^(winget^) is unavailable. Install Python 3.10 or newer from https://www.python.org/downloads/windows/ and rerun this build."
    goto :failed
  )

  set "FAILED_STEP=Install Python 3.12"
  echo [BUILD] !FAILED_STEP!
  winget install --exact --id Python.Python.3.12 --source winget --scope user --accept-package-agreements --accept-source-agreements >>"%LOG%" 2>&1
  if errorlevel 1 (
    set "FAILURE_DETAIL=Windows Package Manager could not install Python 3.12. Review build.log, install Python manually, and rerun this build."
    goto :failed
  )

  set "FAILED_STEP=Verify installed Python 3.12"
  echo [BUILD] !FAILED_STEP!
  call :find_python
  if not defined PY_EXE (
    set "FAILURE_DETAIL=Python installation completed but could not be verified in this command session. Close this window and run build_windows.bat again."
    goto :failed
  )
)

"%PY_EXE%" %PY_ARGS% --version >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Generate and validate release metadata"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% generate_release_files.py >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Capture pip version"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% -m pip --version >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Upgrade build tools"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% -m pip install --upgrade pip setuptools wheel >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Install production dependencies"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% -m pip install --upgrade -r requirements.txt pyinstaller >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Verify production imports"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% -c "import cv2,numpy,PIL,requests,msal,platformdirs,serial,torch,torchvision; print('Dependency verification passed'); print('torch',torch.__version__); print('torchvision',torchvision.__version__)" >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Verify Windows camera dependencies"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% -c "import pygrabber,comtypes; print('Windows camera dependencies verified')" >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Run automated source validation tests"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% -m unittest discover -s tests -v >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

"%PY_EXE%" %PY_ARGS% -c "import platform,sys; print('platform=',platform.platform()); print('python=',sys.version)" >>"%ENV%" 2>&1
"%PY_EXE%" %PY_ARGS% -m pip freeze >>"%ENV%" 2>&1

set "FAILED_STEP=Build ONEDIR package"
echo [BUILD] !FAILED_STEP!
"%PY_EXE%" %PY_ARGS% -m PyInstaller --clean --noconfirm ShibbyPrintsCaseSorter.spec >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

if not exist "dist\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe" (
  set "FAILED_STEP=Verify expected ONEDIR executable"
  goto :failed
)

set "FAILED_STEP=Verify packaged runtime"
echo [BUILD] !FAILED_STEP!
"dist\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe" --verify-runtime >>"%LOG%" 2>&1
if errorlevel 1 (
  >>"%ERR%" echo packaged executable could not load PyTorch or another required runtime component.
  goto :failed
)

dir /s /b "dist\ShibbyPrintsCaseSorter" >"%MANIFEST%" 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-FileHash 'dist\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe' -Algorithm SHA256 | Format-List" >"%CHECKSUMS%" 2>&1

>>"%LOG%" echo BUILD RESULT: SUCCESS %DATE% %TIME%
>"%ERR%" echo No errors detected.
echo.
echo ONEDIR build completed successfully.
echo Build report: %CD%\%REPORT%
echo Distribute the ENTIRE folder: dist\ShibbyPrintsCaseSorter\
echo.
if "%NO_PAUSE%"=="0" pause
exit /b 0

:failed
>"%ERR%" echo BUILD FAILED at: !FAILED_STEP!
>>"%ERR%" echo Timestamp: %DATE% %TIME%
if defined FAILURE_DETAIL >>"%ERR%" echo !FAILURE_DETAIL!
>>"%ERR%" echo See build.log for complete stdout/stderr.
>>"%LOG%" echo.
>>"%LOG%" echo BUILD RESULT: FAILED at !FAILED_STEP! %DATE% %TIME%
echo.
echo BUILD FAILED at: !FAILED_STEP!
echo Review:
echo   %CD%\%ERR%
echo   %CD%\%LOG%
echo.
if "%NO_PAUSE%"=="0" pause
exit /b 1

:find_python
set "PY_EXE="
set "PY_ARGS="

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=py"
  set "PY_ARGS=-3"
  exit /b 0
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=python"
  exit /b 0
)

python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=python3"
  exit /b 0
)

for %%V in (314 313 312 311 310) do (
  for %%P in (
    "%LocalAppData%\Programs\Python\Python%%V\python.exe"
    "%ProgramFiles%\Python%%V\python.exe"
  ) do (
    if exist "%%~fP" (
      "%%~fP" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
      if not errorlevel 1 (
        set "PY_EXE=%%~fP"
        exit /b 0
      )
    )
  )
)

exit /b 1

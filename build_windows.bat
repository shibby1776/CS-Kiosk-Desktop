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
set "MANIFEST_CPU=%REPORT%\package_manifest_cpu.txt"
set "MANIFEST_CUDA=%REPORT%\package_manifest_cuda.txt"
set "CHECKSUMS=%REPORT%\checksums.txt"
set "WORKER_CPU=%REPORT%\training_worker_cpu.txt"
set "WORKER_CUDA=%REPORT%\training_worker_cuda.txt"
set "CACHE_CPU=%REPORT%\dependency_cache_cpu.env"
set "CACHE_CUDA=%REPORT%\dependency_cache_cuda.env"
set "FAILED_STEP=Unknown build step"
set "FAILURE_DETAIL="

if defined SHIBBYPRINTS_BUILD_CACHE (
  set "BUILD_CACHE_ROOT=%SHIBBYPRINTS_BUILD_CACHE%"
) else if defined LOCALAPPDATA (
  set "BUILD_CACHE_ROOT=%LOCALAPPDATA%\ShibbyPrints\BuildCache"
) else (
  set "BUILD_CACHE_ROOT=%TEMP%\ShibbyPrintsBuildCache"
)
set "PIP_CACHE_DIR=%BUILD_CACHE_ROOT%\pip-downloads"
set "CACHE_FORCE_ARG="
if /I "%SHIBBYPRINTS_REBUILD_DEPENDENCIES%"=="1" set "CACHE_FORCE_ARG=--force"

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

set "FAILED_STEP=Prepare reusable CPU dependency environment"
echo [BUILD] !FAILED_STEP!
call :prepare_cached_environment cpu "%CACHE_CPU%"
if errorlevel 1 goto :failed

set "FAILED_STEP=Verify CPU production imports"
echo [BUILD] !FAILED_STEP!
"%CPU_PY%" -c "import cv2,numpy,PIL,requests,msal,platformdirs,serial,torch,torchvision; from sorter.torch_security import require_safe_torch; require_safe_torch(torch); assert torch.version.cuda is None, 'CPU build resolved a CUDA Torch wheel'; print('CPU dependency verification passed'); print('torch',torch.__version__); print('torchvision',torchvision.__version__)" >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Verify Windows camera dependencies"
echo [BUILD] !FAILED_STEP!
"%CPU_PY%" -c "import pygrabber,comtypes; print('Windows camera dependencies verified')" >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

set "FAILED_STEP=Run automated source validation tests"
echo [BUILD] !FAILED_STEP!
"%CPU_PY%" -m unittest discover -s tests -v >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

"%CPU_PY%" -c "import platform,sys; print('platform=',platform.platform()); print('python=',sys.version)" >>"%ENV%" 2>&1
>>"%ENV%" echo CPU dependency cache: %CPU_ENV%
"%CPU_PY%" -m pip freeze >>"%ENV%" 2>&1

if exist "dist_cpu" rmdir /S /Q "dist_cpu"
if exist "build_cpu" rmdir /S /Q "build_cpu"
set "FAILED_STEP=Build CPU ONEDIR package"
echo [BUILD] !FAILED_STEP!
"%CPU_PY%" -m PyInstaller --clean --noconfirm --distpath dist_cpu --workpath build_cpu ShibbyPrintsCaseSorter.spec >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

if not exist "dist_cpu\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe" (
  set "FAILED_STEP=Verify expected CPU ONEDIR executable"
  goto :failed
)
if not exist "dist_cpu\ShibbyPrintsCaseSorter\ShibbyPrintsTrainingWorker.exe" (
  set "FAILED_STEP=Verify packaged CPU training worker"
  set "FAILURE_DETAIL=The CPU package did not contain ShibbyPrintsTrainingWorker.exe."
  goto :failed
)

set "FAILED_STEP=Verify packaged CPU runtime"
echo [BUILD] !FAILED_STEP!
"dist_cpu\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe" --verify-runtime --expect-runtime cpu >>"%LOG%" 2>&1
if errorlevel 1 (
  >>"%ERR%" echo packaged CPU executable failed its tensor, safe-checkpoint, or ConvNeXt validation.
  goto :failed
)

set "FAILED_STEP=Verify packaged CPU training-worker entry point"
echo [BUILD] !FAILED_STEP!
"dist_cpu\ShibbyPrintsCaseSorter\ShibbyPrintsTrainingWorker.exe" --training-worker --help >"%WORKER_CPU%" 2>&1
if errorlevel 1 goto :failed
findstr /C:"ConvNeXt trainer" "%WORKER_CPU%" >nul
if errorlevel 1 (
  set "FAILURE_DETAIL=The CPU training worker did not return the expected trainer help output."
  goto :failed
)

set "FAILED_STEP=Prepare reusable CUDA dependency environment"
echo [BUILD] !FAILED_STEP!
call :prepare_cached_environment cuda "%CACHE_CUDA%"
if errorlevel 1 goto :failed

set "FAILED_STEP=Verify CUDA package availability"
echo [BUILD] !FAILED_STEP!
"%CUDA_PY%" -c "import torch,torchvision; from sorter.torch_security import require_safe_torch; require_safe_torch(torch); assert torch.version.cuda, 'CUDA wheel was not installed'; print('CUDA package verification passed'); print('torch',torch.__version__); print('torchvision',torchvision.__version__); print('compiled CUDA',torch.version.cuda)" >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

>>"%ENV%" echo.
>>"%ENV%" echo CUDA dependency cache: %CUDA_ENV%
"%CUDA_PY%" -m pip freeze >>"%ENV%" 2>&1

if exist "dist_cuda" rmdir /S /Q "dist_cuda"
if exist "build_cuda" rmdir /S /Q "build_cuda"
set "FAILED_STEP=Build CUDA ONEDIR package"
echo [BUILD] !FAILED_STEP!
"%CUDA_PY%" -m PyInstaller --clean --noconfirm --distpath dist_cuda --workpath build_cuda ShibbyPrintsCaseSorter.spec >>"%LOG%" 2>&1
if errorlevel 1 goto :failed

if not exist "dist_cuda\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe" (
  set "FAILED_STEP=Verify expected CUDA ONEDIR executable"
  goto :failed
)
if not exist "dist_cuda\ShibbyPrintsCaseSorter\ShibbyPrintsTrainingWorker.exe" (
  set "FAILED_STEP=Verify packaged CUDA training worker"
  set "FAILURE_DETAIL=The CUDA package did not contain ShibbyPrintsTrainingWorker.exe."
  goto :failed
)

set "FAILED_STEP=Verify packaged CUDA-capable runtime"
echo [BUILD] !FAILED_STEP!
"dist_cuda\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe" --verify-runtime --expect-runtime cuda >>"%LOG%" 2>&1
if errorlevel 1 (
  >>"%ERR%" echo packaged CUDA executable does not contain the pinned CUDA runtime.
  goto :failed
)

set "FAILED_STEP=Verify packaged CUDA training-worker entry point"
echo [BUILD] !FAILED_STEP!
"dist_cuda\ShibbyPrintsCaseSorter\ShibbyPrintsTrainingWorker.exe" --training-worker --help >"%WORKER_CUDA%" 2>&1
if errorlevel 1 goto :failed
findstr /C:"ConvNeXt trainer" "%WORKER_CUDA%" >nul
if errorlevel 1 (
  set "FAILURE_DETAIL=The CUDA training worker did not return the expected trainer help output."
  goto :failed
)

dir /s /b "dist_cpu\ShibbyPrintsCaseSorter" >"%MANIFEST_CPU%" 2>&1
dir /s /b "dist_cuda\ShibbyPrintsCaseSorter" >"%MANIFEST_CUDA%" 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-FileHash 'dist_cpu\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe','dist_cuda\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe' -Algorithm SHA256 | Format-List" >"%CHECKSUMS%" 2>&1

rem Preserve the traditional portable dist path using the CUDA-capable build.
rem It remains CPU compatible when no supported NVIDIA GPU is available.
if exist "dist" rmdir /S /Q "dist"
mkdir "dist\ShibbyPrintsCaseSorter" >nul 2>&1
robocopy "dist_cuda\ShibbyPrintsCaseSorter" "dist\ShibbyPrintsCaseSorter" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  set "FAILED_STEP=Prepare traditional portable dist folder"
  goto :failed
)

>>"%LOG%" echo BUILD RESULT: SUCCESS %DATE% %TIME%
>"%ERR%" echo No errors detected.
echo.
echo CPU and CUDA ONEDIR builds completed successfully.
echo Build report: %CD%\%REPORT%
echo Installer inputs: dist_cpu\ShibbyPrintsCaseSorter and dist_cuda\ShibbyPrintsCaseSorter
echo Portable CUDA-capable folder: dist\ShibbyPrintsCaseSorter\
echo.
if "%NO_PAUSE%"=="0" pause
exit /b 0

:prepare_cached_environment
set "CACHE_PROFILE=%~1"
set "CACHE_STATE_FILE=%~2"
set "CACHE_ENV_PATH="
set "CACHE_PYTHON_PATH="
set "CACHE_CACHE_KEY="
set "CACHE_READY="

"%PY_EXE%" %PY_ARGS% build_dependency_cache.py prepare --profile "%CACHE_PROFILE%" --cache-root "%BUILD_CACHE_ROOT%" --output "%CACHE_STATE_FILE%" %CACHE_FORCE_ARG% >>"%LOG%" 2>&1
if errorlevel 1 (
  set "FAILURE_DETAIL=The dependency cache fingerprint could not be calculated."
  exit /b 1
)
for /F "usebackq tokens=1,* delims==" %%A in ("%CACHE_STATE_FILE%") do set "CACHE_%%A=%%B"
if not defined CACHE_ENV_PATH (
  set "FAILURE_DETAIL=The dependency cache did not return an environment path."
  exit /b 1
)
if not defined CACHE_PYTHON_PATH (
  set "FAILURE_DETAIL=The dependency cache did not return a Python path."
  exit /b 1
)

if /I "%CACHE_PROFILE%"=="cpu" (
  set "CPU_ENV=!CACHE_ENV_PATH!"
  set "CPU_PY=!CACHE_PYTHON_PATH!"
) else (
  set "CUDA_ENV=!CACHE_ENV_PATH!"
  set "CUDA_PY=!CACHE_PYTHON_PATH!"
)

if "!CACHE_READY!"=="1" (
  echo [CACHE] Reusing verified %CACHE_PROFILE% dependencies.
  >>"%LOG%" echo Dependency cache hit: %CACHE_PROFILE% !CACHE_ENV_PATH!
  exit /b 0
)

echo [CACHE] Creating %CACHE_PROFILE% dependencies once for reuse by future builds.
>>"%LOG%" echo Dependency cache miss: %CACHE_PROFILE% !CACHE_ENV_PATH!
if exist "!CACHE_ENV_PATH!\" rmdir /S /Q "!CACHE_ENV_PATH!"
if exist "!CACHE_ENV_PATH!\" (
  set "FAILURE_DETAIL=The old cached %CACHE_PROFILE% environment is in use and could not be replaced: !CACHE_ENV_PATH!"
  exit /b 1
)

set "FAILED_STEP=Create reusable %CACHE_PROFILE% dependency environment"
"%PY_EXE%" %PY_ARGS% -m venv "!CACHE_ENV_PATH!" >>"%LOG%" 2>&1
if errorlevel 1 exit /b 1

set "FAILED_STEP=Upgrade pip in reusable %CACHE_PROFILE% environment"
"!CACHE_PYTHON_PATH!" -m pip install --upgrade pip >>"%LOG%" 2>&1
if errorlevel 1 exit /b 1

set "FAILED_STEP=Install shared %CACHE_PROFILE% build dependencies"
"!CACHE_PYTHON_PATH!" -m pip install -r requirements-base.txt -r requirements-build.txt >>"%LOG%" 2>&1
if errorlevel 1 exit /b 1

set "FAILED_STEP=Install pinned %CACHE_PROFILE% inference runtime"
"!CACHE_PYTHON_PATH!" -m pip install -r "requirements-torch-%CACHE_PROFILE%.txt" >>"%LOG%" 2>&1
if errorlevel 1 exit /b 1

set "FAILED_STEP=Verify and record reusable %CACHE_PROFILE% dependency environment"
"%PY_EXE%" %PY_ARGS% build_dependency_cache.py mark --profile "%CACHE_PROFILE%" --cache-root "%BUILD_CACHE_ROOT%" >>"%LOG%" 2>&1
if errorlevel 1 exit /b 1
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

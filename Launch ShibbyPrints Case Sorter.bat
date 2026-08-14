@echo off
setlocal
cd /d "%~dp0"

set "APP_EXE=%CD%\dist\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe"
if not exist "%APP_EXE%" (
  echo ShibbyPrints Case Sorter has not been built yet.
  echo.
  echo Run build_windows.bat first, then use this launcher.
  echo Expected application:
  echo   %APP_EXE%
  echo.
  pause
  exit /b 1
)

start "ShibbyPrints Case Sorter" /D "%CD%\dist\ShibbyPrintsCaseSorter" "%APP_EXE%"
exit /b 0

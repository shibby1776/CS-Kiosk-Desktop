# Install or Build ShibbyPrints Kiosk 2.2

This guide covers the official 64-bit Windows release.

## Which file should I download?

Most users need only:

`ShibbyPrints-Kiosk-2.2-Public-Setup.exe`

The Source ZIP is for developers, maintainers, and GPL corresponding-source
access. It is not required to operate an installed sorter.

## Install the software

1. Download `ShibbyPrints-Kiosk-2.2-Public-Setup.exe` from the official GitHub
   release.
2. Close any running copy of ShibbyPrints Kiosk Sorter.
3. Double-click the downloaded Setup executable.
4. Approve the Windows User Account Control prompt.
5. Review the license and notice shown by Setup.
6. Keep the default installation folder unless the machine requires a
   different location.
7. Leave **Create a desktop shortcut** selected if desired.
8. Complete Setup and launch **ShibbyPrints Kiosk Sorter** from the Desktop or
   Start Menu.

The Setup executable contains the packaged Python runtime and application
dependencies. End users do not need to install Python, Inno Setup, or the
Source ZIP.

### Windows SmartScreen

If the Setup executable is not code-signed, Windows may display **Windows
protected your PC**. Confirm that the file came from the official GitHub
release and verify its published SHA-256 checksum. Then select **More info**
and **Run anyway**.

To verify a checksum in PowerShell:

```powershell
Get-FileHash .\ShibbyPrints-Kiosk-2.2-Public-Setup.exe -Algorithm SHA256
```

The displayed hash must exactly match the checksum published with the release.

## Upgrade an existing installation

1. Stop sorting and close the application.
2. Download the newer official Setup executable.
3. Run Setup normally. Do not uninstall the existing version first.

The installer keeps a permanent application identity and recognizes an
existing installation. It replaces only the packaged application runtime.
Models, configuration, login state, Saved Bins, and diagnostic exports remain
in their established user-data locations.

## First-launch checks

After installation:

1. Confirm the camera preview appears.
2. Confirm the correct serial controller connects.
3. Confirm the saved camera-light brightness is restored.
4. Select or configure the required local or remote model.
5. Verify slot assignments before starting a production run.

If the application warns that Windows shared-camera mode is active, disable
**Allow multiple apps to use camera at the same time** in Windows camera
settings and restart the application.

## Build the official Setup executable

Building is intended for maintainers. It requires:

- 64-bit Windows;
- an internet connection for Python packages;
- Python 3.10 or newer; and
- Inno Setup 6.

The build downloads large machine-learning dependencies and requires several
gigabytes of temporary disk space.

### 1. Prepare the source

1. Download `ShibbyPrints-Kiosk-2.2-Public-Source.zip`.
2. Use **Extract All**. Do not run build scripts from inside the ZIP viewer.
3. For convenience, extract to a short local path such as:

   `C:\ShibbyPrints-Kiosk-2.2-Public`

4. Confirm the extracted folder contains `build_installer.bat`,
   `build_windows.bat`, `ShibbyPrintsCaseSorterInstaller.iss`, `LICENSE`, and
   `NOTICE`.

### 2. Install Inno Setup 6

Install Inno Setup 6 from its official website:

https://jrsoftware.org/isinfo.php

The build script searches the normal 32-bit and 64-bit Inno Setup installation
locations and also accepts `ISCC.exe` on the system PATH.

### 3. Run the build

Double-click:

`build_installer.bat`

The script performs these steps:

1. Locates Python 3.10 or newer.
2. If Python is missing, asks permission before installing Python 3.12 with
   Windows Package Manager. Declining stops the build without installing it.
3. Generates and validates release metadata.
4. Installs and verifies production dependencies.
5. Runs the automated test suite.
6. Builds the PyInstaller ONEDIR application.
7. Verifies the packaged runtime.
8. Copies the runtime to a short temporary staging path.
9. Compiles the Inno Setup installer.
10. Copies the completed Setup executable back to `installer_output` and opens
    that folder in File Explorer.

The completed installer is:

`installer_output\ShibbyPrints-Kiosk-2.2-Public-Setup.exe`

That Setup executable is the only application file an ordinary Windows user
needs for installation. Publish the matching Source ZIP beside it.

## Build only the unpackaged application

Run:

`build_windows.bat`

The unpackaged application is created under:

`dist\ShibbyPrintsCaseSorter`

The executable depends on the rest of that ONEDIR folder. Do not move or
distribute the executable by itself.

## Recompile after an installer-only failure

If `build_windows.bat` completed and the ONEDIR runtime is valid, but Inno Setup
was missing or installer compilation failed:

1. Correct the Inno Setup problem.
2. Double-click `Compile Installer From Existing Build.bat`.

This reuses `dist\ShibbyPrintsCaseSorter` and avoids rebuilding Python,
PyTorch, and the application.

## Build troubleshooting

Build diagnostics are written to:

- `build_report\build.log`
- `build_report\errors.log`
- `build_report\environment.txt`
- `build_report\checksums.txt`

If a build fails, read `errors.log` first, then inspect the end of `build.log`.
Do not manually move the executable out of the ONEDIR folder or compile the
`.iss` file against an incomplete `dist` directory.

# Install or Build ShibbyPrints Kiosk 2.3

This guide covers the official Kiosk 2.3 release for 64-bit Windows.

## Which file should I download?

Most users need only:

`ShibbyPrints-Kiosk-2.3-Public-Setup.exe`

The Source ZIP is for developers, maintainers, and GPL corresponding-source
access. It is not required to operate an installed sorter.

## Install the software

1. Download `ShibbyPrints-Kiosk-2.3-Public-Setup.exe` from the official GitHub
   release.
2. Close any running copy of ShibbyPrints Kiosk Sorter.
3. Double-click the downloaded Setup executable.
4. Approve the Windows User Account Control prompt.
5. Review the license and notice shown by Setup.
6. Keep the default installation folder unless the computer requires another
   location.
7. Leave **Allow sorter Web/API access from this private network** selected if
   this computer will provide browser access or the integrated API server.
8. If Setup detects a supported NVIDIA GPU, choose whether to install NVIDIA
   CUDA acceleration. Leave it selected for GPU inference and training.
9. Leave **Create a desktop shortcut** selected if desired.
10. Complete Setup and launch **ShibbyPrints Kiosk Sorter**.

The Setup executable contains the packaged Python runtime and application
dependencies. End users do not need Python, Inno Setup, or the Source ZIP.

### Windows SmartScreen

If the Setup executable is not code-signed, Windows may display **Windows
protected your PC**. Confirm that the file came from the official release and
verify its published SHA-256 checksum. Then select **More info** and **Run
anyway**.

```powershell
Get-FileHash .\ShibbyPrints-Kiosk-2.3-Public-Setup.exe -Algorithm SHA256
```

The displayed hash must exactly match the checksum published with the release.

## Upgrade an existing installation

1. Stop sorting and close the application.
2. Download the newer official Setup executable.
3. Run Setup normally. Do not uninstall the existing version first.

The installer keeps a permanent application identity and recognizes an
existing installation. It replaces the packaged runtime while preserving
models, configuration, login state, Saved Bins, and diagnostic exports.

## First-launch checks

1. Confirm the camera preview appears.
2. Confirm the correct sorter connection is selected.
3. Confirm the saved camera-light brightness is restored.
4. Select or configure the required local or remote model.
5. Verify the image crop and slot assignments before sorting.

If Windows shared-camera mode prevents the preferred camera format, disable
**Allow multiple apps to use camera at the same time** in Windows camera
settings and restart Kiosk.

## Connect the sorter

USB is the default connection.

For a compatible Kiosk Node:

1. Open **Maintenance → Serial**.
2. Select **Kiosk Node (network sorter)**.
3. Enter the sorter's IP address.
4. Connect and confirm that machine settings can be read.

The connection method does not change the normal Operator controls, image
processing, bin assignments, or run workflow.

## Configure remote inference

Open the AI configuration page, enter the compatible server endpoint and
optional API key, then use **Connect and Find Models**. Choose a model and load
its classifications. Server-provided classifications are read-only; assign
them to bins from the Run interface.

## Configure the integrated API server

1. Open **Maintenance → Server**.
2. Assign a short API name to each installed model clients may request.
3. Optionally generate an API key and save the same key on each client.
4. Enable **Preload** only for models needed immediately.
5. Select **Local network**, save, and press **Start Server**.
6. On each client, use **Connect and Find Models**.

The attached sorter can continue using local inference while remote clients
use the integrated server. Stop the server before changing assignments or
network settings. Never expose the server directly to the Internet.

## Enable browser access

1. Open **Maintenance → LAN Access**.
2. Enter a sorter name containing letters, numbers, and interior hyphens.
3. Select **Enable Web Interface**.
4. Keep **Keep the Desktop Interface available on this PC** checked for both
   interfaces, or clear it for browser-only presentation on the next launch.
5. Save and open the displayed `.local` or direct-IP URL from a device on the
   same private network.

The web interface is disabled by default. Use it only on a trusted private
network.

## Build the official Setup executable

Building is intended for maintainers. It requires:

- 64-bit Windows;
- an internet connection for Python packages;
- Python 3.10 or newer;
- Inno Setup 6; and
- several gigabytes of temporary disk space.

### 1. Prepare the source

1. Download `ShibbyPrints-Kiosk-2.3-Public-Source.zip`.
2. Use **Extract All**. Do not run scripts from inside the ZIP viewer.
3. For convenience, extract to a short path such as:

   `C:\ShibbyPrints-Kiosk-2.3-Public-Source`

4. Confirm the folder contains `build_installer.bat`, `build_windows.bat`,
   `ShibbyPrintsCaseSorterInstaller.iss`, `LICENSE`, and `NOTICE`.

### 2. Install Inno Setup 6

Install Inno Setup 6 from:

https://jrsoftware.org/isinfo.php

The build searches the normal 32-bit and 64-bit installation locations and
also accepts `ISCC.exe` on `PATH`.

### 3. Run the build

Double-click `build_installer.bat`.

The script:

1. Locates a supported Python installation.
2. Offers to install Python through Windows Package Manager if necessary.
3. Generates and validates public release metadata.
4. Creates or reuses fingerprinted CPU and CUDA dependency environments.
5. Verifies production imports and camera dependencies.
6. Runs the automated source-validation suite.
7. Builds and validates separate CPU and CUDA ONEDIR applications.
8. Compiles the Inno Setup installer.
9. Copies the completed Setup executable to `installer_output`.

The completed installer is:

`installer_output\ShibbyPrints-Kiosk-2.3-Public-Setup.exe`

Publish the matching Source ZIP beside it.

Set `SHIBBYPRINTS_REBUILD_DEPENDENCIES=1` before launching the build to force
fresh dependency environments for troubleshooting.

## Build only the unpackaged application

Run `build_windows.bat`. The applications are created under
`dist_cpu\ShibbyPrintsCaseSorter` and `dist_cuda\ShibbyPrintsCaseSorter`.
Each executable depends on the rest of its ONEDIR folder.

## Recompile after an installer-only failure

If application builds completed but Inno Setup was missing or installer
compilation failed, correct the installer problem and run
`Compile Installer From Existing Build.bat`.

## Build troubleshooting

Build diagnostics are written to:

- `build_report\build.log`
- `build_report\errors.log`
- `build_report\environment.txt`
- `build_report\checksums.txt`

Read `errors.log` first, then inspect the end of `build.log`. Do not move an
executable out of its ONEDIR folder or compile the installer against an
incomplete staging directory.

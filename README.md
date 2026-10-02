# ShibbyPrints Kiosk Sorter Software

**Kiosk 2.3 Public**

## Overview

ShibbyPrints Kiosk Sorter Software is a production-focused Windows application
for operating a compatible CS7.2 case sorter. It provides a simplified
Operator interface, technician-focused Maintenance controls, local and remote
classification, portable slot configurations, browser access, an optional
local inference server, training, evaluation, and on-demand diagnostics.

Kiosk 2.3 supports a directly attached USB sorter or a compatible Kiosk Node
without changing the normal sorting workflow. The
Windows and browser interfaces use the same camera, machine connection,
configuration, routing state, counters, and inference engine.

## Safety and Intended Use

This software controls a motorized case sorter intended only for sorting inert,
already-fired brass cartridge cases by their headstamps. It is not intended for
live ammunition and contains no ammunition-loading or load-data functions.

Connected sorter hardware contains moving mechanisms, pinch points, motors,
and electrical components. Keep hands, loose clothing, tools, and other objects
clear while the machine is powered or moving. Stop the application and
disconnect power before clearing jams, adjusting components, or servicing the
sorter.

The operator is responsible for verifying that the equipment is assembled,
configured, and operating safely. This software and its associated hardware
are used at the operator's own risk and are provided without warranty.

The supported public Windows installation method is the Setup executable
published with the corresponding Source ZIP in GitHub Releases. See
[`INSTALL.md`](INSTALL.md) for end-user installation, upgrade, source-build,
and troubleshooting instructions.

## License, source, and attribution

The ShibbyPrints Kiosk Edition is based on software originally created by
SJSeth Solutions and licensed under the GNU General Public License, version 3
or later (GPL-3.0-or-later). This software is provided without warranty. The
complete license terms are included in `LICENSE`.

ShibbyPrints independently maintains this Kiosk Edition as an optional
companion to the software and hardware ecosystem created by SJSeth Solutions.
This edition is not an official SJSeth Solutions product. No sponsorship or
endorsement by SJSeth Solutions is claimed or implied. Support for this Kiosk
Edition is provided by ShibbyPrints.

Original project:
https://github.com/sjseth/AI-Case-Sorter-Py

Upstream reference commit:
`62e879b0a57f3f857f59a8cc3e6e9ba701dc9bc9`

See `NOTICE` for copyright, attribution, and modification information. This
Source ZIP is the corresponding source for the matching public Setup
executable.

## How the pieces fit together

| Project | What it is | Link |
| --- | --- | --- |
| **Kiosk (this repository)** | Windows application for capture, classification, routing, training, evaluation, browser access, and optional model serving. | — |
| **CS7.2 hardware** | Printable models, build kits, assembly guides, and the controller firmware used by the sorter. | [AI-Case-Sorter-CS7.2](https://github.com/sjseth/AI-Case-Sorter-CS7.2) |
| **CaseSorter AI Server** | Optional standalone server for hosting trained ConvNeXt models behind an OpenAI-compatible API. Kiosk 2.3 can also serve selected installed models directly. | [AI-Case-Sorter-Server](https://github.com/sjseth/AI-Case-Sorter-Server) |
| **Community backend** | Optional hosted model sharing, downloads, and feedback service. It is not part of this source release. | [reloadingrecipes.com](https://www.reloadingrecipes.com/HeadstampSorter) |

An account is not required for ordinary local operation. Community sign-in is
needed only for community-specific features.

## Installation

1. Open the Kiosk 2.3 release on GitHub.
2. Download `ShibbyPrints-Kiosk-2.3-Public-Setup.exe`.
3. Close any running copy of ShibbyPrints Kiosk Sorter.
4. Run Setup and accept the default installation location.
5. Leave the private-network access task selected if browser access or the
   integrated API server will be used.
6. Launch **ShibbyPrints Kiosk Sorter** from the Desktop or Start Menu.

The installer contains its Python runtime and production dependencies. End
users do not need to install Python or browse the application directory. The
permanent installer identity allows an in-place upgrade while preserving
models, settings, Saved Bins, and diagnostic files.

## Operator and Maintenance interfaces

<img width="2667" height="1356" alt="Operator Run interface" src="https://github.com/user-attachments/assets/c81ca1eb-8f76-425b-8631-3f0e72037c9a" />

Operator Mode contains the controls needed during normal sorting. Maintenance
Mode contains model, camera, connection, routing, training, server, browser,
and service controls intended for setup and troubleshooting.

<img width="2666" height="1371" alt="Maintenance Run interface" src="https://github.com/user-attachments/assets/a892f00f-9aef-4835-bf42-9709f7723518" />

**Clear Slots** removes current slot assignments and resets counters after
confirmation. Classification names remain available for manual mapping,
automatic selection, or a saved slot configuration.

## Sorter connections

USB remains the default connection. Under **Maintenance → Serial**, enable the
Kiosk Node option and enter the sorter's IP address only when using a
compatible network-connected machine. Machine settings, air-drop behavior,
camera settings, routing, and run controls remain in their normal locations.

Saved sorter profiles can retain connection, crop, machine, and bin settings
for convenient switching while stopped. One active sorter is controlled by
each running application instance.

## Camera and image processing

The Windows camera path uses native DirectShow. MJPG is preferred at supported
camera resolutions; NV12 and YUY2 remain controlled compatibility formats.

If Windows shared-camera mode prevents the preferred format from being
advertised, the application displays a warning. Disable **Allow multiple apps
to use camera at the same time** in Windows camera settings, then restart the
application.

Automatic rim detection uses the configured expected center and radius limits.
Preview and accept the crop after changing the camera mode or physical camera
position.

## Local and remote inference

Kiosk can run an installed local model or call a compatible remote
classification endpoint. Remote classification names are supplied by the
selected server model and are read-only in Kiosk; bin assignments do not alter
the server's classification list.

The optional integrated API server allows this Windows installation to serve
selected installed models to other sorters on the same private network. Local
sorting remains in-process and does not loop through HTTP.

Configure the server under **Maintenance → Server**:

1. Assign a short API name to each model clients may request.
2. Optionally configure an API key and save the same key on each client.
3. Enable preload only for models expected to be used immediately.
4. Select **Local network**, review queue limits, and press **Start Server**.

The server uses bounded concurrency and backpressure. Do not expose it directly
to the Internet.

## Browser access

The browser interface is another presentation of the Windows backend. It does
not create a second camera, machine connection, database, routing system, or
inference engine.

Configure it under **Maintenance → LAN Access**:

1. Enter a DNS-safe sorter name such as `sorter-2`.
2. Enable the Web Interface.
3. Choose whether the local Desktop Interface remains available.
4. Save, then open the displayed `.local` or direct-IP address from a device on
   the same private network.

Maintenance pages require time-limited technician confirmation. Only one
browser owns motion and routing control at a time. Browser access is intended
for a trusted private network, not direct Internet exposure.

## Community models and feedback

Community model browsing, downloads, updates, and feedback are optional. Model
classifications supplied by the service cannot be removed locally. When a
publisher requests feedback and the operator enables it, Kiosk uses one upload
owner and bounded per-label capture. Images are not uploaded merely because
the application is installed or diagnostics are enabled.

## Saved slot configurations

Maintenance → Run → **Load Slot Config** opens the Saved Bins manager.

<img width="891" height="590" alt="Saved Bins manager" src="https://github.com/user-attachments/assets/f76f899d-7564-44fe-91d6-c80b2f21d151" />

Portable configurations:

- use the `.bins.json` filename suffix;
- identify classifications by case-insensitive name;
- support multiple classifications in one bin;
- preserve unavailable names when an older model is selected;
- add newly available model names without assigning them automatically; and
- work with local and synchronized remote models.

Local files are stored in `Documents\ShibbyPrints\Saved Bins`. USB import and
export occur only after an explicit user action.

Catch-All remains implicit during setup. Its session total is displayed, and
the operator can open its classification breakdown or export the session data
before resetting counters or exiting.

## Diagnostics and recovery reports

<img width="1190" height="693" alt="Sensor diagnostics" src="https://github.com/user-attachments/assets/8240e13e-8101-428f-bd10-24b67b328cbf" />

Diagnostics are disabled at startup. Press **Ctrl+D** or use **View → Enable
Diagnostics** to create the diagnostic environment. Disable it when finished
to release its timers, histories, and buffers.

Diagnostic ZIPs can include camera samples, machine traffic, system details,
sensor-test results, and connection evidence. Review an archive before sharing
it. Reports are created only after an operator action and are never uploaded
automatically.

Other desktop shortcuts:

- **Ctrl+O** — Operator Mode
- **Ctrl+M** — Maintenance Mode
- **Ctrl+Q** — close safely
- **F11** — toggle fullscreen

## Training and evaluation

Local training uses a bundled worker and writes a bounded persistent log. CPU
and supported NVIDIA CUDA installations use separate verified runtimes.
Evaluation reports and model archives should be treated as untrusted input
unless they come from a trusted source.

## Building the Windows application

Building requires 64-bit Windows, Python 3.10 or newer, Inno Setup 6, an
internet connection, and enough disk space for the selected application
runtimes.

The first build creates verified dependency environments under
`%LOCALAPPDATA%\ShibbyPrints\BuildCache`. Later builds reuse them only when the
Python interpreter, architecture, requirements, and import probes match.
Application source, validation tests, and PyInstaller output are rebuilt.

- Run `build_installer.bat` to validate the source, build CPU and CUDA ONEDIR
  runtimes, and create the public Setup executable.
- Run `build_windows.bat` for unpackaged ONEDIR runtimes.
- Run `Compile Installer From Existing Build.bat` after an installer-only
  failure to reuse valid application builds.

Build diagnostics are written to `build_report`. Generated output is excluded
from source control.

## Related resources

Electronics kits and components:
https://shop.sjseth.com/

Printed-parts kits and assembled CS7.2 sorter units:
https://www.shibbyprints.com/

Software information and community Discord link:
https://www.reloadingrecipes.com/HeadstampSorter

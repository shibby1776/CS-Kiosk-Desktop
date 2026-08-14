
# ShibbyPrints Kiosk Sorter Software

**Kiosk 2.2 Public**

## Overview

ShibbyPrints Kiosk Sorter Software is a production-focused application
for operating a compatible CS7.2 case sorter. It provides a simplified
Operator interface, technician-focused Maintenance controls, local and remote
classification, portable slot configurations, and on-demand diagnostics. This is the desktop version of the software with the Raspberry Pi version to release in a different repository.

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
published with the corresponding source archive in GitHub Releases.

See [`INSTALL.md`](INSTALL.md) for complete end-user installation, upgrade,
source-build, and troubleshooting instructions.

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
source archive is the corresponding source for the matching public Setup
executable.
How the pieces fit together

## How the pieces fit together

The case sorter is built from a few separate repositories. **This repo is just
the desktop software.**

| Project | What it is | Link |
|---------|-----------|------|
| **Kiosk (this repo)** | The cross-platform desktop app: capture, classify, route, train, evaluate. | — |
| **CS7.2 hardware** | 3D-printable models, build kits, assembly guides, and the Arduino-based firmware the app talks to over serial. | [AI-Case-Sorter-CS7.2](https://github.com/sjseth/AI-Case-Sorter-CS7.2) |
| **CaseSorter AI Server** | A small local HTTP server that hosts your trained ConvNeXt models behind an OpenAI-compatible API. This is what **AI Config mode** points at. | [AI-Case-Sorter-Server](https://github.com/sjseth/AI-Case-Sorter-Server) |
| **Community backend** | Hosted service at [reloadingrecipes.com](https://www.reloadingrecipes.com/HeadstampSorter) for sign-in, model sharing/downloads, and the feedback loop. A separate hosted service — **not** part of this open-source release. | [reloadingrecipes.com](https://www.reloadingrecipes.com/HeadstampSorter) |

You do **not** need an account to use the app. Everything except community
sharing/downloads works locally and offline.

---


## Installation

1. Open the Kiosk 2.2 release on GitHub.
2. Download `ShibbyPrints-Kiosk-2.2-Public-Setup.exe`.
3. Run the Setup executable and accept the default installation location.
4. Leave the Desktop shortcut selected if desired.
5. Launch **ShibbyPrints Kiosk Sorter** from the Desktop or Start Menu.

The installed application contains its Python runtime and production
dependencies. End users do not need to install Python or browse the internal
application directory.

The installer uses a permanent application identity. Installing a newer
official Setup executable over an existing installation replaces the packaged
runtime while preserving models, settings, Saved Bins, and diagnostic files.

## Camera requirement

The Windows camera path uses native DirectShow at 1920×1080 and 30 FPS. MJPG is
preferred. NV12 and YUY2 are controlled compatibility formats.

If Windows shared-camera mode prevents MJPG from being advertised, the
application displays a one-time warning. Disable **Allow multiple apps to use
camera at the same time** in Windows camera settings, then restart the
application.

## Operator and Maintenance interfaces

<img width="2667" height="1356" alt="op-run1" src="https://github.com/user-attachments/assets/c81ca1eb-8f76-425b-8631-3f0e72037c9a" />


Operator Mode contains only the controls needed during normal sorting.
Maintenance Mode contains model, camera, serial, routing, training, and service
controls intended for setup and troubleshooting.
<img width="2666" height="1371" alt="maint-run2" src="https://github.com/user-attachments/assets/a892f00f-9aef-4835-bf42-9709f7723518" />

**Clear Slots** removes current slot assignments and resets counters after
confirmation. Classification names remain available so the operator can remap
them manually, through automatic selection, or by applying a saved slot
configuration.

## Saved slot configurations

Maintenance → Run → **Load Slot Config** opens the Saved Bins manager.
<img width="891" height="590" alt="save-bins" src="https://github.com/user-attachments/assets/f76f899d-7564-44fe-91d6-c80b2f21d151" />

Portable configurations:

- use the `.bins.json` filename suffix;
- identify classifications by case-insensitive name;
- support multiple classifications in the same bin;
- preserve unavailable names when an older model is selected;
- add newly available model names without assigning them automatically;
- work with installed local models and synchronized remote API models.

Local files are stored in:

`Documents\ShibbyPrints\Saved Bins`

**Import from USB** scans only the removable drive root. **Export to USB**
writes directly to the removable drive root and preserves the local copy. USB
operations occur only when a user presses an import or export button; no drive
watcher or polling task runs in the background.

## Hidden diagnostics


<img width="1190" height="693" alt="sensor tests" src="https://github.com/user-attachments/assets/8240e13e-8101-428f-bd10-24b67b328cbf" />

Diagnostics are disabled at startup. Press **Ctrl+D** to create and enable the
diagnostic environment. Press **Ctrl+D** again to stop collection, destroy the
diagnostic interface, and release its timers, histories, and buffers.

Diagnostic exports include the detailed Desktop release, internal build, and
release-channel identifiers needed for technical support. Those identifiers
are not displayed in the normal application interface.

Other desktop shortcuts:

- **Ctrl+O** — Operator Mode
- **Ctrl+M** — Maintenance Mode
- **Ctrl+Q** — close the application safely
- **F11** — toggle fullscreen

## Building the Windows application

Building requires Windows, Python 3.10 or newer, and Inno Setup 6.

- Run `build_installer.bat` to validate the source, build the ONEDIR runtime,
  and produce the public Setup executable.
- Run `build_windows.bat` when only the unpackaged ONEDIR runtime is needed for
  testing.
- Run `Compile Installer From Existing Build.bat` to reuse a successful
  existing ONEDIR build.

Build diagnostics are written to `build_report`. Generated application and
installer output is excluded from source control.

## Related resources

Electronics kits and components:
https://shop.sjseth.com/

Printed-parts kits and assembled CS7.2 sorter units:
https://www.shibbyprints.com/

Mainline Software information and community Discord link:
https://www.reloadingrecipes.com/HeadstampSorter

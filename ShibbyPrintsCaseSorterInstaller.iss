#include "installer_version.iss"

#define MyAppName "ShibbyPrints Kiosk Sorter"
#define MyAppExeName "ShibbyPrintsCaseSorter.exe"
#define MyAppPublisher "ShibbyPrints"

[Setup]
; Keep this AppId unchanged in every future Desktop installer. Inno Setup uses
; it to identify an existing installation and perform an in-place upgrade.
AppId={{C95E90DB-EA7E-4C95-A22C-6579E36FD70D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} Kiosk {#MyPublicVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ShibbyPrints Kiosk Sorter
DefaultGroupName=ShibbyPrints Kiosk Sorter
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=installer_output
OutputBaseFilename=ShibbyPrints-Kiosk-{#MyPublicVersion}-Public-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\app\{#MyAppExeName}
LicenseFile=LICENSE
InfoBeforeFile=NOTICE
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[InstallDelete]
; Application data is stored under the user's application-data and Documents
; folders. Only the packaged runtime is replaced during an upgrade, preventing
; obsolete PyInstaller files from remaining between versions.
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "dist\ShibbyPrintsCaseSorter\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "NOTICE"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\ShibbyPrints Kiosk Sorter"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"
Name: "{autodesktop}\ShibbyPrints Kiosk Sorter"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"; Tasks: desktopicon

[Run]
Filename: "{app}\app\{#MyAppExeName}"; Description: "Launch ShibbyPrints Kiosk Sorter"; WorkingDir: "{app}\app"; Flags: nowait postinstall skipifsilent

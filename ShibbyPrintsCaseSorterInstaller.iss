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
AppVerName={#MyAppName} {#MyDisplayVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ShibbyPrints Kiosk Sorter
DefaultGroupName=ShibbyPrints Kiosk Sorter
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer_output
OutputBaseFilename=ShibbyPrints-Kiosk-{#MyPublicVersion}-{#MyInstallerSuffix}-Setup
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
Name: "lanaccess"; Description: "Allow sorter Web/API access from this private network"; GroupDescription: "Network access:"; Flags: checkedonce
Name: "cuda"; Description: "Install NVIDIA CUDA GPU acceleration (supported NVIDIA GPU detected)"; GroupDescription: "Inference runtime:"; Flags: checkedonce; Check: SupportedCudaGpu

[InstallDelete]
; Application data is stored under the user's application-data and Documents
; folders. Only the packaged runtime is replaced during an upgrade, preventing
; obsolete PyInstaller files from remaining between versions.
Type: filesandordirs; Name: "{app}\app"

[Files]
; CPU and CUDA are complete, mutually exclusive ONEDIR payloads.  Never mix
; their Torch DLLs in one installed runtime.
; The physical Inno staging tree deliberately uses one-character source
; directory names. Torch contains very deep third-party license paths and the
; longer build-output names can exceed Windows path handling during compression.
Source: "c\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: not cuda
Source: "g\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: cuda
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "NOTICE"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\ShibbyPrints Kiosk Sorter"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"
Name: "{autodesktop}\ShibbyPrints Kiosk Sorter"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"; Tasks: desktopicon

[Run]
; Always remove prior rules first so changing the task during an upgrade is
; deterministic and does not leave a stale broad rule behind.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ShibbyPrints Kiosk Sorter TCP"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ShibbyPrints Kiosk Sorter mDNS"""; Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ShibbyPrints Kiosk Sorter TCP"" dir=in action=allow program=""{app}\app\{#MyAppExeName}"" enable=yes profile=private remoteip=localsubnet protocol=TCP"; Flags: runhidden waituntilterminated; Tasks: lanaccess
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ShibbyPrints Kiosk Sorter mDNS"" dir=in action=allow program=""{app}\app\{#MyAppExeName}"" enable=yes profile=private remoteip=localsubnet protocol=UDP localport=5353"; Flags: runhidden waituntilterminated; Tasks: lanaccess
Filename: "{app}\app\{#MyAppExeName}"; Description: "Launch ShibbyPrints Kiosk Sorter"; WorkingDir: "{app}\app"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ShibbyPrints Kiosk Sorter TCP"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFirewallTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ShibbyPrints Kiosk Sorter mDNS"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFirewallMdns"

[Code]
var
  GpuDetectionComplete: Boolean;
  GpuSupported: Boolean;

function SupportedCudaGpu(): Boolean;
var
  ResultCode: Integer;
  TempFile: String;
  CommandLine: String;
  Lines: TArrayOfString;
  I: Integer;
  CommaAt: Integer;
  DotAt: Integer;
  CapabilityText: String;
  MajorCapability: Integer;
begin
  if not GpuDetectionComplete then
  begin
    GpuDetectionComplete := True;
    GpuSupported := False;
    TempFile := ExpandConstant('{tmp}\shibbyprints-nvidia-gpu.txt');
    CommandLine := '/C nvidia-smi --query-gpu=name,compute_cap ' +
      '--format=csv,noheader > "' + TempFile + '" 2>NUL';
    if Exec(ExpandConstant('{cmd}'), CommandLine, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) and
      LoadStringsFromFile(TempFile, Lines) then
    begin
      for I := 0 to GetArrayLength(Lines) - 1 do
      begin
        CommaAt := Pos(',', Lines[I]);
        if CommaAt > 0 then
        begin
          CapabilityText := Trim(Copy(Lines[I], CommaAt + 1, MaxInt));
          DotAt := Pos('.', CapabilityText);
          if DotAt > 0 then
            CapabilityText := Copy(CapabilityText, 1, DotAt - 1);
          MajorCapability := StrToIntDef(CapabilityText, 0);
          if MajorCapability >= 8 then
          begin
            GpuSupported := True;
            Break;
          end;
        end;
      end;
    end;
    DeleteFile(TempFile);
  end;
  Result := GpuSupported;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Parameters: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if WizardIsTaskSelected('cuda') then
    Parameters := '--verify-runtime --expect-runtime cuda --test-cuda-device'
  else
    Parameters := '--verify-runtime --expect-runtime cpu';
  if (not Exec(ExpandConstant('{app}\app\{#MyAppExeName}'), Parameters,
    ExpandConstant('{app}\app'), SW_HIDE, ewWaitUntilTerminated, ResultCode)) or
    (ResultCode <> 0) then
  begin
    MsgBox(
      'The installed inference runtime did not pass validation.' + #13#10 +
      'Rerun this installer and leave NVIDIA CUDA acceleration unchecked, ' +
      'or update the NVIDIA driver before enabling CUDA.',
      mbError, MB_OK
    );
  end;
end;

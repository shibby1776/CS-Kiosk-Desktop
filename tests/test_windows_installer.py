import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsInstallerTests(unittest.TestCase):
    def test_installer_has_permanent_upgrade_identity_and_location(self) -> None:
        script = (ROOT / "ShibbyPrintsCaseSorterInstaller.iss").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "AppId={{C95E90DB-EA7E-4C95-A22C-6579E36FD70D}", script
        )
        self.assertIn("UsePreviousAppDir=yes", script)
        self.assertIn(
            r"DefaultDirName={autopf}\ShibbyPrints Kiosk Sorter", script
        )

    def test_upgrade_replaces_only_packaged_runtime(self) -> None:
        script = (ROOT / "ShibbyPrintsCaseSorterInstaller.iss").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'Type: filesandordirs; Name: "{app}\\app"', script
        )
        self.assertNotIn(
            'Type: filesandordirs; Name: "{app}"', script
        )
        self.assertNotRegex(script, r"(?i)InstallDelete.*(?:appdata|documents)")
        self.assertIn(
            'Source: "c\\*"; '
            'DestDir: "{app}\\app"',
            script,
        )
        self.assertIn(
            'Source: "g\\*"; '
            'DestDir: "{app}\\app"',
            script,
        )

    def test_installer_creates_start_menu_and_desktop_shortcuts(self) -> None:
        script = (ROOT / "ShibbyPrintsCaseSorterInstaller.iss").read_text(
            encoding="utf-8"
        )

        self.assertIn('{autoprograms}\\ShibbyPrints Kiosk Sorter', script)
        self.assertIn('{autodesktop}\\ShibbyPrints Kiosk Sorter', script)
        self.assertIn('Flags: nowait postinstall skipifsilent', script)

    def test_portable_launcher_keeps_executable_in_onedir(self) -> None:
        launcher = (ROOT / "Launch ShibbyPrints Case Sorter.bat").read_text(
            encoding="utf-8"
        )

        expected = (
            r"dist\ShibbyPrintsCaseSorter\ShibbyPrintsCaseSorter.exe"
        )
        self.assertIn(expected, launcher)
        self.assertIn('start "ShibbyPrints Case Sorter"', launcher)

    def test_installer_build_uses_release_version_without_hardcoding(self) -> None:
        build = (ROOT / "build_installer.bat").read_text(encoding="utf-8")

        self.assertIn("call build_windows.bat --no-pause", build)
        self.assertIn('findstr /B /C:"APP_VERSION=" "RELEASE.env"', build)
        self.assertIn('findstr /B /C:"KIOSK_VERSION=" "RELEASE.env"', build)
        self.assertIn("INSTALLER_SUFFIX", build)
        self.assertIn(
            "ShibbyPrints-Kiosk-%PUBLIC_VERSION%-%INSTALLER_SUFFIX%-Setup.exe",
            build,
        )
        self.assertIn(
            '"%ISCC_EXE%" "%STAGE_ROOT%\\ShibbyPrintsCaseSorterInstaller.iss"',
            build,
        )
        self.assertIsNone(re.search(r"v1\.27\.12-Setup\.exe", build))

    def test_installer_uses_physical_stage_and_can_reuse_existing_dist(self) -> None:
        build = (ROOT / "build_installer.bat").read_text(encoding="utf-8")
        recovery = (
            ROOT / "Compile Installer From Existing Build.bat"
        ).read_text(encoding="utf-8")

        self.assertIn('if /I "%~1"=="--compile-only"', build)
        self.assertIn('set "STAGE_ROOT=%TEMP%\\SPI-%RANDOM%-%RANDOM%"', build)
        self.assertIn('robocopy "%CD%\\dist_cpu\\ShibbyPrintsCaseSorter"', build)
        self.assertIn('robocopy "%CD%\\dist_cuda\\ShibbyPrintsCaseSorter"', build)
        self.assertIn('rmdir /S /Q "%STAGE_ROOT%"', build)
        self.assertNotIn("subst ", build.casefold())
        self.assertIn(
            'dist_cpu\\ShibbyPrintsCaseSorter\\ShibbyPrintsCaseSorter.exe',
            build,
        )
        self.assertIn('build_installer.bat" --compile-only', recovery)

    def test_staged_setup_is_copied_back_before_cleanup(self) -> None:
        build = (ROOT / "build_installer.bat").read_text(encoding="utf-8")

        staged = build.index('set "STAGED_SETUP=')
        copied = build.index('copy /Y "%STAGED_SETUP%" "%SETUP_EXE%"')
        cleaned = build.index('rmdir /S /Q "%STAGE_ROOT%"', copied)
        self.assertLess(staged, copied)
        self.assertLess(copied, cleaned)

    def test_generated_installer_version_matches_release_metadata(self) -> None:
        release = (ROOT / "RELEASE.env").read_text(encoding="utf-8")
        version = re.search(r"(?m)^APP_VERSION=(.+)$", release).group(1)
        public_version = re.search(
            r"(?m)^KIOSK_VERSION=(.+)$", release
        ).group(1)
        generated = (ROOT / "installer_version.iss").read_text(encoding="utf-8")

        self.assertIn(f'#define MyAppVersion "{version}"', generated)
        self.assertIn(f'#define MyPublicVersion "{public_version}"', generated)
        self.assertIn('#define MyDisplayVersion "Kiosk 2.3"', generated)
        self.assertIn('#define MyInstallerSuffix "Public"', generated)

    def test_installer_selects_cuda_only_for_a_supported_nvidia_gpu(self) -> None:
        script = (ROOT / "ShibbyPrintsCaseSorterInstaller.iss").read_text(
            encoding="utf-8"
        )

        self.assertIn('Name: "cuda"', script)
        self.assertIn('Check: SupportedCudaGpu', script)
        self.assertIn('nvidia-smi --query-gpu=name,compute_cap', script)
        self.assertIn('MajorCapability >= 8', script)
        self.assertIn('Tasks: not cuda', script)
        self.assertIn('Tasks: cuda', script)
        self.assertIn('--test-cuda-device', script)

    def test_installer_firewall_rules_are_private_and_removed(self) -> None:
        script = (ROOT / "ShibbyPrintsCaseSorterInstaller.iss").read_text(
            encoding="utf-8"
        )

        self.assertIn('Name: "lanaccess"', script)
        self.assertIn('profile=private', script)
        self.assertIn('remoteip=localsubnet', script)
        self.assertIn('protocol=TCP', script)
        self.assertIn('protocol=UDP localport=5353', script)
        self.assertIn('[UninstallRun]', script)
        self.assertIn('ArchitecturesAllowed=x64compatible', script)
        self.assertIn('ArchitecturesInstallIn64BitMode=x64compatible', script)
        self.assertEqual(2, script.count('RunOnceId:'))
        self.assertGreaterEqual(
            script.count('firewall delete rule name=""ShibbyPrints Kiosk Sorter'),
            4,
        )


if __name__ == "__main__":
    unittest.main()

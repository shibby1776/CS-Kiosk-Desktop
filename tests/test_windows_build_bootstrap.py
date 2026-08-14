from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsBuildBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (ROOT / "build_windows.bat").read_text(
            encoding="utf-8"
        )

    def test_python_install_requires_explicit_yes(self) -> None:
        prompt = self.script.index(
            'choice /C YN /N /M "Install Python 3.12'
        )
        install = self.script.index(
            "winget install --exact --id Python.Python.3.12"
        )
        self.assertLess(prompt, install)
        self.assertIn("if errorlevel 2 (", self.script[prompt:install])

    def test_python_detection_accepts_supported_entry_points(self) -> None:
        self.assertIn("py -3 -c", self.script)
        self.assertIn("python -c", self.script)
        self.assertIn("python3 -c", self.script)
        self.assertIn("sys.version_info >= (3, 10)", self.script)

    def test_every_build_command_uses_verified_interpreter(self) -> None:
        self.assertNotIn("%PY%", self.script)
        self.assertIn(
            'set "FAILED_STEP=Run automated source validation tests"',
            self.script,
        )
        self.assertNotIn(
            'set "FAILED_STEP=Run camera compatibility tests"',
            self.script,
        )
        self.assertIn(
            '"%PY_EXE%" %PY_ARGS% -m PyInstaller',
            self.script,
        )
        self.assertIn(
            '"%PY_EXE%" %PY_ARGS% -m unittest discover',
            self.script,
        )


if __name__ == "__main__":
    unittest.main()

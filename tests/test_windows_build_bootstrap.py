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
            '"%CPU_PY%" -m PyInstaller',
            self.script,
        )
        self.assertIn(
            '"%CUDA_PY%" -m PyInstaller',
            self.script,
        )
        self.assertIn(
            '"%CPU_PY%" -m unittest discover',
            self.script,
        )

    def test_build_produces_pinned_cpu_and_cuda_payloads(self) -> None:
        self.assertIn('requirements-torch-%CACHE_PROFILE%.txt', self.script)
        cache_helper = (ROOT / "build_dependency_cache.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('PROFILES = ("cpu", "cuda")', cache_helper)
        self.assertIn('f"requirements-torch-{profile}.txt"', cache_helper)
        self.assertIn("--distpath dist_cpu", self.script)
        self.assertIn("--distpath dist_cuda", self.script)
        self.assertIn("--expect-runtime cpu", self.script)
        self.assertIn("--expect-runtime cuda", self.script)
        self.assertIn("assert torch.version.cuda", self.script)

    def test_build_reuses_fingerprinted_dependency_environments(self) -> None:
        self.assertIn("build_dependency_cache.py prepare", self.script)
        self.assertIn("build_dependency_cache.py mark", self.script)
        self.assertIn("SHIBBYPRINTS_BUILD_CACHE", self.script)
        self.assertIn("SHIBBYPRINTS_REBUILD_DEPENDENCIES", self.script)
        self.assertIn("PIP_CACHE_DIR", self.script)
        self.assertNotIn("--no-cache-dir", self.script)
        self.assertNotIn("--force-reinstall", self.script)

    def test_build_requires_and_probes_packaged_training_workers(self) -> None:
        self.assertGreaterEqual(
            self.script.count("ShibbyPrintsTrainingWorker.exe"), 4
        )
        self.assertIn("--training-worker --help", self.script)
        self.assertIn('findstr /C:"ConvNeXt trainer"', self.script)


if __name__ == "__main__":
    unittest.main()

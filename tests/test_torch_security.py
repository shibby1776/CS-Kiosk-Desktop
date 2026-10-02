from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from sorter.torch_security import (
    MIN_TORCH_VERSION_TEXT,
    UnsafeTorchVersionError,
    require_safe_torch,
    stable_release_tuple,
)


ROOT = Path(__file__).resolve().parents[1]


class TorchSecurityTests(unittest.TestCase):
    def test_approved_stable_versions_are_accepted(self) -> None:
        for version in ("2.10.0", "2.13.0", "2.13.0+cpu", "3.0.0+cu128"):
            with self.subTest(version=version):
                self.assertEqual(version, require_safe_torch(SimpleNamespace(__version__=version)))

    def test_unsafe_unknown_and_prerelease_versions_fail_closed(self) -> None:
        for version in ("", "unknown", "2.9.1", "2.10.0rc1", "2.10.0.dev20260817"):
            with self.subTest(version=version):
                with self.assertRaises(UnsafeTorchVersionError):
                    require_safe_torch(SimpleNamespace(__version__=version))

    def test_version_parser_handles_local_build_suffix(self) -> None:
        self.assertEqual((2, 13, 0), stable_release_tuple("2.13.0+cu128"))
        self.assertIsNone(stable_release_tuple("2.13.0a1"))
        self.assertEqual("2.10.0", MIN_TORCH_VERSION_TEXT)

    def test_dependency_sources_pin_the_reviewed_pair(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        installer = (ROOT / "sorter" / "ui" / "dialog_install_torch.py").read_text(
            encoding="utf-8"
        )
        for source in (requirements, pyproject, installer):
            self.assertIn("torch==2.13.0", source)
            self.assertIn("torchvision==0.28.0", source)
        self.assertNotIn("torch>=2.2", requirements)
        self.assertNotIn("torch==2.9.1", installer)

    def test_checkpoint_load_has_an_adjacent_runtime_guard(self) -> None:
        source = (ROOT / "sorter" / "local_inference.py").read_text(encoding="utf-8")
        load_index = source.index("ckpt = torch.load")
        guard_index = source.rindex("require_safe_torch(torch)", 0, load_index)
        self.assertLess(load_index - guard_index, 500)
        self.assertIn("weights_only=True", source[load_index:load_index + 180])

    def test_source_and_packaged_build_verification_enforce_minimum(self) -> None:
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        build = (ROOT / "build_windows.bat").read_text(encoding="utf-8")
        self.assertIn("require_safe_torch(torch)", main)
        self.assertIn("require_safe_torch(torch)", build)
        self.assertIn("--verify-runtime", build)


if __name__ == "__main__":
    unittest.main()

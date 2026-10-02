from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import build_dependency_cache as cache


class BuildDependencyCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "requirements-base.txt").write_text("requests==1\n")
        (self.root / "requirements-build.txt").write_text("pyinstaller==1\n")
        (self.root / "requirements-torch-cpu.txt").write_text("torch==1+cpu\n")
        (self.root / "requirements-torch-cuda.txt").write_text("torch==1+cuda\n")
        self.signature = {
            "implementation": "CPython",
            "version": [3, 12, 1],
            "architecture_bits": 64,
            "machine": "amd64",
            "executable": "C:/Python312/python.exe",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_key_changes_with_profile_or_requirement_content(self) -> None:
        cpu_payload = cache.fingerprint_payload("cpu", self.root, self.signature)
        cuda_payload = cache.fingerprint_payload("cuda", self.root, self.signature)
        self.assertNotEqual(cache.cache_key(cpu_payload), cache.cache_key(cuda_payload))
        old_key = cache.cache_key(cpu_payload)
        (self.root / "requirements-base.txt").write_text("requests==2\n")
        new_payload = cache.fingerprint_payload("cpu", self.root, self.signature)
        self.assertNotEqual(old_key, cache.cache_key(new_payload))

    def test_marker_must_exactly_match_the_fingerprint(self) -> None:
        payload = cache.fingerprint_payload("cpu", self.root, self.signature)
        environment = self.root / "environment"
        environment.mkdir()
        (environment / cache.MARKER_NAME).write_text(json.dumps(payload))
        with mock.patch.object(cache, "environment_python") as python_path:
            python_path.return_value = self.root / "missing-python.exe"
            self.assertFalse(cache.is_ready(environment, payload, self.root))

    def test_batch_environment_file_reports_paths_and_state(self) -> None:
        output = self.root / "state.env"
        environment = self.root / "cached-env"
        with mock.patch.object(cache, "environment_python") as python_path:
            python_path.return_value = environment / "Scripts" / "python.exe"
            cache.write_batch_environment(output, environment, "abc123", False)
        content = output.read_text()
        self.assertIn(f"ENV_PATH={environment}", content)
        self.assertIn("CACHE_KEY=abc123", content)
        self.assertIn("READY=0", content)


if __name__ == "__main__":
    unittest.main()

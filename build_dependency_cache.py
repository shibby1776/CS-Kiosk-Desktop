"""Manage reusable, fingerprinted Windows build environments.

The cache is deliberately outside the extracted source tree so a newer source
bundle can reuse dependencies when its requirements and Python ABI are
unchanged. Application source and PyInstaller output are never cached here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import struct
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
CACHE_SCHEMA = 1
MARKER_NAME = ".shibbyprints-build-environment.json"
PROFILES = ("cpu", "cuda")


def requirement_paths(profile: str, root: Path = ROOT) -> tuple[Path, ...]:
    if profile not in PROFILES:
        raise ValueError(f"Unsupported build profile: {profile}")
    return (
        root / "requirements-base.txt",
        root / "requirements-build.txt",
        root / f"requirements-torch-{profile}.txt",
    )


def python_signature() -> dict[str, object]:
    return {
        "implementation": platform.python_implementation(),
        "version": list(sys.version_info[:3]),
        "architecture_bits": struct.calcsize("P") * 8,
        "machine": platform.machine().lower(),
        "executable": str(Path(sys.executable).resolve()),
    }


def fingerprint_payload(
    profile: str,
    root: Path = ROOT,
    signature: dict[str, object] | None = None,
) -> dict[str, object]:
    requirements: dict[str, str] = {}
    for path in requirement_paths(profile, root):
        requirements[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "schema": CACHE_SCHEMA,
        "profile": profile,
        "python": signature if signature is not None else python_signature(),
        "requirements": requirements,
    }


def cache_key(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def environment_path(cache_root: Path, profile: str, key: str) -> Path:
    return cache_root / "environments" / f"{profile}-{key}"


def environment_python(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def probe_code(profile: str) -> str:
    expected_cuda = "True" if profile == "cuda" else "False"
    return (
        "import cv2,numpy,PIL,requests,msal,platformdirs,serial,torch,torchvision,PyInstaller;"
        "from sorter.torch_security import require_safe_torch;"
        "require_safe_torch(torch);"
        f"assert bool(torch.version.cuda) is {expected_cuda};"
        "import platform;"
        "exec('import pygrabber,comtypes' if platform.system() == 'Windows' else 'pass')"
    )


def is_ready(environment: Path, payload: dict[str, object], root: Path = ROOT) -> bool:
    marker = environment / MARKER_NAME
    python = environment_python(environment)
    try:
        saved = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if saved != payload or not python.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(python), "-c", probe_code(str(payload["profile"]))],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def write_batch_environment(
    output: Path, environment: Path, key: str, ready: bool
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(
            (
                f"ENV_PATH={environment}",
                f"PYTHON_PATH={environment_python(environment)}",
                f"CACHE_KEY={key}",
                f"READY={1 if ready else 0}",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def prepare(args: argparse.Namespace) -> int:
    cache_root = Path(args.cache_root).expanduser().resolve()
    payload = fingerprint_payload(args.profile)
    key = cache_key(payload)
    environment = environment_path(cache_root, args.profile, key)
    ready = False if args.force else is_ready(environment, payload)
    write_batch_environment(Path(args.output), environment, key, ready)
    return 0


def mark(args: argparse.Namespace) -> int:
    cache_root = Path(args.cache_root).expanduser().resolve()
    payload = fingerprint_payload(args.profile)
    key = cache_key(payload)
    environment = environment_path(cache_root, args.profile, key)
    if not is_ready_without_marker(environment, args.profile):
        raise RuntimeError(
            f"Refusing to cache an incomplete {args.profile} build environment"
        )
    marker = environment / MARKER_NAME
    marker.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Cached {args.profile} build environment: {environment}")
    return 0


def is_ready_without_marker(
    environment: Path, profile: str, root: Path = ROOT
) -> bool:
    python = environment_python(environment)
    if not python.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(python), "-c", probe_code(profile)],
            cwd=root,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--profile", choices=PROFILES, required=True)
    prepare_parser.add_argument("--cache-root", required=True)
    prepare_parser.add_argument("--output", required=True)
    prepare_parser.add_argument("--force", action="store_true")
    prepare_parser.set_defaults(handler=prepare)

    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("--profile", choices=PROFILES, required=True)
    mark_parser.add_argument("--cache-root", required=True)
    mark_parser.set_defaults(handler=mark)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Build dependency cache error: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())

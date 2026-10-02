"""Generate deterministic public release metadata from RELEASE.env."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
REQUIRED_KEYS = {"KIOSK_VERSION", "APP_VERSION", "RELEASE_DATE"}
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")


def load_release_info() -> dict[str, str]:
    info: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        (ROOT / "RELEASE.env").read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"RELEASE.env line {line_number} must contain '='")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            raise ValueError(f"RELEASE.env line {line_number} is incomplete")
        if key in info:
            raise ValueError(f"RELEASE.env contains duplicate key {key}")
        info[key] = value

    missing = REQUIRED_KEYS - info.keys()
    extra = info.keys() - REQUIRED_KEYS
    if missing:
        raise ValueError(f"RELEASE.env is missing: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"RELEASE.env has unknown keys: {', '.join(sorted(extra))}")
    for key in ("KIOSK_VERSION", "APP_VERSION"):
        if not VERSION_RE.fullmatch(info[key]):
            raise ValueError(f"{key} must contain only dotted numbers")

    parsed_date = date.fromisoformat(info["RELEASE_DATE"])
    info["RELEASE_DATE_LONG"] = (
        f"{parsed_date.strftime('%B')} {parsed_date.day}, {parsed_date.year}"
    )
    return info


def render_notice(info: dict[str, str]) -> str:
    template = (ROOT / "NOTICE.template").read_text(encoding="utf-8")
    rendered = template.format_map(info)
    if re.search(r"\{[A-Z0-9_]+\}", rendered):
        raise ValueError("NOTICE contains an unresolved release placeholder")
    return rendered


def render_version_module(info: dict[str, str]) -> str:
    kiosk = info["KIOSK_VERSION"]
    return (
        '"""Generated public release metadata. Edit RELEASE.env and run '
        'generate_release_files.py."""\n'
        f'PUBLIC_VERSION = "Kiosk {kiosk}"\n'
        f'PUBLIC_VERSION_NUMBER = "{kiosk}"\n'
        f'APP_VERSION = "{info["APP_VERSION"]}"\n'
        f'RELEASE_DATE = "{info["RELEASE_DATE"]}"\n'
    )


def render_installer_version(info: dict[str, str]) -> str:
    kiosk = info["KIOSK_VERSION"]
    return (
        "; Generated public release metadata. Do not edit directly.\n"
        f'#define MyAppVersion "{info["APP_VERSION"]}"\n'
        f'#define MyPublicVersion "{kiosk}"\n'
        f'#define MyDisplayVersion "Kiosk {kiosk}"\n'
        '#define MyInstallerSuffix "Public"\n'
    )


def validate_project_version(info: dict[str, str]) -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', pyproject)
    if not match:
        raise ValueError("pyproject.toml does not contain a project version")
    if match.group(1) != info["APP_VERSION"]:
        raise ValueError(
            "pyproject.toml version does not match APP_VERSION: "
            f"{match.group(1)} != {info['APP_VERSION']}"
        )


def update_file(path: Path, expected: str, check_only: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == expected:
        return False
    if check_only:
        raise ValueError(f"{path.relative_to(ROOT)} is not generated from RELEASE.env")
    path.write_text(expected, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate generated files without changing them.",
    )
    args = parser.parse_args()

    try:
        info = load_release_info()
        validate_project_version(info)
        outputs = {
            ROOT / "NOTICE": render_notice(info),
            ROOT / "sorter" / "version.py": render_version_module(info),
            ROOT / "installer_version.iss": render_installer_version(info),
        }
        changed = [
            path.name
            for path, expected in outputs.items()
            if update_file(path, expected, args.check)
        ]
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Release metadata error: {exc}\n")

    if args.check:
        print("Release metadata validation passed.")
    elif changed:
        print(f"Generated: {', '.join(changed)}")
    else:
        print("Release metadata is already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

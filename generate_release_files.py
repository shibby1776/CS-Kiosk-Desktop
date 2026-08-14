"""Generate deterministic release metadata from RELEASE.env."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent
REQUIRED_KEYS = {
    "KIOSK_VERSION",
    "DESKTOP_VERSION",
    "RELEASE_DATE",
    "INTERNAL_VERSION",
    "RELEASE_CHANNEL",
}
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
INTERNAL_RE = re.compile(r"^v[0-9]+$")


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
    if not VERSION_RE.fullmatch(info["KIOSK_VERSION"]):
        raise ValueError("KIOSK_VERSION must contain only dotted numbers")
    if not VERSION_RE.fullmatch(info["DESKTOP_VERSION"]):
        raise ValueError("DESKTOP_VERSION must contain only dotted numbers")
    if not INTERNAL_RE.fullmatch(info["INTERNAL_VERSION"]):
        raise ValueError("INTERNAL_VERSION must be formatted like v17")
    if not re.fullmatch(r"[a-z]+", info["RELEASE_CHANNEL"]):
        raise ValueError("RELEASE_CHANNEL must contain lowercase letters only")

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
    internal = info["INTERNAL_VERSION"]
    channel = info["RELEASE_CHANNEL"]
    channel_title = {
        "verification": "Format Verification",
        "savedbins": "Saved Bins Test",
        "savedbinsremote": "Saved Bins Remote Test",
        "savedbinsmulti": "Saved Bins Multi-Select Test",
        "savedbinscontrols": "Saved Bins Controls Test",
        "slotconfiglabel": "Slot Config Label Test",
        "maintenancepreview": "Maintenance Preview Layout Test",
        "sensordiagnostics": "Sensor Diagnostics Test",
        "reintigration": "Reintigration Test",
        "binsfilename": "Saved Bins Filename Policy Test",
        "slotsync": "Slot Assignment Sync Test",
        "installerupgrade": "Windows Installer Upgrade Test",
        "binscompatibility": "Saved Bins Compatibility Test",
        "usbtransfer": "Saved Bins USB Transfer Test",
        "usbimportfix": "Saved Bins USB Import Fix Test",
        "public": "Public",
    }.get(channel, channel.title())
    public_version = f"Kiosk {kiosk}"
    return (
        '"""Generated release metadata. Do not edit directly; '
        'edit RELEASE.env and run generate_release_files.py."""\n'
        f'PUBLIC_VERSION = "{public_version}"\n'
        f'PUBLIC_VERSION_NUMBER = "{kiosk}"\n'
        f'DESKTOP_RELEASE_VERSION = "{info["DESKTOP_VERSION"]}"\n'
        f'RELEASE_DATE = "{info["RELEASE_DATE"]}"\n'
        f'INTERNAL_VERSION = "{internal}"\n'
        f'RELEASE_CHANNEL = "{channel}"\n'
        f'RELEASE_LABEL = "{internal} {channel_title}"\n'
        f'IMAGE_BASENAME = "ai-case-sorter-kiosk-v{kiosk}-{internal}-{channel}"\n'
        f'ARCHIVE_BASENAME = "AI-Case-Sorter-Kiosk-v{kiosk}-Internal-'
        f'{internal}-{channel_title.replace(" ", "-")}-Ubuntu-pi-gen"\n'
    )


def render_installer_version(info: dict[str, str]) -> str:
    return (
        "; Generated release metadata. Do not edit directly.\n"
        f'#define MyAppVersion "{info["DESKTOP_VERSION"]}"\n'
        f'#define MyPublicVersion "{info["KIOSK_VERSION"]}"\n'
    )


def validate_project_version(info: dict[str, str]) -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', pyproject)
    if not match:
        raise ValueError("pyproject.toml does not contain a project version")
    def normalized(value: str) -> tuple[int, ...]:
        parts = [int(part) for part in value.split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)

    project_version = normalized(match.group(1))
    desktop_version = normalized(info["DESKTOP_VERSION"])
    if project_version != desktop_version:
        raise ValueError(
            "pyproject.toml version does not match DESKTOP_VERSION: "
            f"{match.group(1)} != {info['DESKTOP_VERSION']}"
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

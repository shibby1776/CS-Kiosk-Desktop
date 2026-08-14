"""Entry point — initialize SQLite, load config, launch the Tk main window."""
from __future__ import annotations

import sys
from pathlib import Path



def verify_runtime() -> int:
    """Verify packaged runtime dependencies without starting the UI."""
    import traceback
    try:
        import torch
        import torchvision
        print(f"torch={torch.__version__} path={getattr(torch, '__file__', '')}")
        print(f"torchvision={torchvision.__version__} path={getattr(torchvision, '__file__', '')}")
        print("PYTORCH_RUNTIME_OK")
        return 0
    except BaseException as exc:
        print(f"PYTORCH_RUNTIME_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 2

def main() -> int:
    if "--verify-runtime" in sys.argv:
        return verify_runtime()

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    from sorter import paths
    from sorter.config import Config
    from sorter.db import Database
    from sorter.ui.app import MainWindow

    paths.ensure_directories()
    legacy_json = here / "data" / "config.json"

    db = Database()
    db.ensure_initialized(legacy_config_json=legacy_json if legacy_json.exists() else None)

    config = Config(db).load()
    MainWindow(config).run()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

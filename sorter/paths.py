"""Cross-platform writable locations for the Desktop Edition."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "ShibbyPrints AI Case Sorter"
APP_AUTHOR = "ShibbyPrints"


def _app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def app_data_dir() -> Path:
    """Writable user data directory, overrideable with CASESORTER_DATA_DIR."""
    override = os.environ.get("CASESORTER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    try:
        from platformdirs import user_data_dir
        return Path(user_data_dir(APP_NAME, APP_AUTHOR, roaming=False))
    except Exception:
        return Path.home() / ".shibbyprints-case-sorter"


def config_dir() -> Path:
    return app_data_dir() / "config"


def db_path() -> Path:
    return config_dir() / "casesorter.db"


def token_cache_path() -> Path:
    return config_dir() / "msal_cache.bin"


def models_dir() -> Path:
    return app_data_dir() / "models"


def export_temp_dir() -> Path:
    return app_data_dir() / "tmp"


def documents_dir() -> Path:
    """Return the user's visible Documents folder."""
    try:
        from platformdirs import user_documents_dir
        return Path(user_documents_dir())
    except Exception:
        return Path.home() / "Documents"


def saved_bins_dir() -> Path:
    """Portable Saved Bins files live outside the private application data."""
    return documents_dir() / "ShibbyPrints" / "Saved Bins"


def model_dir(model_id: int) -> Path:
    return models_dir() / str(model_id)


def model_images_dir(model_id: int) -> Path:
    return model_dir(model_id) / "images"


def model_run_images_dir(model_id: int) -> Path:
    return model_dir(model_id) / "run_images"


def model_feedback_dir(model_id: int) -> Path:
    return model_dir(model_id) / "feedback_images"


def model_reports_dir(model_id: int) -> Path:
    return model_dir(model_id) / "reports"


def model_trained_dir(model_id: int) -> Path:
    return model_dir(model_id) / "trainedmodel"


def model_trained_path(model_id: int) -> Path:
    return model_trained_dir(model_id) / f"{model_id}.pth"


def ensure_directories() -> None:
    for directory in (
        app_data_dir(),
        config_dir(),
        models_dir(),
        saved_bins_dir(),
    ):
        directory.mkdir(parents=True, exist_ok=True)


def ensure_model_subtree(model_id: int) -> None:
    model_images_dir(model_id).mkdir(parents=True, exist_ok=True)
    model_trained_dir(model_id).mkdir(parents=True, exist_ok=True)

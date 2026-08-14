"""Dispatcher: pick local PyTorch inference or HTTP classify based on active model.

Called from `RunController` so the run loop doesn't need to know which backend
is active. Routing:
  - Active model has a `model_path` pointing to an existing file → local
  - Otherwise → HTTP via `api_client.classify`
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from . import api_client, local_inference
from .db import Database
from .repository import ModelRepo, SettingsRepo



def uses_local_backend(db: Database | None) -> bool:
    """Return True when the active model resolves to an existing local model file."""
    if db is None:
        return False
    active_id = SettingsRepo(db).get_active_model_id()
    if active_id is None:
        return False
    model = ModelRepo(db).get(active_id)
    return bool(model is not None and model.model_path and Path(model.model_path).exists())

def classify_active(
    image_bgr: np.ndarray,
    headstamps: list[str],
    api_cfg: dict[str, Any],
    db: Database | None,
) -> tuple[str, float]:
    """Classify `image_bgr` using whichever backend the active model selects.

    Falls back to HTTP if `db` is None (used in tests that don't need
    a database).
    """
    if db is not None:
        active_id = SettingsRepo(db).get_active_model_id()
        if active_id is not None:
            model = ModelRepo(db).get(active_id)
            if (
                model is not None
                and model.model_path
                and Path(model.model_path).exists()
            ):
                # Pass the trained image size from the model record so
                # imported community models (often trained at 480) get
                # the right resolution at inference.
                image_size = (
                    int(model.training_config.image_size)
                    if model.training_config else None
                )
                return local_inference.classify(
                    image_bgr, model.model_path, image_size=image_size,
                )

    return api_client.classify(image_bgr, headstamps, api_cfg)

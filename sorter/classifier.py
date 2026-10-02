"""Dispatcher: pick local PyTorch inference or HTTP classify based on active model.

Called from `RunController` so the run loop doesn't need to know which backend
is active. The operator's selection is authoritative:
  - An active model always means local inference.
  - No active model means the configured HTTP API.

An active model whose checkpoint is missing fails closed. It must never fall
back to the HTTP endpoint: that could route a case with a different model than
the operator selected.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from . import api_client, local_inference
from .db import Database
from .models import Model
from .repository import ModelRepo, SettingsRepo
from .torch_security import stable_release_tuple


class NoLocalCheckpointError(Exception):
    """The selected local model has no usable checkpoint on disk."""


def active_model(db: Database | None) -> Model | None:
    if db is None:
        return None
    active_id = SettingsRepo(db).get_active_model_id()
    return None if active_id is None else ModelRepo(db).get(active_id)


def has_local_checkpoint(model: Model | None) -> bool:
    return bool(
        model is not None
        and model.model_path
        and Path(model.model_path).is_file()
    )


def checkpoint_problem(db: Database | None) -> str | None:
    """Explain why the selected local model cannot classify, if applicable."""
    model = active_model(db)
    if model is None:
        return None
    if has_local_checkpoint(model):
        return torch_floor_problem(model)
    name = model.name or f"model #{model.id}"
    if not model.model_path:
        return (
            f"“{name}” has no trained model file. Train it or re-download it "
            "before sorting, or explicitly switch to API mode on the Models tab."
        )
    return (
        f"“{name}” points to a trained model file that is missing:\n\n"
        f"{model.model_path}\n\nRestore or re-download the file, or explicitly "
        "switch to API mode on the Models tab."
    )


def torch_floor_problem(model: Model | None) -> str | None:
    """Explain a known checkpoint/runtime version mismatch before sorting."""
    required = model.checkpoint_env.torch if model is not None else ""
    if not required:
        return None
    have = local_inference.installed_version()
    required_version = stable_release_tuple(required)
    installed = stable_release_tuple(have or "")
    if required_version is None or installed is None or installed >= required_version:
        return None
    name = (model.name if model is not None else "") or "This model"
    return (
        f"“{name}” was trained with PyTorch {required}, but this machine has {have}.\n\n"
        "Update the local PyTorch runtime before sorting with this model, or "
        "select a compatible model."
    )


def uses_local_backend(db: Database | None) -> bool:
    """Return True when the operator selected a local model.

    Checkpoint presence is deliberately not part of this decision. A missing
    checkpoint remains a local-model error and cannot turn into API mode.
    """
    return active_model(db) is not None

def classify_active(
    image_bgr: np.ndarray,
    headstamps: list[str],
    api_cfg: dict[str, Any],
    db: Database | None,
) -> tuple[str, float]:
    """Classify `image_bgr` using whichever backend the active model selects.

    Uses HTTP only when no model is active. Raises NoLocalCheckpointError when
    the selected local model has no checkpoint.
    """
    model = active_model(db)
    if model is None:
        return api_client.classify(image_bgr, headstamps, api_cfg)
    if not has_local_checkpoint(model) or not model.model_path:
        raise NoLocalCheckpointError(checkpoint_problem(db) or "The selected model is not ready.")
    floor_problem = torch_floor_problem(model)
    if floor_problem:
        raise NoLocalCheckpointError(floor_problem)

    # Imported community models are often trained at 480 rather than 224.
    image_size = int(model.training_config.image_size) if model.training_config else None
    return local_inference.classify(
        image_bgr,
        model.model_path,
        image_size=image_size,
    )

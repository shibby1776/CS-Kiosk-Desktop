"""Community feedback-loop policy + I/O.

When a community model's publisher enabled the feedback loop, predictions whose
confidence falls below the publisher's threshold are captured during a run and
uploaded to the server for the owner to moderate. This module owns the
*decision* (should this prediction be captured?) and the *I/O* (stage the
image, drain the upload queue), kept free of any Tk dependency so it is
unit-testable.

The staging folder ``data/models/<id>/feedback_images/`` IS the queue — one
JPEG per below-threshold capture, named ``{label}__{confidence}__{ticks}.jpg``
so the label + confidence travel with the file. There is no database mirror:
the folder is tiny and transient, so polling it is the source of truth for the
OnRunComplete / Manual modes.

Drop-on-failure: anything that can't be uploaded — signed out, token expired,
network error, or a server "not accepting feedback" reply — has its file
deleted rather than retained. There is no durable cross-session retry queue.

The effective floor is ``max(50, publisher_floor)`` and upload is a SAS
round-trip (request ticket -> PUT blob -> complete).
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from . import paths
from .models import Model
from .repository import ModelRepo
from .training.dataset import parse_feedback_filename, save_feedback_image


# Floor on the publisher's floor: even if a publisher set a very low
# threshold, anything under 50% confidence is worth moderating.
MIN_EFFECTIVE_FLOOR = 50
MAX_WISH_LIST_CAPTURES_PER_LABEL = 40


def _debug_enabled() -> bool:
    """Feedback-loop tracing is OFF unless ``CASESORTER_FEEDBACK_DEBUG=1``."""
    return os.environ.get("CASESORTER_FEEDBACK_DEBUG", "0") == "1"


def debug_log(msg: str) -> None:
    """Console trace for the feedback pipeline (capture → queue → upload).

    Off by default; enable with ``CASESORTER_FEEDBACK_DEBUG=1`` to print the
    whole chain to stderr with a ``[feedback]`` prefix.
    """
    if _debug_enabled():
        print(f"[feedback] {msg}", file=sys.stderr, flush=True)


def is_feedback_model(model: Model | None) -> bool:
    """True when ``model`` is a community model with the feedback loop enabled."""
    return bool(
        model is not None
        and model.feedback_loop_enabled
        and model.community_model_uid
    )


class FeedbackService:
    def __init__(self, db: Any) -> None:
        self.db = db
        self._wish_lock = threading.Lock()
        self._wish_model_id: int | None = None
        self._wish_list: set[str] = set()
        self._wish_counts: dict[str, int] = {}
        self._server_lock = threading.Lock()
        self._server_model_id: int | None = None
        self._server_floor = 0
        self._server_capture_allowed = True

    # ----- server policy and wish list ---------------------------------------

    def set_wish_list(self, model_id: int | None, names: Iterable[str]) -> None:
        cleaned = {name.strip().lower() for name in (names or ()) if name and name.strip()}
        with self._wish_lock:
            self._wish_model_id = model_id if cleaned else None
            self._wish_list = cleaned
            self._wish_counts = {}
        debug_log(f"wish list for model {model_id}: {sorted(cleaned) or '(empty)'}")

    def clear_wish_list(self) -> None:
        self.set_wish_list(None, ())

    def wish_list(self) -> list[str]:
        with self._wish_lock:
            return sorted(self._wish_list)

    def _claim_wish_capture(self, model: Model | None, label: str) -> bool:
        key = (label or "").strip().lower()
        if model is None or not key:
            return False
        with self._wish_lock:
            if model.id != self._wish_model_id or key not in self._wish_list:
                return False
            used = self._wish_counts.get(key, 0)
            if used >= MAX_WISH_LIST_CAPTURES_PER_LABEL:
                return False
            self._wish_counts[key] = used + 1
        return True

    def apply_server_settings(
        self,
        model_id: int | None,
        *,
        confidence_floor: int = 0,
        feedback_enabled: bool = True,
        blocked: bool = False,
    ) -> None:
        with self._server_lock:
            self._server_model_id = model_id
            self._server_floor = max(0, int(confidence_floor))
            self._server_capture_allowed = bool(feedback_enabled) and not bool(blocked)
        debug_log(
            f"server settings for model {model_id}: floor={confidence_floor} "
            f"enabled={feedback_enabled} blocked={blocked}"
        )

    def clear_server_settings(self) -> None:
        self.apply_server_settings(None)

    def _server_policy(self, model: Model) -> tuple[int, bool]:
        with self._server_lock:
            if model.id != self._server_model_id:
                return 0, True
            return self._server_floor, self._server_capture_allowed

    def refresh_server_settings(self, model: Model | None, *, auth: Any) -> Any:
        """Fetch and apply the source application's current feedback contract.

        Failures clear transient server policy and wish-list state, returning
        to the installed model's local confidence rule without blocking a run.
        """
        if not is_feedback_model(model) or auth is None:
            self.clear_server_settings()
            self.clear_wish_list()
            return None
        try:
            from .community_api import CommunityApi

            settings = CommunityApi(auth=auth).fetch_model_settings(
                str(model.community_model_uid)
            )
        except Exception:
            debug_log("model settings fetch FAILED:\n" + traceback.format_exc())
            settings = None
        if settings is None:
            self.clear_server_settings()
            self.clear_wish_list()
            return None
        self.apply_server_settings(
            model.id,
            confidence_floor=settings.confidence_floor,
            feedback_enabled=settings.feedback_enabled,
            blocked=settings.blocked,
        )
        self.set_wish_list(model.id, settings.wish_list)
        return settings

    # ----- policy -------------------------------------------------------------

    def effective_floor(self, model: Model) -> int:
        floor, _allowed = self._server_policy(model)
        return max(
            MIN_EFFECTIVE_FLOOR,
            int(floor or model.feedback_loop_confidence_floor),
        )

    def should_capture(
        self,
        model: Model | None,
        confidence: float,
        label: str = "",
        *,
        wish_list: bool = False,
    ) -> bool:
        """Capture below-floor predictions on feedback-enabled community models.

        Confidence is a 0-100 percentage; ``-1`` (unknown — e.g. an HTTP
        backend returned no confidence) is never captured.
        """
        if not is_feedback_model(model):
            debug_log(
                "should_capture=False: not a feedback model "
                f"(enabled={getattr(model, 'feedback_loop_enabled', None)}, "
                f"community_uid={getattr(model, 'community_model_uid', None)!r})"
            )
            return False
        if not self._server_policy(model)[1]:
            debug_log("should_capture=False: server disabled or blocked feedback")
            return False
        floor = self.effective_floor(model)
        if confidence is not None and confidence >= 0 and float(confidence) < floor:
            debug_log(
                f"should_capture=True: confidence={confidence} vs effective_floor={floor} "
                f"(publisher_floor={model.feedback_loop_confidence_floor}, mode={model.feedback_loop_upload_mode})"
            )
            return True
        if wish_list and self._claim_wish_capture(model, label):
            debug_log(f"should_capture=True: {label!r} is on the model wish list")
            return True
        debug_log(
            f"should_capture=False: confidence={confidence} vs effective_floor={floor}; "
            f"label={label!r}; wish_list_checked={wish_list}"
        )
        return False

    # ----- staging folder (the queue) ----------------------------------------

    def pending_files(self, model_id: int) -> list[Path]:
        """JPEGs staged for upload, oldest first (filenames are tick-ordered)."""
        d = paths.model_feedback_dir(model_id)
        if not d.exists():
            return []
        return sorted(
            p for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg")
        )

    def count_pending(self, model_id: int) -> int:
        return len(self.pending_files(model_id))

    def has_pending(self, model_id: int) -> bool:
        return bool(self.pending_files(model_id))

    # ----- capture ------------------------------------------------------------

    def capture(
        self, model: Model, image_bgr: np.ndarray, label: str, confidence: float
    ) -> str | None:
        """Stage a below-floor image in the model's feedback folder.

        Returns its path, or ``None`` on failure (best-effort — a failure here
        never interrupts a run).
        """
        try:
            dest = save_feedback_image(
                image_bgr, paths.model_feedback_dir(model.id), label or "unknown", confidence,
            )
            debug_log(f"captured -> {dest}")
            return str(dest)
        except Exception:
            debug_log("capture FAILED:\n" + traceback.format_exc())
            return None

    # ----- upload -------------------------------------------------------------

    @staticmethod
    def _delete_file(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _drop_all(self, files: list[Path], result: dict[str, Any]) -> None:
        for path in files:
            self._delete_file(path)
            result["dropped"] += 1

    def upload_pending(self, model_id: int, *, auth: Any) -> dict[str, Any]:
        """Drain a model's feedback folder. Drop-on-failure throughout.

        Returns ``{"uploaded", "dropped", "declined", "skipped"}``. ``declined``
        is True when the server replied "not accepting feedback" (the caller
        should stop trying for this model this session); ``skipped`` is True
        when we couldn't authenticate at all.
        """
        result: dict[str, Any] = {
            "uploaded": 0, "dropped": 0, "declined": False, "skipped": False,
        }
        files = self.pending_files(model_id)
        debug_log(f"upload_pending: model_id={model_id} pending_files={len(files)} auth={auth is not None}")
        if not files:
            return result

        model = ModelRepo(self.db).get(model_id)
        if not is_feedback_model(model):
            debug_log(f"upload_pending: model {model_id} not feedback-enabled — dropping {len(files)} staged")
            self._drop_all(files, result)
            return result

        # Can't authenticate → drop silently, don't retain (user's choice).
        token = None
        if auth is not None:
            try:
                token = auth.acquire_token_silent()
            except Exception:
                debug_log("upload_pending: acquire_token_silent raised:\n" + traceback.format_exc())
        if auth is None or token is None:
            debug_log(f"upload_pending: no usable auth token — dropping {len(files)} staged (skipped)")
            self._drop_all(files, result)
            result["skipped"] = True
            return result

        # Lazy import so the capture path carries no requests/msal dependency.
        from .community_api import CommunityApi, CommunityApiError

        api = CommunityApi(auth=auth)
        version = model.model_version or 1

        for i, path in enumerate(files):
            parsed = parse_feedback_filename(path.name)
            label, confidence = parsed if parsed else ("", 0)
            try:
                debug_log(f"requesting upload ticket for {path.name} (uid={model.community_model_uid!r})")
                ticket = api.request_feedback_upload(
                    filename=path.name,
                    community_model_uid=model.community_model_uid,
                    classification=label,
                    confidence=confidence,
                )
            except CommunityApiError as exc:
                # Auth/network problem mid-drain → drop the rest silently.
                debug_log(f"request ticket CommunityApiError ({exc}) — dropping remaining {len(files) - i}")
                self._drop_all(files[i:], result)
                break
            except Exception:
                debug_log("request ticket FAILED:\n" + traceback.format_exc())
                self._delete_file(path)
                result["dropped"] += 1
                continue

            if ticket is None:
                debug_log(f"request ticket returned None (endpoint unavailable) — dropping remaining {len(files) - i}")
                self._drop_all(files[i:], result)
                break
            if not ticket.feedback_accepted:
                debug_log(f"server declined feedback (message={ticket.feedback_message!r}) — dropping remaining {len(files) - i}")
                result["declined"] = True
                self._drop_all(files[i:], result)
                break

            try:
                debug_log(f"uploading blob for {path.name}")
                api.upload_feedback_blob(str(path), ticket, version=version)
                api.complete_feedback_upload(ticket)
            except Exception:
                debug_log("blob upload/complete FAILED:\n" + traceback.format_exc())
                self._delete_file(path)
                result["dropped"] += 1
                continue

            self._delete_file(path)
            result["uploaded"] += 1
            debug_log(f"uploaded {path.name} ✓")

        debug_log(f"upload_pending done: {result}")
        return result

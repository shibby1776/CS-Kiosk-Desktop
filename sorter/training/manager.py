"""Subprocess-based training launcher.

Spawns `train_convnext.py` with Python during source development, or the
bundled training worker in a frozen Windows installation. It reads stdout and
stderr line by line and surfaces `[PROGRESS]` JSON markers as event-bus events:

    training/start         {"epochs": 15, "classes": 7, "images": 320, ...}
    training/epoch         {"epoch": 3, "train_loss": 0.42, "val_acc": 0.87, ...}
    training/log           "raw stdout line"
    training/error         "raw stderr line"
    training/done          {"best_val_acc": 0.94, "best_val_loss": 0.12, ...}
    training/cancelled     None
    training/failed        "error description"

Cancellation: `cancel()` sends SIGTERM and after 5 s escalates to SIGKILL.
On Windows, `terminate()` is used as the SIGTERM equivalent.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import paths
from ..events import EventBus
from ..models import TrainingConfig


_PROGRESS_PREFIX = "[PROGRESS] "
_KILL_GRACE_S = 5.0
LOG_PREFIX = "training-"
LOG_SUFFIX = ".log"
MAX_TRAINING_LOGS = 10
MAX_TRAINING_LOG_BYTES = 4 * 1024 * 1024
TRAINING_WORKER_NAME = "ShibbyPrintsTrainingWorker.exe"


def training_logs(newest_first: bool = True) -> list[Path]:
    directory = paths.logs_dir()
    if not directory.exists():
        return []
    found = sorted(directory.glob(f"{LOG_PREFIX}*{LOG_SUFFIX}"))
    return list(reversed(found)) if newest_first else found


def _prune_training_logs() -> None:
    for stale in training_logs()[MAX_TRAINING_LOGS - 1:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _checkpoint_state(path: Path) -> tuple[int, int] | None:
    """Return a cheap identity for a non-empty checkpoint, if one exists."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file() or stat.st_size <= 0:
        return None
    return stat.st_size, stat.st_mtime_ns


@dataclass
class TrainingJob:
    image_dir: Path
    output_model: Path
    config: TrainingConfig
    extra_env: dict[str, str] = field(default_factory=dict)


def build_command(job: TrainingJob, *, python: str | None = None,
                  script: Path | None = None,
                  frozen: bool | None = None,
                  executable: str | Path | None = None) -> list[str]:
    """Construct the argv that `manager.spawn` would use. Exposed for tests."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    # Explicit interpreter/script overrides are retained for source tests and
    # development. The installed app instead invokes its console-subsystem
    # worker, which shares the packaged runtime but never initializes the GUI.
    if is_frozen and python is None and script is None:
        app_executable = Path(executable or sys.executable)
        prefix = [str(app_executable.with_name(TRAINING_WORKER_NAME)), "--training-worker"]
    else:
        script_path = script or Path(__file__).with_name("train_convnext.py")
        prefix = [python or sys.executable, "-u", str(script_path)]
    args = prefix + [
        "--image_dir", str(job.image_dir),
        "--output_model", str(job.output_model),
        "--model_name", job.config.model_name,
        "--epochs", str(job.config.epochs),
        "--batch_size", str(job.config.batch_size),
        "--lr", str(job.config.learning_rate),
        "--weight_decay", str(job.config.weight_decay),
        "--dropout", str(job.config.dropout_rate),
        "--val_split", str(job.config.val_split),
        "--imgsize", str(job.config.image_size),
        "--max_workers", str(job.config.max_workers),
        "--stochastic_depth_prob", str(job.config.stochastic_depth_prob),
        "--focal_gamma", str(job.config.focal_gamma),
        "--swa_start", str(job.config.swa_start),
        "--swa_mode", str(job.config.swa_mode),
        "--swa_acc_threshold", str(job.config.swa_acc_threshold),
        "--swa_patience", str(job.config.swa_patience),
        "--swa_min_epoch", str(job.config.swa_min_epoch),
    ]
    if job.config.train_all:
        args.append("--trainall")
    if job.config.freeze_backbone:
        args.append("--freeze_backbone")
    if job.config.use_focal_loss:
        args.append("--use_focal_loss")
    if job.config.use_swa:
        args.append("--use_swa")
    return args


class TrainingManager:
    """Owns at most one active training subprocess."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._proc: subprocess.Popen | None = None
        self._reader_threads: list[threading.Thread] = []
        self._cancel_requested = False
        self._lock = threading.Lock()
        self._completion_event = threading.Event()
        self._last_result: dict[str, Any] | None = None
        self._expected_output: Path | None = None
        self._output_state_before: tuple[int, int] | None = None
        self.log_path: Path | None = None
        self._log: Any = None
        self._log_lock = threading.Lock()
        self._log_full = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def spawn(self, job: TrainingJob, *, python: str | None = None,
              script: Path | None = None) -> subprocess.Popen:
        if self.is_running:
            raise RuntimeError("Training already in progress")

        cmd = build_command(job, python=python, script=script)
        env = dict(os.environ)
        env.update(job.extra_env)
        expected_output = Path(job.output_model)
        output_state_before = _checkpoint_state(expected_output)

        self._open_log(cmd)

        try:
            popen_kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                # The bundled worker has a console subsystem so stdout/stderr
                # remain reliable, but its console window must stay hidden.
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                **popen_kwargs,
            )
        except Exception:
            self._write_log("# process failed to start")
            self._close_log()
            raise
        with self._lock:
            self._proc = proc
            self._cancel_requested = False
            self._last_result = None
            self._expected_output = expected_output
            self._output_state_before = output_state_before
            self._completion_event.clear()

        t_out = threading.Thread(
            target=self._pump_stdout, args=(proc,), daemon=True, name="train-stdout",
        )
        t_err = threading.Thread(
            target=self._pump_stderr, args=(proc,), daemon=True, name="train-stderr",
        )
        t_wait = threading.Thread(
            target=self._wait_for_exit, args=(proc,), daemon=True, name="train-wait",
        )
        self._reader_threads = [t_out, t_err, t_wait]
        for t in self._reader_threads:
            t.start()
        return proc

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._cancel_requested = True
        if sys.platform == "win32":
            proc.terminate()
        else:
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        # Escalate to SIGKILL after grace period.
        def _escalate() -> None:
            try:
                proc.wait(timeout=_KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                if proc.poll() is None:
                    proc.kill()

        threading.Thread(target=_escalate, daemon=True).start()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the active training run completes."""
        return self._completion_event.wait(timeout=timeout)

    def last_result(self) -> dict[str, Any] | None:
        return self._last_result

    def _open_log(self, cmd: list[str]) -> None:
        self.log_path = None
        self._log = None
        self._log_full = False
        try:
            paths.logs_dir().mkdir(parents=True, exist_ok=True)
            _prune_training_logs()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = paths.logs_dir() / f"{LOG_PREFIX}{stamp}{LOG_SUFFIX}"
            self._log = path.open("a", encoding="utf-8", errors="replace")
            self.log_path = path
        except OSError:
            return
        self._write_log(f"# training log — {datetime.now().isoformat(timespec='seconds')}")
        self._write_log(f"# command: {' '.join(cmd)}")
        self.bus.post("training/log", f"[SETUP] Log file: {path}")

    def _write_log(self, line: str) -> None:
        with self._log_lock:
            handle = self._log
            if handle is None or self._log_full:
                return
            try:
                if handle.tell() >= MAX_TRAINING_LOG_BYTES:
                    handle.write("# log truncated at the configured size limit\n")
                    handle.flush()
                    self._log_full = True
                    return
                handle.write(line + "\n")
                handle.flush()
            except (OSError, ValueError):
                self._log = None

    def _close_log(self) -> None:
        with self._log_lock:
            handle, self._log = self._log, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    # ----- internal ----------------------------------------------------------

    def _pump_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                self._write_log(line)
                if line.startswith(_PROGRESS_PREFIX):
                    try:
                        payload = json.loads(line[len(_PROGRESS_PREFIX):])
                    except json.JSONDecodeError:
                        self.bus.post("training/log", line)
                        continue
                    event = payload.pop("event", "log")
                    if event == "done":
                        self._last_result = payload
                        # A completion marker is provisional until the process
                        # exits cleanly and the checkpoint is present.
                        continue
                    self.bus.post(f"training/{event}", payload)
                else:
                    self.bus.post("training/log", line)
        finally:
            proc.stdout.close()

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        try:
            for raw in proc.stderr:
                line = raw.rstrip("\r\n")
                self._write_log(f"[stderr] {line}")
                self.bus.post("training/error", line)
        finally:
            proc.stderr.close()

    def _wait_for_exit(self, proc: subprocess.Popen) -> None:
        rc = proc.wait()
        # Drain final threads briefly so any tail output appears before "done".
        for t in self._reader_threads:
            if t is threading.current_thread():
                continue
            t.join(timeout=2.0)
        self._write_log(
            f"# exit code: {rc}{' (cancelled)' if self._cancel_requested else ''}"
        )
        self._close_log()
        if self._cancel_requested:
            self.bus.post("training/cancelled", {"return_code": rc})
        elif rc != 0:
            self.bus.post("training/failed", {
                "return_code": rc,
                "reason": "worker_exit",
                "message": f"Training worker exited with code {rc}.",
                "log_path": str(self.log_path or ""),
            })
        elif self._last_result is None:
            self.bus.post("training/failed", {
                "return_code": rc,
                "reason": "missing_completion_marker",
                "message": (
                    "Training stopped without reporting completion. No model "
                    "was accepted; review the training log."
                ),
                "log_path": str(self.log_path or ""),
            })
        elif self._expected_output is None or _checkpoint_state(self._expected_output) is None:
            self._last_result = None
            self.bus.post("training/failed", {
                "return_code": rc,
                "reason": "missing_checkpoint",
                "message": (
                    "Training reported completion but did not create a valid "
                    "model checkpoint. No model was accepted."
                ),
                "log_path": str(self.log_path or ""),
            })
        elif _checkpoint_state(self._expected_output) == self._output_state_before:
            self._last_result = None
            self.bus.post("training/failed", {
                "return_code": rc,
                "reason": "unchanged_checkpoint",
                "message": (
                    "Training reported completion but did not update the model "
                    "checkpoint. The previous model was left unchanged."
                ),
                "log_path": str(self.log_path or ""),
            })
        else:
            self.bus.post("training/done", self._last_result)
        with self._lock:
            self._proc = None
        self._completion_event.set()

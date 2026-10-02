"""Entry point — initialize SQLite, load config, launch the Tk main window."""
from __future__ import annotations

import sys
import tempfile
import multiprocessing
from pathlib import Path


TRAINING_WORKER_FLAG = "--training-worker"
TRAINING_WORKER_EXE = "ShibbyPrintsTrainingWorker.exe"


def run_training_worker(argv: list[str]) -> int:
    """Run the bundled trainer without initializing any application UI."""
    from sorter.training.train_convnext import main as training_main

    return training_main(argv)


def _is_training_worker_executable() -> bool:
    return Path(sys.executable).name.casefold() == TRAINING_WORKER_EXE.casefold()



def verify_runtime(*, expected: str = "", test_cuda_device: bool = False) -> int:
    """Exercise the packaged inference runtime without starting the UI."""
    import traceback
    try:
        import torch
        import torchvision
        from torchvision import models
        from sorter.torch_security import require_safe_torch
        require_safe_torch(torch)
        if not str(torch.__version__).startswith("2.13.0"):
            raise RuntimeError(f"Unexpected Torch version: {torch.__version__}")
        if not str(torchvision.__version__).startswith("0.28.0"):
            raise RuntimeError(
                f"Unexpected TorchVision version: {torchvision.__version__}"
            )

        cpu_tensor = torch.arange(12, dtype=torch.float32).reshape(3, 4)
        if float(cpu_tensor.sum().item()) != 66.0:
            raise RuntimeError("CPU tensor verification failed.")
        with tempfile.TemporaryDirectory(prefix="sp-runtime-check-") as tmp:
            checkpoint = Path(tmp) / "safe-checkpoint.pth"
            torch.save({"weights": cpu_tensor}, checkpoint)
            loaded = torch.load(checkpoint, map_location="cpu", weights_only=True)
            if not torch.equal(loaded["weights"], cpu_tensor):
                raise RuntimeError("Safe checkpoint verification failed.")

        network = models.convnext_tiny(weights=None).eval()
        with torch.inference_mode():
            output = network(torch.zeros(1, 3, 64, 64))
        if tuple(output.shape) != (1, 1000):
            raise RuntimeError("ConvNeXt CPU forward-pass verification failed.")

        packaged_cuda = bool(getattr(torch.version, "cuda", None))
        if expected == "cpu" and packaged_cuda:
            raise RuntimeError("CPU package unexpectedly contains a CUDA runtime.")
        if expected == "cuda" and not packaged_cuda:
            raise RuntimeError("CUDA package contains a CPU-only Torch runtime.")

        if test_cuda_device:
            if not torch.cuda.is_available():
                raise RuntimeError(
                    "The CUDA runtime is installed, but the NVIDIA driver/GPU "
                    "is not available to PyTorch."
                )
            device = torch.device("cuda")
            gpu_tensor = torch.ones(8, device=device)
            if float(gpu_tensor.sum().item()) != 8.0:
                raise RuntimeError("CUDA tensor verification failed.")
            network = network.to(device)
            with torch.inference_mode():
                gpu_output = network(torch.zeros(1, 3, 64, 64, device=device))
            if tuple(gpu_output.shape) != (1, 1000):
                raise RuntimeError("ConvNeXt CUDA forward-pass verification failed.")
            print(f"cuda_device={torch.cuda.get_device_name(0)}")

        print(f"torch={torch.__version__} path={getattr(torch, '__file__', '')}")
        print(f"torchvision={torchvision.__version__} path={getattr(torchvision, '__file__', '')}")
        print(f"packaged_cuda={getattr(torch.version, 'cuda', None)}")
        print("CPU_TENSOR_OK SAFE_CHECKPOINT_OK CONVNEXT_CPU_OK")
        print("PYTORCH_RUNTIME_OK")
        return 0
    except BaseException as exc:
        print(f"PYTORCH_RUNTIME_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 2

def main() -> int:
    # This must run before importing the GUI or trainer. In a frozen Windows
    # build it dispatches DataLoader multiprocessing children without allowing
    # them to re-enter the application entry point.
    multiprocessing.freeze_support()

    if TRAINING_WORKER_FLAG in sys.argv:
        index = sys.argv.index(TRAINING_WORKER_FLAG)
        return run_training_worker(sys.argv[index + 1:])

    # The worker executable is an internal component. Refuse an accidental
    # direct launch instead of opening a second copy of the sorter UI.
    if _is_training_worker_executable():
        print(
            f"{TRAINING_WORKER_EXE} is an internal training component.",
            file=sys.stderr,
        )
        return 2

    if "--verify-runtime" in sys.argv:
        expected = ""
        if "--expect-runtime" in sys.argv:
            index = sys.argv.index("--expect-runtime") + 1
            if index >= len(sys.argv) or sys.argv[index] not in {"cpu", "cuda"}:
                print("--expect-runtime requires cpu or cuda", file=sys.stderr)
                return 2
            expected = sys.argv[index]
        return verify_runtime(
            expected=expected,
            test_cuda_device="--test-cuda-device" in sys.argv,
        )

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    from sorter import paths
    from sorter.config import Config
    from sorter.crash_reporter import CrashReporter
    from sorter.db import Database
    from sorter.ui.app import MainWindow
    from sorter.version import APP_VERSION, PUBLIC_VERSION

    reporter: CrashReporter | None = CrashReporter(
        paths.crash_reports_dir(),
        app_info={
            "public_version": PUBLIC_VERSION,
            "app_version": APP_VERSION,
        },
    )
    try:
        reporter.start()
    except Exception as exc:
        # Crash reporting must never become a new reason the sorter cannot
        # start. Continue without it if the private log directory is unusable.
        print(f"Crash reporting unavailable: {exc}", file=sys.stderr)
        reporter = None
    db: Database | None = None
    clean_shutdown = False
    try:
        paths.ensure_directories()
        legacy_json = here / "data" / "config.json"

        db = Database()
        db.ensure_initialized(
            legacy_config_json=legacy_json if legacy_json.exists() else None
        )

        config = Config(db).load()
        if reporter is not None:
            reporter.register_secret(config.api.get("api_key"))
        MainWindow(
            config,
            db=db,
            crash_reporter=reporter,
            web_only="--web-interface" in sys.argv,
        ).run()
        db.close()
        db = None
        clean_shutdown = True
        return 0
    except BaseException:
        if reporter is not None:
            reporter.record_current_exception(source="application_main", notify=False)
        raise
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        if reporter is not None:
            reporter.close(clean=clean_shutdown)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and child.name == method_name:
                    return ast.get_source_segment(source, child) or ""
    raise AssertionError(f"{class_name}.{method_name} not found in {path}")


class FeedbackOwnershipTests(unittest.TestCase):
    def test_desktop_owner_does_not_defer_because_web_is_running(self) -> None:
        path = ROOT / "sorter" / "ui" / "tab_run.py"
        queued = method_source(path, "RunTab", "_on_feedback_queued")
        stopped = method_source(path, "RunTab", "_on_run_stopped")

        self.assertNotIn("web_server", queued)
        self.assertNotIn("web_server", stopped)
        self.assertIn("_trigger_feedback_drain", queued)
        self.assertIn('feedback_loop_upload_mode == "OnRunComplete"', stopped)

    def test_web_owner_defers_only_when_desktop_run_tab_exists(self) -> None:
        path = ROOT / "sorter" / "web_interface.py"
        queued = method_source(
            path, "WindowsWebOperations", "_feedback_queued"
        )
        stopped = method_source(
            path, "WindowsWebOperations", "_feedback_run_stopped"
        )

        for source in (queued, stopped):
            self.assertIn('getattr(self.app, "run_tab", None)', source)
            self.assertIn("return", source)
        self.assertIn("_feedback_drain", queued)
        self.assertIn("_feedback_drain", stopped)

    def test_desktop_and_web_start_paths_refresh_server_feedback_settings(self) -> None:
        desktop = (ROOT / "sorter" / "ui" / "app.py").read_text(
            encoding="utf-8"
        )
        web = (ROOT / "sorter" / "web_interface.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("_prepare_feedback_then", desktop)
        self.assertIn("controller.refresh_community_feedback", desktop)
        self.assertIn("controller.refresh_community_feedback", web)


if __name__ == "__main__":
    unittest.main()

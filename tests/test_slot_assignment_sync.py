import ast
from pathlib import Path
import unittest

from sorter.events import (
    EventBus,
    plan_assignment_refresh,
    post_assignment_changed,
)


ROOT = Path(__file__).resolve().parents[1]


def method_calls(path: Path, class_name: str, method_name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    item.name == method_name
                ):
                    return [
                        call.func.attr
                        for call in ast.walk(item)
                        if isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                    ]
    raise AssertionError(f"{class_name}.{method_name} not found in {path}")


class SlotAssignmentSyncTests(unittest.TestCase):
    def test_operator_manual_assignment_notifies_all_run_subscribers(self) -> None:
        bus = EventBus()
        operator_received = []
        maintenance_received = []
        bus.subscribe("run/assignment_changed", operator_received.append)
        bus.subscribe("run/assignment_changed", maintenance_received.append)

        post_assignment_changed(bus, "operator_manual")
        bus.drain()

        expected = [{"source": "operator_manual"}]
        self.assertEqual(expected, operator_received)
        self.assertEqual(expected, maintenance_received)

    def test_maintenance_manual_assignment_notifies_all_run_subscribers(self) -> None:
        bus = EventBus()
        operator_received = []
        maintenance_received = []
        bus.subscribe("run/assignment_changed", operator_received.append)
        bus.subscribe("run/assignment_changed", maintenance_received.append)

        post_assignment_changed(bus, "maintenance_manual")
        bus.drain()

        expected = [{"source": "maintenance_manual"}]
        self.assertEqual(expected, operator_received)
        self.assertEqual(expected, maintenance_received)

    def test_assignment_event_preserves_targeted_render_hint(self) -> None:
        bus = EventBus()
        received = []
        bus.subscribe("run/assignment_changed", received.append)

        post_assignment_changed(
            bus,
            "operator_manual",
            {
                "full_refresh": False,
                "kind": "headstamp",
                "name": "TULA",
                "slot": 5,
            },
        )
        bus.drain()

        self.assertEqual(
            [{
                "source": "operator_manual",
                "full_refresh": False,
                "kind": "headstamp",
                "name": "TULA",
                "slot": 5,
            }],
            received,
        )

    def test_flow_grid_batches_layout_until_finish(self) -> None:
        source = ROOT / "sorter" / "ui" / "tab_run.py"

        self.assertNotIn("_reflow", method_calls(source, "FlowGrid", "add"))
        self.assertIn("_reflow", method_calls(source, "FlowGrid", "finish"))

    def test_single_checkbox_persists_without_rebuilding_details(self) -> None:
        calls = method_calls(
            ROOT / "sorter" / "ui" / "tab_run.py",
            "SlotDetailsPanel",
            "_toggle_headstamp",
        )

        self.assertIn("set_headstamp_slot", calls)
        self.assertIn("on_assignment_change", calls)
        self.assertNotIn("show_slot", calls)

    def test_hidden_surface_defers_all_rendering(self) -> None:
        action, slots = plan_assignment_refresh(
            {"source": "operator_manual", "full_refresh": False, "slot": 5},
            visible=False,
            local_source="maintenance_",
        )

        self.assertEqual("defer", action)
        self.assertIsNone(slots)

    def test_originating_surface_skips_queued_duplicate_render(self) -> None:
        action, slots = plan_assignment_refresh(
            {"source": "maintenance_manual", "full_refresh": False, "slot": 5},
            visible=True,
            local_source="maintenance_",
        )

        self.assertEqual("skip", action)
        self.assertIsNone(slots)

    def test_external_single_change_targets_only_affected_slot(self) -> None:
        action, slots = plan_assignment_refresh(
            {"source": "maintenance_manual", "full_refresh": False, "slot": 5},
            visible=True,
            local_source="operator_manual",
        )

        self.assertEqual("targeted", action)
        self.assertEqual({5}, slots)

    def test_structural_change_requests_full_refresh(self) -> None:
        action, slots = plan_assignment_refresh(
            {"source": "clear_slots"},
            visible=True,
            local_source="operator_manual",
        )

        self.assertEqual("full", action)
        self.assertIsNone(slots)

    def test_visibility_transition_forces_authoritative_refresh(self) -> None:
        action, slots = plan_assignment_refresh(
            {"source": "operator_manual", "full_refresh": False, "slot": 5},
            visible=True,
            local_source="operator_manual",
            force=True,
        )

        self.assertEqual("full", action)
        self.assertIsNone(slots)


if __name__ == "__main__":
    unittest.main()

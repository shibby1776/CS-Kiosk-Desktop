import unittest

from sorter.events import EventBus, post_assignment_changed


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


if __name__ == "__main__":
    unittest.main()

import sys
import threading
import time
import types
import unittest


try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    serial_stub = types.ModuleType("serial")

    class SerialException(Exception):
        pass

    serial_stub.SerialException = SerialException
    serial_stub.Serial = object
    serial_stub.EIGHTBITS = 8
    serial_stub.PARITY_NONE = "N"
    serial_stub.STOPBITS_ONE = 1
    tools_stub = types.ModuleType("serial.tools")
    list_ports_stub = types.ModuleType("serial.tools.list_ports")
    list_ports_stub.comports = lambda: []
    tools_stub.list_ports = list_ports_stub
    sys.modules["serial"] = serial_stub
    sys.modules["serial.tools"] = tools_stub
    sys.modules["serial.tools.list_ports"] = list_ports_stub

from sorter.serial_broker import _matches_token
from sorter.serial_emulator import EmulatorBroker


class SerialSafetyTests(unittest.TestCase):
    def test_protocol_tokens_do_not_match_inside_words(self) -> None:
        self.assertTrue(_matches_token("ok", "ok"))
        self.assertTrue(_matches_token("done: slot 5", "done"))
        self.assertFalse(_matches_token("broken", "ok"))
        self.assertFalse(_matches_token("undone", "done"))
        self.assertFalse(_matches_token("awaiting", "waiting"))

    def test_pending_command_returns_immediately_when_link_dies(self) -> None:
        broker = EmulatorBroker(response_delay_s=5.0)
        broker.try_open()
        result = []
        started = time.monotonic()
        worker = threading.Thread(target=lambda: result.append(broker.feed_one()))
        worker.start()
        time.sleep(0.02)
        broker.simulate_disconnect()
        worker.join(timeout=0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual([False], result)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([], broker.on_disconnect)


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkerDispatchTests(unittest.TestCase):
    def test_worker_reply_is_one_shot_and_unsubscribed(self) -> None:
        source = (ROOT / "sorter" / "ui" / "app.py").read_text(
            encoding="utf-8"
        )
        worker = source.split("    def run_worker(", 1)[1].split(
            "    # ----- bus drain loop", 1
        )[0]

        self.assertIn("token = next(self._worker_tokens)", worker)
        self.assertIn("self.bus.unsubscribe(topic_done, _deliver_done)", worker)
        self.assertIn("self.bus.unsubscribe(topic_err, _deliver_err)", worker)
        self.assertNotIn("id(fn)", worker.replace("Never key replies on id(fn)", ""))
        self.assertIn("expected_disconnect", worker)


if __name__ == "__main__":
    unittest.main()

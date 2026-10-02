import socket
import tempfile
import threading
import unittest
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import numpy as np

from sorter import local_inference
from sorter.api_server import ApiServerRuntime, _Handler
from sorter.api_server_settings import ApiServerSettings, ApiServerSettingsRepo
from sorter.db import Database, SCHEMA_VERSION
from sorter.models import Model
from sorter.repository import ApiModelAliasRepo, CartridgeRepo, ModelRepo


class IntegratedApiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "casesorter.db")
        self.db.ensure_initialized()
        self.models = ModelRepo(self.db)
        self.aliases = ApiModelAliasRepo(self.db)
        self.cartridge_id = int(CartridgeRepo(self.db).list()[0].id)

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def _model(self, name: str) -> Model:
        checkpoint = self.root / f"{name}.pth"
        checkpoint.write_bytes(b"test checkpoint placeholder")
        return self.models.create(
            Model(
                name=name,
                cartridge_id=self.cartridge_id,
                model_mode="convnext_tiny",
                model_path=str(checkpoint),
            )
        )

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def test_aliases_are_case_insensitive_and_block_model_deletion(self) -> None:
        served = self._model("community-9mm")
        self._model("fallback")

        self.aliases.assign("9mm", int(served.id), preload=True)
        self.assertEqual(int(served.id), self.aliases.get("9MM").model_id)
        self.assertTrue(self.aliases.get("9mm").preload)

        with self.assertRaisesRegex(ValueError, "API server"):
            self.models.delete(int(served.id))

    def test_api_key_is_stored_only_as_a_hash(self) -> None:
        raw_key = "correct-horse-battery-staple"
        settings = ApiServerSettings()
        settings.set_api_key(raw_key)
        ApiServerSettingsRepo(self.db).save(settings)

        stored = ApiServerSettingsRepo(self.db).load()
        self.assertNotIn(raw_key, str(stored.to_dict()))
        self.assertTrue(stored.verify_api_key(raw_key))
        self.assertFalse(stored.verify_api_key("incorrect-key-value"))

    def test_api_key_may_be_short_or_disabled(self) -> None:
        settings = ApiServerSettings()
        settings.set_api_key("x")
        self.assertTrue(settings.verify_api_key("x"))
        self.assertFalse(settings.verify_api_key("y"))

        settings.set_api_key("")
        self.assertEqual("", settings.api_key_hash)
        self.assertTrue(settings.verify_api_key(""))
        self.assertTrue(settings.verify_api_key("unused"))

    def test_server_can_run_without_api_key(self) -> None:
        model = self._model("no-key-model")
        self.aliases.assign("9mm", int(model.id), preload=True)
        port = self._free_port()
        runtime = ApiServerRuntime(self.db)

        with patch("sorter.api_server.local_inference.warm_model"):
            runtime.start(ApiServerSettings(port=port))
        try:
            with urlopen(
                Request(f"http://127.0.0.1:{port}/v1/models"),
                timeout=3.0,
            ) as response:
                self.assertEqual(200, response.status)
        finally:
            runtime.stop()

    def test_server_accepts_only_loopback_rfc1918_and_ipv6_ula_clients(self) -> None:
        handler = object.__new__(_Handler)
        for address in (
            "127.0.0.1",
            "10.4.5.6",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.50.10",
            "::1",
            "fd12:3456::1",
        ):
            handler.client_address = (address, 12345)
            self.assertTrue(handler._request_allowed(), address)
        for address in (
            "8.8.8.8",
            "100.64.0.1",
            "172.32.0.1",
            "203.0.113.10",
            "2001:4860:4860::8888",
        ):
            handler.client_address = (address, 12345)
            self.assertFalse(handler._request_allowed(), address)

    def test_application_has_no_api_server_autostart_path(self) -> None:
        app_source = (
            Path(__file__).resolve().parents[1] / "sorter" / "ui" / "app.py"
        ).read_text(encoding="utf-8")
        settings_source = (
            Path(__file__).resolve().parents[1]
            / "sorter"
            / "api_server_settings.py"
        ).read_text(encoding="utf-8")

        self.assertIn("self.api_server_runtime = None", app_source)
        self.assertNotIn("_auto_start_api_server", app_source)
        self.assertNotIn("auto_start", settings_source)

    def test_schema_3_database_upgrades_without_replacing_existing_models(self) -> None:
        original_models = [model.name for model in self.models.list()]
        self.db.conn.execute("DROP TABLE api_model_aliases")
        self.db.conn.execute("PRAGMA user_version = 3")
        self.db.close()

        self.db = Database(self.root / "casesorter.db")
        self.db.ensure_initialized()
        self.models = ModelRepo(self.db)
        self.aliases = ApiModelAliasRepo(self.db)

        tables = {
            row[0]
            for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertIn("api_model_aliases", tables)
        self.assertEqual(
            SCHEMA_VERSION,
            self.db.conn.execute("PRAGMA user_version").fetchone()[0],
        )
        self.assertEqual(original_models, [model.name for model in self.models.list()])

    def test_runtime_is_dormant_until_start_and_releases_port_on_stop(self) -> None:
        model_9mm = self._model("9mm-v9")
        model_556 = self._model("556-v3")
        self.aliases.assign("9mm", int(model_9mm.id), preload=True)
        self.aliases.assign("5.56", int(model_556.id), preload=False)
        port = self._free_port()
        settings = ApiServerSettings(port=port)
        settings.set_api_key("demonstrator-api-key-12345")
        runtime = ApiServerRuntime(self.db)

        self.assertFalse(runtime.is_running)
        self.assertIsNone(runtime._server)
        self.assertIsNone(runtime._thread)

        with (
            patch("sorter.api_server.local_inference.warm_model") as warm,
            patch(
                "sorter.api_server.local_inference.classes_for_model",
                return_value=["TULA", "NORMA"],
            ),
            patch(
                "sorter.api_server.local_inference.classify",
                return_value=("TULA", 98.5),
            ),
            patch("sorter.api_server.local_inference.evict_model"),
            patch.object(
                runtime,
                "decode_request_image",
                return_value=np.zeros((16, 16, 3), dtype=np.uint8),
            ),
        ):
            runtime.start(settings)
            endpoint = f"http://127.0.0.1:{port}"
            self.assertTrue(runtime.is_running)
            warm.assert_called_once_with(
                model_9mm.model_path,
                image_size=model_9mm.training_config.image_size,
                priority="remote",
                remote_queue_limit=4,
            )

            with self.assertRaises(HTTPError) as unauthorized:
                urlopen(Request(endpoint + "/v1/models"), timeout=3.0)
            self.assertEqual(401, unauthorized.exception.code)

            headers = {"Authorization": "Bearer demonstrator-api-key-12345"}
            with urlopen(Request(endpoint + "/v1/models", headers=headers), timeout=3.0) as response:
                advertised = json.loads(response.read().decode("utf-8"))
            self.assertEqual(["5.56", "9mm"], [item["id"] for item in advertised["data"]])

            with urlopen(
                Request(endpoint + "/getheadstamps?model=5.56", headers=headers),
                timeout=3.0,
            ) as response:
                classes = json.loads(response.read().decode("utf-8"))
            self.assertEqual(["TULA", "NORMA"], classes)

            body = json.dumps({
                    "model": "9mm",
                    "messages": [{
                        "role": "user",
                        "content": [{
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64,ignored"},
                        }],
                    }],
                }).encode("utf-8")
            request = Request(
                endpoint + "/v1/chat/completions",
                data=body,
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual("TULA", payload["choices"][0]["message"]["content"])
            self.assertAlmostEqual(0.985, payload["confidence"])
            runtime.stop()

        self.assertFalse(runtime.is_running)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            self.assertNotEqual(0, sock.connect_ex(("127.0.0.1", port)))


class InferenceCoordinatorTests(unittest.TestCase):
    def test_local_work_moves_ahead_of_queued_remote_work(self) -> None:
        coordinator = local_inference._PriorityInferenceCoordinator()
        started = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def first_remote() -> str:
            started.set()
            release.wait(2.0)
            order.append("remote-1")
            return "remote-1"

        def record(name: str) -> str:
            order.append(name)
            return name

        first = coordinator.submit(
            first_remote, priority="remote", remote_queue_limit=1
        )
        self.assertTrue(started.wait(1.0))
        second = coordinator.submit(
            record, "remote-2", priority="remote", remote_queue_limit=1
        )
        overflow = coordinator.submit(
            record, "overflow", priority="remote", remote_queue_limit=1
        )
        local = coordinator.submit(
            record, "local", priority="local", remote_queue_limit=1
        )
        with self.assertRaises(local_inference.InferenceBusyError):
            overflow.result(timeout=1.0)

        release.set()
        self.assertEqual("remote-1", first.result(timeout=2.0))
        self.assertEqual("local", local.result(timeout=2.0))
        self.assertEqual("remote-2", second.result(timeout=2.0))
        self.assertEqual(["remote-1", "local", "remote-2"], order)
        coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()

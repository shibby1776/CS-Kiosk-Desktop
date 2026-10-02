import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np

# These tests exercise selection logic without requiring the large OpenCV
# package in the source-validation environment.
import cv2  # Required production dependency; never globally shadow it.
from sorter.camera import Camera


def capability(index: int, media_type: str, width: int = 1920,
               height: int = 1080, fps: float = 30.0) -> dict[str, object]:
    return {
        "index": index,
        "media_type_str": media_type,
        "width": width,
        "height": height,
        "min_framerate": fps,
        "max_framerate": fps,
    }


class WindowsCapabilitySelectionTests(unittest.TestCase):
    def candidates(self, formats: list[dict[str, object]]):
        return Camera._windows_capability_candidates(
            formats, width=1920, height=1080, fps=30.0
        )

    def test_prefers_mjpg_then_nv12_then_yuy2(self) -> None:
        selected = self.candidates([
            capability(1, "YUY2"),
            capability(2, "NV12"),
            capability(3, "MJPG"),
        ])
        self.assertEqual(["MJPG", "NV12", "YUY2"], [row[0] for row in selected])

    def test_uses_nv12_when_mjpg_is_not_advertised(self) -> None:
        selected = self.candidates([
            capability(4, "YUY2"),
            capability(7, "NV12"),
        ])
        self.assertEqual(("NV12", 7), (selected[0][0], selected[0][2]["index"]))

    def test_uses_yuy2_when_it_is_the_only_supported_exact_mode(self) -> None:
        selected = self.candidates([capability(5, "YUY2")])
        self.assertEqual("YUY2", selected[0][0])

    def test_rejects_wrong_resolution_rate_and_unrecognized_formats(self) -> None:
        selected = self.candidates([
            capability(1, "MJPG", width=1280, height=720),
            capability(2, "NV12", fps=15.0),
            capability(3, "RGB24"),
        ])
        self.assertEqual([], selected)

    def test_failed_mjpg_graph_advances_to_nv12(self) -> None:
        formats = [capability(1, "MJPG"), capability(2, "NV12")]

        class FakeInput:
            def __init__(self, graph) -> None:
                self.graph = graph

            def get_formats(self):
                return formats

            def set_format(self, index: int) -> None:
                self.graph.selected_index = index

        class FakeGraph:
            def __init__(self) -> None:
                self.selected_index = None
                self.callback = None
                self.input = FakeInput(self)

            def get_input_devices(self):
                return ["Test Camera"]

            def add_video_input_device(self, _index: int) -> None:
                return None

            def get_input_device(self):
                return self.input

            def add_sample_grabber(self, callback) -> None:
                self.callback = callback

            def add_null_render(self) -> None:
                return None

            def prepare_preview_graph(self) -> None:
                if self.selected_index == 1:
                    raise RuntimeError("simulated MJPG graph failure")

            def run(self) -> None:
                return None

            def grab_frame(self) -> None:
                assert self.callback is not None
                self.callback(np.ones((1080, 1920, 3), dtype=np.uint8))

            def stop(self) -> None:
                return None

            def remove_filters(self) -> None:
                return None

        comtypes = ModuleType("comtypes")
        comtypes.CoInitialize = lambda: None
        comtypes.CoUninitialize = lambda: None
        pygrabber = ModuleType("pygrabber")
        dshow_graph = ModuleType("pygrabber.dshow_graph")
        dshow_graph.FilterGraph = FakeGraph

        camera = Camera(device_index=0, width=1920, height=1080)
        with patch.dict(
            sys.modules,
            {
                "comtypes": comtypes,
                "pygrabber": pygrabber,
                "pygrabber.dshow_graph": dshow_graph,
            },
        ):
            self.assertTrue(camera._open_windows_native_dshow())
            info = camera.diagnostic_info()
            camera.stop()

        self.assertEqual("NV12", info["source_format"])
        self.assertTrue(info["compatibility_mode"])
        self.assertFalse(info["mjpg_verified"])
        self.assertEqual(
            ["capability_graph_failed", None],
            [attempt.get("reason") for attempt in info["mode_attempts"]],
        )


if __name__ == "__main__":
    unittest.main()

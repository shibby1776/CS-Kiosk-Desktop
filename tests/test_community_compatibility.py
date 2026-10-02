from __future__ import annotations

import sys
import types
import unittest


# requests is a required runtime dependency. Do not replace it globally:
# later HTTP integration tests must exercise a real session.
import requests
if "msal" not in sys.modules:
    msal_stub = types.ModuleType("msal")
    msal_stub.PublicClientApplication = object
    msal_stub.SerializableTokenCache = object
    sys.modules["msal"] = msal_stub
# OpenCV is a required runtime dependency; loading it also avoids stale
# NumPy module snapshots in later tests that temporarily patch sys.modules.
import cv2

from sorter.community_api import ModelInfo


class CommunityCompatibilityTests(unittest.TestCase):
    def test_numeric_export_modes_are_displayed_by_name(self) -> None:
        expected = {0: "ModelOnly", 1: "ModelAndImages", 2: "ImagesOnly"}
        for raw, name in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(name, ModelInfo.from_json({"ModelExportMode": raw}).export_mode)

    def test_named_and_missing_export_modes_remain_compatible(self) -> None:
        self.assertEqual(
            "ImagesOnly",
            ModelInfo.from_json({"ModelExportMode": "ImagesOnly"}).export_mode,
        )
        self.assertEqual("ModelAndImages", ModelInfo.from_json({}).export_mode)


if __name__ == "__main__":
    unittest.main()

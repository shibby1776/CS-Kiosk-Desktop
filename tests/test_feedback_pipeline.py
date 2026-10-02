from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from sorter.community_api import API_BASE, CommunityApi, ModelSettings, ModeratorNote
from sorter.feedback import FeedbackService, MAX_WISH_LIST_CAPTURES_PER_LABEL
from sorter.models import Model


def feedback_model(*, floor: int = 95) -> Model:
    return Model(
        id=7,
        name="Community model",
        community_model_uid="community-uid",
        feedback_loop_enabled=True,
        feedback_loop_confidence_floor=floor,
        feedback_loop_upload_mode="Instant",
    )


class _Response:
    def __init__(self, data, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data


class _Auth:
    def acquire_token_silent(self, _scopes=None):
        return SimpleNamespace(access_token="TOKEN")


class _Session:
    def __init__(self, response):
        self.response = response
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.response


class FeedbackPolicyTests(unittest.TestCase):
    def test_server_wish_list_captures_high_confidence_predictions(self) -> None:
        service = FeedbackService(db=None)
        model = feedback_model()
        service.set_wish_list(model.id, ["FC"])

        self.assertTrue(
            service.should_capture(model, 99.0, "fc", wish_list=True)
        )
        self.assertFalse(
            service.should_capture(model, 99.0, "WIN", wish_list=True)
        )
        self.assertFalse(
            service.should_capture(model, 99.0, "FC", wish_list=False)
        )

    def test_wish_list_is_bounded_per_label_but_low_confidence_is_not(self) -> None:
        service = FeedbackService(db=None)
        model = feedback_model()
        service.set_wish_list(model.id, ["FC"])

        for _ in range(MAX_WISH_LIST_CAPTURES_PER_LABEL):
            self.assertTrue(
                service.should_capture(model, 99.0, "FC", wish_list=True)
            )
        self.assertFalse(
            service.should_capture(model, 99.0, "FC", wish_list=True)
        )
        self.assertTrue(
            service.should_capture(model, 10.0, "FC", wish_list=True)
        )

    def test_server_floor_and_block_override_transient_capture_policy(self) -> None:
        service = FeedbackService(db=None)
        model = feedback_model(floor=95)
        service.apply_server_settings(model.id, confidence_floor=70)
        self.assertFalse(service.should_capture(model, 80.0))
        self.assertTrue(service.should_capture(model, 60.0))

        service.apply_server_settings(model.id, blocked=True)
        self.assertFalse(service.should_capture(model, 10.0))

    def test_refresh_applies_attached_source_contract(self) -> None:
        service = FeedbackService(db=None)
        model = feedback_model()
        settings = ModelSettings(
            wish_list=["FC", "R-P"],
            confidence_floor=80,
            feedback_enabled=True,
            blocked=False,
        )
        api = mock.Mock()
        api.fetch_model_settings.return_value = settings
        with mock.patch("sorter.community_api.CommunityApi", return_value=api):
            returned = service.refresh_server_settings(model, auth=object())

        self.assertIs(settings, returned)
        self.assertEqual(["fc", "r-p"], service.wish_list())
        self.assertEqual(80, service.effective_floor(model))


class FeedbackApiContractTests(unittest.TestCase):
    def test_fetch_model_settings_matches_attached_source_contract(self) -> None:
        response = _Response(
            {
                "wishlist": ["FC", "R-P"],
                "confidencefloor": 80,
                "feedbackenabled": True,
                "blocked": False,
                "version": 4,
                "notes": [
                    {
                        "id": 7,
                        "note": "Center the case.",
                        "created": "2026-08-01T09:30:00",
                    }
                ],
            }
        )
        session = _Session(response)
        api = CommunityApi(auth=_Auth(), session=session)

        settings = api.fetch_model_settings("community uid")

        self.assertEqual(
            ModelSettings(
                wish_list=["FC", "R-P"],
                confidence_floor=80,
                feedback_enabled=True,
                blocked=False,
                version=4,
                notes=[
                    ModeratorNote(
                        id=7,
                        note="Center the case.",
                        created="2026-08-01T09:30:00",
                    )
                ],
            ),
            settings,
        )
        self.assertEqual(
            f"{API_BASE}/Models/FetchModelSettings?communityModelId=community+uid",
            session.get_calls[0][0],
        )


if __name__ == "__main__":
    unittest.main()

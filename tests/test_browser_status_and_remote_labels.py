import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sorter.config import Config
from sorter.db import Database
from sorter.events import EventBus
from sorter.web_interface import WebRuntimeState, WindowsWebOperations, _app_page


class BrowserStatusTests(unittest.TestCase):
    def test_camera_health_uses_existing_diagnostic_contract_without_capture(self):
        with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db')) as db:
            db.ensure_initialized()
            camera = SimpleNamespace(diagnostic_info=Mock(return_value={'opened': True, 'last_error': None}))
            app = SimpleNamespace(config=Config(db).load(), bus=EventBus(), camera=camera)
            state = WebRuntimeState(app, lambda: False)
            try:
                self.assertTrue(state.snapshot(control_owned=False, technician=False)['camera_ready'])
                camera.diagnostic_info.return_value={'opened': False}
                self.assertFalse(state.snapshot(control_owned=False, technician=False)['camera_ready'])
                camera.diagnostic_info.return_value={'opened': True, 'last_error': 'JPEG failed'}
                self.assertFalse(state.snapshot(control_owned=False, technician=False)['camera_ready'])
            finally:
                state.shutdown()

    def test_successful_cycle_clears_old_fault_but_stop_does_not(self):
        state=WebRuntimeState(SimpleNamespace(bus=EventBus()),lambda: False)
        try:
            state._error_event('Inference unavailable')
            state._status_event('Stopped')
            self.assertEqual(state._last_error,'Inference unavailable')
            state._result_event({'ok':False})
            self.assertEqual(state._last_error,'Inference unavailable')
            state._result_event({'ok':True,'slot':1})
            self.assertEqual(state._last_error,'')
            state._error_event('Camera failed')
            state._classified_event({'label':'FC'})
            self.assertEqual(state._last_error,'')
        finally:
            state.shutdown()


class ReadOnlyRemoteLabelsTests(unittest.TestCase):
    def test_remote_mutations_rejected_without_touching_config(self):
        ops=WindowsWebOperations.__new__(WindowsWebOperations)
        ops.app=SimpleNamespace(config=Mock())
        for action in ('add','remove','clear'):
            with self.assertRaisesRegex(ValueError,'managed by the AI server'):
                ops.remote_headstamp_action(action,'FC')
        self.assertEqual(ops.app.config.mock_calls,[])

    def test_model_edit_endpoints_cannot_bypass_remote_protection(self):
        ops=WindowsWebOperations.__new__(WindowsWebOperations)
        ops.ensure_stopped=Mock()
        ops.settings=SimpleNamespace(get_active_model_id=lambda:None)
        ops.app=SimpleNamespace(config=Mock())
        for method in (ops.add_headstamp,ops.remove_headstamp):
            with self.assertRaisesRegex(ValueError,'managed by the AI server'):
                method('FC')
        self.assertEqual(ops.app.config.mock_calls,[])

    def test_browser_contains_explicit_bin_picker_search_and_no_remote_edit_buttons(self):
        page=_app_page('Sorter','test-csrf')
        self.assertIn('<select id=opBinPicker>',page)
        self.assertIn('<select id=runBinPicker>',page)
        self.assertIn('id=opBinOptions class=hide',page)
        self.assertIn('id=runBinOptions class=hide',page)
        self.assertIn('class=classification-rows',page)
        for field in ('opFilter','runFilter','remoteHeadstampFilter'):
            self.assertIn('id='+field,page)
        for removed in ('data-rhs','id=remoteHeadstampAdd','id=remoteHeadstampClear','id=remoteHeadstampName'):
            self.assertNotIn(removed,page)


if __name__=='__main__':
    unittest.main()

import csv
import io
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import requests

from sorter.catch_all_report import CatchAllReport
from sorter.config import Config
from sorter.db import Database
from sorter.events import EventBus
from sorter.run_controller import RunController
from sorter.web_interface import WebInterfaceServer
from sorter.web_settings import WebSettings


class CatchAllReportTests(unittest.TestCase):
    def setUp(self):
        self.bus=EventBus()
        self.report=CatchAllReport(self.bus)

    def tearDown(self):
        self.report.shutdown()

    def drop(self, label='FC', reason='unassigned', **extra):
        self.bus.post('run/dropped', {'acknowledged':True,'slot':0,'label':label,'reason':reason,'model':'9mm','sorter_connection':'http://192.168.4.92',**extra})
        self.bus.drain()

    def test_counts_only_acknowledged_catch_all_not_predictions_or_other_bins(self):
        self.bus.post('run/classified', {'label':'SAR','slot':0})
        self.bus.post('run/result', {'ok':True,'label':'SAR','slot':0})
        self.bus.drain()
        self.assertEqual(self.report.snapshot()['count'],0)
        self.drop();self.drop();self.drop('SAR','below_confidence_floor')
        self.drop(slot=2);self.drop(acknowledged=False)
        state=self.report.snapshot()
        self.assertEqual(state['count'],3)
        self.assertEqual(state['rows'][0]['classification'],'FC')
        self.assertEqual(state['rows'][0]['count'],2)

    def test_csv_labels_predictions_quotes_fields_and_blocks_formulas(self):
        self.drop('FC, commercial')
        self.drop('=HYPERLINK("bad")')
        rows=list(csv.DictReader(io.StringIO(self.report.csv_bytes().decode('utf-8-sig'))))
        self.assertEqual(sum(int(x['count']) for x in rows),2)
        labels=[x['predicted_classification'] for x in rows]
        self.assertIn('FC, commercial',labels)
        self.assertIn('\'=HYPERLINK("bad")',labels)
        self.assertTrue(all(x['period_start_utc'] and x['exported_at_utc'] for x in rows))

    def test_reset_all_or_catch_all_clears_but_other_slot_reset_does_not(self):
        self.drop();self.bus.post('run/slot_counter_reset',2);self.bus.drain()
        self.assertEqual(self.report.snapshot()['count'],1)
        self.bus.post('run/slot_counter_reset',0);self.bus.drain()
        self.assertEqual(self.report.snapshot()['count'],0)
        self.drop();self.bus.post('run/counters_reset',None);self.bus.drain()
        self.assertEqual(self.report.snapshot()['rows'],[])

    def test_separate_model_and_connection_counts_are_not_conflated(self):
        self.drop();self.drop(model='556');self.drop(sorter_connection='COM7')
        self.assertEqual(len(self.report.snapshot()['rows']),3)

    def test_empty_csv_still_has_headers_and_shutdown_releases_subscriptions(self):
        rows=list(csv.reader(io.StringIO(self.report.csv_bytes().decode('utf-8-sig'))))
        self.assertEqual(len(rows),1)
        self.assertIn('predicted_classification',rows[0])
        self.report.shutdown()
        self.assertTrue(all(not handlers for handlers in self.bus._subs.values()))


class AcknowledgedDropIntegrationTests(unittest.TestCase):
    def test_continuous_drop_counts_only_after_successful_motion(self):
        with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db')) as db:
            db.ensure_initialized();config=Config(db).load();config.settings.clear_active_model()
            bus=EventBus();report=CatchAllReport(bus)
            controller=RunController(config=config,broker=Mock(),camera=Mock(),bus=bus,db=db)
            image=np.zeros((8,8,3),dtype=np.uint8)
            for name in ('_mark_motion_complete','_ensure_remote_headstamps','_maybe_store_run_image','_maybe_capture_feedback','_post_history','_commit_package_count'):
                setattr(controller,name,Mock())
            controller._capture_post_motion_frame=Mock(return_value=(image,{}))
            controller._resolve_destination=Mock(return_value=(0,True,False))
            controller._parent_label=Mock(return_value=None)
            with patch('sorter.run_controller.classifier.classify_active',return_value=('FC',99)),patch('sorter.run_controller.image_proc.crop_headstamp',return_value=image),patch('sorter.run_controller.image_proc.apply_primer_mask',return_value=image):
                controller.broker.sort_and_move.return_value=False
                self.assertFalse(controller.run_once()['ok']);bus.drain()
                self.assertEqual(report.snapshot()['count'],0)
                controller.broker.sort_and_move.return_value=True
                self.assertTrue(controller.run_once()['ok']);bus.drain()
                self.assertEqual(report.snapshot()['count'],1)
                self.assertEqual(report.snapshot()['rows'][0]['classification'],'FC')
                self.assertEqual(report.snapshot()['rows'][0]['reason'],'unassigned')
            report.shutdown()

    def test_manual_classification_waits_for_next_ack_and_failed_movement_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db')) as db:
            db.ensure_initialized();config=Config(db).load();config.settings.clear_active_model()
            bus=EventBus();report=CatchAllReport(bus)
            controller=RunController(config=config,broker=Mock(),camera=Mock(),bus=bus,db=db)
            image=np.zeros((8,8,3),dtype=np.uint8)
            for name in ('_check_inference_ready','_mark_motion_complete','_ensure_remote_headstamps','_maybe_store_run_image','_maybe_capture_feedback','_post_history'):
                setattr(controller,name,Mock())
            controller._capture_post_motion_frame=Mock(return_value=(image,{}))
            controller._resolve_destination=Mock(return_value=(0,False,False))
            controller._parent_label=Mock(return_value=None)
            controller.broker.force_sort_and_move.return_value=True
            with patch('sorter.run_controller.classifier.classify_active',return_value=('SAR',20)), patch('sorter.run_controller.image_proc.crop_headstamp',return_value=image),patch('sorter.run_controller.image_proc.apply_primer_mask',return_value=image):
                self.assertTrue(controller.cycle_once()['ok']);bus.drain()
                self.assertEqual(report.snapshot()['count'],0,'First classification is still queued')
                self.assertTrue(controller.cycle_once()['ok']);bus.drain()
                self.assertEqual(report.snapshot()['count'],1)
                controller.broker.force_sort_and_move.return_value=False
                self.assertFalse(controller.cycle_once()['ok']);bus.drain()
                self.assertEqual(report.snapshot()['count'],1,'Unacknowledged motion must not be counted')
                controller.broker.force_sort_and_move.return_value=True
                controller._capture_post_motion_frame.return_value=(None,{})
                self.assertFalse(controller.cycle_once()['ok']);bus.drain()
                self.assertEqual(report.snapshot()['count'],2,'Previous case dropped even though next image failed')
            self.assertEqual(report.snapshot()['rows'][0]['reason'],'below_confidence_floor')
            report.shutdown()

    def test_csv_download_is_attachment_and_requires_browser_session(self):
        with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db')) as db:
            db.ensure_initialized();bus=EventBus();report=CatchAllReport(bus)
            app=SimpleNamespace(db=db,config=Config(db).load(),bus=bus,catch_all_report=report)
            server=WebInterfaceServer(app,WebSettings(enabled=True,sorter_name='test-sorter',port=0))
            with patch('sorter.mdns_advertiser.MdnsAdvertiser.start',return_value=False):
                server.start()
            try:
                base='http://127.0.0.1:'+str(server._server.server_address[1])
                response=requests.get(base+'/api/catch-all-export',timeout=3)
                self.assertEqual(response.status_code,401)
                client=requests.Session();self.assertEqual(client.get(base+'/',timeout=3).status_code,200)
                bus.post('run/dropped',{'acknowledged':True,'slot':0,'label':'FC'});bus.drain()
                response=client.get(base+'/api/catch-all-export',timeout=3)
                self.assertEqual(response.status_code,200)
                self.assertIn('attachment;',response.headers['Content-Disposition'])
                self.assertIn('text/csv',response.headers['Content-Type'])
                rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
                self.assertEqual(rows[0]['predicted_classification'],'FC')
                self.assertEqual(rows[0]['count'],'1')
            finally:
                server.stop();report.shutdown()

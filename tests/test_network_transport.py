import json,tempfile,threading,time,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock,patch
import cv2,numpy as np
from sorter.transports.network import NetworkBroker,NetworkCamera
from sorter.transports.esp_http import TrialCancelled,TrialDisconnected,TrialError
from sorter import image_proc

class Client:
 def __init__(self,url):
  self.url=url;self.calls=[];self.last_command_timing={};self.http=Mock();self.failure=None;self.frames=0
  self.on_state=None;self.wait_for_stop=False;self.motion_started=threading.Event();self.stopped=threading.Event()
  self.evidence_lock=threading.RLock();self.last=None;self.recovery_calls=0
 def connect(self):return self
 def network_snapshot(self):return {'ps_mode':'none','tcp_profile':'send32k','lease_active':False}
 def command(self,kind,arg=''):
  self.calls.append((kind,arg))
  if self.failure:raise self.failure
  if self.wait_for_stop and kind in ('sort','force'):
   self.motion_started.set()
   if self.on_state:self.on_state({'machine_state':'waiting_for_brass','waiting_for_brass_count':1,'last_serial_line':'waiting for brass'})
   self.stopped.wait(2)
   raise TrialCancelled('Sorter command cancelled by Stop')
  if kind=='read':return {'response':'7.2.test' if arg=='version' else json.dumps({'FeedMotorSpeed':90,'SortMotorSpeed':90,'SortSteps':20}) if arg=='getconfig' else 'ok'}
  return {'response':'done' if kind in ('sort','force') else 'sent' if kind=='stop' else 'ok'}
 def capture(self):
  self.frames+=1
  raw=cv2.imencode('.jpg',np.full((720,1280,3),self.frames%255,dtype=np.uint8))[1].tobytes()
  return raw,{'sequence':self.frames,'camera':{'width':1280,'height':720}}
 def touch(self):pass
 def release(self):pass
 def emergency_stop(self):
  self.calls.append(('emergency_stop',''));self.stopped.set()
  return {'phase':'cancelled','response':'stop_sent','machine_state':'stop_dispatched'}
 def recover_usb(self,timeout=30):
  self.recovery_calls+=1;self.failure=None
  return {'generation':1,'attempts':1,'disconnects':1}

class TransportTests(unittest.TestCase):
 def broker(self):
  b=NetworkBroker('http://192.168.4.92',client_factory=Client);self.assertTrue(b.try_open());self.addCleanup(b.stop);return b
 def test_commands_use_original_wire_meanings(self):
  b=self.broker();b.client.calls.clear();self.assertTrue(b.force_sort_and_move(3));self.assertTrue(b.sort_and_move(6));b.move_sorter_to_slot(5)
  self.assertEqual(b.client.calls,[('force','3'),('sort','6'),('move','5')])
 def test_sync_done_is_not_lost_by_late_subscription(self):
  b=self.broker();done=[];b.on_done.append(done.append);self.assertTrue(b.feed_one());self.assertEqual(done,['done'])
 def test_failed_motion_never_retried_and_disconnects(self):
  b=self.broker();b.client.calls.clear();b.client.failure=TrialError('unknown result');lost=[];b.on_disconnect.append(lost.append)
  self.assertFalse(b.sort_and_move(3));self.assertEqual(b.client.calls,[('sort','3')]);self.assertFalse(b.is_connected);self.assertEqual(lost,['unknown result'])
 def test_topology_fault_recovers_without_replaying_motion(self):
  b=self.broker();b.client.calls.clear();b.client.failure=TrialError('USB topology lost')
  b.client.last={'usb_recovery':{'state':'grace'}};recovering=[];recovered=[];lost=[]
  b.on_recovering.append(recovering.append);b.on_recovered.append(recovered.append);b.on_disconnect.append(lost.append)
  self.assertFalse(b.sort_and_move(3));self.assertEqual(b.client.calls,[('sort','3')])
  self.assertTrue(b.is_connected);self.assertEqual(b.client.recovery_calls,1);self.assertEqual(lost,[])
  self.assertEqual(len(recovering),1);self.assertEqual(recovered[0]['generation'],1)
  self.assertIn('not retried',b.last_operation_error)
 def test_waiting_motion_reports_sensor_state_and_stop_preempts_without_disconnect(self):
  b=self.broker();b.client.calls.clear();b.client.wait_for_stop=True;waiting=[];lost=[]
  b.on_waiting.append(waiting.append);b.on_disconnect.append(lost.append);result=[]
  worker=threading.Thread(target=lambda:result.append(b.sort_and_move(2)));worker.start()
  self.assertTrue(b.client.motion_started.wait(1));b.stop_run();b._stop_dispatch.join(2);worker.join(2)
  self.assertFalse(worker.is_alive());self.assertEqual(result,[False]);self.assertEqual(waiting,['waiting for brass'])
  self.assertEqual(b.client.calls,[('sort','2'),('emergency_stop','')]);self.assertTrue(b.is_connected);self.assertEqual(lost,[])
  self.assertIn('command_cancelled',[event['event'] for event in b.events]);self.assertIn('emergency_stop',[event['event'] for event in b.events])
 def test_reject_injected_or_out_of_range_commands_before_send(self):
  b=self.broker();before=list(b.client.calls)
  for wire in ['xf:8','sortto:-1','1\n2','reboot','feedspeed:90\n3']:
   with self.assertRaises(ValueError):b.send_command(wire)
  self.assertEqual(b.client.calls,before)
 def test_new_capture_cannot_reuse_preview(self):
  b=self.broker();c=NetworkCamera(b,settle_ms=0);seq,old=c.latest_frame_with_sequence();frame,meta=c.capture_frame_after_sequence(seq)
  self.assertGreater(meta['selected_sequence'],seq);self.assertTrue(meta['fresh_frame']);self.assertFalse(np.array_equal(old,frame));self.assertEqual(frame.shape,(720,1280,3))
 def test_preview_does_not_issue_capture_during_run(self):
  b=self.broker();c=NetworkCamera(b);b.run_active=True;before=b.client.frames;c.start_preview();time.sleep(.55);c.stop();self.assertEqual(b.client.frames,before)
 def test_disconnect_cancels_preview_and_future_capture_is_expected(self):
  b=self.broker();c=NetworkCamera(b);c.start_preview();b._lost(TrialError('network lost'))
  self.assertTrue(c._preview_stop.is_set())
  with self.assertRaises(TrialDisconnected) as raised:c.capture_frame()
  self.assertTrue(raised.exception.expected_disconnect)
 def test_report_contains_timing_without_separate_diagnostic_runner(self):
  b=self.broker();b.force_sort_and_move(1);report=b.report();self.assertEqual(report['events'][-1]['event'],'command');self.assertNotIn('owner',str(report));self.assertNotIn('api_key',str(report))
 def test_resolution_request_cannot_silently_change_bridge_mode(self):
  from sorter.transports.network import validate_camera_mode
  b=self.broker();validate_camera_mode(b,1280,720)
  with self.assertRaisesRegex(ValueError,'detected mode'):validate_camera_mode(b,1920,1080)
 def test_original_rim_profile_scales_and_rejects_offcentre_circle(self):
  p=image_proc.HoughParams.from_dict({'dp':1.1,'min_dist':225,'param1':250,'param2':20,'min_radius':240,'max_radius':300,'_reference_width':1920,'_expected_x':860,'_expected_y':506})
  img=np.zeros((720,1280,3),dtype=np.uint8)
  with patch.object(cv2,'HoughCircles',return_value=np.array([[[540,350,181],[100,100,199]]],dtype=np.float32)) as h:
   self.assertEqual(image_proc.hough_detect(img,p),(540.,350.,181.));self.assertEqual(h.call_args.kwargs['minRadius'],160)
  with patch.object(cv2,'HoughCircles',return_value=np.array([[[100,100,199]]],dtype=np.float32)):
   with self.assertRaisesRegex(ValueError,'saved centre'):image_proc.hough_crop(img,p)

class OriginalLayoutTests(unittest.TestCase):
 def test_no_alternative_operator_or_network_ui_is_packaged(self):
  root=Path(__file__).resolve().parents[1]
  self.assertFalse((root/'sorter/ui/network_sorter.py').exists());self.assertFalse((root/'sorter/network_ui.js').exists());self.assertFalse((root/'sorter/esp_runtime').exists())
  web=(root/'sorter/web_interface.py').read_text(encoding='utf-8');self.assertNotIn('NETWORK SORTERS',web);self.assertNotIn('sorterSelect',web)

class MigrationTests(unittest.TestCase):
 def test_profile_persists_bins_crop_and_led_in_normal_database(self):
  from sorter.db import Database
  from sorter.config import Config
  from sorter.transports.migration import import_profile
  cfg={'esp_url':'http://192.168.4.92','remote':{'endpoint':'http://127.0.0.1:8000','model':'9mm'},'crop':{'mode':'hough','x':860,'y':506,'primer_mode':'hide','primer_radius':135,'hough':{'dp':1.1,'min_dist':225,'param1':250,'param2':20,'min_radius':240,'max_radius':300}},'slots':{'FC':1,'SAR':3,'NORMA':6},'labels':['FC','SAR','NORMA'],'saved_machine':{'CameraLEDLevel':150,'FeedMotorSpeed':1}}
  # Contexts exit in reverse order: close SQLite before deleting the directory.
  # Windows cannot unlink the database while its connection remains open.
  with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'settings.db')) as db:
   db.ensure_initialized()
   config=Config(db).load();motor=config.serial['init_settings']['feedspeed']
   with patch('sorter.transports.migration.paths.app_data_dir',return_value=Path(tmp)):
    result=import_profile(config,{'sorter':{'config':cfg}})
   restored=Config(db).load()
   self.assertEqual({x['name']:x['slot'] for x in restored.remote_headstamps()},cfg['slots'])
   self.assertEqual(restored.image_proc['hough']['_expected_x'],860)
   self.assertEqual(restored.serial['init_settings']['cameraledlevel'],150)
   self.assertEqual(restored.serial['init_settings']['feedspeed'],motor)
   self.assertFalse(restored.serial['init_on_startup']);self.assertTrue((Path(tmp)/result['backup']).exists())

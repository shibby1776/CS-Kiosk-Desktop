import copy,tempfile,threading,unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock,patch
from sorter.db import Database
from sorter.config import Config
from sorter.repository import SettingsRepo
from sorter.connections import connection_settings,wifi_address
from sorter.machine_settings import settings_for_board,normalized_board_config
from sorter.sorter_profiles import SorterProfiles

class DatabaseConcurrencyTests(unittest.TestCase):
 def test_different_row_shapes_and_settings_reads_across_threads(self):
  with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db.sqlite')) as db:
   db.ensure_initialized();repo=SettingsRepo(db);repo.set('confidence',95)
   def work(worker):
    for i in range(100):
     self.assertEqual(repo.get('confidence'),95)
     row=db.conn.execute('SELECT ? AS a, ? AS b',(worker,i)).fetchone()
     self.assertEqual((row['a'],row['b']),(worker,i))
     self.assertEqual(db.conn.execute('SELECT ?',(worker,)).fetchall()[0][0],worker)
     repo.set('worker:'+str(worker),{'i':i})
   with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(work,range(8)))
 def test_standalone_queries_cannot_enter_another_threads_transaction(self):
  with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db.sqlite')) as db:
   db.ensure_initialized();repo=SettingsRepo(db);repo.set('value','before')
   started=threading.Event();finished=threading.Event();seen=[]
   def reader():
    started.set();seen.append(repo.get('value'));finished.set()
   with db.transaction():
    repo.set('value','during');thread=threading.Thread(target=reader);thread.start()
    self.assertTrue(started.wait(1));self.assertFalse(finished.wait(.05))
    repo.set('value','after')
   thread.join(2);self.assertFalse(thread.is_alive());self.assertEqual(seen,['after'])
 def test_cursor_iteration_and_fetchmany_preserve_row_shape(self):
  with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db.sqlite')) as db:
   db.ensure_initialized();cursor=db.conn.cursor().execute('SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3')
   self.assertEqual(cursor.fetchone()['n'],1);self.assertEqual(cursor.fetchmany(1)[0]['n'],2);self.assertEqual(next(cursor)['n'],3);self.assertIsNone(cursor.fetchone())

class ConnectionTests(unittest.TestCase):
 def test_usb_default_and_wifi_ip_are_saved_separately(self):
  usb=connection_settings({},wifi_enabled=False,usb_port='COM7');self.assertEqual(usb['port'],'COM7')
  remote=connection_settings(usb,wifi_enabled=True,address='192.168.4.92',usb_port='COM7');self.assertEqual(remote['port'],'http://192.168.4.92')
  restored=connection_settings(remote,wifi_enabled=False,usb_port='COM7');self.assertEqual(restored['port'],'COM7');self.assertEqual(restored['wifi_address'],'192.168.4.92')
 def test_reject_credentials_paths_or_invalid_ip(self):
  for value in ('http://user:pass@192.168.4.92','http://192.168.4.92/test','not-an-ip','192.168.4.999'):
   with self.assertRaises(ValueError):wifi_address(value)
 def test_disabled_airdrop_omits_timing_on_all_transports(self):
  values={'feedspeed':90,'airdropenabled':0,'airdroppredelay':30,'airdropdsignalduration':100,'airdroppostdelay':100}
  self.assertEqual(settings_for_board(values),{'feedspeed':90,'airdropenabled':0})
  values['airdropenabled']=1;self.assertEqual(settings_for_board(values),values)
 def test_board_readback_updates_exact_form_keys(self):
  self.assertEqual(normalized_board_config({'AirDropEnabled':0,'FeedMotorSpeed':90,'CameraLEDLevel':150}),{'airdropenabled':0,'feedspeed':90,'cameraledlevel':150})

class ProfilesTests(unittest.TestCase):
 def test_machine_profiles_keep_crop_bins_and_runtime_settings_separate(self):
  with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db.sqlite')) as db:
   db.ensure_initialized();c=Config(db).load();c.settings.clear_active_model();profiles=SorterProfiles(c)
   c.serial.update(port='http://192.168.4.92',wifi_enabled=True,wifi_address='192.168.4.92',usb_port='COM7')
   c.set_headstamps([{'name':'FC','slot':1}]);c.image_proc['primer_radius']=135;c.set_run_confidence_floor(80);profiles.save('Node')
   c.serial.update(port='COM5',wifi_enabled=False,usb_port='COM5');c.set_remote_headstamp_slots({'FC':6});c.image_proc['primer_radius']=90;c.set_run_confidence_floor(95);profiles.save('USB')
   profiles.select('Node');self.assertEqual(c.serial['port'],'http://192.168.4.92');self.assertEqual(c.slot_for_headstamp('FC'),1);self.assertEqual(c.image_proc['primer_radius'],135);self.assertEqual(c.run_confidence_floor,80)
   c.set_remote_headstamp_slots({'FC':3});profiles.select('USB');self.assertEqual(c.slot_for_headstamp('FC'),6);self.assertEqual(c.image_proc['primer_radius'],90)
   profiles.select('Node');self.assertEqual(c.slot_for_headstamp('FC'),3);self.assertEqual(profiles.active(),'Node')
 def test_local_model_bin_rows_are_preserved_when_switching_profiles(self):
  with tempfile.TemporaryDirectory() as tmp, closing(Database(Path(tmp)/'db.sqlite')) as db:
   db.ensure_initialized();c=Config(db).load();mid=db.conn.execute('SELECT id FROM models LIMIT 1').fetchone()[0];c.settings.set_active_model_id(mid);c.add_headstamp('FC',1);profiles=SorterProfiles(c);profiles.save('A')
   c.set_headstamp_slot('FC',7);profiles.save('B');profiles.select('A');self.assertEqual(c.slot_for_headstamp('FC'),1);profiles.select('B');self.assertEqual(c.slot_for_headstamp('FC'),7)

class ReadinessTests(unittest.TestCase):
 def test_failed_server_preflight_prevents_any_manual_motion(self):
  from sorter.run_controller import RunController
  controller=RunController.__new__(RunController);controller._operation_lock=threading.Lock();controller.broker=Mock();controller.bus=Mock();controller._record_event=Mock();controller._record_exception=Mock();controller._check_inference_ready=Mock(side_effect=RuntimeError('Inference server stopped'))
  with patch('sorter.run_controller.traceback.print_exc'):
   result=controller.cycle_once()
  self.assertEqual(result['error'],'Inference server stopped');controller.broker.force_sort_and_move.assert_not_called();self.assertFalse(controller.operation_busy)
 def test_readiness_checks_selected_model_without_changing_assignments(self):
  from sorter.api_client import ensure_ready
  cfg={'endpoint_url':'http://127.0.0.1:8000','model':'9mm','api_key':''}
  with patch('sorter.api_client.get_headstamps',return_value=['FC']) as request:
   self.assertEqual(ensure_ready(cfg),['FC']);request.assert_called_once_with(cfg['endpoint_url'],'9mm','',timeout=30.0)
 def test_failed_server_preflight_prevents_continuous_priming(self):
  from sorter.run_controller import RunController
  controller=RunController.__new__(RunController);controller._operation_lock=threading.Lock();controller._operation_lock.acquire();controller.broker=Mock();controller.bus=Mock();controller._record_event=Mock();controller._check_inference_ready=Mock(side_effect=RuntimeError('Inference server stopped'))
  controller._loop();controller.broker.force_sort_and_move.assert_not_called();self.assertFalse(controller.operation_busy);controller.bus.post.assert_any_call('run/error','Inference server stopped')

class CommunityGeometryTests(unittest.TestCase):
 def method(self,filename,class_name,method_name):
  import ast
  tree=ast.parse((Path(__file__).resolve().parents[1]/'sorter/ui'/filename).read_text(encoding='utf-8'))
  cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==class_name)
  method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==method_name)
  module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),method],type_ignores=[])
  ast.fix_missing_locations(module);namespace={};exec(compile(module,filename,'exec'),namespace);return namespace[method_name]
 def test_scroll_range_tracks_async_content_growth_without_viewport_resize(self):
  from types import SimpleNamespace
  canvas=Mock();canvas.winfo_height.return_value=500;canvas.bbox.return_value=(0,0,800,4000)
  body=Mock();body.winfo_reqheight.return_value=4000
  scroller=SimpleNamespace(_canvas=canvas,body=body,_window_id=1)
  self.method('widgets.py','ScrollableFrame','_on_body_configure')(scroller,None)
  canvas.itemconfigure.assert_called_once_with(1,height=4000);canvas.configure.assert_called_once_with(scrollregion=(0,0,800,4000))
 def test_every_model_info_cell_remains_in_layout_on_narrow_windows(self):
  from types import SimpleNamespace
  cells=[(Mock(),Mock()) for _ in range(8)];card=SimpleNamespace(_info_grid=Mock(),_info_cells=cells,_wrapped_labels=[Mock()])
  layout=self.method('tab_community.py','CommunityModelCard','_layout_card');layout(card,SimpleNamespace(width=420))
  for index,(cell,label) in enumerate(cells):
   self.assertEqual(cell.grid.call_args.kwargs['row'],index);self.assertEqual(cell.grid.call_args.kwargs['column'],0)
  layout(card,SimpleNamespace(width=1100))
  self.assertEqual(cells[7][0].grid.call_args.kwargs['row'],2);self.assertEqual(cells[7][0].grid.call_args.kwargs['column'],1)

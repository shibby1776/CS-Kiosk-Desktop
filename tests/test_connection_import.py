import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from sorter.config import Config
from sorter.db import Database
from sorter.transports.migration import import_profile

class ConnectionImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.db=Database(self.path/'settings.sqlite');self.addCleanup(self.db.close);self.db.ensure_initialized()
        self.config=Config(self.db).load()
        self.profile={'sorter':{'config':{'esp_url':'http://192.168.4.92','remote':{'endpoint':'http://127.0.0.1:8000','model':'9mm'},'crop':{'mode':'hough','x':860,'y':506,'primer_mode':'hide','primer_radius':135,'hough':{'dp':1.1,'min_dist':225,'param1':250,'param2':20,'min_radius':240,'max_radius':300}},'slots':{'FC':1,'SAR':3},'labels':['FC','SAR'],'confidence_floor':80,'settle_ms':150,'saved_machine':{'CameraLEDLevel':150,'FeedMotorSpeed':20}}}}
    def test_roundtrip_restores_bins_crop_led_without_replacing_motor_settings(self):
        before=copy.deepcopy(self.config.serial['init_settings'])
        with patch('sorter.transports.migration.paths.app_data_dir',return_value=self.path):result=import_profile(self.config,self.profile)
        saved=Config(self.db).load()
        self.assertEqual(saved.serial['port'],'http://192.168.4.92')
        self.assertEqual(saved.serial['init_settings']['cameraledlevel'],150)
        self.assertEqual(saved.serial['init_settings']['feedspeed'],before['feedspeed'])
        self.assertFalse(saved.serial['init_on_startup'])
        self.assertEqual({v['name']:v['slot'] for v in saved.remote_headstamps()},{'FC':1,'SAR':3})
        self.assertEqual(saved.image_proc['hough']['_expected_x'],860)
        self.assertEqual(saved.api['api_key'],'')
        backup=json.loads((self.path/result['backup']).read_text(encoding='utf-8'));self.assertEqual(backup['serial']['init_settings'],before)
    def test_invalid_assignment_does_not_change_configuration(self):
        before=copy.deepcopy(self.config.data);self.profile['sorter']['config']['slots']['FC']=8
        with self.assertRaises(ValueError):import_profile(self.config,self.profile)
        self.assertEqual(self.config.data,before)
    def test_invalid_machine_profile_does_not_partially_apply(self):
        before=copy.deepcopy(self.config.data);self.profile['sorter']['config']['saved_machine']=[]
        with self.assertRaises(ValueError):import_profile(self.config,self.profile)
        self.assertEqual(self.config.data,before)

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from sorter.diagnostics import DiagnosticCollector
from sorter.transports.esp_http import ESP, TrialError, VERSION
from sorter.transports.network import NetworkBroker, refresh_disconnected_report

ROOT=Path(__file__).resolve().parents[1]

def app_method(name):
    # Exercise the actual method without importing a Tk display/runtime.
    tree=ast.parse((ROOT/'sorter/ui/app.py').read_text(encoding='utf-8'))
    function=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
    module=ast.Module(body=[function],type_ignores=[])
    scope={'__package__':'sorter.ui','Any':object,'copy':copy,
           'PUBLIC_VERSION':'Kiosk 2.3','APP_VERSION':'2.3.0'}
    exec(compile(module,'app.py','exec'),scope)
    return scope[name]

class EvidenceTests(unittest.TestCase):
    def esp(self):
        e=ESP('http://192.168.4.92');e.boot='boot';self.addCleanup(e.http.close);return e
    def state(self,**extra):
        return {'kit':VERSION,'boot':'boot','owned':True,'ready':True,'disconnects':0,
                'fault':False,'id':4,'phase':'serial',**extra}
    def test_fault_preserved_before_lease_check_and_not_mutated(self):
        e=self.esp();state=self.state(fault=True,owned=False,error='Serial acknowledgement timeout',
                                    usb_at_fault={'command_id':4})
        with self.assertRaisesRegex(TrialError,'acknowledgement'):e.checked(state)
        state['usb_at_fault']['command_id']=99
        self.assertEqual(e.diagnostic_evidence()['first_fault']['usb_at_fault']['command_id'],4)
    def test_first_fault_not_overwritten_by_later_cleanup(self):
        e=self.esp();first=self.state(fault=True,error='serial timeout')
        with self.assertRaises(TrialError):e.checked(first)
        e.remember_state(self.state(fault=True,error='later camera cleanup'))
        e.request=Mock()
        report=e.diagnostic_evidence(refresh=True)
        e.request.assert_not_called();self.assertEqual(report['first_fault']['error'],'serial timeout')
    def test_invalid_identity_is_still_retained_for_diagnostics(self):
        e=self.esp()
        with self.assertRaises(TrialError):e.checked(self.state(boot='reboot'))
        self.assertEqual(e.diagnostic_evidence()['last_state']['boot'],'reboot')
    def test_disconnect_refresh_is_one_read_only_get(self):
        e=self.esp();response=Mock();response.json.return_value=self.state(fault=True,error='timeout')
        e.request=Mock(return_value=response)
        self.assertTrue(e.diagnostic_evidence(refresh=True)['first_fault']['fault'])
        e.request.assert_called_once_with('GET','/api/trial',timeout=2)
    def test_network_failure_keeps_cached_state_with_age_and_error(self):
        e=self.esp();e.remember_state(self.state());e.request=Mock(side_effect=TrialError('offline'))
        report=e.diagnostic_evidence(refresh=True)
        self.assertEqual(report['refresh_error'],'offline')
        self.assertIsNotNone(report['last_state_observed_at'])
        self.assertIsNone(report['first_fault'])
    def test_same_boot_usb_recovery_acknowledges_without_command_replay(self):
        e=self.esp();e.owner='0123456789abcdef';e.disconnects=2;e.sequence=9
        states=[
            self.state(fault=True,ready=False,disconnects=3,error='USB topology lost',usb_recovery={'state':'enumerating','generation':0,'attempts':1}),
            self.state(fault=True,ready=True,disconnects=4,error='USB topology lost',usb_recovery={'state':'recovered','generation':1,'attempts':1}),
            self.state(fault=False,ready=True,disconnects=4,accepted=True,usb_recovery={'state':'idle','generation':1,'attempts':0}),
        ]
        responses=[]
        for state in states:
            response=Mock();response.json.return_value=state;responses.append(response)
        e.request=Mock(side_effect=responses)
        with patch('sorter.transports.esp_http.time.sleep'):
            result=e.recover_usb(timeout=2)
        self.assertEqual(result['generation'],1);self.assertEqual(e.disconnects,4);self.assertEqual(e.sequence,9)
        self.assertEqual(e.request.call_args_list[-1].args[:2],('POST','/api/trial'))
        self.assertEqual(e.request.call_args_list[-1].args[2]['action'],'ack_usb_recovery')
    def test_http409_fault_body_is_kept_and_owner_not_exported(self):
        e=self.esp();response=Mock();response.status_code=409
        body=json.dumps(self.state(fault=True,error='restart Kiosk Node and machine')).encode()
        response.iter_content.return_value=iter([body]);response.headers={}
        response.json.return_value=json.loads(body);e.http.request=Mock(return_value=response)
        with self.assertRaisesRegex(TrialError,'restart Kiosk Node'):e.request('POST','/api/trial',{'owner':e.owner})
        report=e.diagnostic_evidence()
        self.assertEqual(report['last_http_error']['status'],409)
        self.assertNotIn(e.owner,json.dumps(report))
    def test_broker_failure_evidence_is_frozen_before_callback(self):
        b=NetworkBroker('http://192.168.4.92');self.addCleanup(b.client.http.close)
        b.is_connected=True;b.pending_command={'kind':'sort','argument':'3'}
        b.client.diagnostic_evidence=Mock(return_value={'first_fault':{'id':2098}})
        seen=[];b.on_disconnect.append(lambda _:seen.append(b.report()))
        b._lost(TrialError('timeout'));b._lost(TrialError('secondary'))
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['disconnect_evidence']['pending_command']['argument'],'3')
        self.assertEqual(seen[0]['disconnect_evidence']['device']['first_fault']['id'],2098)
        b.client.diagnostic_evidence.assert_called_once_with(refresh=True)
    def test_app_preserves_history_before_broker_teardown(self):
        report={'type':'esp','events':[{'event':'error'}],'disconnect_evidence':{'reason':'timeout'}}
        broker=Mock();broker.port='http://192.168.4.92';broker.report.return_value=copy.deepcopy(report)
        app=SimpleNamespace(broker=broker,config=SimpleNamespace(serial={}),run_controller=None,
                            _set_serial_indicator=Mock(),_record_runtime_event=Mock(),set_status=Mock())
        broker.stop.side_effect=lambda:broker.report.return_value.clear()
        app_method('_on_serial_disconnected')(app,'timeout')
        self.assertIsNone(app.broker);self.assertEqual(app._last_disconnect_transport,report)
    def test_export_after_disconnect_and_usb_reconnect_keeps_old_esp_evidence(self):
        refresh=patch('sorter.transports.network.refresh_disconnected_report',side_effect=copy.deepcopy)
        refresh.start();self.addCleanup(refresh.stop)
        prior={'type':'esp','disconnect_evidence':{'device':{'first_fault':{'id':2098}}}}
        app=SimpleNamespace(broker=None,_last_disconnect_transport=prior,
             config=SimpleNamespace(camera={}),_inference_runtime_diagnostic_info=lambda:{},
             _api_server_diagnostic_info=lambda:{},_web_interface_diagnostic_info=lambda:{})
        method=app_method('_diagnostic_app_info')
        self.assertEqual(method(app)['connection_transport'],prior)
        app.broker=SimpleNamespace(is_connected=True)
        info=method(app)
        self.assertEqual(info['connection_transport']['type'],'serial')
        with tempfile.TemporaryDirectory() as tmp:
            target=DiagnosticCollector().export_zip(Path(tmp)/'report.zip',{},info)
            with zipfile.ZipFile(target) as archive:
                evidence=json.loads(archive.read('esp_transport_evidence.json'))
                self.assertEqual(evidence['last_disconnect_transport'],prior)
                full=json.loads(archive.read('diagnostic_report.json'))
                self.assertEqual(full['application']['last_disconnect_transport'],prior)
    def test_export_observation_captures_later_camera_timeout_without_replacing_first_fault(self):
        frozen={'boot':'boot','id':2098,'usb_at_fault':{'at_ms':1}}
        previous={'type':'esp','connection':'http://192.168.4.92',
                  'disconnect_evidence':{'device':{'first_fault':frozen}}}
        client=Mock();client.diagnostic_evidence.return_value={'last_state':{
            'boot':'boot','usb_at_camera_timeout':{'at_ms':2}}}
        with patch('sorter.transports.network.ESP',return_value=client):
            report=refresh_disconnected_report(previous)
        self.assertTrue(report['export_matches_fault_boot'])
        self.assertEqual(report['disconnect_evidence']['device']['first_fault'],frozen)
        self.assertEqual(report['device_at_export']['last_state']['usb_at_camera_timeout']['at_ms'],2)
        client.diagnostic_evidence.assert_called_once_with(refresh=True);client.http.close.assert_called_once()
        self.assertNotIn('device_at_export',previous)
    def test_device_reboot_or_unreachable_at_export_does_not_erase_original_fault(self):
        frozen={'boot':'old','id':2098}
        previous={'type':'esp','connection':'http://192.168.4.92',
                  'disconnect_evidence':{'device':{'first_fault':frozen}}}
        client=Mock();client.diagnostic_evidence.return_value={'last_state':{'boot':'new','reboot_evidence':{
            'reset_reason':'task_watchdog','previous_boot':{'operation':'capture_send','command_id':2098}}}}
        with patch('sorter.transports.network.ESP',return_value=client):
            report=refresh_disconnected_report(previous)
            self.assertFalse(report['export_matches_fault_boot'])
            self.assertIn('task_watchdog',report['restart_detected']['message'])
            self.assertEqual(report['restart_detected']['reset']['previous_boot']['operation'],'capture_send')
            client.diagnostic_evidence.side_effect=TrialError('unreachable')
            failed=refresh_disconnected_report(previous)
        self.assertEqual(failed['disconnect_evidence']['device']['first_fault'],frozen)
        self.assertEqual(failed['export_refresh_error'],'unreachable')

    def test_restart_notice_identifies_unconfirmed_capture(self):
        from sorter.transports.esp_http import restart_notice
        state={'boot':'new','reboot_evidence':{'reset_reason':'panic',
               'previous_boot':{'operation':'capture_submit','command_id':1378}}}
        notice=restart_notice(state,'old',{'id':1378,'kind':'capture'})
        self.assertEqual(notice,'Kiosk Node restarted during capture command 1378 (reset reason: panic; last Kiosk Node phase: capture_submit)')

if __name__=='__main__':unittest.main()

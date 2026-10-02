"""Kiosk Node implementations of the existing SerialBroker and Camera contracts.

Neither adapter owns routing, inference, UI state, or an alternative run loop.
The application's existing RunController remains the sole control owner.
"""
from collections import deque
import copy,json,threading,time
import cv2
import numpy as np
from ..serial_broker import SerialBroker
from ..camera import Camera
from .esp_http import ESP,TrialCancelled,TrialDisconnected,TrialError,base_url,restart_notice


def is_network_address(value):
    return str(value).strip().lower().startswith(('http://','https://'))

def refresh_disconnected_report(report):
    """One read-only device observation at explicit Diagnostic ZIP export.

    Preserve the original frozen failure. Later camera cleanup evidence or a
    changed boot is recorded separately, never substituted for the first fault.
    No lease, settings, camera start, serial command or recovery is requested.
    """
    result=copy.deepcopy(report)
    if not result or result.get('type')!='esp':return result
    client=None
    try:
        client=ESP(result['connection'])
        observed=client.diagnostic_evidence(refresh=True)
        result['device_at_export']=observed
        original=(result.get('disconnect_evidence') or {}).get('device') or {}
        frozen=original.get('first_fault') or original.get('last_state') or {}
        latest=observed.get('last_state') or {}
        result['export_matches_fault_boot']=latest.get('boot')==frozen.get('boot') if latest.get('boot') and frozen.get('boot') else None
        if result['export_matches_fault_boot'] is False:
            pending=(result.get('disconnect_evidence') or {}).get('device',{}).get('last_attempt') or {}
            notice=restart_notice(latest,frozen.get('boot'),pending)
            result['restart_detected']={'message':notice or 'Kiosk Node restarted after the recorded fault',
                'fault_boot':frozen.get('boot'),'export_boot':latest.get('boot'),
                'reset':copy.deepcopy(latest.get('reboot_evidence') or {}),
                'pending_command':copy.deepcopy(pending)}
    except Exception as exc:result['export_refresh_error']=str(exc)
    finally:
        if client:client.http.close()
    return result

def validate_camera_mode(broker, width=None, height=None):
    """Reject unsupported bridge modes before saving camera settings."""
    if not isinstance(broker, NetworkBroker):
        return
    frame = broker.initial_meta.get('camera', {})
    actual = (int(frame.get('width', 1280)), int(frame.get('height', 720)))
    requested = (int(width) if width is not None else actual[0],
                 int(height) if height is not None else actual[1])
    if requested != actual:
        raise ValueError(f'The sorter camera is configured for {actual[0]}×{actual[1]}. Select its detected mode. Changing resolution requires updating the bridge firmware configuration.')


class NetworkBroker(SerialBroker):
    def __init__(self,port,baud=9600,require_serial_ready=True,handshake_timeout_s=4,client_factory=ESP):
        super().__init__(port,baud,require_serial_ready,handshake_timeout_s)
        self.port=base_url(port);self.client=client_factory(self.port)
        self.operation_lock=threading.RLock();self.run_active=False;self.last_motion_completed=0.0
        self.error='';self.events=deque(maxlen=500);self.camera=None;self._heartbeat=None;self._stop_dispatch=None
        self._evidence_lock=threading.RLock();self.disconnect_evidence=None;self.pending_command=None
        self._recovery_lock=threading.Lock();self.recoveries=deque(maxlen=50);self.last_operation_error=''
        self.on_recovering=[];self.on_recovered=[]
        self.restart_notice=''
        self._last_machine_state='';self._last_wait_count=0;self.client.on_state=self._client_state
    def _record(self,event,**fields):
        with self._evidence_lock:self.events.append({'time':time.time(),'event':event,**fields})
    def _client_state(self,state):
        machine_state=str(state.get('machine_state') or '')
        count=int(state.get('waiting_for_brass_count',0) or 0)
        if machine_state and machine_state!=self._last_machine_state:
            self._record('machine_state',state=machine_state,last_serial_line=state.get('last_serial_line',''))
        self._last_machine_state=machine_state
        if machine_state=='waiting_for_brass' and count>self._last_wait_count:
            line=str(state.get('last_serial_line') or 'waiting for brass')
            self._fire(self.on_received,line);self._fire(self.on_waiting,line)
        self._last_wait_count=max(self._last_wait_count,count)
    def try_open(self):
        try:
            with self.operation_lock:
                self.client.connect()
                self.restart_notice=getattr(self.client,'restart_notice','')
                network=self.client.network_snapshot()
                if network.get('ps_mode')!='none' or network.get('tcp_profile')!='send32k' or network.get('lease_active'):
                    raise TrialError('Kiosk Node operating profile must use the required network settings')
                self.firmware_version=self.client.command('read','version')['response']
                current=json.loads(self.client.command('read','getconfig')['response'])
                if not all(k in current for k in ('FeedMotorSpeed','SortMotorSpeed','SortSteps')):raise TrialError('Incomplete machine configuration')
                self.initial_raw,self.initial_meta=self.client.capture()
                self.is_connected=True;self._link_lost=False;self._stop_event.clear()
                self._record('connected',firmware=self.firmware_version,machine=current)
                return True
        except Exception as exc:
            self.error=str(exc);self.is_connected=False
            try:self.client.release()
            except Exception:pass
            return False
    def start(self):
        if self._heartbeat and self._heartbeat.is_alive():return
        def heartbeat():
            while not self._stop_event.wait(10):
                # A command can hold the serial transaction lock for 30s. Touch
                # uses the HTTP lock only so the 45s owner lease remains alive.
                try:self.client.touch()
                except Exception as exc:
                    if self._lost(exc):continue
                    break
        self._heartbeat=threading.Thread(target=heartbeat,daemon=True,name='ESPHeartbeat');self._heartbeat.start()
    def _is_usb_recovery(self,exc):
        message=str(exc).lower()
        state={}
        evidence_lock=getattr(self.client,'evidence_lock',None)
        if evidence_lock is not None:
            with evidence_lock:state=copy.deepcopy(getattr(self.client,'last',None) or {})
        recovery=state.get('usb_recovery') or {}
        return 'usb topology' in message or recovery.get('state') in ('grace','enumerating','recovered','failed')
    def _recover_usb(self,exc):
        if not self._is_usb_recovery(exc):return False
        with self._recovery_lock:
            self._fire(self.on_recovering,str(exc));self._record('usb_recovery_started',reason=str(exc))
            started=time.time()
            try:
                result=self.client.recover_usb(timeout=30)
                item={'started_at':started,'finished_at':time.time(),'ok':True,**result}
                with self._evidence_lock:self.recoveries.append(item)
                self.error='';self.is_connected=True;self._link_lost=False;self._stop_event.clear()
                self._record('usb_recovered',**result);self._fire(self.on_recovered,result)
                return True
            except Exception as recovery_error:
                with self._evidence_lock:self.recoveries.append({'started_at':started,'finished_at':time.time(),'ok':False,'error':str(recovery_error)})
                self.error=str(recovery_error)
                return False
    def _lost(self,exc):
        if self._recover_usb(exc):return True
        with self._evidence_lock:
            if self._link_lost:return False
            self._link_lost=True
        self.error=str(exc);self._record('error',message=self.error)
        self.is_connected=False;self._stop_event.set()
        if self.camera:self.camera.cancel_pending()
        evidence={}
        try:
            if hasattr(self.client,'diagnostic_evidence'):evidence=self.client.diagnostic_evidence(refresh=True)
        except Exception as diagnostic_error:evidence={'collection_error':str(diagnostic_error)}
        with self._evidence_lock:
            self.disconnect_evidence={'at':time.time(),'reason':self.error,
                'pending_command':copy.deepcopy(self.pending_command),'device':evidence}
        self._fire(self.on_disconnect,self.error)
        return False
    def _execute(self,kind,arg='',wire=None):
        with self.operation_lock:
            self.last_operation_error=''
            if kind in ('force','sort','move') and self._stop_dispatch and self._stop_dispatch.is_alive():
                raise TrialError('Stop is still finishing; wait before starting another movement')
            if not self.is_connected:raise TrialError(self.error or 'Sorter disconnected')
            self._fire(self.on_sent,wire or (kind+(':'+arg if arg else '')))
            started=time.monotonic()
            with self._evidence_lock:self.pending_command={'kind':kind,'argument':arg,'started_at':time.time()}
            self._record('command_started',kind=kind,argument=arg)
            try:
                result=self.client.command(kind,arg)
                reply=result.get('response','')
                self._most_recent_response=reply;self._fire(self.on_received,reply)
                if reply=='done':
                    self.last_motion_completed=time.monotonic();self._fire(self.on_done,reply)
                elif reply=='ok':self._fire(self.on_ok,reply)
                elif reply.startswith('{'):self._fire(self.on_response,reply)
                self._record('command',kind=kind,argument=arg,response=reply,elapsed_ms=round((time.monotonic()-started)*1000,3),timing=self.client.last_command_timing)
                with self._evidence_lock:self.pending_command=None
                return result
            except TrialCancelled:
                self._record('command_cancelled',kind=kind,argument=arg,
                    elapsed_ms=round((time.monotonic()-started)*1000,3))
                with self._evidence_lock:self.pending_command=None
                raise
            except Exception as exc:
                if self._lost(exc):
                    self.last_operation_error='USB bridge recovered; the interrupted operation was not retried'
                    raise TrialError(self.last_operation_error) from exc
                self.last_operation_error=str(exc);raise
    @staticmethod
    def translate(command):
        c=str(command).strip()
        if not c or '\n' in c or '\r' in c:raise ValueError('Invalid serial command')
        if c in ('version','getconfig','ping'):return 'read',c
        if c=='stop':return 'stop',''
        if c.isdigit() and 0<=int(c)<=7:return 'sort',str(int(c))
        for prefix,kind in [('xf:','force'),('sortto:','move')]:
            if c.startswith(prefix) and c[len(prefix):].isdigit() and 0<=int(c[len(prefix):])<=7:return kind,str(int(c[len(prefix):]))
        # Settings are range-validated again by firmware before any wire write.
        keys={'feedmotorcurrent','sortmotorcurrent','feedspeed','sortspeed','feedsteps','sortsteps','notificationdelay','slotdropdelay','airdropenabled','airdroppostdelay','airdroppredelay','airdropdsignalduration','feedhomingoffset','sorthomingoffset','automotorstandbytimeout','fan','cameraledlevel','debounceTimeout','debounceTime'}
        if ':' in c:
            key,value=c.split(':',1)
            if key in keys and value.isdigit():return 'set',c
        raise ValueError('The connected bridge does not support this command: '+c)
    def send_command(self,command):
        kind,arg=self.translate(command)
        try:self._execute(kind,arg,wire=str(command));return True
        except TrialError:return False
    def feed_one(self):return self.force_sort_and_move(0)
    def force_sort_and_move(self,slot):
        try:return self._execute(*self.translate('xf:'+str(slot)),wire='xf:'+str(slot)).get('response')=='done'
        except TrialError:return False
    def sort_and_move(self,slot):
        try:return self._execute(*self.translate(str(slot)),wire=str(slot)).get('response')=='done'
        except TrialError:return False
    def get_config(self,timeout_s=3.0):
        try:return json.loads(self._execute('read','getconfig',wire='getconfig')['response'])
        except (TrialError,ValueError):return None
    def update_init_settings(self,settings):
        from ..machine_settings import settings_for_board
        settings = settings_for_board(settings)
        for key,value in settings.items():
            if not self.send_command(f'{key}:{int(value)}'):raise RuntimeError(self.error or 'Setting write failed')
        return self.get_config()
    def stop_run(self):
        # This control path deliberately bypasses operation_lock so Stop can
        # interrupt a CS7.2 command that is waiting at the brass sensor.
        if self._stop_dispatch and self._stop_dispatch.is_alive():return
        def stop():
            try:
                if self.is_connected:
                    self._fire(self.on_sent,'stop')
                    result=self.client.emergency_stop()
                    reply=result.get('response','stop_sent')
                    self._most_recent_response=reply;self._fire(self.on_received,reply)
                    self._record('emergency_stop',response=reply,machine_state=result.get('machine_state',''))
            except Exception as exc:
                self._fire(self.on_error,str(exc));self._lost(exc)
        self._stop_dispatch=threading.Thread(target=stop,daemon=True,name='ESPStop');self._stop_dispatch.start()
    def stop(self):
        self._stop_event.set()
        if self.camera:self.camera.stop()
        if self._heartbeat and self._heartbeat is not threading.current_thread():self._heartbeat.join(timeout=12)
        with self.operation_lock:
            if self.is_connected:
                try:self.client.release()
                except Exception:pass
            self.is_connected=False
        self.client.http.close()
    close=stop
    def report(self):
        with self._evidence_lock:
            report=copy.deepcopy({'type':'esp','connection':self.port,'firmware':self.firmware_version,
                'connected':self.is_connected,'error':self.error,'events':list(self.events),
                'disconnect_evidence':self.disconnect_evidence,'pending_command':self.pending_command,
                'recoveries':list(self.recoveries),'last_operation_error':self.last_operation_error})
        # A disconnect already froze the authoritative device evidence before
        # callbacks ran. Reuse that snapshot instead of consulting the client
        # again from a callback/report path. While connected, expose the
        # client's current cached state without causing a network refresh.
        if self.disconnect_evidence is not None:
            report['bridge_evidence']=copy.deepcopy(self.disconnect_evidence.get('device',{}))
        else:
            try:report['bridge_evidence']=self.client.diagnostic_evidence(refresh=False)
            except Exception as exc:report['bridge_evidence']={'collection_error':str(exc)}
        report['camera']=self.camera.diagnostic_info() if self.camera else {}
        return report

class NetworkCamera(Camera):
    """Keep preview frames separate from fresh, explicitly requested captures."""
    def __init__(self,broker,width=1280,height=720,settle_ms=150):
        super().__init__(0,width,height);self.broker=broker;broker.camera=self
        self.settle_ms=settle_ms;self._preview_stop=threading.Event();self.metadata={}
        if getattr(broker,'initial_raw',None):
            frame=cv2.imdecode(np.frombuffer(broker.initial_raw,dtype=np.uint8),cv2.IMREAD_COLOR)
            if frame is not None:self.height,self.width=frame.shape[:2];self.metadata=broker.initial_meta;self._publish_frame(frame)
        self._selected_backend='Kiosk Node MJPEG';self._negotiated_fourcc='MJPG';self._mjpg_verified=True
    def open(self):return self.broker.is_connected
    def cancel_pending(self):
        """Prevent preview/manual workers from starting another HTTP capture."""
        self._preview_stop.set()
    def start_preview(self):
        if not self.open():return False
        if self._thread and self._thread.is_alive():return True
        self._preview_stop.clear()
        def preview():
            while not self._preview_stop.wait(.5):
                if self.broker.run_active:continue
                if not self.broker.operation_lock.acquire(blocking=False):continue
                try:
                    if not self.broker.run_active:self._capture()
                except Exception as exc:self._dshow_callback_error=str(exc)
                finally:self.broker.operation_lock.release()
        self._thread=threading.Thread(target=preview,daemon=True,name='ESPPreview');self._thread.start();return True
    def _capture(self):
        with self.broker.operation_lock:
            if not self.broker.is_connected:raise TrialDisconnected('Sorter disconnected')
            try:raw,meta=self.broker.client.capture()
            except Exception as exc:
                recovered=self.broker._lost(exc)
                if recovered:raise
                raise TrialDisconnected(self.broker.error or 'Sorter disconnected') from exc
            frame=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
            if frame is None:raise TrialError('Invalid camera JPEG')
            self.height,self.width=frame.shape[:2];self.metadata=meta;self._publish_frame(frame)
            self._dshow_callback_error=None
            self.broker._record('capture',metadata=meta)
            return frame
    def capture_frame(self):
        # Capture RPC discards the first frame after its boundary in firmware.
        delay=max(self._discard_until,self.broker.last_motion_completed+self.settle_ms/1000)-time.monotonic()
        if delay>0:time.sleep(delay)
        return self._capture()
    def capture_frame_after_sequence(self,baseline_sequence,*,timeout_s=1.2,frames_after_boundary=2):
        started=time.monotonic();frame=self.capture_frame()
        return frame,{'capture_source':'fresh_bridge_capture','fresh_frame':True,'timeout_fallback':False,'selected_sequence':self._frame_sequence,'baseline_sequence':baseline_sequence,'wait_ms':round((time.monotonic()-started)*1000,3),'samples':[],'bridge':self.metadata}
    def capture_stable_frame(self,*args,**kwargs):return self.capture_frame()
    def diagnostic_info(self):
        return {'device_index':0,'opened':self.broker.is_connected,'width':self.width,'height':self.height,'fps':30,'fourcc':'MJPG','backend_name':self._selected_backend,'compatibility_mode':False,'published_frame_sequence':self._frame_sequence,'last_error':self._dshow_callback_error,'bridge':self.metadata.get('camera',{})}
    def stop(self):
        self._preview_stop.set()
        if self._thread and self._thread is not threading.current_thread():self._thread.join(timeout=12)
        if self._thread and self._thread.is_alive():raise RuntimeError('Camera capture is still finishing; wait before reconnecting.')
        self._thread=None

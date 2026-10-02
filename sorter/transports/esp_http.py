"""One outstanding operation per Kiosk Node; never retry a machine-command POST."""
import json
import copy
import secrets
import threading
import time
import zlib
from contextlib import nullcontext
from urllib.parse import urlparse
import requests

VERSION='2.6.16'
class TrialError(Exception): pass
class TrialCancelled(TrialError): pass
class TrialDisconnected(TrialError):
    """Expected result for workers invalidated by a lost sorter link."""
    expected_disconnect=True

def restart_notice(state, expected_boot=None, pending=None):
    """Describe a bridge reboot using the reset-retained firmware evidence."""
    if not isinstance(state,dict):return ''
    current=str(state.get('boot') or '')
    if expected_boot and current==expected_boot:return ''
    evidence=state.get('reboot_evidence') or {}
    previous=evidence.get('previous_boot') or {}
    reason=str(evidence.get('reset_reason') or 'unknown')
    operation=str(previous.get('operation') or '')
    command_id=int(previous.get('command_id',0) or 0)
    pending=pending or {}
    kind=str(pending.get('kind') or ('capture' if operation.startswith('capture') else 'operation'))
    pending_id=int(pending.get('id',0) or command_id)
    detail=f' during {kind}' if kind else ''
    if pending_id:detail+=f' command {pending_id}'
    phase=f'; last Kiosk Node phase: {operation}' if operation else ''
    return f'Kiosk Node restarted{detail} (reset reason: {reason}{phase})'

def base_url(value):
    value=str(value).strip().rstrip('/')
    parsed=urlparse(value)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise ValueError('Use a base URL such as http://192.168.137.119 with no credentials or path')
    return value

def handler_timing(value,headers_ms):
    if not value:return {}
    try:
        parts=[int(v) for v in value.split(',')]
        if len(parts)!=3 or any(v<0 for v in parts):return {'invalid':True}
        total,body,wait=parts
        return {'handler_to_response_ms':total/1000,'entry_to_body_ms':body/1000,
                'command_wait_ms':wait/1000,
                'outside_measured_handler_ms':round(headers_ms-total/1000,3)}
    except (ValueError,AttributeError):return {'invalid':True}

class ESP:
    def __init__(self,url):
        self.url=base_url(url);self.owner=secrets.token_hex(16);self.boot=None;self.sequence=0
        self.http=requests.Session();self.http.trust_env=False
        self.lock=threading.Lock();self.disconnects=0;self.last=None;self.last_command_timing={}
        self.evidence_lock=threading.RLock();self.last_observed_at=None
        self.first_fault=None;self.last_http_error=None;self.last_attempt={};self.on_state=None
        self.restart_notice=''
    def remember_state(self,data):
        if not isinstance(data,dict):raise TrialError('Invalid Kiosk Node state response')
        with self.evidence_lock:
            self.last=copy.deepcopy(data);self.last_observed_at=time.time()
            if data.get('fault') and self.first_fault is None:
                self.first_fault=copy.deepcopy(data)
        callback=self.on_state
        if callback:
            try:callback(copy.deepcopy(data))
            except Exception:pass
    def diagnostic_evidence(self,refresh=False):
        # At most one read-only GET on disconnect, only when the failure reply
        # did not already supply frozen evidence. Never claim/touch/replay POST.
        refresh_error=None
        with self.evidence_lock:has_fault=self.first_fault is not None
        if refresh and not has_fault:
            try:
                data=self.request('GET','/api/trial',timeout=2).json()
                self.remember_state(data)
            except Exception as exc:refresh_error=str(exc)
        with self.evidence_lock:
            return copy.deepcopy({'last_state':self.last,'last_state_observed_at':self.last_observed_at,
                'first_fault':self.first_fault,'last_attempt':self.last_attempt,
                'last_http_error':self.last_http_error,'refresh_error':refresh_error})
    def request(self,method,path,body=None,timeout=8,fresh=False,independent=False):
        started=time.monotonic();cpu_started=time.thread_time()
        with (nullcontext() if independent else self.lock):
            locked=time.monotonic();session=self.http
            if fresh:session=requests.Session();session.trust_env=False
            try:
                r=session.request(method,self.url+path,json=body,timeout=(3,timeout),allow_redirects=False,
                    stream=True,headers={'Connection':'close'} if fresh else None)
                headers_at=time.monotonic();parts=[];total=0;blocks=[];previous=headers_at;consumer_gap=0
                iterator=r.iter_content(chunk_size=16384)
                while True:
                    before=time.monotonic();consumer_gap=max(consumer_gap,before-previous)
                    try:part=next(iterator)
                    except StopIteration:break
                    after=time.monotonic();blocks.append({'offset':total,'bytes':len(part),'read_ms':round((after-before)*1000,3)})
                    parts.append(part);total+=len(part);previous=after
                    if total>4*1024*1024:raise TrialError('Kiosk Node response exceeded 4 MiB')
                finished=time.monotonic()
                # Preserve requests.Response's content/json interface after its
                # streamed iterator was consumed for timing and size checks.
                r._content=b''.join(parts);r._content_consumed=True
                r.cs72_timing={'lock_wait_ms':round((locked-started)*1000,3),
                    'headers_ms':round((headers_at-locked)*1000,3),'body_ms':round((finished-headers_at)*1000,3),
                    'total_ms':round((finished-started)*1000,3),'read_blocks':len(blocks),
                    'slowest_reads':sorted(blocks,key=lambda x:x['read_ms'],reverse=True)[:4],
                    'consumer_gap_max_ms':round(consumer_gap*1000,3),'fresh_connection':fresh,'bytes':total}
                r.cs72_timing['thread_cpu_ms']=round((time.thread_time()-cpu_started)*1000,3)
                r.cs72_timing['esp_http']=handler_timing(r.headers.get('X-CS72-HTTP-Us'),r.cs72_timing['headers_ms'])
            except requests.RequestException as e:
                raise TrialError('Kiosk Node request failed; no motion command was retried') from e
            finally:
                if 'r' in locals():r.close()
                if fresh:session.close()
        if r.status_code!=200:
            # The bridge returns its fault state even with a rejected claim or
            # command. Retain it before throwing away the disconnected broker.
            detail=None
            try:
                data=r.json()
                if isinstance(data,dict) and 'kit' in data and 'boot' in data:
                    self.remember_state(data)
                    if data.get('fault'):detail=data.get('error')
            except (ValueError,TypeError):pass
            with self.evidence_lock:
                self.last_http_error={'status':r.status_code,'path':path.split('?')[0],'at':time.time()}
            if detail:raise TrialError(detail)
            raise TrialError(f'Kiosk Node rejected request (HTTP {r.status_code}); inspect connection and control lease')
        return r
    def network_snapshot(self):
        return self.request('GET',f'/network?owner={self.owner}').json()
    def validate_image(self,r,command_id,frame_sequence):
        if r.headers.get('X-Trial-Boot')!=self.boot or r.headers.get('X-Trial-Id')!=str(command_id) or r.headers.get('X-Frame-Sequence')!=str(frame_sequence):
            raise TrialError('Image does not belong to this capture')
        raw=r.content
        if len(raw)>4*1024*1024 or not raw or f'{zlib.crc32(raw)&0xffffffff:08x}'!=r.headers.get('X-Frame-CRC32'):
            raise TrialError('Image length or CRC validation failed')
    def checked(self,data,owned=True):
        self.remember_state(data)
        if data.get('kit')!=VERSION or data.get('boot')!=self.boot:
            notice=restart_notice(data,self.boot,self.last_attempt)
            raise TrialError(notice or 'Kiosk Node restarted or has incompatible firmware; reconnect and inspect the machine')
        if data.get('fault'):raise TrialError(data.get('error') or 'Kiosk Node command outcome uncertain; restart the Kiosk Node and machine')
        if owned and not data.get('owned'):raise TrialError('Kiosk Node control lease was lost')
        if not data.get('ready') or data.get('disconnects')!=self.disconnects:
            raise TrialError('USB topology changed; reconnect after inspecting the machine')
        return data
    def connect(self):
        info=self.request('GET','/api/trial').json()
        self.remember_state(info)
        if info.get('kit')!=VERSION:raise TrialError('Kiosk Node firmware is incompatible with this Kiosk release')
        if info.get('leased'):raise TrialError('Kiosk Node is controlled by another session; close it or wait 45 seconds')
        self.boot=info['boot'];self.disconnects=info.get('disconnects',0)
        self.restart_notice=restart_notice(info)
        self.post('claim');return self
    def post(self,action,**extra):
        response=self.request('POST','/api/trial',dict(action=action,owner=self.owner,boot=self.boot,**extra))
        data=response.json();data['_http_timing']=getattr(response,'cs72_timing',{})
        return self.checked(data,owned=action!='release')
    def recover_usb(self,timeout=30):
        """Observe one bounded firmware recovery and acknowledge it.

        This method performs no command POST and never changes ``sequence``;
        the interrupted capture or motion therefore cannot be replayed.
        """
        deadline=time.monotonic()+timeout;last_error=None
        while time.monotonic()<deadline:
            try:
                response=self.request('GET',f'/api/trial?owner={self.owner}',timeout=2)
                data=response.json();self.remember_state(data)
                if data.get('kit')!=VERSION or data.get('boot')!=self.boot:
                    raise TrialError('Kiosk Node restarted during USB recovery; reconnect and inspect the machine')
                recovery=data.get('usb_recovery') or {}
                state=str(recovery.get('state') or '')
                if state=='failed':raise TrialError('Kiosk Node could not recover the USB camera/sorter topology')
                if state=='recovered' and data.get('ready'):
                    response=self.request('POST','/api/trial',dict(action='ack_usb_recovery',owner=self.owner,boot=self.boot),timeout=4)
                    acknowledged=response.json();self.remember_state(acknowledged)
                    if (acknowledged.get('kit')!=VERSION or acknowledged.get('boot')!=self.boot or
                        not acknowledged.get('accepted') or not acknowledged.get('owned') or
                        acknowledged.get('fault') or not acknowledged.get('ready')):
                        raise TrialError('Recovered USB topology could not be safely acknowledged')
                    self.disconnects=int(acknowledged.get('disconnects',self.disconnects) or 0)
                    return {'generation':int(recovery.get('generation',0) or 0),
                            'attempts':int(recovery.get('attempts',0) or 0),
                            'disconnects':self.disconnects}
            except TrialError as exc:
                last_error=exc
                if 'rebooted' in str(exc) or 'could not recover' in str(exc):raise
            time.sleep(.25)
        raise TrialError(str(last_error) if last_error else 'USB recovery timed out')
    def touch(self):return self.post('touch')
    def release(self):return self.post('release')
    def emergency_stop(self):
        """Dispatch stop outside the normal command lock and confirm USB send."""
        body=dict(action='emergency_stop',owner=self.owner,boot=self.boot)
        response=self.request('POST','/api/trial',body,timeout=4,fresh=True,independent=True)
        data=self.checked(response.json())
        command_id=int(data.get('id',0) or 0);until=time.monotonic()+4
        while data.get('phase')!='cancelled' and time.monotonic()<until:
            path=f'/api/trial?owner={self.owner}'
            if command_id:path+=f'&wait_id={command_id}'
            response=self.request('GET',path,timeout=2,fresh=True,independent=True)
            data=self.checked(response.json())
            if data.get('phase')=='failed':raise TrialError(data.get('error') or 'Emergency stop failed')
        if data.get('phase')!='cancelled' or data.get('response')!='stop_sent':
            raise TrialError('Emergency stop dispatch was not confirmed by the Kiosk Node')
        return data
    def command(self,kind,argument=''):
        self.sequence+=1;command_id=self.sequence;started=time.monotonic()
        with self.evidence_lock:self.last_attempt={'id':command_id,'kind':kind,'argument':argument,'started_at':time.time(),'state':'unconfirmed'}
        data=self.post('command',id=command_id,kind=kind,argument=argument,wait=True)
        return self.finish_command(data,command_id,started,time.monotonic())
    def finish_command(self,data,command_id,started,posted):
        polls=0
        until=started+35
        while True:
            if data.get('id')!=command_id:raise TrialError('Kiosk Node command identity changed')
            if data['phase']=='failed':raise TrialError(data.get('error') or 'Kiosk Node operation failed')
            if data['phase']=='cancelled':
                with self.evidence_lock:self.last_attempt['state']='cancelled'
                raise TrialCancelled('Sorter command cancelled by Stop')
            if data['phase']=='done':
                with self.evidence_lock:self.last_attempt['state']='confirmed'
                self.last_command_timing={'post_ms':round((posted-started)*1000,3),
                    'wait_ms':round((time.monotonic()-posted)*1000,3),'wait_requests':polls,
                    'device':data.get('device_timing',{}),'http':data.get('_http_timing',{})}
                return data
            if data.get('machine_state')=='waiting_for_brass':
                # The CS7.2 reports this once per second while its feed sensor
                # is empty. Keep waiting while the bridge continues receiving
                # those messages; Stop remains independently dispatchable.
                until=max(until,time.monotonic()+6)
            if time.monotonic()>=until:break
            # The firmware waits up to one second for this exact command.
            # Only this read is repeated. The command POST is sent once.
            response=self.request('GET',f'/api/trial?owner={self.owner}&wait_id={command_id}')
            data=self.checked(response.json());data['_http_timing']=getattr(response,'cs72_timing',{});polls+=1
        raise TrialError('Command result not confirmed; do not repeat motion until machine state is checked')
    def capture(self):
        self.sequence+=1;command_id=self.sequence;started=time.monotonic()
        with self.evidence_lock:self.last_attempt={'id':command_id,'kind':'capture','argument':'','started_at':time.time(),'state':'unconfirmed'}
        # One POST normally returns both the exact capture state and its JPEG.
        # Never repeat this POST if the reply is lost or malformed.
        r=self.request('POST','/api/trial?nd=0',dict(action='command',owner=self.owner,boot=self.boot,
            id=command_id,kind='capture',argument='',wait=True,reply_image=True))
        posted=time.monotonic();inline=r.headers.get('Content-Type','').split(';')[0]=='image/jpeg'
        if inline:
            try:data=json.loads(r.headers['X-Trial-State'])
            except (KeyError,ValueError,TypeError) as e:raise TrialError('Capture response metadata missing or invalid') from e
            if not isinstance(data,dict):raise TrialError('Invalid capture response metadata')
            data=self.checked(data)
            if data.get('id')!=command_id or data.get('phase')!='done':raise TrialError('Image does not belong to a completed capture')
            self.last_command_timing={'post_ms':round((posted-started)*1000,3),'wait_ms':0,'wait_requests':0,'device':data.get('device_timing',{})}
            with self.evidence_lock:self.last_attempt['state']='confirmed'
            download_ms=None;request_count=1
        else:
            # A camera starting cold may exceed the bounded POST wait. Continue
            # reading the SAME command, then download its retained image once.
            data=self.finish_command(self.checked(r.json()),command_id,started,posted)
            download_started=time.monotonic()
            path=f'/trial.jpg?owner={self.owner}&boot={self.boot}&id={command_id}&nd=0'
            r=self.request('GET',path);download_ms=round((time.monotonic()-download_started)*1000,3)
            request_count=2+self.last_command_timing['wait_requests']
        if r.headers.get('X-TCP-Nodelay')!='0':
            raise TrialError('Image TCP setting did not verify; sorting paused before motion')
        crc_started=time.monotonic()
        self.validate_image(r,command_id,data['frame_sequence']);raw=r.content
        return raw,{'boot':self.boot,'command_id':command_id,'sequence':data['frame_sequence'],'received_ms':data['frame_ms'],'camera':data.get('camera',{}),
            'host_timing':{'command':dict(self.last_command_timing),'jpeg_download_ms':download_ms,
                'capture_exchange_ms':round((time.monotonic()-started)*1000,3),'inline_image':inline,'http_requests':request_count,
                'transfer':getattr(r,'cs72_timing',{}),'transfer_id':r.headers.get('X-Transfer-Id'),
                'tcp_nodelay':r.headers.get('X-TCP-Nodelay'),'crc_ms':round((time.monotonic()-crc_started)*1000,3),'jpeg_bytes':len(raw)}}

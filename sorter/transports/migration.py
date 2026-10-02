"""Explicit one-time import into the normal application's configuration."""
import copy,json,time
from pathlib import Path
from .esp_http import base_url
from .. import paths

def import_profile(config,data):
    if not isinstance(data,dict):raise ValueError('Choose a settings or sorter report JSON file')
    if isinstance(data.get('sorter'),dict):cfg=data['sorter'].get('config',{})
    else:
        entries=list(data.values())
        if len(entries)!=1:raise ValueError('Import a report for the individual sorter when the settings file contains multiple sorters.')
        cfg=entries[0]
    if not isinstance(cfg,dict):raise ValueError('Invalid sorter settings')
    address=base_url(cfg.get('esp_url',''));remote=cfg.get('remote',{});endpoint=base_url(remote.get('endpoint',''))
    crop=cfg.get('crop',{});h=copy.deepcopy(crop.get('hough',{}))
    if not isinstance(h,dict):raise ValueError('Invalid rim-detection settings')
    required=('dp','min_dist','param1','param2','min_radius','max_radius')
    if crop.get('mode')!='hough' or any(type(h.get(k)) not in (int,float) or h[k]<=0 for k in required):raise ValueError('Import requires a valid automatic rim-detection profile')
    if h['min_radius']>h['max_radius']:raise ValueError('Minimum rim radius exceeds maximum')
    if crop.get('primer_mode') not in ('none','hide','use'):raise ValueError('Invalid primer mask')
    radius=crop.get('primer_radius',0)
    if type(radius)is not int or not 0<=radius<=240:raise ValueError('Invalid primer radius')
    x,y=crop.get('x'),crop.get('y')
    if type(x)is not int or type(y)is not int or not 0<=x<1920 or not 0<=y<1080:raise ValueError('Invalid expected centre')
    h.update(_reference_width=1920,_expected_x=x,_expected_y=y)
    slots=cfg.get('slots',{});labels=cfg.get('labels',[])
    if not isinstance(slots,dict) or not isinstance(labels,list) or any(not isinstance(k,str) or not k.strip() for k in slots) or any(type(v)is not int or not 0<=v<=7 for v in slots.values()) or any(not isinstance(v,str) for v in labels):raise ValueError('Invalid bin assignments')
    floor=cfg.get('confidence_floor',80)
    if type(floor)is not int or not 0<=floor<=100:raise ValueError('Invalid confidence floor')
    settle=cfg.get('settle_ms',150)
    if type(settle)is not int or not 0<=settle<=5000:raise ValueError('Invalid settling delay')
    saved_machine=cfg.get('saved_machine',{})
    if not isinstance(saved_machine,dict):raise ValueError('Invalid saved machine settings')
    # Durable rollback snapshot before updating any normal application settings.
    backup=paths.app_data_dir()/('before-network-import-'+str(time.time_ns())+'.json')
    backup.parent.mkdir(parents=True,exist_ok=True)
    backup.write_text(json.dumps({'serial':config.serial,'api':config.api,'image_proc':config.image_proc,'remote_headstamps':config.remote_headstamps(),'active_model':config.settings.get_active_model_id()},indent=2),encoding='utf-8')
    config.serial['port']=address;config.serial['wifi_enabled']=True;config.serial['wifi_address']=address;config.serial['init_on_startup']=False
    # Do not import or send motor settings. Preserve the machine's live values;
    # restore the saved LED on connect using the existing application behavior.
    led=saved_machine.get('CameraLEDLevel')
    if type(led)is int and 0<=led<=255:config.serial.setdefault('init_settings',{})['cameraledlevel']=led
    config.api.update(endpoint_url=endpoint,model=str(remote.get('model','')),api_key=str(remote.get('api_key','')))
    config.image_proc.update(strategy='hough',hough=h,primer_mode=crop['primer_mode'],primer_radius=radius,settle_ms=settle)
    config.settings.clear_active_model();config.synchronize_remote_headstamps(list(dict.fromkeys([*labels,*slots])))
    config.set_remote_headstamp_slots(slots);config.set_run_confidence_floor(floor);config.set_run_auto_select_trays(False);config.save()
    return {'message':'Imported connection, crop and bin assignments. Connect to read the machine; no motor settings were sent.','backup':backup.name}

"""Saved operator profiles for explicit, stopped machine switching.

This registry is configuration groundwork, not a concurrent fleet scheduler.
"""
import copy
from .repository import ModelRepo

class SorterProfiles:
    def __init__(self, config):
        self.config = config
    def names(self):
        return sorted(self.config.settings.get('sorter_profiles', {}), key=str.casefold)
    def active(self):
        return self.config.settings.get('active_sorter_profile', '')
    def snapshot(self):
        c = self.config
        mid = c.settings.get_active_model_id()
        keys = ['run_confidence_floor','run_store_images','run_package_mode','run_package_size','run_auto_select_trays',c._package_slots_key()]
        if mid is not None: keys.append('use_parent_runtime:'+str(mid))
        return {'sections':copy.deepcopy(c.data), 'active_model':mid,
                'remote_labels':c.remote_headstamps(),
                'headstamps':c.headstamps, 'parents':c.parents_with_slots(),
                'run_settings':{k:c.settings.get(k) for k in keys}}
    def save(self, name):
        name = str(name or '').strip()
        if not name or len(name)>80: raise ValueError('Enter a sorter name of 1–80 characters.')
        c = self.config
        with c.db.transaction():
            profiles = c.settings.get('sorter_profiles', {})
            profiles[name] = self.snapshot()
            c.settings.set('sorter_profiles', profiles)
            c.settings.set('active_sorter_profile', name)
        return name
    def select(self, name):
        c = self.config
        with c.db.transaction():
            profiles = c.settings.get('sorter_profiles', {})
            if name not in profiles: raise ValueError('Saved sorter profile not found.')
            target = copy.deepcopy(profiles[name])
            mid = target['active_model']
            if mid is not None and ModelRepo(c.db).get(mid) is None:
                raise ValueError('The saved local model is no longer installed.')
            previous = self.active()
            if previous in profiles: profiles[previous] = self.snapshot()
            c.data = target['sections']
            if mid is None: c.settings.clear_active_model()
            else: c.settings.set_active_model_id(mid)
            c.synchronize_remote_headstamps([v['name'] for v in target['remote_labels']])
            c.set_remote_headstamp_slots({v['name']:v['slot'] for v in target['remote_labels']})
            if mid is not None:
                for v in target['headstamps']: c.set_headstamp_slot(v['name'],v['slot'])
                for v in target['parents']: c.set_parent_slot(v['id'],v['slot'])
            for k,v in target['run_settings'].items():
                if v is None: c.settings.delete(k)
                else: c.settings.set(k,v)
            c.save()
            c.settings.set('sorter_profiles',profiles)
            c.settings.set('active_sorter_profile',name)

"""Settings permitted to reach the controller, shared by all send paths."""
AIRDROP_TIMINGS = ('airdroppredelay', 'airdropdsignalduration', 'airdroppostdelay')
def settings_for_board(settings):
    result = dict(settings)
    enabled = bool(int(result.get('airdropenabled', 0)))
    result['airdropenabled'] = int(enabled)
    if not enabled:
        for key in AIRDROP_TIMINGS:
            result.pop(key, None)
    return result

BOARD_KEYS = dict(zip(
    ('FeedMotorCurrent','FeedMotorSpeed','FeedCycleSteps','SortMotorCurrent','SortMotorSpeed','SortSteps','NotificationDelay','SlotDropDelay','AirDropEnabled','AirDropPostDelay','AirDropPreDelay','AirDropSignalTime','FeedHomingOffset','SortHomingOffset','AutoMotorStandbyTimeout','CaseFanLevel','CameraLEDLevel','DebounceTimeout','DebouncePauseTime'),
    ('feedmotorcurrent','feedspeed','feedsteps','sortmotorcurrent','sortspeed','sortsteps','notificationdelay','slotdropdelay','airdropenabled','airdroppostdelay','airdroppredelay','airdropdsignalduration','feedhomingoffset','sorthomingoffset','automotorstandbytimeout','fan','cameraledlevel','debounceTimeout','debounceTime')))
def normalized_board_config(payload):
    return {BOARD_KEYS.get(key,key):value for key,value in payload.items()}

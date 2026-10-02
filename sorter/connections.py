"""Connection form semantics, independent of widgets and transport drivers."""
import ipaddress
from urllib.parse import urlparse

def wifi_address(value):
    value = str(value or '').strip()
    url = value if '://' in value else 'http://' + value
    from .transports.esp_http import base_url
    url = base_url(url)
    ipaddress.ip_address(urlparse(url).hostname)
    return url

def connection_settings(current, *, wifi_enabled, address='', usb_port=''):
    result = dict(current)
    result['wifi_enabled'] = bool(wifi_enabled)
    result['usb_port'] = str(usb_port or '').strip()
    if address:
        result['wifi_address'] = str(address).strip()
    if wifi_enabled:
        result['port'] = wifi_address(address or result.get('wifi_address', ''))
    else:
        if result['usb_port'].lower().startswith(('http://', 'https://')):
            raise ValueError('Select a USB port, or enable Kiosk Node.')
        result['port'] = result['usb_port']
    return result

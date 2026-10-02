import unittest
import sys
import types
from unittest.mock import Mock

try:
    import requests  # noqa: F401
except ImportError:  # Minimal source-test fallback; production pins requests.
    requests = types.ModuleType('requests')
    class Session:
        trust_env = False
        def close(self): pass
    requests.Session = Session
    requests.RequestException = Exception
    sys.modules['requests'] = requests

from sorter.transports.esp_http import ESP, TrialCancelled, VERSION


def state(**changes):
    value = {
        'kit': VERSION, 'boot': 'boot-1', 'owned': True, 'ready': True,
        'disconnects': 0, 'fault': False, 'id': 7, 'phase': 'serial',
        'response': '', 'machine_state': 'waiting_for_brass',
        'waiting_for_brass_count': 3, 'last_serial_line': 'waiting for brass',
    }
    value.update(changes)
    return value


class ESPWaitStopTests(unittest.TestCase):
    def esp(self):
        client = ESP('http://192.168.4.92')
        self.addCleanup(client.http.close)
        client.boot = 'boot-1'
        client.disconnects = 0
        return client

    def test_cancelled_command_is_not_reported_as_disconnect_fault(self):
        client = self.esp()
        client.last_attempt = {'id': 7, 'state': 'unconfirmed'}
        with self.assertRaisesRegex(TrialCancelled, 'cancelled by Stop'):
            client.finish_command(
                state(phase='cancelled', response='stop_sent'), 7, 0.0, 0.0
            )
        self.assertEqual(client.last_attempt.get('state'), 'cancelled')
        self.assertIsNone(client.first_fault)

    def test_emergency_stop_uses_independent_requests_and_confirms_dispatch(self):
        client = self.esp()
        replies = [state(stop_requested=True),
                   state(phase='cancelled', response='stop_sent',
                         machine_state='stop_dispatched', stop_requested=False)]
        calls = []

        def request(method, path, body=None, **kwargs):
            calls.append((method, path, body, kwargs))
            response = Mock()
            response.json.return_value = replies.pop(0)
            return response

        client.request = request
        result = client.emergency_stop()
        self.assertEqual(result['response'], 'stop_sent')
        self.assertEqual([call[0] for call in calls], ['POST', 'GET'])
        self.assertTrue(all(call[3]['fresh'] and call[3]['independent'] for call in calls))


if __name__ == '__main__':
    unittest.main()

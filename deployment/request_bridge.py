"""Private stdin-JSON bridge; invoke with /usr/bin/python3 -I -B.
No listener, command-line visitor data, email, or diagnostic output.
Input: {op: issue|submit, client_ip: str, session: str, fields?: object}.
Output: {ok: bool, status: int, token?: str, message?: str}.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parent
UNAVAILABLE = 'Requests are temporarily unavailable. Please try again later.'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key')
        result[key] = value
    return result


def timeout(_signum, _frame):
    raise TimeoutError()


def run():
    os.umask(0o077)
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(17)
    try:
        raw = sys.stdin.buffer.read(131073)
        if not raw or len(raw) > 131072:
            raise ValueError()
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        if not isinstance(data, dict) or data.get('op') not in ('issue', 'submit'):
            raise ValueError()
        keys = {'op', 'client_ip', 'session'}
        if data['op'] == 'submit':
            keys.add('fields')
        if set(data) != keys:
            raise ValueError()
        for key, maximum in (('client_ip', 128), ('session', 256)):
            if not isinstance(data[key], str) or not 0 < len(data[key]) <= maximum:
                raise ValueError()
    except (ValueError, UnicodeError):
        return {'ok': False, 'status': 400, 'message': 'Invalid request data.'}

    # Load the fixed sibling module, never a cwd/PYTHONPATH-selected store.
    spec = importlib.util.spec_from_file_location('qid_private_request_store', ROOT / 'request_store.py')
    store_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store_module)
    try:
        store = store_module.RequestStore(ROOT / 'private' / 'feature-requests.sqlite3')
        if data['op'] == 'issue':
            token = store.issue(data['client_ip'], data['session'])
            return {'ok': True, 'status': 200, 'token': token}
        fields = data['fields']
        if isinstance(fields, dict) and isinstance(fields.get('details'), str):
            fields['details'] = fields['details'].replace('\r\n', '\n')
        store.submit(data['client_ip'], data['session'], fields)
        return {'ok': True, 'status': 200}
    except store_module.RequestError as error:
        return {'ok': False, 'status': error.status, 'message': error.message}


if __name__ == '__main__':
    try:
        result = run()
    except Exception:
        result = {'ok': False, 'status': 503, 'message': UNAVAILABLE}
    finally:
        signal.alarm(0)
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')) + '\n')

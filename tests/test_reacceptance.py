"""Reviewer-owned acceptance probes. Synthetic data only; no external network/account writes.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_reacceptance -v
The only HTTP traffic goes to two ephemeral loopback servers.
"""
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wondercv as w
from tests.fakes import FakeAPI


class MemoryAPI(FakeAPI):
    """Models the already observed unguarded opus delete and shared section title."""
    def __init__(self):
        super().__init__(cv_tokens=('ACTIVE', 'TRASHED'))
        self.trash.add('TRASHED')
        for cv in self.cvs.values():
            sub = cv['submodule_info']
            sub.update(opus_ids=[], opus_title='作品集')
        for token in ('ACTIVE', 'TRASHED'):
            sub = self.cvs[token]['submodule_info']
            sub['opus_ids'] = [888]
            sub['custom_sections_ids'] = [999]
            sub['custom_sections_titles'] = {'999_title': 'original'}
            sub['sections_position'].extend([
                {'id': None, 'type': 'opus', 'info_ids': []},
                {'id': 999, 'type': 'custom', 'info_ids': []},
            ])
        self.objects[('opus', 888)] = {
            'id': 888, 'title': 'original', 'url': 'https://example.test/work',
            'qrcode': 'https://example.test/qr.png', 'language': 'cn',
        }

    def _handle(self, method, path, params, body):
        if method == 'GET' and path == '/v3/cvs/TRASHED':
            raise w.WcvError('trashed resumes cannot be read', code=80120)
        if method == 'GET' and path == '/v3/opus':
            ids = [int(x) for x in params.get('ids', '').split(',') if x]
            return {'opus': [copy.deepcopy(self.objects[('opus', i)]) for i in ids
                             if ('opus', i) in self.objects]}
        match = re.fullmatch(r'/v3/opus/(\d+)', path)
        if match:
            key = ('opus', int(match.group(1)))
            if method == 'DELETE':
                self.objects.pop(key, None)  # Real endpoint has no reference protection.
                return None
            if method == 'PUT':
                self.objects[key].update(body)
                return {'opus': copy.deepcopy(self.objects[key])}
        if method == 'PUT' and path == '/v3/custom_sections/999':
            for token in ('ACTIVE', 'TRASHED'):
                self.cvs[token]['submodule_info']['custom_sections_titles']['999_title'] = body['section_title']
            return None
        if method == 'GET' and path.endswith('/download_url'):
            return {'url': 'https://synthetic.oss-cn-example.aliyuncs.com/cv.pdf'}
        return super()._handle(method, path, params, body)


def invoke(api, *argv):
    args = w.build_parser().parse_args(list(argv))
    return w.run(args, api)


class SafetyAcceptance(unittest.TestCase):
    def test_trashed_source_opus_is_not_deleted(self):
        api = MemoryAPI()
        try:
            invoke(api, 'opus', 'rm', 'ACTIVE', '888')
        except w.WcvError:
            pass  # Conservative refusal is an acceptable safe outcome.
        self.assertIn(('opus', 888), api.objects, 'Deleting a copy destroyed a trashed source object')
        self.assertEqual(api.cvs['TRASHED']['submodule_info']['opus_ids'], [888])

    def test_orphan_cleanup_cannot_delete_a_trashed_reference(self):
        api = MemoryAPI()
        api.cvs['ACTIVE']['submodule_info']['opus_ids'] = []
        try:
            invoke(api, 'orphan', 'clean', 'opus', '888')
        except w.WcvError:
            pass
        self.assertIn(('opus', 888), api.objects, 'orphan clean ignored an unreadable trashed holder')

    def test_shared_title_in_trash_is_not_modified(self):
        api = MemoryAPI()
        try:
            invoke(api, 'section', 'rename', 'ACTIVE', '999', '--title', 'changed')
        except w.WcvError:
            pass
        self.assertEqual(api.cvs['TRASHED']['submodule_info']['custom_sections_titles']['999_title'],
                         'original', 'Renaming a copy changed its trashed source')

    def test_title_only_edit_keeps_existing_qrcode(self):
        api = MemoryAPI()
        invoke(api, 'opus', 'set', 'ACTIVE', '888', '--title', 'changed')
        self.assertEqual(api.objects[('opus', 888)]['qrcode'], 'https://example.test/qr.png')

    def test_first_post_lost_response_has_actionable_recovery(self):
        api = FakeAPI()
        api.inject_after('POST', '/v3/works', 'unknown')
        with self.assertRaises(w.WcvError) as cm:
            invoke(api, 'work', 'add', 'AAAA0001', '--title', 'synthetic-lost-response')
        self.assertTrue(cm.exception.hint, 'First-phase result unknown has no public recovery guidance')
        # A green result here is necessary but not sufficient: independently exercise
        # the documented recovery command to locate/reconcile the actual orphan.

    def test_partial_profile_update_is_not_reported_as_no_write(self):
        class ProfileAPI:
            def __init__(self):
                self.profile = {'username': 'before', 'format_pref': {}}
            def call(self, method, path, params=None, body=None):
                if method == 'GET':
                    return {'profile': copy.deepcopy(self.profile)}
                if 'format_pref' in body:
                    raise w.WcvError('synthetic later-stage rejection', state='none')
                self.profile.update(body)
        api = ProfileAPI()
        with self.assertRaises(w.WcvError) as cm:
            invoke(api, 'profile', 'set', 'SYNTHETIC', 'username=after', 'age_birthday=age')
        self.assertEqual(api.profile['username'], 'after')
        self.assertIn(cm.exception.state, ('partial', 'unknown'), 'Earlier successful write was hidden')

    def test_export_does_not_clobber_file_created_during_download(self):
        api = MemoryAPI()
        with tempfile.TemporaryDirectory(prefix='wcv-review-') as directory:
            out = Path(directory) / 'cv.pdf'
            def fetch(_url):
                out.write_bytes(b'new file created by another process')
                return b'%PDF-1.7\nsynthetic\n%%EOF\n'
            try:
                w.export_resume(api, 'ACTIVE', 'pdf', str(out), force=False, fetch=fetch)
            except w.WcvError:
                pass
            self.assertEqual(out.read_bytes(), b'new file created by another process')
            self.assertFalse(list(Path(directory).glob('.wcv-*')), 'Leftover temporary file')

    def test_authenticated_api_redirect_never_leaks_token_across_origins(self):
        received = []
        marker = 'SYNTHETIC-TOKEN-NOT-A-CREDENTIAL'
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                body = json.dumps({'code': 1000, 'data': {}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        destination = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
        class Origin(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{destination.server_port}/sink')
                self.send_header('Content-Length', '0')
                self.end_headers()
            def log_message(self, *args):
                pass
        origin = ThreadingHTTPServer(('127.0.0.1', 0), Origin)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (origin, destination)]
        for thread in threads:
            thread.start()
        try:
            with mock.patch.object(w, 'API', f'http://127.0.0.1:{origin.server_port}'):
                try:
                    w.Client(marker).call('GET', '/redirect')
                except w.WcvError:
                    pass
            self.assertNotIn(marker, received, 'Authorization crossed the origin boundary')
        finally:
            for server in (origin, destination):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

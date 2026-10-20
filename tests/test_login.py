"""QR login at the public CLI seam. All network responses and credentials are synthetic."""
import base64
import contextlib
import io
import itertools
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import wondercv as w

JWT = 'synthetic-login-credential-not-a-real-jwt'
OLD = 'synthetic-existing-credential'
IMAGE = b'\xff\xd8\xff\xe0synthetic-official-qr-image'


class Reply(io.BytesIO):
    status = 200


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='wcv-login-test-')
        self.addCleanup(self.directory.cleanup)
        self.token_file = Path(self.directory.name) / 'token'
        self.patchers = [mock.patch.object(w, 'TOKEN_FILE', str(self.token_file)),
                         mock.patch.dict(os.environ, {'WONDERCV_TOKEN': ''}),
                         mock.patch.object(w.time, 'sleep'),
                         mock.patch.object(w.urllib.request, 'urlopen', side_effect=self.network)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        self.polls = [{'code': 1000, 'data': {'jwt': JWT, 'jwt_timestamp': int(time.time()) + 86400,
                                             'new_account': False}}]
        self.device_code = 1000
        self.valid_token = True
        self.bad_base64 = False
        self.bad_image = False
        self.image = IMAGE

    def network(self, request, *args, **kwargs):
        url = urllib.parse.urlparse(request.full_url)
        method = request.get_method()
        self.calls.append((method, url.path))
        if url.path == '/auth/v3/wechat_wxa_qrcode':
            self.scene = urllib.parse.parse_qs(url.query)['scene_str'][0]
            self.assertTrue(self.scene.startswith('login_'))
            self.assertEqual(urllib.parse.parse_qs(url.query)['account_type'], ['wechat_miniprogram'])
            self.assertEqual(url.hostname, 'api.wondercv.com')
            self.assertFalse(request.get_header('Authorization'))
            body = 'invalid base64!' if self.bad_base64 else base64.b64encode(b'not an image' if self.bad_image else self.image).decode()
            data = {'code': 1000, 'data': {'body': body}}
        elif url.path == '/cv/v3/auth/wechat_scan_users/wxa_result':
            self.assertEqual(urllib.parse.parse_qs(url.query)['scene_str'][0], self.scene)
            data = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        elif url.path == '/cv/v3/auth/trusted_devices':
            self.assertEqual(method, 'POST')
            self.assertEqual(request.get_header('Authorization'), JWT)
            body = json.loads(request.data)
            self.assertTrue(body['device_number'])
            self.assertIn('WonderCV CLI', body['device_name'])
            data = {'code': self.device_code, 'data': {}, 'message': 'synthetic device response'}
        elif url.path == '/cv/v3/cvs':
            self.assertEqual(request.get_header('Authorization'), JWT)
            data = {'code': 1000 if self.valid_token else 2001, 'data': {'cvs': [], 'total_count': 0}}
        else:
            raise AssertionError('Unexpected request: ' + url.path)
        return Reply(json.dumps(data).encode())

    def invoke(self, *args, tty=False):
        out, err = io.StringIO(), io.StringIO()
        err.isatty = lambda: tty
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = w.main(['--json', *args])
            except SystemExit as error:
                rc = error.code
        self.assertNotIn(JWT, out.getvalue() + err.getvalue())
        self.assertNotIn(OLD, out.getvalue() + err.getvalue())
        return rc, out.getvalue(), err.getvalue()

    def start(self, replace=False):
        args = ['auth', 'login', '--start', '--image-only']
        if replace:
            args.append('--replace')
        rc, out, err = self.invoke(*args)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def old_token(self):
        self.token_file.write_text(OLD)
        self.token_file.chmod(0o600)

    def test_start_needs_no_credential_and_returns_real_image_artifact(self):
        data = self.start()
        self.assertEqual(data['status'], 'waiting')
        image = Path(data['qr_path'])
        self.assertEqual(image.read_bytes(), IMAGE)
        self.assertEqual(stat.S_IMODE(image.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(image.parent.stat().st_mode), 0o700)
        self.assertFalse(self.token_file.exists())
        self.assertEqual(self.calls, [('GET', '/auth/v3/wechat_wxa_qrcode')])
        self.assertGreaterEqual(len(self.scene[len('login_'):]), 24)
        self.assertNotIn(self.scene, json.dumps(data))

    def test_wait_registers_device_validates_then_saves_without_printing_token(self):
        start = self.start()
        rc, out, err = self.invoke('auth', 'wait')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['status'], 'authenticated')
        self.assertEqual(self.token_file.read_text(), JWT)
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertFalse(Path(start['qr_path']).exists())
        device = self.calls.index(('POST', '/cv/v3/auth/trusted_devices'))
        verify = self.calls.index(('GET', '/cv/v3/cvs'))
        self.assertLess(device, verify)

    def test_pending_is_polled_at_the_official_interval(self):
        self.polls.insert(0, {'code': 201201, 'message': 'waiting'})
        self.start()
        rc, _, err = self.invoke('auth', 'wait')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls.count(('GET', '/cv/v3/auth/wechat_scan_users/wxa_result')), 2)
        w.time.sleep.assert_called_with(3)

    def test_existing_credential_requires_explicit_replace(self):
        self.old_token()
        rc, _, _ = self.invoke('auth', 'login', '--start', '--image-only')
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_device_refusal_never_saves_or_uses_provisional_token(self):
        self.old_token()
        self.device_code = 3000
        self.start(replace=True)
        rc, _, err = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertIn('设备', err)
        self.assertEqual(self.token_file.read_text(), OLD)
        self.assertNotIn(('GET', '/cv/v3/cvs'), self.calls)
        self.assertFalse(any(method == 'DELETE' for method, _ in self.calls))

    def test_failed_token_validation_preserves_old_credential(self):
        self.old_token()
        self.valid_token = False
        self.start(replace=True)
        rc, _, _ = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_new_account_requires_website_completion(self):
        self.polls[0]['data']['new_account'] = True
        self.start()
        rc, _, err = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertIn('官网', err)
        self.assertFalse(self.token_file.exists())
        self.assertNotIn(('POST', '/cv/v3/auth/trusted_devices'), self.calls)

    def test_cancel_does_not_touch_existing_credential(self):
        self.old_token()
        data = self.start(replace=True)
        rc, out, err = self.invoke('auth', 'cancel')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['status'], 'cancelled')
        self.assertEqual(self.token_file.read_text(), OLD)
        self.assertFalse(Path(data['qr_path']).exists())
        rc, _, _ = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)

    def test_expiry_does_not_overwrite_old_credential(self):
        self.old_token()
        data = self.start(replace=True)
        with mock.patch.object(w.time, 'time', return_value=time.time() + 120):
            rc, _, err = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertIn('过期', err)
        self.assertEqual(self.token_file.read_text(), OLD)
        self.assertFalse(Path(data['qr_path']).exists())

    def test_wait_timeout_returns_waiting_not_fake_success(self):
        self.polls = [{'code': 201201, 'message': 'waiting'}]
        self.start()
        with mock.patch.object(w.time, 'monotonic', side_effect=itertools.count(0, 3)):
            rc, out, err = self.invoke('auth', 'wait', '--timeout', '5')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['status'], 'waiting')
        self.assertFalse(self.token_file.exists())

    def test_timeout_does_not_return_a_link_to_a_cancelled_session(self):
        self.old_token()
        started = self.start(replace=True)
        self.polls = [{'code': 201201, 'message': 'waiting'}]
        network = self.network
        def cancel_during_last_poll(request, *args, **kwargs):
            response = network(request, *args, **kwargs)
            self.invoke('auth', 'cancel')
            return response
        with mock.patch.object(w.urllib.request, 'urlopen', side_effect=cancel_during_last_poll), \
                mock.patch.object(w.time, 'monotonic', side_effect=[0, 0, 1]):
            rc, out, err = self.invoke('auth', 'wait', '--timeout', '1')
        self.assertNotEqual(rc, 0)
        self.assertIn('取消', err)
        self.assertNotIn('waiting', out)
        self.assertFalse(Path(started['qr_path']).exists())
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_timeout_does_not_report_an_expired_image_as_waiting(self):
        self.old_token()
        started = self.start(replace=True)
        self.polls = [{'code': 201201, 'message': 'waiting'}]
        network = self.network
        session = Path(started['qr_path']).parent / 'session.json'
        def expire_during_last_poll(request, *args, **kwargs):
            response = network(request, *args, **kwargs)
            value = json.loads(session.read_text())
            value['expires_at'] = time.time() - 1
            session.write_text(json.dumps(value))
            return response
        with mock.patch.object(w.urllib.request, 'urlopen', side_effect=expire_during_last_poll), \
                mock.patch.object(w.time, 'monotonic', side_effect=[0, 0, 1]):
            rc, out, err = self.invoke('auth', 'wait', '--timeout', '1')
        self.assertNotEqual(rc, 0)
        self.assertIn('过期', err)
        self.assertNotIn('waiting', out)
        self.assertFalse(Path(started['qr_path']).exists())
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_network_failure_preserves_old_credential(self):
        self.old_token()
        self.start(replace=True)
        with mock.patch.object(w.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')):
            rc, _, err = self.invoke('auth', 'wait')
        self.assertEqual(rc, w.EXIT_NETWORK, err)
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_unknown_business_code_is_not_silently_polled_forever(self):
        self.polls = [{'code': 987654, 'message': 'synthetic unexpected response'}]
        self.start()
        rc, _, _ = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.calls.count(('GET', '/cv/v3/auth/wechat_scan_users/wxa_result')), 1)

    def test_invalid_base64_is_rejected_without_creating_an_image(self):
        self.bad_base64 = True
        rc, _, err = self.invoke('auth', 'login', '--start', '--image-only')
        self.assertEqual(rc, w.EXIT_NETWORK, err)
        self.assertFalse(self.token_file.exists())
        self.assertFalse(list(self.token_file.parent.glob('login/qr-*')))

    def test_bad_image_is_not_returned_as_a_qr(self):
        self.bad_image = True
        rc, _, _ = self.invoke('auth', 'login', '--start', '--image-only')
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.token_file.exists())

    def test_failed_atomic_save_keeps_old_token(self):
        self.old_token()
        self.start(replace=True)
        replace = w.os.replace
        def fail_token(source, destination):
            if str(destination) == str(self.token_file):
                raise OSError('synthetic disk failure')
            return replace(source, destination)
        with mock.patch.object(w.os, 'replace', side_effect=fail_token):
            rc, _, _ = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_unknown_terminal_gets_an_openable_file_link_without_graphics(self):
        with mock.patch.dict(os.environ, {'TERM': 'xterm-256color', 'TERM_PROGRAM': 'unknown'}, clear=True):
            rc, out, err = self.invoke('auth', 'login', '--start', tty=True)
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data['qr_uri'], Path(data['qr_path']).as_uri())
        self.assertIn(data['qr_uri'], err)
        self.assertNotIn('\x1b', out + err)
        self.assertNotIn('█', err)

    def test_iterm_terminal_displays_the_exact_official_image_without_polluting_json(self):
        with mock.patch.dict(os.environ, {'TERM': 'xterm-256color', 'TERM_PROGRAM': 'iTerm.app'}, clear=True):
            rc, out, err = self.invoke('auth', 'login', '--start', tty=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['status'], 'waiting')
        self.assertNotIn('\x1b', out)
        match = re.search(r'\x1b\]1337;File=([^:]+):([^\x07]+)\x07', err)
        self.assertIsNotNone(match)
        self.assertIn('inline=1', match.group(1))
        self.assertEqual(base64.b64decode(match.group(2)), IMAGE)

    def test_kitty_sends_png_in_bounded_native_image_chunks(self):
        self.image = b'\x89PNG\r\n\x1a\n' + b'synthetic-png-pixels' * 500
        with mock.patch.dict(os.environ, {'TERM': 'xterm-kitty', 'TERM_PROGRAM': 'kitty'}, clear=True):
            rc, out, err = self.invoke('auth', 'login', '--start', tty=True)
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        packets = re.findall(r'\x1b_G([^;]+);([^\x1b]+)\x1b\\', err)
        self.assertGreater(len(packets), 1)
        self.assertIn('a=T', packets[0][0])
        self.assertIn('f=100', packets[0][0])
        self.assertIn('m=0', packets[-1][0])
        self.assertTrue(all(len(payload) <= 4096 for _, payload in packets))
        self.assertEqual(base64.b64decode(''.join(payload for _, payload in packets)), self.image)
        self.assertEqual(Path(data['qr_path']).read_bytes(), self.image)
        self.assertNotIn('\x1b', out)

    def test_redirected_unsupported_and_multiplexed_outputs_fall_back_to_links(self):
        cases = [({'TERM': 'xterm-256color', 'TERM_PROGRAM': 'iTerm.app'}, False, False),
                 ({'TERM': 'dumb', 'TERM_PROGRAM': 'iTerm.app'}, True, False),
                 ({'TERM': 'screen-256color', 'TERM_PROGRAM': 'iTerm.app'}, True, False),
                 ({'TERM': 'xterm-256color', 'TERM_PROGRAM': 'iTerm.app', 'TMUX': 'synthetic'}, True, False),
                 ({'TERM': 'xterm-256color', 'TERM_PROGRAM': 'iTerm.app', 'STY': 'synthetic'}, True, False),
                 ({'TERM': 'xterm-256color', 'COLORTERM': 'truecolor'}, True, False),
                 ({'TERM': 'xterm-256color', 'TERM_PROGRAM': 'iTerm.app'}, True, True)]
        for env, tty, image_only in cases:
            with self.subTest(env=env, tty=tty, image_only=image_only), mock.patch.dict(os.environ, env, clear=True):
                args = ['auth', 'login', '--start'] + (['--image-only'] if image_only else [])
                rc, out, err = self.invoke(*args, tty=tty)
                self.assertEqual(rc, 0, err)
                self.assertIn(json.loads(out)['qr_uri'], err)
                self.assertNotIn('\x1b', out + err)
                self.invoke('auth', 'cancel')

    def test_kitty_without_a_jpeg_decoder_falls_back_without_installing_packages(self):
        with mock.patch.dict(os.environ, {'TERM': 'xterm-kitty', 'TERM_PROGRAM': 'kitty'}, clear=True), \
                mock.patch.dict('sys.modules', {'PIL': None}):
            rc, out, err = self.invoke('auth', 'login', '--start', tty=True)
        self.assertEqual(rc, 0, err)
        self.assertIn(json.loads(out)['qr_uri'], err)
        self.assertNotIn('\x1b', out + err)

    def test_remote_file_link_is_explicitly_identified_as_remote(self):
        with mock.patch.dict(os.environ, {'SSH_CONNECTION': 'synthetic'}, clear=True):
            rc, out, err = self.invoke('auth', 'login', '--start')
        self.assertEqual(rc, 0, err)
        self.assertIn('file://', json.loads(out)['qr_uri'])
        self.assertIn('远端', err)
        self.assertIn('下载', err)

    def test_file_link_percent_encodes_spaces_and_non_ascii(self):
        self.token_file = self.token_file.parent / '中文 configuration' / 'token'
        with mock.patch.object(w, 'TOKEN_FILE', str(self.token_file)):
            data = self.start()
        self.assertEqual(Path(urllib.request.url2pathname(urllib.parse.urlparse(data['qr_uri']).path)),
                         Path(data['qr_path']))
        self.assertNotIn(' ', data['qr_uri'])
        self.assertIn('%', data['qr_uri'])

    def test_wezterm_uses_the_original_image_in_the_supported_inline_protocol(self):
        with mock.patch.dict(os.environ, {'TERM': 'xterm-256color', 'TERM_PROGRAM': 'WezTerm'}, clear=True):
            rc, out, err = self.invoke('auth', 'login', '--start', tty=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['status'], 'waiting')
        self.assertIn('\x1b]1337;File=', err)
        self.assertIn(base64.b64encode(self.image).decode(), err)

    def test_cancel_can_clear_a_malformed_session_without_touching_credentials(self):
        self.old_token()
        directory = self.token_file.parent / 'login'
        directory.mkdir(mode=0o700)
        session = directory / 'session.json'
        session.write_text('{broken-json')
        session.chmod(0o600)
        rc, out, err = self.invoke('auth', 'cancel')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['status'], 'cancelled')
        self.assertFalse(session.exists())
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_response_after_expiry_does_not_register_device_or_save(self):
        self.old_token()
        data = self.start(replace=True)
        session = Path(data['qr_path']).parent / 'session.json'
        network = self.network
        def expire_during_poll(request, *args, **kwargs):
            response = network(request, *args, **kwargs)
            if '/wechat_scan_users/wxa_result' in request.full_url:
                value = json.loads(session.read_text())
                value['expires_at'] = time.time() - 1
                session.write_text(json.dumps(value))
            return response
        with mock.patch.object(w.urllib.request, 'urlopen', side_effect=expire_during_poll):
            rc, _, err = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertIn('过期', err)
        self.assertEqual(self.token_file.read_text(), OLD)
        self.assertNotIn(('POST', '/cv/v3/auth/trusted_devices'), self.calls)

    def test_environment_precedence_is_explained_without_printing_its_value(self):
        self.start()
        with mock.patch.dict(os.environ, {'WONDERCV_TOKEN': OLD}):
            rc, out, err = self.invoke('auth', 'wait')
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertTrue(result['environment_override'])
        self.assertIn('WONDERCV_TOKEN', result['hint'])
        self.assertEqual(self.token_file.read_text(), JWT)

    def test_cancel_during_qr_creation_cannot_later_save_credentials(self):
        self.old_token()
        network = self.network
        def cancel_during_creation(request, *args, **kwargs):
            response = network(request, *args, **kwargs)
            if '/auth/v3/wechat_wxa_qrcode' in request.full_url:
                rc, _, err = self.invoke('auth', 'cancel')
                self.assertEqual(rc, 0, err)
            return response
        with mock.patch.object(w.urllib.request, 'urlopen', side_effect=cancel_during_creation):
            rc, _, _ = self.invoke('auth', 'login', '--image-only', '--replace')
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.token_file.read_text(), OLD)
        self.assertNotIn(('POST', '/cv/v3/auth/trusted_devices'), self.calls)

    def test_expiry_during_verification_prevents_final_credential_save(self):
        self.old_token()
        data = self.start(replace=True)
        session = Path(data['qr_path']).parent / 'session.json'
        network = self.network
        def expire_during_verification(request, *args, **kwargs):
            response = network(request, *args, **kwargs)
            if urllib.parse.urlparse(request.full_url).path == '/cv/v3/cvs':
                value = json.loads(session.read_text())
                value['expires_at'] = time.time() - 1
                session.write_text(json.dumps(value))
            return response
        with mock.patch.object(w.urllib.request, 'urlopen', side_effect=expire_during_verification):
            rc, _, err = self.invoke('auth', 'wait')
        self.assertNotEqual(rc, 0)
        self.assertIn('过期', err)
        self.assertEqual(self.token_file.read_text(), OLD)

    def test_miniprogram_returns_the_unmodified_official_image(self):
        rc, out, err = self.invoke('auth', 'login', '--start', '--image-only')
        self.assertEqual(rc, 0, err)
        image = Path(json.loads(out)['qr_path'])
        self.assertEqual(image.read_bytes(), IMAGE)
        self.assertNotIn(('GET', '/auth/v3/wechat_qr_codes'), self.calls)

    def test_miniprogram_waits_on_the_matching_endpoint_and_saves(self):
        rc, _, err = self.invoke('auth', 'login', '--start', '--image-only')
        self.assertEqual(rc, 0, err)
        rc, out, err = self.invoke('auth', 'wait')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['status'], 'authenticated')
        self.assertIn(('GET', '/cv/v3/auth/wechat_scan_users/wxa_result'), self.calls)
        self.assertNotIn(('GET', '/cv/v3/auth/wechat_scan_users/result'), self.calls)
        self.assertEqual(self.token_file.read_text(), JWT)

    def test_two_pending_logins_do_not_replace_each_other(self):
        first = self.start()
        rc, _, _ = self.invoke('auth', 'login', '--start', '--image-only')
        self.assertNotEqual(rc, 0)
        self.assertTrue(Path(first['qr_path']).exists())
        self.assertEqual(self.calls.count(('GET', '/auth/v3/wechat_wxa_qrcode')), 1)


if __name__ == '__main__':
    unittest.main()

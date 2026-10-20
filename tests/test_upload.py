"""返工阶段 B 的上传本地测试（不访问真实 OSS）。运行：python3 -m unittest tests.test_upload -v

签名由测试内独立实现重算（OSS V1 HMAC-SHA1），并通过本地回环 HTTP 服务器验证；生产路径仍强制 https 与 .aliyuncs.com。
"""
import base64
import contextlib
import hashlib
import hmac
import http.server
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wondercv as w  # noqa: E402
from tests.fakes import RichFake  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
JPG = b'\xff\xd8\xff' + b'\x00' * 64
CREDS = {'access_key_id': 'AKID-SYNTHETIC', 'access_key_secret': 'SECRET-SYNTHETIC',
         'security_token': 'STS-SYNTHETIC', 'bucket_name': 'prod-file-uploaded',
         'endpoint': 'oss-cn-beijing.aliyuncs.com', 'region': 'oss-cn-beijing'}


def independent_v1_signature(secret, method, content_type, date, token, resource):
    """与 OSS V1 规范独立编写的签名计算（不复用被测代码）。"""
    to_sign = f'{method}\n\n{content_type}\n{date}\nx-oss-security-token:{token}\n{resource}'
    return base64.b64encode(hmac.new(secret.encode(), to_sign.encode(), hashlib.sha1).digest()).decode()


class _VerifyingOSS(http.server.BaseHTTPRequestHandler):
    """本地回环 OSS 替身：按 V1 规范重算签名，签名正确返回 200，否则 403。"""
    secret = CREDS['access_key_secret']
    received = None

    def do_PUT(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        auth = self.headers.get('Authorization', '')
        expected = independent_v1_signature(self.secret, 'PUT', self.headers.get('Content-Type'),
                                            self.headers.get('Date'), self.headers.get('x-oss-security-token'),
                                            self.path)  # 回环路径即 /<bucket>/<key>，与 OSS 资源路径一致
        ok = auth == f"OSS {CREDS['access_key_id']}:{expected}" and body in (PNG, JPG)
        self.received.append((self.path, self.headers.get('Content-Type'), len(body), ok))
        self.send_response(200 if ok else 403)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


class SigningTest(unittest.TestCase):
    def test_request_signature_matches_independent_computation(self):
        url, headers = w.oss_put_request(CREDS, 'cv/opus/abc.png', PNG, 'image/png', date='Mon, 01 Jan 2024 00:00:00 GMT')
        self.assertEqual(url, 'https://prod-file-uploaded.oss-cn-beijing.aliyuncs.com/cv/opus/abc.png')
        expected = independent_v1_signature(CREDS['access_key_secret'], 'PUT', 'image/png',
                                            'Mon, 01 Jan 2024 00:00:00 GMT', CREDS['security_token'],
                                            '/prod-file-uploaded/cv/opus/abc.png')
        self.assertEqual(headers['Authorization'], f"OSS {CREDS['access_key_id']}:{expected}")
        self.assertEqual(headers['x-oss-security-token'], CREDS['security_token'])

    def test_loopback_oss_accepts_signed_request_and_rejects_wrong_secret(self):
        received = []
        handler = type('H', (_VerifyingOSS,), {'received': received})
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for secret, expect in ((CREDS['access_key_secret'], 200), ('WRONG', 403)):
                creds = dict(CREDS, access_key_secret=secret)
                _, headers = w.oss_put_request(creds, 'cv/school_logs/x.jpg', JPG, 'image/jpeg')
                req = w.urllib.request.Request(f'http://127.0.0.1:{server.server_port}/prod-file-uploaded/cv/school_logs/x.jpg',
                                               data=JPG, method='PUT', headers=headers)
                try:
                    with w.urllib.request.urlopen(req, timeout=10) as resp:
                        status = resp.status
                except w.urllib.error.HTTPError as e:
                    status = e.code
                self.assertEqual(status, expect)
            self.assertEqual([r[3] for r in received], [True, False])
        finally:
            server.shutdown()
            server.server_close()


class ImageAndCredentialTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='wcv-img-')

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _file(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_type_is_decided_by_content_not_extension(self):
        self.assertEqual(w.read_image(self._file('a.png', PNG))[1:], ('png', 'image/png'))
        self.assertEqual(w.read_image(self._file('b.jpg', JPG))[1:], ('jpg', 'image/jpeg'))
        self.assertEqual(w.read_image(self._file('c.gif', PNG))[1:], ('png', 'image/png'))
        with self.assertRaises(w.WcvError) as cm:
            w.read_image(self._file('d.png', b'GIF89a' + b'\x00' * 10))
        self.assertEqual(cm.exception.exit_code, w.EXIT_INPUT)

    def test_size_limit_and_empty_file(self):
        with self.assertRaises(w.WcvError):
            w.read_image(self._file('big.png', PNG + b'\x00' * w.UPLOAD_MAX_BYTES))
        with self.assertRaises(w.WcvError):
            w.read_image(self._file('empty.png', b''))
        with self.assertRaises(w.WcvError):
            w.read_image(os.path.join(self.dir, 'missing.png'))

    def test_credentials_must_point_to_aliyun_oss(self):
        class Stub:
            def __init__(self, data):
                self.data = data

            def call(self, method, path, params=None, body=None):
                return self.data
        for bad in ({'endpoint': 'evil.example.com', 'bucket_name': 'prod-file-uploaded'},
                    {'endpoint': 'oss-cn-beijing.aliyuncs.com/x', 'bucket_name': 'prod-file-uploaded'},
                    {'endpoint': 'oss-cn-beijing.aliyuncs.com', 'bucket_name': 'Bad_Bucket'}):
            with self.subTest(bad=bad), self.assertRaises(w.WcvError):
                w.upload_credentials(Stub(dict(CREDS, **bad)))
        self.assertEqual(w.upload_credentials(Stub(CREDS))['bucket_name'], 'prod-file-uploaded')

    def test_upload_url_must_be_https_aliyun(self):
        with self.assertRaises(w.WcvError):
            w._check_upload_url('http://prod-file-uploaded.oss-cn-beijing.aliyuncs.com/x.png')
        with self.assertRaises(w.WcvError):
            w._check_upload_url('https://evil.example.com/x.png')

    def test_key_prefix_per_kind_and_failure_returns_no_url(self):
        class Stub:
            def call(self, method, path, params=None, body=None):
                return CREDS
        keys = []

        def fake_open(req, timeout=None):
            keys.append(req.full_url)
            raise w.urllib.error.HTTPError(req.full_url, 403, 'denied', {}, None)
        with mock.patch('urllib.request.urlopen', side_effect=fake_open), self.assertRaises(w.WcvError):
            w.upload_image(Stub(), 'opus', self._file('q.png', PNG))
        self.assertTrue(keys[0].startswith('https://prod-file-uploaded.oss-cn-beijing.aliyuncs.com/cv/opus/'))
        self.assertTrue(keys[0].endswith('.png'))


class _ProfileStub(RichFake):
    """补充个人信息与校徽接口的假服务端（用于上传后的挂载测试）。"""

    def __init__(self):
        super().__init__(cv_tokens=('AAAA0001',))
        self.profile = {'username': '测试', 'format_pref': {}, 'avatar_url': 'https://x/old.png'}
        self.opus_ids_ = None

    def _handle(self, method, path, params, body):
        if path == '/v3/cvs/AAAA0001/profile' and method == 'GET':
            return {'profile': dict(self.profile)}
        if path == '/v3/cvs/AAAA0001/membership_profile' and method == 'PUT':
            self.profile.update(body)
            return None
        if path == '/v3/cvs/AAAA0001/membership_cv' and method == 'PUT':
            self.cvs['AAAA0001']['basic_info'].update(school_logo_url=body['school_logo_url'],
                                                      school_logo_id=body['school_logo_id'])
            return None
        return super()._handle(method, path, params, body)


class UploadFlowTest(unittest.TestCase):
    def setUp(self):
        self.api = _ProfileStub()
        self.dir = tempfile.mkdtemp(prefix='wcv-flow-')
        self.png = os.path.join(self.dir, 'a.png')
        with open(self.png, 'wb') as f:
            f.write(PNG)
        self.patch = mock.patch.object(w, 'upload_image', side_effect=lambda c, kind, path:
                                       f'https://prod-file-uploaded.oss-cn-beijing.aliyuncs.com/{kind}/fake.png')

    def tearDown(self):
        os.unlink(self.png)
        os.rmdir(self.dir)

    def test_avatar_set_then_remove(self):
        with self.patch:
            w.set_avatar(self.api, 'AAAA0001', self.png)
        self.assertEqual(self.api.profile['avatar_url'], 'https://prod-file-uploaded.oss-cn-beijing.aliyuncs.com/avatar/fake.png')
        self.assertEqual(self.api.profile['username'], '测试', '头像写入不应影响其它字段')
        w.remove_avatar(self.api, 'AAAA0001')
        self.assertEqual(self.api.profile['avatar_url'], '')

    def test_school_logo_upload_clears_logo_id(self):
        with self.patch:
            w.set_school_logo_file(self.api, 'AAAA0001', self.png)
        basic = self.api.cvs['AAAA0001']['basic_info']
        self.assertEqual((basic['school_logo_id'], basic['school_logo_url'][-4:]), (None, '.png'))

    def test_failed_upload_changes_nothing(self):
        before = dict(self.api.profile)
        with mock.patch.object(w, 'upload_image', side_effect=w.WcvError('上传失败', exit_code=w.EXIT_NETWORK)):
            with self.assertRaises(w.WcvError):
                w.set_avatar(self.api, 'AAAA0001', self.png)
        self.assertEqual(self.api.profile, before)

    def test_opus_qrcode_added_and_replaced_with_title_and_url_kept(self):
        with self.patch:
            oid = w.add_opus(self.api, 'AAAA0001', '作品甲', 'https://example.test/a', qrcode_path=self.png)['id']
            self.assertEqual(self.api.opus_lib[oid]['qrcode'], 'https://prod-file-uploaded.oss-cn-beijing.aliyuncs.com/opus/fake.png')
            w.set_opus(self.api, 'AAAA0001', oid, qrcode_path=self.png)
        self.assertEqual(self.api.opus_lib[oid]['title'], '作品甲')
        self.assertEqual(self.api.opus_lib[oid]['url'], 'https://example.test/a')

    def test_opus_upload_failure_creates_nothing(self):
        with mock.patch.object(w, 'upload_image', side_effect=w.WcvError('上传失败', exit_code=w.EXIT_NETWORK)):
            with self.assertRaises(w.WcvError):
                w.add_opus(self.api, 'AAAA0001', '作品乙', '', qrcode_path=self.png)
        self.assertEqual(self.api.opus_lib, {})
        self.assertFalse([c for c in self.api.calls if c[0] == 'POST' and c[1] == '/v3/opus'])

    def test_invalid_image_is_rejected_before_any_request(self):
        bad = os.path.join(self.dir, 'bad.png')
        with open(bad, 'wb') as f:
            f.write(b'not an image')
        err = io_capture_error(lambda: w.run(w.build_parser().parse_args(
            ['avatar' if False else 'profile', 'avatar', 'set', 'AAAA0001', '--file', bad]), self.api))
        self.assertIsInstance(err, w.WcvError)
        self.assertEqual(err.exit_code, w.EXIT_INPUT)
        self.assertEqual(self.api.calls, [], '无效图片不得发出任何请求')
        os.unlink(bad)


def io_capture_error(fn):
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            fn()
        except w.WcvError as e:
            return e
    raise AssertionError('应当失败')


if __name__ == '__main__':
    unittest.main(verbosity=2)

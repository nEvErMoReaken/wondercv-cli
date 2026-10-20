"""返工阶段 A 的本地回归测试（不访问网络）。运行：python3 -m unittest tests.test_rework -v

覆盖 docs/rework-acceptance.md 的 A01–A06：引用守卫（fail-closed）、作品保留二维码、首个 POST 结果未知后的
跨操作恢复（recover）、整条命令的部分成功报告、删除结果未知的核对、认证与下载的重定向边界、文件原子不覆盖发布。
"""
import contextlib
import copy
import http.server
import io
import os
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wondercv as w  # noqa: E402
from tests.fakes import FakeAPI, RichFake  # noqa: E402


def run(api, *argv):
    return w.run(w.build_parser().parse_args(list(argv)), api)


def fail_op(api, *argv):
    """执行命令并要求失败，返回 WcvError。"""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            run(api, *argv)
        except w.WcvError as e:
            return e
    raise AssertionError(f'{argv} 本应失败')


def op_ids(api):
    return [r['id'] for r in w.recover_list(api)]


class _Pending(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(w, 'CONFIRM_DELAY', 0)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()


class GuardTest(_Pending):
    """A01：回收站非空、或存在活动引用时，共享对象的改名/删除必须拒绝（或只解除本简历引用）。"""

    def setUp(self):
        super().setUp()
        self.api = RichFake(cv_tokens=('AAAA0001', 'AAAA0002', 'TRASHED'))
        self.api.trash.add('TRASHED')

    def _section(self):
        return w.add_section(self.api, 'AAAA0001', '共享模块', category='custom')

    def test_rename_refused_when_trash_is_not_empty(self):
        sid = self._section()
        err = fail_op(self.api, 'section', 'rename', 'AAAA0001', str(sid), '--title', '改名')
        self.assertIn('回收站', str(err))
        self.assertEqual(self.api.sections[sid]['title'], '共享模块')

    def test_rename_allowed_without_trash_and_holders(self):
        self.api.trash.clear()
        sid = self._section()
        run(self.api, 'section', 'rename', 'AAAA0001', str(sid), '--title', '改名')
        self.assertEqual(self.api.sections[sid]['title'], '改名')

    def test_section_with_active_holder_is_detached_but_kept(self):
        self.api.trash.clear()
        sid = self._section()
        self.api.cvs['AAAA0002']['submodule_info']['custom_sections_ids'] = [sid]
        result = w.rm_section(self.api, 'AAAA0001', sid)
        self.assertEqual(result, {'deleted': False, 'still_referenced_by': ['AAAA0002']})
        self.assertFalse(self.api.sections[sid]['hidden'], '仍被引用的模块不能被删除')

    def test_opus_delete_refused_when_trash_is_not_empty(self):
        oid = w.add_opus(self.api, 'AAAA0001', '作品', 'https://example.test/p').get('id')
        err = fail_op(self.api, 'opus', 'rm', 'AAAA0001', str(oid))
        self.assertIn('回收站', str(err))
        self.assertFalse(self.api.opus_lib[oid]['hidden'], '回收站非空时不得删除作品')
        self.assertIn(oid, self.api.cvs['AAAA0001']['submodule_info']['opus_ids'])

    def test_opus_delete_and_orphan_clean_work_when_trash_empty(self):
        self.api.trash.clear()
        oid = w.add_opus(self.api, 'AAAA0001', '作品', 'https://example.test/p')['id']
        run(self.api, 'opus', 'rm', 'AAAA0001', str(oid))
        self.assertTrue(self.api.opus_lib[oid]['hidden'])
        oid2 = w.add_opus(self.api, 'AAAA0001', '作品2', '')['id']
        self.api.cvs['AAAA0001']['submodule_info']['opus_ids'] = []  # 模拟孤立对象
        self.assertEqual(w.orphan_clean(self.api, 'opus', oid2)['deleted'], True)

    def test_orphan_clean_refused_when_trash_is_not_empty(self):
        oid = w.add_opus(self.api, 'AAAA0001', '作品', '')['id']
        self.api.cvs['AAAA0001']['submodule_info']['opus_ids'] = []
        err = fail_op(self.api, 'orphan', 'clean', 'opus', str(oid))
        self.assertIn('回收站', str(err))
        self.assertFalse(self.api.opus_lib[oid]['hidden'])

    def test_opus_delete_with_active_holder_only_detaches(self):
        self.api.trash.clear()
        oid = w.add_opus(self.api, 'AAAA0001', '作品', '')['id']
        self.api.cvs['AAAA0002']['submodule_info']['opus_ids'] = [oid]
        result = w.rm_opus(self.api, 'AAAA0001', oid)
        self.assertEqual(result, {'deleted': False, 'still_referenced_by': ['AAAA0002']})
        self.assertFalse(self.api.opus_lib[oid]['hidden'])

    def test_entry_removal_is_not_blocked_by_trash(self):
        """经历与条目的副本是新对象（实测），不属于共享类别，回收站非空时仍可删除。"""
        item = w.add_entry(self.api, 'AAAA0001', 'work', {'title': '正常删除'})
        run(self.api, 'work', 'rm', 'AAAA0001', str(item['id']))
        self.assertEqual(w.list_entries(self.api, 'AAAA0001', 'work'), [])


class GuardEdgeTest(_Pending):
    """A01 补充：引用扫描失败、多个活动引用时，共享对象不得被删除或改名。"""

    def setUp(self):
        super().setUp()
        self.api = RichFake(cv_tokens=('AAAA0001', 'AAAA0002', 'AAAA0003'))
        self.api.trash.clear()

    def test_unreadable_active_holder_blocks_opus_delete(self):
        oid = w.add_opus(self.api, 'AAAA0001', '作品', '')['id']
        self.api.inject('GET', '/v3/cvs/AAAA0002', 'none')  # 无法读取另一份活动简历
        with self.assertRaises(w.WcvError):
            w.rm_opus(self.api, 'AAAA0001', oid)
        self.assertFalse(self.api.opus_lib[oid]['hidden'], '无法确认引用时不得删除作品')

    def test_several_active_holders_keep_the_object(self):
        oid = w.add_opus(self.api, 'AAAA0001', '作品', '')['id']
        self.api.cvs['AAAA0002']['submodule_info']['opus_ids'] = [oid]
        self.api.cvs['AAAA0003']['submodule_info']['opus_ids'] = [oid]
        result = w.rm_opus(self.api, 'AAAA0001', oid)
        self.assertEqual(result['deleted'], False)
        self.assertEqual(sorted(result['still_referenced_by']), ['AAAA0002', 'AAAA0003'])
        self.assertFalse(self.api.opus_lib[oid]['hidden'])

    def test_unreadable_holder_blocks_section_rename(self):
        sid = w.add_section(self.api, 'AAAA0001', '共享', category='custom')
        self.api.inject('GET', '/v3/cvs/AAAA0002', 'none')
        with self.assertRaises(w.WcvError):
            w.rename_section(self.api, 'AAAA0001', sid, '改名')
        self.assertEqual(self.api.sections[sid]['title'], '共享')


class ProfileStepTest(_Pending):
    """A04 补充：个人信息第三步结果未知时，报告已完成与未知步骤。"""

    def test_unknown_third_step_reports_completed_and_unknown(self):
        class API(FakeAPI):
            def call(self, method, path, params=None, body=None):
                if method == 'PUT' and body and 'show_attr_name' in body:
                    raise w.WcvError('no response', state='unknown')
                return super().call(method, path, params, body)
        api = API()
        api.cvs['AAAA0001']['profile'] = None
        profile = {'username': 'before', 'format_pref': {}}

        def fake_profile(method, path, params=None, body=None):
            if method == 'GET' and path.endswith('/profile'):
                return {'profile': copy.deepcopy(profile)}
            if method in ('PUT', 'POST') and path.endswith('/profile'):
                profile.update({k: v for k, v in body.items() if k != 'format_pref'})
                if 'format_pref' in body:
                    profile['format_pref'] = body['format_pref']
                if 'show_attr_name' in body:
                    raise w.WcvError('no response', state='unknown')
                return None
            return FakeAPI.call(api, method, path, params, body)
        api.call = fake_profile
        err = fail_op(api, 'profile', 'set', 'AAAA0001', 'username=after', 'age_birthday=age', 'show_attr_name=0')
        self.assertEqual(err.state, 'partial')
        self.assertIn('已完成', str(err))
        self.assertIn('结果未知', str(err))  # 第三步的未知结果在报告中单独说明
        self.assertIn('信息名称显示', str(err))
        self.assertIn('已完成 ', str(err))
        self.assertEqual(profile['username'], 'after')


class OpusEditTest(_Pending):
    """A02：修改作品只改传入字段，二维码保留；只有明确要求才清空。"""

    def setUp(self):
        super().setUp()
        self.api = RichFake()
        self.oid = w.add_opus(self.api, 'AAAA0001', '原标题', 'https://example.test/old')['id']
        self.api.opus_lib[self.oid]['qrcode'] = 'https://example.test/qr-original.png'

    def qr(self):
        return self.api.opus_lib[self.oid]['qrcode']

    def test_title_only_edit_keeps_qrcode(self):
        w.set_opus(self.api, 'AAAA0001', self.oid, title='新标题')
        self.assertEqual((self.api.opus_lib[self.oid]['title'], self.qr()),
                         ('新标题', 'https://example.test/qr-original.png'))

    def test_url_only_edit_keeps_qrcode_and_title(self):
        w.set_opus(self.api, 'AAAA0001', self.oid, url='https://example.test/new')
        self.assertEqual((self.api.opus_lib[self.oid]['title'], self.api.opus_lib[self.oid]['url'], self.qr()),
                         ('原标题', 'https://example.test/new', 'https://example.test/qr-original.png'))

    def test_both_fields_edit_keeps_qrcode(self):
        w.set_opus(self.api, 'AAAA0001', self.oid, title='甲', url='https://example.test/乙')
        self.assertEqual(self.qr(), 'https://example.test/qr-original.png')

    def test_remove_qrcode_is_explicit(self):
        w.set_opus(self.api, 'AAAA0001', self.oid, title='甲', remove_qrcode=True)
        self.assertEqual(self.qr(), '')

    def test_title_length_limit_on_edit(self):
        with self.assertRaises(w.WcvError):
            w.set_opus(self.api, 'AAAA0001', self.oid, title='字' * 21)
        self.assertEqual(self.api.opus_lib[self.oid]['title'], '原标题')


class RecoveryTest(_Pending):
    """A03：首个非幂等写请求结果未知时，通过持久化记录 + 公开 recover 命令找回并处理实际对象。"""

    def setUp(self):
        super().setUp()
        self.api = RichFake(cv_tokens=('AAAA0001', 'AAAA0002'))

    def _lost_work(self, title):
        self.api.inject_after('POST', '/v3/works', 'unknown')
        err = fail_op(self.api, 'work', 'add', 'AAAA0001', '--title', title)
        self.assertEqual(err.state, 'unknown')
        self.assertTrue(err.hint and 'recover' in err.hint, '结果未知时必须给出可执行的恢复命令')
        return op_ids(self.api)[0]

    def _titles(self):
        return sorted(x['title'] for x in w.list_entries(self.api, 'AAAA0001', 'work'))

    def test_lost_response_then_recover_attach_creates_no_duplicate(self):
        op = self._lost_work('丢响应')
        cand = run(self.api, 'recover', op)['candidates']
        self.assertEqual(len(cand), 1)
        run(self.api, 'recover', op, '--attach', str(cand[0]['id']))
        self.assertEqual(self._titles(), ['丢响应'])
        self.assertEqual(op_ids(self.api), [])

    def test_lost_response_then_recover_discard(self):
        op = self._lost_work('丢弃测试')
        cand_id = run(self.api, 'recover', op)['candidates'][0]['id']
        run(self.api, 'recover', op, '--discard', str(cand_id))
        self.assertNotIn(('works', cand_id), self.api.objects, '丢弃后对象应已删除')
        self.assertEqual(w.list_entries(self.api, 'AAAA0001', 'work'), [])
        self.assertEqual(op_ids(self.api), [])

    def test_not_created_is_confirmed_with_none(self):
        self.api.inject('POST', '/v3/works', 'unknown')  # 请求未到达服务端
        err = fail_op(self.api, 'work', 'add', 'AAAA0001', '--title', '未创建')
        self.assertEqual(err.state, 'unknown')
        op = op_ids(self.api)[0]
        self.assertEqual(run(self.api, 'recover', op)['candidates'], [])
        self.assertEqual(run(self.api, 'recover', op, '--none'), {'resolved': 'none'})
        self.assertEqual(op_ids(self.api), [])

    def test_none_refused_while_candidate_exists(self):
        op = self._lost_work('有候选')
        with self.assertRaises(w.WcvError):
            run(self.api, 'recover', op, '--none')

    def test_multiple_candidates_require_explicit_choice(self):
        op = self._lost_work('同名')
        other = w.run(w.build_parser().parse_args(['work', 'add', 'AAAA0002', '--title', '同名']), self.api)
        cands = run(self.api, 'recover', op)['candidates']
        self.assertGreaterEqual(len(cands), 1)
        # 另一位操作者创建的同名对象也会出现在候选中，但不能被自动挂载
        self.assertTrue(other is not None)
        bad = fail_op(self.api, 'recover', op, '--attach', '999999')
        self.assertIn('候选对象中没有', str(bad))
        self.assertEqual(self._titles(), [])

    def test_unrelated_object_is_not_a_candidate(self):
        op = self._lost_work('我的作品')
        self.api.call('POST', '/v3/works', body={'title': '别人的作品', 'lang': 'cn'})
        cands = run(self.api, 'recover', op)['candidates']
        self.assertEqual([c['label'] for c in cands], ['我的作品'])

    def test_candidate_referenced_elsewhere_cannot_be_attached(self):
        op = self._lost_work('被引用')
        cand_id = run(self.api, 'recover', op)['candidates'][0]['id']
        self.api.cvs['AAAA0002']['submodule_info']['works_ids'] = [cand_id]
        err = fail_op(self.api, 'recover', op, '--attach', str(cand_id))
        self.assertIn('已被', str(err))
        self.assertEqual(self._titles(), [])

    def test_second_add_blocked_until_recovered(self):
        op = self._lost_work('第一次')
        posts_before = len([c for c in self.api.calls if c == ('POST', '/v3/works')])
        err = fail_op(self.api, 'work', 'add', 'AAAA0001', '--title', '第二次')
        self.assertIn(op, str(err))
        self.assertEqual(len([c for c in self.api.calls if c == ('POST', '/v3/works')]), posts_before,
                         '未解决的同类操作存在时不得再次 POST')

    def test_paged_library_is_complete_and_short_page_is_refused(self):
        class Paged(RichFake):
            def _handle(self, method, path, params, body):
                if path == '/v3/works' and method == 'GET' and 'ids' not in params:
                    items = [dict(v, id=k[1]) for k, v in sorted(self.objects.items()) if k[0] == 'works']
                    page = int(params.get('page', 1))
                    chunk = items[(page - 1) * 2: page * 2]
                    if self.short_pages:
                        chunk = chunk[:1]
                    return {'works': copy.deepcopy(chunk), 'total_count': len(items), 'page_count': 2}
                return super()._handle(method, path, params, body)
        api = Paged(cv_tokens=('AAAA0001',))
        api.short_pages = False
        for i in range(3):
            w.add_entry(api, 'AAAA0001', 'work', {'title': f'分页{i}'})
        self.assertEqual(len(w._lib_for(api, {'kind': 'work', 'lang': 'cn'})), 3)
        api.short_pages = True
        posts = len([c for c in api.calls if c == ('POST', '/v3/works')])
        with self.assertRaises(w.WcvError) as cm:
            w.add_entry(api, 'AAAA0001', 'work', {'title': '不完整'})
        self.assertIn('无法完整列出', str(cm.exception))
        self.assertEqual(len([c for c in api.calls if c == ('POST', '/v3/works')]), posts, '不完整时不得写入')

    def test_section_lost_response_recover_discard(self):
        self.api.inject_after('POST', '/v3/custom_sections', 'unknown')
        fail_op(self.api, 'section', 'add', 'AAAA0001', '--title', '丢失模块')
        op = op_ids(self.api)[0]
        cand_id = run(self.api, 'recover', op)['candidates'][0]['id']
        run(self.api, 'recover', op, '--discard', str(cand_id))
        self.assertTrue(self.api.sections[cand_id]['hidden'])
        self.assertNotIn(cand_id, self.api.cvs['AAAA0001']['submodule_info']['custom_sections_ids'])

    def test_item_lost_response_recover_attach(self):
        sid = w.add_section(self.api, 'AAAA0001', '条目模块')
        self.api.inject_after('POST', f'/v3/custom_sections/{sid}/infos', 'unknown')
        fail_op(self.api, 'item', 'add', 'AAAA0001', str(sid), '--title', '丢失条目')
        op = op_ids(self.api)[0]
        cand_id = run(self.api, 'recover', op)['candidates'][0]['id']
        run(self.api, 'recover', op, '--attach', str(cand_id))
        self.assertEqual([x['title'] for x in w.list_items(self.api, 'AAAA0001', sid)], ['丢失条目'])

    def test_extra_lost_response_recover_attach(self):
        self.api.inject_after('POST', '/v3/extras', 'unknown')
        fail_op(self.api, 'extra', 'set', 'AAAA0001', 'skill', '--desc', '丢失技能')
        op = op_ids(self.api)[0]
        cand_id = run(self.api, 'recover', op)['candidates'][0]['id']
        run(self.api, 'recover', op, '--attach', str(cand_id))
        self.assertEqual([x['description'] for x in w.list_extras(self.api, 'AAAA0001')], ['丢失技能'])

    def test_opus_lost_response_recover_attach(self):
        self.api.inject_after('POST', '/v3/opus', 'unknown')
        fail_op(self.api, 'opus', 'add', 'AAAA0001', '--title', '丢失作品')
        op = op_ids(self.api)[0]
        cand_id = run(self.api, 'recover', op)['candidates'][0]['id']
        run(self.api, 'recover', op, '--attach', str(cand_id))
        self.assertEqual([x['title'] for x in w.list_opus(self.api, 'AAAA0001')], ['丢失作品'])

    def test_discard_refuses_when_target_reference_cannot_be_read(self):
        self.api.inject_after('POST', '/v3/opus', 'unknown')
        fail_op(self.api, 'opus', 'add', 'AAAA0001', '--title', '不能误删')
        op = op_ids(self.api)[0]
        oid = run(self.api, 'recover', op)['candidates'][0]['id']
        self.api.cvs['AAAA0001']['submodule_info']['opus_ids'] = [oid]
        self.api.inject('GET', '/v3/cvs/AAAA0001', 'unknown')
        self.api.calls.clear()
        with self.assertRaises(w.WcvError):
            run(self.api, 'recover', op, '--discard', str(oid))
        self.assertIn(oid, self.api.opus_lib)
        self.assertIn(op, op_ids(self.api))
        self.assertFalse(any(method == 'DELETE' for method, _ in self.api.calls))

    def test_attach_refuses_when_target_reference_cannot_be_read(self):
        op = self._lost_work('不要重复挂载')
        oid = run(self.api, 'recover', op)['candidates'][0]['id']
        self.api.cvs['AAAA0001']['submodule_info']['works_ids'] = [oid]
        self.api.inject('GET', '/v3/cvs/AAAA0001', 'unknown')
        self.api.calls.clear()
        with self.assertRaises(w.WcvError):
            run(self.api, 'recover', op, '--attach', str(oid))
        self.assertEqual(self.api.cvs['AAAA0001']['submodule_info']['works_ids'], [oid])
        self.assertIn(op, op_ids(self.api))
        self.assertFalse(any(method == 'PUT' for method, _ in self.api.calls))

    def test_resume_lost_response_recover_adopt(self):
        self.api.inject_after('POST', '/v3/cvs', 'unknown')
        fail_op(self.api, 'resume', 'create', '--name', '丢失简历')
        op = op_ids(self.api)[0]
        token = run(self.api, 'recover', op)['candidates'][0]['id']
        self.assertEqual(run(self.api, 'recover', op, '--adopt', token)['token'], token)
        self.assertEqual(op_ids(self.api), [])


class DeleteReconcileTest(_Pending):
    """A04：删除结果未知时只读核对；能证明已不存在才收束为成功。"""

    def setUp(self):
        super().setUp()
        self.api = RichFake()
        self.item = w.add_entry(self.api, 'AAAA0001', 'work', {'title': '删除核对'})
        self.wid = self.item['id']

    def _present(self):
        return any(o['id'] == self.wid for o in w._lib_for(self.api, {'kind': 'work', 'lang': 'cn'}))

    def test_delete_executed_but_response_lost_is_success(self):
        self.api.inject_after('DELETE', f'/v3/works/{self.wid}', 'unknown')
        result = w.rm_entry(self.api, 'AAAA0001', 'work', self.wid)
        self.assertEqual(result['deleted'], True)
        self.assertFalse(self._present())

    def test_delete_not_executed_is_reported_unknown_not_deleted(self):
        self.api.inject('DELETE', f'/v3/works/{self.wid}', 'unknown')
        with self.assertRaises(w.WcvError) as cm:
            w.rm_entry(self.api, 'AAAA0001', 'work', self.wid)
        self.assertEqual(cm.exception.state, 'unknown')
        self.assertTrue(self._present(), '未执行的删除不得被报告为已删除')

    def test_definite_delete_failure_is_partial(self):
        self.api.inject('DELETE', f'/v3/works/{self.wid}', 'none')
        with self.assertRaises(w.WcvError) as cm:
            w.rm_entry(self.api, 'AAAA0001', 'work', self.wid)
        self.assertEqual(cm.exception.state, 'partial')
        self.assertTrue(self._present())

    def test_orphan_clean_is_idempotent_when_absent(self):
        self.api.trash.clear()
        self.api.cvs['AAAA0001']['submodule_info']['works_ids'] = []  # 先解除引用，才是孤立对象
        self.api.objects.pop(('works', self.wid))
        self.assertEqual(w.orphan_clean(self.api, 'work', self.wid)['already_absent'], True)

    def test_orphan_clean_cannot_prove_absence_then_fails(self):
        self.api.cvs['AAAA0001']['submodule_info']['works_ids'] = []
        self.api.inject('GET', '/v3/works', 'none')
        with self.assertRaises(w.WcvError):
            w.orphan_clean(self.api, 'work', self.wid)
        self.assertTrue(self._present())


class _Redirecting(http.server.BaseHTTPRequestHandler):
    status = 302
    location = ''
    hits = None

    def do_GET(self):
        if self.location:  # 源服务器：任何路径都返回重定向
            self.send_response(self.status)
            self.send_header('Location', self.location)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.hits.append(self.headers.get('Authorization'))
        body = b'{"code": 1000, "data": {}}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TransportTest(unittest.TestCase):
    """A05：认证 API 不跟随任何重定向；下载的每一跳都必须是 https 的阿里云 OSS 地址。"""

    def _servers(self, status):
        hits = []
        dest = http.server.ThreadingHTTPServer(('127.0.0.1', 0), type('D', (_Redirecting,), {'hits': hits}))
        handler = type('O', (_Redirecting,), {'status': status, 'hits': hits,
                                              'location': f'http://127.0.0.1:{dest.server_port}/sink'})
        origin = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        for srv in (origin, dest):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        return origin, dest, hits

    def test_api_never_follows_any_redirect_status(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                origin, dest, hits = self._servers(status)
                try:
                    with mock.patch.object(w, 'API', f'http://127.0.0.1:{origin.server_port}'):
                        with self.assertRaises(w.WcvError) as cm:
                            w.Client('SYNTHETIC-CREDENTIAL').call('GET', '/redirect')
                    self.assertIn('重定向', str(cm.exception))
                    self.assertEqual(hits, [], 'Redirect destination was contacted')
                finally:
                    for srv in (origin, dest):
                        srv.shutdown()
                        srv.server_close()

    def _redirect_error(self, location):
        return urllib.error.HTTPError('https://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf', 302, 'Found',
                                      {'Location': location}, None)

    def test_download_redirect_to_non_oss_host_is_refused(self):
        calls = []

        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            raise self._redirect_error('https://evil.example.com/steal')
        with mock.patch('urllib.request.urlopen', side_effect=fake_open), self.assertRaises(w.WcvError) as cm:
            w._fetch_file('https://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf')
        self.assertEqual(len(calls), 1, '不允许的跳转目标不得被请求')
        self.assertIn('允许', str(cm.exception))

    def test_download_redirect_to_http_is_refused(self):
        def fake_open(req, timeout=None):
            raise self._redirect_error('http://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf')
        with mock.patch('urllib.request.urlopen', side_effect=fake_open), self.assertRaises(w.WcvError):
            w._fetch_file('https://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf')

    def test_download_redirect_to_oss_is_followed_without_credentials(self):
        seen = []

        def fake_open(req, timeout=None):
            seen.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
            if len(seen) == 1:
                raise self._redirect_error('https://other-bucket.oss-cn-beijing.aliyuncs.com/y.pdf')
            return _FakeResponse(b'%PDF-1.4\n%%EOF\n')
        with mock.patch('urllib.request.urlopen', side_effect=fake_open):
            data = w._fetch_file('https://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf')
        self.assertTrue(data.startswith(b'%PDF-'))
        self.assertEqual(len(seen), 2)
        self.assertTrue(all('authorization' not in headers for _, headers in seen))


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PublishTest(unittest.TestCase):
    """A06：不覆盖发布是原子的；失败不留下半成品。"""

    PDF = b'%PDF-1.4\n%%EOF\n'

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='wcv-publish-')

    def tearDown(self):
        for name in os.listdir(self.dir):
            path = os.path.join(self.dir, name)
            if os.path.isdir(path):
                os.rmdir(path)
            else:
                os.unlink(path)
        os.rmdir(self.dir)

    def _client(self):
        class Stub:
            def call(self, method, path, params=None, body=None):
                if path.endswith('download_url'):
                    return {'url': 'https://prod-resumes.oss-cn-beijing.aliyuncs.com/a.pdf'}
                return {'basic_info': {'token': 'AAAA0001'}, 'submodule_info': {}}
        return Stub()

    def test_force_replaces_existing_file(self):
        out = os.path.join(self.dir, 'cv.pdf')
        with open(out, 'wb') as f:
            f.write(b'old')
        w.export_resume(self._client(), 'AAAA0001', 'pdf', out, force=True, fetch=lambda url: self.PDF)
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), self.PDF)
        self.assertEqual(os.listdir(self.dir), ['cv.pdf'])

    def test_dangling_symlink_is_not_followed_or_created_through(self):
        target = os.path.join(self.dir, 'elsewhere.pdf')
        out = os.path.join(self.dir, 'cv.pdf')
        os.symlink(target, out)
        with self.assertRaises(w.WcvError):
            w.export_resume(self._client(), 'AAAA0001', 'pdf', out, fetch=lambda url: self.PDF)
        self.assertFalse(os.path.exists(target), '不得经由符号链接写入其它位置')

    def test_link_failure_leaves_no_partial_file(self):
        out = os.path.join(self.dir, 'cv.pdf')
        with mock.patch('os.link', side_effect=PermissionError(1, 'Operation not permitted')):
            with self.assertRaises(w.WcvError):
                w.export_resume(self._client(), 'AAAA0001', 'pdf', out, fetch=lambda url: self.PDF)
        self.assertEqual(os.listdir(self.dir), [], '失败后不得残留临时文件或目标文件')

    def test_existing_directory_is_refused(self):
        out = os.path.join(self.dir, 'folder')
        os.mkdir(out)
        with self.assertRaises(w.WcvError):
            w.export_resume(self._client(), 'AAAA0001', 'pdf', out, fetch=lambda url: self.PDF)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class ShareSettingsTest(unittest.TestCase):
    """R16 本地：分享设置的请求体与撤销路径（真实读回见 test_live.LiveP2Test.test_p2_16）。"""

    class Stub:
        def __init__(self):
            self.settings = {'public_visibility': True, 'can_comment': True, 'visibility_type': 20}
            self.puts = []

        def call(self, method, path, params=None, body=None):
            if method == 'GET' and path.endswith('/encoded_token'):
                return {'encoded_token': 'TOK-ENC'}
            if method == 'GET' and path.endswith('/settings'):
                return dict(self.settings)
            if method == 'PUT' and path.endswith('/settings'):
                self.puts.append(dict(body))
                self.settings.update(body)
                return None
            raise AssertionError(f'未实现 {method} {path}')

    def test_revoke_then_restore_writes_only_given_fields(self):
        stub = self.Stub()
        status = w.set_share(stub, 'AAAA0001', public=False)
        self.assertEqual(stub.puts, [{'public_visibility': False}])
        self.assertFalse(status['settings']['public_visibility'])
        self.assertEqual(status['share_url'], 'https://www.wondercv.com/cvs/TOK-ENC/share')
        w.set_share(stub, 'AAAA0001', public=True, visibility='privacy', comment=False)
        self.assertEqual(stub.puts[-1], {'public_visibility': True, 'visibility_type': 10, 'can_comment': False})

    def test_no_put_without_changes(self):
        stub = self.Stub()
        w.set_share(stub, 'AAAA0001')
        self.assertEqual(stub.puts, [])

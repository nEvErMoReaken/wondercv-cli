"""纯逻辑与故障路径单元测试（不访问网络）。运行：python3 -m unittest tests.test_unit -v"""
import contextlib
import copy
import io
import os
import re
import socket
import ssl
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import wondercv  # noqa: E402
from tests.fakes import FakeAPI  # noqa: E402


def _args(**kw):
    base = dict(start=None, end=None, degree=None, present=False, university=None, major=None,
                edu_type=None, city=None, description=None, company=None, title=None, dept=None, name=None)
    base.update(kw)
    return Namespace(**base)


class DateTest(unittest.TestCase):
    def test_accepts_real_dates(self):
        for ok in ['2018-09', '2018-09-01', '2024-02-29', '2000-02-29']:
            self.assertEqual(wondercv._date(ok), ok)

    def test_rejects_format_and_calendar_errors(self):
        bad = ['2018', '2018/09', '09-2018', '2024-9', '2024-1-01', 'abc',
               '2023-02-29', '2024-13', '2024-00', '2024-04-31', '2024-02-30', '2024-09-00', '2024-09-32']
        for value in bad:
            with self.subTest(value=value), self.assertRaises(wondercv.WcvError) as cm:
                wondercv._date(value)
            self.assertEqual(cm.exception.exit_code, wondercv.EXIT_INPUT)


class PeriodTest(unittest.TestCase):
    def test_present_and_end_conflict_is_rejected(self):
        with self.assertRaises(wondercv.WcvError):
            wondercv._entry_body('work', _args(present=True, start='2023-07', end='2024-01'))
        with self.assertRaises(wondercv.WcvError):
            wondercv._item_body(_args(present=True, end='2024-01', title=None, name=None, dept=None,
                                      city=None, description=None))

    def test_end_date_clears_present_for_work_and_items(self):
        self.assertEqual(wondercv._entry_body('work', _args(end='2024-03')), {'end_at': '2024-03', 'present': None})
        self.assertEqual(wondercv._item_body(_args(end='2024-03', title=None, name=None, dept=None,
                                                   city=None, description=None, present=False)),
                         {'end_at': '2024-03', 'present': None})

    def test_present_clears_end_date(self):
        self.assertEqual(wondercv._entry_body('work', _args(present=True, start='2023-07')),
                         {'start_at': '2023-07', 'present': '至今', 'end_at': None})

    def test_education_has_no_present_field(self):
        self.assertEqual(wondercv._entry_body('edu', _args(end='2022-06')), {'end_at': '2022-06'})
        with self.assertRaises(wondercv.WcvError):
            wondercv._entry_body('edu', _args(present=True))

    def test_only_given_fields_are_sent(self):
        self.assertEqual(wondercv._entry_body('org', _args(title='社长')), {'title': '社长'})
        self.assertEqual(wondercv._entry_body('work', _args()), {})

    def test_degree_name_maps_to_id(self):
        self.assertEqual(wondercv._entry_body('edu', _args(degree='硕士', university='清华大学')),
                         {'degree': '硕士', 'degree_id': 3, 'university': '清华大学'})
        with self.assertRaises(wondercv.WcvError):
            wondercv._entry_body('edu', _args(degree='初中'))


class TextTest(unittest.TestCase):
    def test_renders_nested_structure_and_strips_html(self):
        text = wondercv.to_text({'cv_name': '简历', 'profile': None, 'sections': [
            {'title': '工作', 'items': [{'id': 1, 'description': '<ul><li>做事</li></ul>'}]}, {'title': '空', 'items': []}]})
        self.assertEqual(text, 'cv_name: 简历\nprofile: -\nsections:\n  -\n    title: 工作\n    items:\n'
                               '      -\n        id: 1\n        description: 做事\n  -\n    title: 空\n'
                               '    items: (空)')

    def test_scalar_lists_show_values(self):
        text = wondercv.to_text({'skills': ['Python', 'Go'], 'none': [], 'nums': [11, 12]})
        self.assertEqual(text, 'skills:\n  - Python\n  - Go\nnone: (空)\nnums:\n  - 11\n  - 12')


class OrderTest(unittest.TestCase):
    def test_ordered_follows_saved_ids_and_drops_unreferenced(self):
        objs = [{'id': 1}, {'id': 2}, {'id': 3}, {'id': 9}]
        self.assertEqual([o['id'] for o in wondercv._ordered(objs, [3, 1, 2])], [3, 1, 2])

    def test_extra_kinds_follow_extras_position(self):
        sub = {'extras_position': ['interests', 'skills', 'others']}
        self.assertEqual(wondercv._ordered_extra_kinds(sub), ['interest', 'skill', 'other', 'language', 'certificate'])


class ListOrderTest(unittest.TestCase):
    """用内存服务端验证：接口按 id 返回，CLI 仍按保存顺序输出。"""

    def setUp(self):
        self.api = FakeAPI()
        self.tok = 'AAAA0001'

    def test_entries_follow_saved_order_not_api_order(self):
        ids = [wondercv.add_entry(self.api, self.tok, 'work', {'title': f'岗位{i}'})['id'] for i in (1, 2, 3)]
        wondercv.modify_submodule(self.api, self.tok, lambda sub: sub.__setitem__('works_ids', list(reversed(ids))))
        listed = wondercv.list_entries(self.api, self.tok, 'work')
        self.assertEqual([x['id'] for x in listed], list(reversed(ids)))
        self.assertEqual([x['title'] for x in listed], ['岗位3', '岗位2', '岗位1'])


class ValidateBeforeWriteTest(unittest.TestCase):
    """R03：输入校验失败时，任何写请求都不得发出。"""

    def setUp(self):
        self.api = FakeAPI()

    def _writes(self):
        return [c for c in self.api.calls if c[0] != 'GET']

    def test_invalid_date_rejected_before_any_write(self):
        bad = _args(start='2024-02-30')
        with self.assertRaises(wondercv.WcvError):
            wondercv._entry_body('edu', bad)
        for argv in (['edu', 'add', 'AAAA0001', '--start', '2023-02-29'],
                     ['work', 'add', 'AAAA0001', '--start', '2024-13'],
                     ['item', 'add', 'AAAA0001', '1', '--end', '2024-04-31']):
            with self.subTest(argv=argv):
                self.api.calls.clear()
                parser = wondercv.build_parser()
                args = parser.parse_args(['--json', *argv])
                with self.assertRaises(wondercv.WcvError):
                    wondercv.run(args, self.api)
                self.assertEqual(self._writes(), [], '校验失败不得发出写请求')

    def test_conflicting_present_and_end_rejected_before_any_write(self):
        args = wondercv.build_parser().parse_args(['work', 'add', 'AAAA0001', '--present', '--end', '2024-01'])
        with self.assertRaises(wondercv.WcvError):
            wondercv.run(args, self.api)
        self.assertEqual(self._writes(), [])


class RecoveryTest(unittest.TestCase):
    """多步操作的失败恢复（R01）：注入各步故障，检查引用状态、回滚与提示。"""

    def setUp(self):
        self.api = FakeAPI(cv_tokens=('AAAA0001', 'AAAA0002'))
        self.tok = 'AAAA0001'
        self.patch = mock.patch.object(wondercv, 'CONFIRM_DELAY', 0)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _attached(self, token=None):
        return self.api.cvs[token or self.tok]['submodule_info']['works_ids']

    def test_attach_failure_rolls_back_new_object(self):
        self.api.inject('PUT', f'/v3/cvs/{self.tok}', 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.assertEqual(cm.exception.state, 'none')
        self.assertEqual(self.api.objects, {}, '未挂载的新对象应被回滚删除')
        self.assertEqual(self._attached(), [])

    def test_unknown_attach_not_applied_keeps_object_and_reports(self):
        self.api.inject('PUT', f'/v3/cvs/{self.tok}', 'unknown')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        err = cm.exception
        self.assertEqual(err.state, 'unknown')
        self.assertEqual(len(self.api.objects), 1, '结果未知时不回滚，避免误删')
        oid = next(iter(self.api.objects))[1]
        self.assertIn(str(oid), ' '.join(err.objects))
        self.assertEqual(wondercv.orphan_clean(self.api, 'work', oid)['deleted'], True)
        self.assertEqual(self.api.objects, {})

    def test_unknown_attach_that_applied_is_success_without_duplicate(self):
        self.api.inject_after('PUT', f'/v3/cvs/{self.tok}', 'unknown')
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.assertEqual(self._attached(), [item['id']])
        self.assertEqual(len(self.api.objects), 1)

    def test_rollback_failure_is_partial_and_orphan_clean_recovers(self):
        self.api.inject('PUT', f'/v3/cvs/{self.tok}', 'none')
        self.api.inject('DELETE', '/v3/works/\\d+', 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.assertEqual(cm.exception.state, 'partial')
        oid = int(cm.exception.objects[0].split()[-1])
        self.assertIn('orphan clean', cm.exception.hint)
        wondercv.orphan_clean(self.api, 'work', oid)
        self.assertEqual(self.api.objects, {})

    def test_detach_failure_changes_nothing(self):
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.api.inject('PUT', f'/v3/cvs/{self.tok}', 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        self.assertEqual(cm.exception.state, 'none')
        self.assertEqual(self._attached(), [item['id']])
        self.assertIn(('works', item['id']), self.api.objects)

    def test_detach_applied_but_response_lost_still_deletes(self):
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.api.inject_after('PUT', f'/v3/cvs/{self.tok}', 'unknown')
        result = wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        self.assertEqual(result, {'deleted': True})
        self.assertEqual(self._attached(), [])
        self.assertEqual(self.api.objects, {})

    def test_detach_unknown_not_applied_is_reported_unknown(self):
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.api.inject('PUT', f'/v3/cvs/{self.tok}', 'unknown')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        self.assertEqual(cm.exception.state, 'unknown')
        self.assertEqual(self._attached(), [item['id']])

    def test_delete_failure_after_detach_is_partial_and_retry_cleans(self):
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.api.inject('DELETE', f"/v3/works/{item['id']}", 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        self.assertEqual(cm.exception.state, 'partial')
        self.assertEqual(self._attached(), [], '引用已解除')
        self.assertEqual(len(self.api.objects), 1, '对象仍存在，应可清理')
        # 重试 rm 不会再误删其它对象；按提示执行 orphan clean 完成清理
        with self.assertRaises(wondercv.WcvError):
            wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        wondercv.orphan_clean(self.api, 'work', item['id'])
        self.assertEqual(self.api.objects, {})

    def test_shared_object_is_only_detached(self):
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.api.cvs['AAAA0002']['submodule_info']['works_ids'] = [item['id']]
        result = wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        self.assertEqual(result, {'deleted': False, 'still_referenced_by': ['AAAA0002']})
        self.assertIn(('works', item['id']), self.api.objects)
        self.assertEqual(self._attached('AAAA0002'), [item['id']])
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.orphan_clean(self.api, 'work', item['id'])
        self.assertEqual(cm.exception.exit_code, wondercv.EXIT_INPUT)

    def test_trashed_holder_is_enforced_by_server_guard(self):
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.api.cvs['AAAA0002']['submodule_info']['works_ids'] = [item['id']]
        self.api.trash.add('AAAA0002')  # 回收站中的简历无法读取，客户端扫描不到，只能依赖服务端 80144
        result = wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        self.assertEqual(result, {'deleted': False, 'still_referenced_by': 'server'})
        self.assertIn(('works', item['id']), self.api.objects)

    def test_unreadable_other_resume_keeps_object(self):
        item = wondercv.add_entry(self.api, self.tok, 'work', {'title': 'A'})
        self.api.inject('GET', '/v3/cvs/AAAA0002', 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.rm_entry(self.api, self.tok, 'work', item['id'])
        self.assertEqual(cm.exception.state, 'partial')
        self.assertIn(('works', item['id']), self.api.objects, '无法确认引用时必须保留对象')


class ErrorClassTest(unittest.TestCase):
    """网络与服务端异常分类（R06）。"""

    def _call(self, method, side_effect=None, status=None, raw=None):
        client = wondercv.Client('tok', timeout=1)
        if side_effect is not None:
            patcher = mock.patch('urllib.request.urlopen', side_effect=side_effect)
        else:
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = raw
            patcher = mock.patch('urllib.request.urlopen', return_value=resp)
        with patcher, self.assertRaises(wondercv.WcvError) as cm:
            client.call(method, '/v3/cvs/x', body={} if method != 'GET' else None)
        return cm.exception

    def test_connection_refused_on_write_is_not_applied(self):
        err = self._call('POST', urllib.error.URLError(ConnectionRefusedError()))
        self.assertEqual((err.state, err.exit_code), ('none', wondercv.EXIT_NETWORK))

    def test_timeout_on_write_is_unknown(self):
        for side in (urllib.error.URLError(socket.timeout('timed out')), socket.timeout('read timed out')):
            err = self._call('PUT', side)
            self.assertEqual((err.state, err.exit_code), ('unknown', wondercv.EXIT_NETWORK))

    def test_timeout_on_read_has_no_write_state(self):
        err = self._call('GET', socket.timeout('timed out'))
        self.assertEqual((err.state, err.exit_code), (None, wondercv.EXIT_NETWORK))

    def test_http_errors(self):
        def http(code):
            return urllib.error.HTTPError('u', code, 'x', {}, None)
        self.assertEqual(self._call('PUT', http(500)).state, 'unknown')
        self.assertEqual(self._call('PUT', http(400)).state, 'none')
        self.assertEqual(self._call('PUT', http(400)).exit_code, wondercv.EXIT_API)

    def test_bad_json_and_unexpected_payload(self):
        self.assertEqual(self._call('POST', raw=b'<html>').state, 'unknown')
        self.assertEqual(self._call('GET', raw=b'<html>').state, None)
        self.assertEqual(self._call('POST', raw=b'[1,2]').state, 'unknown')

    def test_business_error_on_write_is_not_applied(self):
        err = self._call('PUT', raw=b'{"code": 2001, "message": "x"}')
        self.assertEqual(err.state, 'none')
        self.assertIn('凭证失效', err.hint)


class MainTest(unittest.TestCase):
    def _main(self, argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = wondercv.main(argv)
        return code, err.getvalue(), out.getvalue()

    def test_missing_token_is_input_error(self):
        env = os.environ.pop('WONDERCV_TOKEN', None)
        path = wondercv.TOKEN_FILE
        wondercv.TOKEN_FILE = os.path.join(tempfile.gettempdir(), 'wondercv-no-such-token-file')
        try:
            code, err, _ = self._main(['resume', 'list'])
        finally:
            wondercv.TOKEN_FILE = path
            if env is not None:
                os.environ['WONDERCV_TOKEN'] = env
        self.assertEqual(code, wondercv.EXIT_INPUT)
        self.assertIn('缺少凭证', err)

    def test_unreadable_token_file_does_not_leak_content(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'\xff\xfe-secret-bytes')
        path = wondercv.TOKEN_FILE
        env = os.environ.pop('WONDERCV_TOKEN', None)
        wondercv.TOKEN_FILE = f.name
        try:
            code, err, _ = self._main(['resume', 'list'])
        finally:
            wondercv.TOKEN_FILE = path
            os.unlink(f.name)
            if env is not None:
                os.environ['WONDERCV_TOKEN'] = env
        self.assertEqual(code, wondercv.EXIT_INPUT)
        self.assertNotIn('secret', err)

    def test_unexpected_exception_has_no_traceback(self):
        with mock.patch.object(wondercv, 'run', side_effect=RuntimeError('boom')), \
                mock.patch.dict(os.environ, {'WONDERCV_TOKEN': 'tok'}, clear=False):
            os.environ.pop('WONDERCV_DEBUG', None)
            code, err, _ = self._main(['resume', 'list'])
        self.assertEqual(code, wondercv.EXIT_API)
        self.assertIn('内部错误', err)
        self.assertNotIn('Traceback', err)


class FormatTest(unittest.TestCase):
    def test_parse_format_accepts_known_values(self):
        self.assertEqual(wondercv.parse_format(['title_color=#4183ff', 'font=font-2', 'font_size=14']),
                         {'title_color': '#4183FF', 'font': 'font-2', 'font_size': 14})

    def test_parse_format_rejects_bad_values(self):
        for bad in ['title_color=blue', 'font=font-999', 'font_size=11', 'font_size=29', 'line_spacing=0',
                    'page_margin=x', 'nosuch=1', 'font']:
            with self.subTest(bad=bad), self.assertRaises(wondercv.WcvError):
                wondercv.parse_format([bad])


class ModuleTitleTest(unittest.TestCase):
    def test_title_length_and_type(self):
        wondercv._check_module_title('edus', '教育背景')
        for typ, title in [('edus', ''), ('edus', '字' * 16), ('custom', '自定义')]:
            with self.subTest(typ=typ, title=title), self.assertRaises(wondercv.WcvError):
                wondercv._check_module_title(typ, title)


class SubtitleTest(unittest.TestCase):
    """R07：小标题校验（去空白、非空、15 字上限、只接受已知目标）。"""

    def test_subtitle_is_trimmed_and_checked(self):
        self.assertEqual(wondercv._check_module_title('skill', '  技能：  '), '技能：')
        self.assertEqual(wondercv._check_module_title('extras', '技能/证书及其他'), '技能/证书及其他')
        for target, title in [('skill', '   '), ('skill', ''), ('skill', '字' * 16), ('bogus', '标题')]:
            with self.subTest(target=target, title=title), self.assertRaises(wondercv.WcvError) as cm:
                wondercv._check_module_title(target, title)
            self.assertEqual(cm.exception.exit_code, wondercv.EXIT_INPUT)

    def test_all_targets_are_listed(self):
        self.assertEqual(set(wondercv.MODULE_TARGETS),
                         {'edus', 'works', 'orgs', 'extras', 'skill', 'language', 'certificate', 'interest', 'other'})


class EnumFormatTest(unittest.TestCase):
    """R08：版式枚举校验。取值来自网页前端，见 docs/structure.md。"""

    def test_enum_values_accepted(self):
        values = ['top_format=neat', 'head_format=body_center', 'title_format=tag', 'body_format=one_line',
                  'time_format=number', 'format_priority=city', 'punctuation_format=not_show_punctuation',
                  'page_size=letter_size', 'page_margin=20', 'font_size=28']
        parsed = wondercv.parse_format(values)
        self.assertEqual(parsed['top_format'], 'neat')
        self.assertEqual(parsed['page_margin'], 20)

    def test_invalid_enum_values_rejected(self):
        for bad in ['top_format=unknown', 'head_format=top', 'title_format=bold', 'page_margin=7',
                    'page_size=a5', 'font_size=29', 'time_format=text']:
            with self.subTest(bad=bad), self.assertRaises(wondercv.WcvError):
                wondercv.parse_format([bad])

    def test_options_list_contains_every_settable_key(self):
        options = wondercv.format_options()
        for key in ['top_format', 'head_format', 'title_format', 'body_format', 'time_format', 'page_size',
                    'page_margin', 'font', 'font_size', 'title_color']:
            self.assertIn(key, options)
        self.assertIn('neat', options['top_format'])


class FormatWriteTest(unittest.TestCase):
    """R08：版式写入的联动规则与主题色四字段（假服务端）。"""

    def setUp(self):
        self.api = FakeAPI()
        self.tok = 'AAAA0001'

    def _fmt(self):
        return self.api.cvs[self.tok]['format_info']

    def test_left_block_requires_margin_five(self):
        with self.assertRaises(wondercv.WcvError):
            wondercv.set_format(self.api, self.tok, {'top_format': 'left_block', 'page_margin': 10})
        self.assertFalse([c for c in self.api.calls if c[0] == 'PUT'])
        wondercv.set_format(self.api, self.tok, {'top_format': 'left_block', 'page_margin': 5})
        self.assertEqual((self._fmt()['top_format'], self._fmt()['page_margin']), ('left_block', 5))
        with self.assertRaises(wondercv.WcvError):  # 已是简约样式时，不能再把页边距改为 10
            wondercv.set_format(self.api, self.tok, {'page_margin': 10})

    def test_icon_title_clears_custom_title_color(self):
        self.api.cvs[self.tok]['format_info'].update(title_select_color=True, title_color='#123456')
        wondercv.set_format(self.api, self.tok, {'title_format': 'icon'})
        self.assertEqual((self._fmt()['title_select_color'], self._fmt()['title_color']), (False, '#000000'))

    def test_theme_sets_four_linked_fields(self):
        result = wondercv.set_theme(self.api, self.tok, '#4183ff')
        self.assertEqual(result, {'title_color': '#4183FF', 'top_line_color': '#4183FF',
                                  'title_select_color': False, 'top_line_select_color': False})
        self.assertEqual(self._fmt()['font_size'], 13, '其它版式字段保持不变')
        wondercv.set_theme(self.api, self.tok, '#000000', custom=True)
        self.assertEqual((self._fmt()['title_select_color'], self._fmt()['top_line_select_color']), (True, True))

    def test_theme_rejects_bad_color_without_write(self):
        with self.assertRaises(wondercv.WcvError):
            wondercv.set_theme(self.api, self.tok, 'red')
        self.assertFalse([c for c in self.api.calls if c[0] == 'PUT'])


class ReorderTest(unittest.TestCase):
    """R09：排序必须是完整排列；非法输入在写入前拒绝。"""

    def setUp(self):
        self.api = FakeAPI()
        self.tok = 'AAAA0001'

    def test_permutation_rules(self):
        wondercv._check_permutation([1, 2, 3], [3, 1, 2], 'x')
        for bad in ([1, 2], [1, 2, 3, 3], [1, 2, 4]):
            with self.subTest(bad=bad), self.assertRaises(wondercv.WcvError) as cm:
                wondercv._check_permutation([1, 2, 3], bad, 'x')
            self.assertEqual(cm.exception.exit_code, wondercv.EXIT_INPUT)

    def test_entries_reorder_and_invalid_leaves_state_untouched(self):
        ids = [wondercv.add_entry(self.api, self.tok, 'work', {'title': f'T{i}'})['id'] for i in range(3)]
        wondercv.reorder_entries(self.api, self.tok, 'work', list(reversed(ids)))
        self.assertEqual([x['id'] for x in wondercv.list_entries(self.api, self.tok, 'work')], list(reversed(ids)))
        before = copy.deepcopy(self.api.cvs[self.tok]['submodule_info'])
        self.api.calls.clear()
        with self.assertRaises(wondercv.WcvError):
            wondercv.reorder_entries(self.api, self.tok, 'work', [ids[0], ids[0], ids[1]])
        self.assertEqual(self.api.cvs[self.tok]['submodule_info'], before)
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])

    def test_module_order_is_written_and_read_back(self):
        keys = wondercv.reorder_sections(self.api, self.tok, ['extras', 'orgs', 'edus'])
        self.assertEqual([k['key'] for k in keys][:3], ['extras', 'orgs', 'edus'])
        with self.assertRaises(wondercv.WcvError):
            wondercv.reorder_sections(self.api, self.tok, ['edus', 'orgs'])


class SharedModuleGuardTest(unittest.TestCase):
    """R11：共享的自定义模块不能被改名（会同时改动其它简历）。"""

    def test_rename_refused_when_shared(self):
        api = FakeAPI(cv_tokens=('AAAA0001', 'AAAA0002'))
        for tok in ('AAAA0001', 'AAAA0002'):
            api.cvs[tok]['submodule_info']['custom_sections_ids'] = [777]
            api.cvs[tok]['submodule_info']['sections_position'].append({'id': 777, 'type': 'custom', 'info_ids': []})
            api.cvs[tok]['submodule_info']['custom_sections_titles']['777_title'] = '共享'
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.rename_section(api, 'AAAA0001', 777, '新名')
        self.assertIn('共享', str(cm.exception))
        self.assertFalse([c for c in api.calls if c[0] == 'PUT'], '拒绝时不得写入')


class TemplateCopyTest(unittest.TestCase):
    """R11：按模板新建与复制（假服务端）。"""

    def setUp(self):
        self.api = FakeAPI()

    def test_unknown_template_rejected_before_write(self):
        with self.assertRaises(wondercv.WcvError):
            wondercv.create_resume(self.api, '简历', template='NOPE')
        self.assertFalse([c for c in self.api.calls if c[0] == 'POST'])

    def test_create_from_template_and_copy_with_name(self):
        token = wondercv.create_resume(self.api, '按模板', template='TPLSTU')
        self.assertEqual(self.api.cvs[token]['basic_info']['cv_name'], '按模板')
        copy_token = wondercv.copy_resume(self.api, token, '副本名')
        self.assertEqual(self.api.cvs[copy_token]['basic_info']['cv_name'], '副本名')

    def test_copy_rename_failure_is_partial(self):
        self.api.inject('PUT', re.escape('/v3/cvs/') + r'DUP\d+', 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.copy_resume(self.api, 'AAAA0001', '副本')
        self.assertEqual(cm.exception.state, 'partial')
        self.assertTrue(cm.exception.objects[0].startswith('resume DUP'))


class _ExportClient:
    """只提供导出所需接口的桩：返回签名下载地址。"""

    def __init__(self, error=None, missing=False):
        self.error = error
        self.missing = missing
        self.calls = []

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, params))
        if path.startswith('/v3/cvs/') and not path.endswith('download_url') and '/img_' not in path:
            if self.missing:
                raise wondercv.WcvError('未找到对应简历', code=80120)
            return {'basic_info': {'token': 'AAAA0001'}, 'submodule_info': {}}
        if self.error:
            raise self.error
        return {'url': 'https://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf?sig=1'}


class ExportTest(unittest.TestCase):
    """R12：导出的签名校验、覆盖保护、原子写入与失败清理。"""

    PDF = b'%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n'
    PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _out(self, name):
        return os.path.join(self.dir, name)

    def _read(self, name):
        with open(self._out(name), 'rb') as f:
            return f.read()

    def test_writes_valid_pdf_and_png(self):
        r = wondercv.export_resume(_ExportClient(), 'AAAA0001', 'pdf', self._out('a.pdf'), fetch=lambda u: self.PDF)
        self.assertEqual((r['format'], r['bytes']), ('pdf', len(self.PDF)))
        with open(self._out('a.pdf'), 'rb') as f:
            self.assertEqual(f.read(), self.PDF)
        wondercv.export_resume(_ExportClient(), 'AAAA0001', 'png', self._out('a.png'), fetch=lambda u: self.PNG)
        self.assertTrue(self._read('a.png').startswith(b'\x89PNG'))

    def test_existing_file_is_not_overwritten_without_force(self):
        with open(self._out('a.pdf'), 'wb') as f:
            f.write(b'OLD')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.export_resume(_ExportClient(), 'AAAA0001', 'pdf', self._out('a.pdf'), fetch=lambda u: self.PDF)
        self.assertEqual(cm.exception.exit_code, wondercv.EXIT_INPUT)
        self.assertEqual(self._read('a.pdf'), b'OLD')
        wondercv.export_resume(_ExportClient(), 'AAAA0001', 'pdf', self._out('a.pdf'), force=True,
                               fetch=lambda u: self.PDF)
        self.assertEqual(self._read('a.pdf'), self.PDF)

    def test_bad_content_is_discarded_without_leftovers(self):
        for blob in [b'<html>denied</html>', b'%PDF-1.4 truncated', b'']:
            fmt = 'pdf'
            with self.subTest(blob=blob[:10]), self.assertRaises(wondercv.WcvError) as cm:
                wondercv.export_resume(_ExportClient(), 'AAAA0001', fmt, self._out('bad.pdf'), fetch=lambda u: blob)
            self.assertEqual(cm.exception.exit_code, wondercv.EXIT_NETWORK)
            self.assertEqual(os.listdir(self.dir), [])

    def test_fetch_failure_leaves_no_file(self):
        def boom(url):
            raise wondercv.WcvError('下载失败', exit_code=wondercv.EXIT_NETWORK)
        with self.assertRaises(wondercv.WcvError):
            wondercv.export_resume(_ExportClient(), 'AAAA0001', 'pdf', self._out('x.pdf'), fetch=boom)
        self.assertEqual(os.listdir(self.dir), [])

    def test_missing_resume_is_reported_before_download(self):
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.export_resume(_ExportClient(missing=True), 'NOSUCH', 'pdf', self._out('m.pdf'),
                                   fetch=lambda u: self.PDF)
        self.assertIn('不存在', str(cm.exception))
        self.assertEqual(os.listdir(self.dir), [])

    def test_payment_and_completeness_codes_are_mapped(self):
        err = wondercv.WcvError('付费', code=80160)
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.export_resume(_ExportClient(err), 'AAAA0001', 'pdf', self._out('p.pdf'), fetch=lambda u: self.PDF)
        self.assertIn('不会自动购买', str(cm.exception))
        err = wondercv.WcvError('未完善', code=80120)
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.export_resume(_ExportClient(err), 'AAAA0001', 'pdf', self._out('p.pdf'), fetch=lambda u: self.PDF)
        self.assertIn('完善简历信息', str(cm.exception))

    def test_download_url_must_be_https_oss_and_carries_no_credentials(self):
        with self.assertRaises(wondercv.WcvError):
            wondercv._fetch_file('http://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf')
        with self.assertRaises(wondercv.WcvError):
            wondercv._fetch_file('https://evil.example.com/x.pdf')
        captured = {}

        def fake_urlopen(req, timeout=None, context=None):
            captured['headers'] = {k.lower(): v for k, v in req.header_items()}
            raise urllib.error.URLError(ConnectionRefusedError())
        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen), \
                self.assertRaises(wondercv.WcvError):
            wondercv._fetch_file('https://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf')
        self.assertNotIn('authorization', captured['headers'])

    def test_certificate_failure_points_to_ca_setting(self):
        ssl_err = ssl.SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED')
        with mock.patch('urllib.request.urlopen', side_effect=urllib.error.URLError(ssl_err)), \
                self.assertRaises(wondercv.WcvError) as cm:
            wondercv._fetch_file('https://prod-resumes.oss-cn-beijing.aliyuncs.com/x.pdf')
        self.assertEqual(cm.exception.exit_code, wondercv.EXIT_NETWORK)
        self.assertIn('WONDERCV_CA_FILE', cm.exception.hint)


class ProfileAndSchoolTest(unittest.TestCase):
    """R10：显示选项与学校字段的本地校验。"""

    def test_display_options_are_validated(self):
        self.assertEqual(wondercv._profile_body(['age_birthday=age', 'show_attr_name=0']),
                         {'age_birthday': 'age', 'show_attr_name': 0})
        for bad in ['age_birthday=year', 'show_attr_name=2']:
            with self.subTest(bad=bad), self.assertRaises(wondercv.WcvError):
                wondercv._profile_body([bad])

    def test_school_tags_must_be_known(self):
        args = _args(tags='985,211', university='清华', uni_school='计算机学院')
        args.tags = '985,211'
        body = wondercv._entry_body('edu', args)
        self.assertEqual(body['show_tags'], ['985', '211'])
        self.assertEqual(body['uni_school'], '计算机学院')
        args.tags = '985,自造标签'
        with self.assertRaises(wondercv.WcvError):
            wondercv._entry_body('edu', args)


class P2ValidationTest(unittest.TestCase):
    """R13/R14：类别与作品的本地校验（写入前拒绝）。"""

    def setUp(self):
        self.api = FakeAPI()
        self.tok = 'AAAA0001'

    def test_unknown_category_rejected_without_write(self):
        with self.assertRaises(wondercv.WcvError):
            wondercv.add_section(self.api, self.tok, '模块', category='nosuch')
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])

    def test_non_custom_category_is_unique_per_resume(self):
        self.api.cvs[self.tok]['submodule_info']['sections_position'].append(
            {'id': None, 'type': 'honor', 'info_ids': []})
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_section(self.api, self.tok, '荣誉', category='honor')
        self.assertIn('已有', str(cm.exception))
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])

    def test_opus_title_limit_rejected_before_create(self):
        with self.assertRaises(wondercv.WcvError):
            wondercv.add_opus(self.api, self.tok, '字' * 21, '')
        with self.assertRaises(wondercv.WcvError):
            wondercv.add_opus(self.api, self.tok, '', '')
        self.assertFalse([c for c in self.api.calls if c[0] != 'GET'])


if __name__ == '__main__':
    unittest.main()

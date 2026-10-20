"""真实账号上的增删改查与故障恢复实测。

只操作测试夹具简历（CLI-TEST-*，token 记录在 ~/.config/wondercv/test-fixtures.json，权限 600）。
不触碰真实简历，不打开网页编辑器。

运行（需要有效凭证）：
  WONDERCV_LIVE=1 python3 -m unittest tests.test_live -v
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import wondercv  # noqa: E402
from tests.fakes import FaultyClient  # noqa: E402

FIXTURE_FILE = os.path.expanduser('~/.config/wondercv/test-fixtures.json')
TRASH_LIMIT_OBSERVED = 10  # 实测：回收站满 10 份后再移入返回 300101


def cli(*argv):
    """以 --json 运行命令，返回解析后的结果；失败时抛 AssertionError 并带上 stderr。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = wondercv.main(['--json', *argv])
    assert rc == 0, f'{argv} 失败(rc={rc}): {err.getvalue().strip()}'
    return json.loads(out.getvalue()) if out.getvalue().strip() else None


def cli_rc(*argv):
    """运行命令并返回 (退出码, stderr)，不断言成功。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = wondercv.main(['--json', *argv])
    return rc, err.getvalue()


def cli_fails(*argv):
    rc, err = cli_rc(*argv)
    assert rc != 0, f'{argv} 本应失败'
    return err


def section(shown, typ=None, sid=None):
    return next(s for s in shown['sections'] if (typ is None or s['type'] == typ) and (sid is None or s['id'] == sid))


def client():
    return wondercv.Client(wondercv.load_token())


def trash_count():
    return len(wondercv.list_trash(client()))


def items_section(token):
    """条目测试专用的持久模块：只创建一次，记录在测试夹具文件中，不删除（避免回收站满时留下残留）。"""
    store = _store()
    sid = store.get('items_sid')
    if sid in {x['id'] for x in cli('section', 'list', token)}:
        return sid
    sid = cli('section', 'add', token, '--title', 'CLI-TEST-条目')['id']
    store['items_sid'] = sid
    _save_store(store)
    return sid


def fixture_token(key):
    """取得（必要时新建）测试夹具简历，并把 token 记录到权限受限的文件中。"""
    c = client()
    store = {}
    if os.path.exists(FIXTURE_FILE):
        with open(FIXTURE_FILE, encoding='utf-8') as f:
            store = json.load(f)
    live = {r['token'] for r in wondercv.list_resumes(c)}
    if store.get(key) in live:
        return store[key]
    token = cli('resume', 'create', '--name', time.strftime(f'CLI-TEST-{key.upper()}-%m%d-%H%M%S'))['token']
    store[key] = token
    os.makedirs(os.path.dirname(FIXTURE_FILE), exist_ok=True)
    fd = os.open(FIXTURE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    return token


def reset_fixture(token):
    """清空夹具中由测试创建的内容（保留零基础模板自带的“个人总结”“项目经历”模块与隔离测试的共享模块）。"""
    c = client()
    protected = {_store().get('iso_sid'), _store().get('items_sid')}
    for kind in ('edu', 'work', 'org'):
        for e in wondercv.list_entries(c, token, kind):
            wondercv.rm_entry(c, token, kind, e['id'])
    for ext in wondercv.list_extras(c, token):
        wondercv.rm_extra(c, token, ext['kind'])
    items_sid = _store().get('items_sid')
    for pos in wondercv.get_cv(c, token)['submodule_info']['sections_position']:
        if pos['id'] is not None and pos['id'] == items_sid:
            for iid in list(pos['info_ids'] or []):
                wondercv.rm_item(c, token, pos['id'], iid)
    if wondercv.list_trash(c):  # 回收站非空：自定义模块删除按 fail-closed 拒绝，保留模块（不影响条目测试）
        return
    for pos in wondercv.get_cv(c, token)['submodule_info']['sections_position']:
        if pos['id'] is not None and pos['type'] == 'custom' and pos['id'] != protected:
            wondercv.rm_section(c, token, pos['id'])


def _store():
    if not os.path.exists(FIXTURE_FILE):
        return {}
    with open(FIXTURE_FILE, encoding='utf-8') as f:
        return json.load(f)


def _save_store(store):
    os.makedirs(os.path.dirname(FIXTURE_FILE), exist_ok=True)
    fd = os.open(FIXTURE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def _raw_submodule(token, fn):
    """测试辅助：直接按给定函数改写 submodule_info（用于构造“保存顺序”不同于接口返回顺序的数据）。"""
    wondercv.modify_submodule(client(), token, fn)


def _gone(path_kind, oid, extra=None):
    """直接查询对象是否仍存在（不经过 CLI）。"""
    c = client()
    if path_kind in ('edus', 'works', 'orgs'):
        return not c.call('GET', f'/v3/{path_kind}', {'ids': str(oid), 'lang': 'cn', 'only_desc': 'false'})[path_kind]
    raise AssertionError(path_kind)


@unittest.skipUnless(os.environ.get('WONDERCV_LIVE') == '1', '设置 WONDERCV_LIVE=1 才访问真实账号')
class LiveCrudTest(unittest.TestCase):
    token = None

    @classmethod
    def setUpClass(cls):
        cls.token = fixture_token('main')
        reset_fixture(cls.token)
        cls.name = cli('resume', 'show', cls.token)['cv_name']

    def test_a_resume_structure(self):
        shown = cli('resume', 'show', self.token)
        types = [s['type'] for s in shown['sections']]
        for typ in ['edus', 'extras', 'personal_summary', 'project_experience']:
            self.assertIn(typ, types, '夹具应包含零基础模板的模块')
        dump = cli('resume', 'dump', self.token)
        self.assertEqual({'basic_info', 'format_info', 'submodule_info', 'modules'} - set(dump), set())
        cli('resume', 'rename', self.token, self.name + '-改')
        self.assertEqual(cli('resume', 'show', self.token)['cv_name'], self.name + '-改')
        cli('resume', 'rename', self.token, self.name)
        self.assertEqual(cli('resume', 'show', self.token)['cv_name'], self.name)

    def test_a2_text_mode_and_bad_token(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wondercv.main(['resume', 'show', self.token]), 0)
        self.assertIn('sections:', out.getvalue())
        err = io.StringIO()
        old = os.environ.get('WONDERCV_TOKEN')
        os.environ['WONDERCV_TOKEN'] = 'bogus-token'
        try:
            with contextlib.redirect_stderr(err):
                self.assertEqual(wondercv.main(['resume', 'list']), wondercv.EXIT_API)
        finally:
            if old is None:
                os.environ.pop('WONDERCV_TOKEN', None)
            else:
                os.environ['WONDERCV_TOKEN'] = old
        self.assertIn('凭证失效', err.getvalue())

    def test_b_profile(self):
        cli('profile', 'set', self.token, 'username=测试用户', 'email=test@example.com', 'phone=13800000000')
        p = cli('profile', 'show', self.token)
        self.assertEqual((p['username'], p['email'], p['phone']), ('测试用户', 'test@example.com', '13800000000'))
        cli('profile', 'set', self.token, 'address=上海')  # 部分更新不应清空其它字段
        p = cli('profile', 'show', self.token)
        self.assertEqual((p['username'], p['address']), ('测试用户', '上海'))
        cli('profile', 'set', self.token, 'birthday=2024-02-29')
        self.assertEqual(cli('profile', 'show', self.token)['birthday'], '2024-02-29')
        self.assertIn('无效字段', cli_fails('profile', 'set', self.token, 'nosuch=1'))
        self.assertIn('不存在', cli_fails('profile', 'set', self.token, 'birthday=2023-02-29'))

    def test_c_education(self):
        e = cli('edu', 'add', self.token, '--university', '测试大学', '--major', '计算机科学', '--degree', '本科',
                '--start', '2018-09', '--end', '2022-06', '--edu-type', '全日制', '--city', '北京',
                '--description', '<p>成绩优秀</p>')
        eid = e['id']
        x = cli('edu', 'list', self.token)[0]
        self.assertEqual((x['id'], x['degree'], x['start_at'][:7], x['edu_type'], x['city']),
                         (eid, '本科', '2018-09', '全日制', '北京'))
        cli('edu', 'set', self.token, str(eid), '--major', '软件工程', '--degree', '硕士', '--end', '2023-06')
        x = cli('edu', 'list', self.token)[0]
        self.assertEqual((x['major'], x['degree'], x['degree_id'], x['end_at'][:7], x['university']),
                         ('软件工程', '硕士', 3, '2023-06', '测试大学'))
        shown = cli('resume', 'show', self.token)
        self.assertEqual([(i['id'], i['start_at']) for i in section(shown, 'edus')['items']], [(eid, '2018-09')])
        cli('edu', 'rm', self.token, str(eid))
        self.assertEqual(cli('edu', 'list', self.token), [])
        self.assertEqual(section(cli('resume', 'show', self.token), 'edus')['items'], [])
        self.assertIn('不在', cli_fails('edu', 'rm', self.token, str(eid)))

    def test_d_work_present_and_end_transitions(self):
        w = cli('work', 'add', self.token, '--company', '测试公司', '--title', '后端工程师', '--dept', '研发部',
                '--start', '2023-07', '--present', '--city', '深圳', '--description', '<ul><li>负责A</li></ul>')
        wid = w['id']
        x = cli('work', 'list', self.token)[0]
        self.assertEqual((x['present'], x['end_at'], x['company']), ('至今', None, '测试公司'))
        cli('work', 'set', self.token, str(wid), '--end', '2024-03')
        x = cli('work', 'list', self.token)[0]
        self.assertEqual((x['end_at'][:7], x['present'], x['title'], x['company']),
                         ('2024-03', None, '后端工程师', '测试公司'))
        cli('work', 'set', self.token, str(wid), '--present')
        x = cli('work', 'list', self.token)[0]
        self.assertEqual((x['present'], x['end_at']), ('至今', None))
        cli('work', 'set', self.token, str(wid), '--title', '高级后端工程师')
        x = cli('work', 'list', self.token)[0]
        self.assertEqual((x['title'], x['present']), ('高级后端工程师', '至今'), '未传入的字段保持不变')
        before = len(cli('work', 'list', self.token))
        self.assertIn('不能同时', cli_fails('work', 'add', self.token, '--present', '--end', '2024-01',
                                           '--company', '不应创建'))
        self.assertEqual(len(cli('work', 'list', self.token)), before, '冲突参数应在写入前拒绝')
        cli('work', 'rm', self.token, str(wid))
        self.assertEqual(cli('work', 'list', self.token), [])

    def test_e_org(self):
        o = cli('org', 'add', self.token, '--name', '测试社团', '--title', '社长', '--start', '2019-09',
                '--end', '2020-06', '--description', '<p>组织活动</p>')
        oid = o['id']
        cli('org', 'set', self.token, str(oid), '--title', '副社长')
        x = cli('org', 'list', self.token)[0]
        self.assertEqual((x['name'], x['title']), ('测试社团', '副社长'))
        cli('org', 'rm', self.token, str(oid))
        self.assertEqual(cli('org', 'list', self.token), [])

    def test_f_extras(self):
        kinds = ['skill', 'language', 'certificate', 'interest', 'other']
        for kind in kinds:
            cli('extra', 'set', self.token, kind, '--desc', f'内容-{kind}')
        listed = {x['kind']: x['description'] for x in cli('extra', 'list', self.token)}
        self.assertEqual(listed, {k: f'内容-{k}' for k in kinds})
        cli('extra', 'set', self.token, 'skill', '--desc', '更新后的技能')  # 已存在则修改
        listed = {x['kind']: x['description'] for x in cli('extra', 'list', self.token)}
        self.assertEqual(listed['skill'], '更新后的技能')
        cli('extra', 'rm', self.token, 'skill')
        listed = {x['kind'] for x in cli('extra', 'list', self.token)}
        self.assertEqual(listed, set(kinds) - {'skill'})
        self.assertIn('没有', cli_fails('extra', 'rm', self.token, 'skill'))

    def test_g_items_on_builtin_section(self):
        """条目增改删与 R02“至今”互斥。使用模板自带的项目经历模块：不创建、不删除模块，回收站满时也可执行。"""
        sid = items_section(self.token)
        self.assertEqual(cli('item', 'list', self.token, str(sid)), [], '夹具中该模块应为空')
        iid = cli('item', 'add', self.token, str(sid), '--title', '职位A', '--name', '项目A', '--start', '2024-01',
                  '--end', '2024-06', '--dept', '测试部', '--description', '<p>做了X</p>')['id']
        try:
            self.assertEqual([i['id'] for i in cli('item', 'list', self.token, str(sid))], [iid])
            cli('item', 'set', self.token, str(sid), str(iid), '--name', '项目A2')
            item = next(i for i in cli('item', 'list', self.token, str(sid)) if i['id'] == iid)
            self.assertEqual((item['name'], item['title'], item['start_at'][:7]), ('项目A2', '职位A', '2024-01'))
            # R02：条目的“至今”与结束日期互相清除
            cli('item', 'set', self.token, str(sid), str(iid), '--present')
            item = next(i for i in cli('item', 'list', self.token, str(sid)) if i['id'] == iid)
            self.assertEqual((item['present'], item['end_at']), ('至今', None))
            cli('item', 'set', self.token, str(sid), str(iid), '--end', '2024-09')
            item = next(i for i in cli('item', 'list', self.token, str(sid)) if i['id'] == iid)
            self.assertEqual((item['present'], item['end_at'][:7]), (None, '2024-09'))
        finally:
            rc, err = cli_rc('item', 'rm', self.token, str(sid), str(iid))
            self.assertEqual(rc, 0, err)
        self.assertEqual(cli('item', 'list', self.token, str(sid)), [])
        self.assertIn('不在模块', cli_fails('item', 'rm', self.token, str(sid), str(iid)))

    def test_g_custom_section_lifecycle(self):
        """自定义模块的创建、改名、删除。回收站非空时删除/改名按 fail-closed 拒绝，本用例跳过且不创建残留。"""
        n = trash_count()
        if n:
            raise unittest.SkipTest(f'回收站非空({n}份)：自定义模块的改名/删除按 fail-closed 拒绝，清理回收站后重跑')
        sid = cli('section', 'add', self.token, '--title', '测试模块')['id']
        try:
            secs = {s['id']: s for s in cli('section', 'list', self.token)}
            self.assertEqual(secs[sid]['title'], '测试模块')
            cli('section', 'rename', self.token, str(sid), '--title', '模块改名')
            self.assertEqual({s['id']: s['title'] for s in cli('section', 'list', self.token)}[sid], '模块改名')
            self.assertIn('不属于', cli_fails('section', 'rename', self.token, '1', '--title', 'x'))
        finally:
            result = cli('section', 'rm', self.token, str(sid))
        self.assertTrue(result['deleted'])
        self.assertNotIn(sid, [s['id'] for s in cli('section', 'list', self.token)])
        self.assertNotIn(sid, [s['id'] for s in cli('resume', 'show', self.token)['sections']])

    def test_ga_module_titles(self):
        titles = cli('module', 'list', self.token)
        self.assertEqual(set(titles), set(wondercv.MODULE_TARGETS))  # 四个总标题 + 五个技能块小标题
        cli('edu', 'add', self.token, '--university', '测试大学', '--degree', '本科', '--start', '2018-09')
        eid = cli('edu', 'list', self.token)[0]['id']
        cli('module', 'title', self.token, 'edus', '--title', '教育背景')
        self.assertEqual(cli('module', 'list', self.token)['edus'], '教育背景')
        self.assertEqual(section(cli('resume', 'show', self.token), 'edus')['title'], '教育背景')
        self.assertEqual([x['id'] for x in cli('edu', 'list', self.token)], [eid], '改标题不应影响条目')
        self.assertIn('长度', cli_fails('module', 'title', self.token, 'edus', '--title', '字' * 16))
        cli('edu', 'rm', self.token, str(eid))
        cli('module', 'title', self.token, 'edus', '--title', '教育经历')  # 还原

    def test_gb_format(self):
        fmt = cli('format', 'show', self.token)
        for key in ['title_color', 'font', 'font_size', 'line_spacing', 'page_margin', 'top_format']:
            self.assertIn(key, fmt)
        top_before = fmt['top_format']
        cli('format', 'set', self.token, 'title_color=#4183ff', 'font=font-2', 'font_size=14',
            'line_spacing=18', 'page_margin=10')
        fmt = cli('format', 'show', self.token)
        self.assertEqual((fmt['title_color'], fmt['font'], fmt['font_size'], fmt['line_spacing'], fmt['page_margin']),
                         ('#4183FF', 'font-2', 14, 18, 10))
        self.assertEqual(fmt['top_format'], top_before, '未传入的版式字段应保持不变')
        self.assertIn('颜色', cli_fails('format', 'set', self.token, 'title_color=red'))
        self.assertIn('字体', cli_fails('format', 'set', self.token, 'font=font-999'))
        self.assertIn('字号', cli_fails('format', 'set', self.token, 'font_size=30'))

    def test_h_trash_restore_fixture(self):
        rc, err = cli_rc('resume', 'rm', self.token)
        if rc != 0 and '300101' in err:
            raise unittest.SkipTest('回收站数量已达上限(300101)，清理后重跑可验证移入/恢复')
        assert rc == 0, err
        self.assertIn(self.token, [r['token'] for r in cli('resume', 'trash')])
        self.assertNotIn(self.token, [r['token'] for r in cli('resume', 'list')])
        cli('resume', 'restore', self.token)
        self.assertIn(self.token, [r['token'] for r in cli('resume', 'list')])
        self.assertNotIn(self.token, [r['token'] for r in cli('resume', 'trash')])

    def test_hb_purge_disposable(self):
        """永久删除有频次保护；此测试用一次性简历，额度不足时明确跳过，不影响夹具。"""
        if len(cli('resume', 'trash')) >= TRASH_LIMIT_OBSERVED:
            raise unittest.SkipTest(f'回收站已达 {TRASH_LIMIT_OBSERVED} 份上限(300101)，先等待永久删除额度恢复')
        disposable = cli('resume', 'create', '--name', time.strftime('CLI-TEST-PURGE-%m%d-%H%M%S'))['token']
        rc, err = cli_rc('resume', 'rm', disposable)
        if rc != 0:
            self._skip_if_platform_limit(err)
            raise AssertionError(err)
        rc, err = cli_rc('resume', 'purge', disposable)
        if rc != 0:
            self._skip_if_platform_limit(err)
            raise AssertionError(err)
        self.assertNotIn(disposable, [r['token'] for r in cli('resume', 'trash')])

    def _skip_if_platform_limit(self, err):
        if '300102' in err:
            raise unittest.SkipTest('永久删除频次保护(300102)：删除间隔需至少 24 小时，之后重跑可验证')
        if '300101' in err:
            raise unittest.SkipTest('回收站数量已达上限(300101)，需先永久删除旧的测试简历（受 300102 限制）')

    def test_i_full_read_contains_all_writable_fields(self):
        """R04：所有可写字段都能从 show/dump 读回，文本输出展示实际值。"""
        cli('edu', 'add', self.token, '--university', '读回大学', '--major', '专业X', '--degree', '硕士',
            '--start', '2015-09', '--end', '2019-06', '--edu-type', '非全日制', '--city', '杭州')
        cli('work', 'add', self.token, '--company', '读回公司', '--title', '岗位Z', '--dept', '部门W',
            '--city', '广州', '--start', '2020-01', '--present', '--description', '<p>工作内容</p>')
        sid = items_section(self.token)
        iid = cli('item', 'add', self.token, str(sid), '--title', '条目T', '--name', '名称N', '--dept', '部门D',
                  '--city', '上海', '--start', '2021-03', '--end', '2021-08', '--description', '<p>条目描述</p>')['id']
        cli('extra', 'set', self.token, 'skill', '--desc', '技能一，技能二')
        dump = cli('resume', 'dump', self.token)
        edu = next(o for m in dump['modules'] for o in m['objects'] if m['type'] == 'edus' and o['university'] == '读回大学')
        self.assertEqual((edu['major'], edu['edu_type'], edu['city'], edu['degree']),
                         ('专业X', '非全日制', '杭州', '硕士'))
        work = next(o for m in dump['modules'] for o in m['objects'] if m['type'] == 'works' and o['company'] == '读回公司')
        self.assertEqual((work['dept'], work['city'], work['present']), ('部门W', '广州', '至今'))
        self.assertIn('工作内容', work['description'])
        info = next(o for m in dump['modules'] if m['id'] == sid for o in m['objects'])
        self.assertEqual((info['title'], info['name'], info['dept'], info['city']), ('条目T', '名称N', '部门D', '上海'))
        self.assertEqual(info['end_at'][:7], '2021-08')
        shown = cli('resume', 'show', self.token)
        self.assertEqual(section(shown, 'edus')['items'][0]['city'], '杭州')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wondercv.main(['resume', 'show', self.token])
        text = out.getvalue()
        for expect in ['读回大学', '非全日制', '杭州', '广州', '部门W', '名称N', '上海', '技能一']:
            self.assertIn(expect, text)
        cli('item', 'rm', self.token, str(sid), str(iid))

    def test_j_saved_order_is_respected(self):
        """R05：接口返回顺序与保存顺序不同，CLI 仍按保存顺序输出。"""
        ids = [cli('work', 'add', self.token, '--title', f'顺序{t}', '--start', '2020-01')['id'] for t in 'ABC']
        _raw_submodule(self.token, lambda sub: sub.__setitem__('works_ids', [ids[2], ids[0], ids[1]]))
        self.assertEqual([x['title'] for x in cli('work', 'list', self.token)], ['顺序C', '顺序A', '顺序B'])
        shown = section(cli('resume', 'show', self.token), 'works')
        self.assertEqual([x['title'] for x in shown['items']], ['顺序C', '顺序A', '顺序B'])
        for oid in ids:
            cli('work', 'rm', self.token, str(oid))

        for kind in ('skill', 'language'):
            cli('extra', 'set', self.token, kind, '--desc', '顺序测试块')
        _raw_submodule(self.token, lambda sub: sub.__setitem__('extras_position', ['languages', 'skills']))
        order = [x['kind'] for x in cli('extra', 'list', self.token) if x['kind'] in ('skill', 'language')]
        self.assertEqual(order, ['language', 'skill'])
        for kind in ('skill', 'language'):
            cli('extra', 'rm', self.token, kind)

    def test_j_items_order_on_builtin_section(self):
        """R05：条目按模块位的 info_ids 保存顺序输出（使用模板自带模块，不创建/删除模块）。"""
        sid = items_section(self.token)
        iids = [cli('item', 'add', self.token, str(sid), '--title', f'条{t}')['id'] for t in 'XYZ']
        try:
            _raw_submodule(self.token, lambda sub: [p.__setitem__('info_ids', [iids[1], iids[2], iids[0]])
                                                    for p in sub['sections_position'] if p['id'] == sid])
            self.assertEqual([x['title'] for x in cli('item', 'list', self.token, str(sid))], ['条Y', '条Z', '条X'])
            self.assertEqual([x['title'] for x in section(cli('resume', 'show', self.token), sid=sid)['items']],
                             ['条Y', '条Z', '条X'])
        finally:
            for iid in iids:
                cli('item', 'rm', self.token, str(sid), str(iid))


@unittest.skipUnless(os.environ.get('WONDERCV_LIVE') == '1', '设置 WONDERCV_LIVE=1 才访问真实账号')
class LiveRecoveryTest(unittest.TestCase):
    """R01：在真实账号上注入步骤故障，验证引用状态、回滚与恢复入口。"""

    token = None

    @classmethod
    def setUpClass(cls):
        cls.token = fixture_token('main')
        reset_fixture(cls.token)

    def setUp(self):
        self.real = client()
        self.fc = FaultyClient(self.real)
        self.cv_path = re.escape(f'/v3/cvs/{self.token}')
        for e in wondercv.list_entries(self.real, self.token, 'work'):  # 清理上一用例的残留
            wondercv.rm_entry(self.real, self.token, 'work', e['id'])

    def _attached_works(self):
        return self.real.call('GET', f'/v3/cvs/{self.token}')['submodule_info']['works_ids']

    def test_r01_attach_failure_rolls_back_new_work(self):
        self.fc.inject('PUT', self.cv_path, 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_entry(self.fc, self.token, 'work', {'title': 'R01-回滚'})
        err = cm.exception
        self.assertEqual(err.state, 'none')
        oid = int(err.objects[0].split()[-1])
        self.assertTrue(_gone('works', oid), '挂载失败后新建对象应被回滚删除')
        self.assertNotIn('R01-回滚', [x['title'] for x in cli('work', 'list', self.token)])

    def test_r01_unknown_attach_that_applied_is_success_once(self):
        self.fc.inject_after('PUT', self.cv_path, 'unknown')
        item = wondercv.add_entry(self.fc, self.token, 'work', {'title': 'R01-已生效'})
        listed = cli('work', 'list', self.token)
        self.assertEqual([x['id'] for x in listed], [item['id']], '核对后应视为成功，且无重复条目')
        cli('work', 'rm', self.token, str(item['id']))

    def test_r01_unknown_attach_not_applied_then_orphan_clean(self):
        self.fc.inject('PUT', self.cv_path, 'unknown')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_entry(self.fc, self.token, 'work', {'title': 'R01-未知'})
        err = cm.exception
        self.assertEqual(err.state, 'unknown')
        oid = int(err.objects[0].split()[-1])
        self.assertFalse(_gone('works', oid), '结果未知时不回滚，对象应仍存在')
        self.assertNotIn(oid, self._attached_works())
        self.assertEqual(cli('orphan', 'clean', 'work', str(oid)), {'deleted': True, 'id': oid})
        self.assertTrue(_gone('works', oid))

    def test_r01_rollback_failure_is_partial_and_recoverable(self):
        self.fc.inject('PUT', self.cv_path, 'none')
        self.fc.inject('DELETE', re.escape('/v3/works/') + r'\d+', 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_entry(self.fc, self.token, 'work', {'title': 'R01-回滚失败'})
        err = cm.exception
        self.assertEqual(err.state, 'partial')
        oid = int(err.objects[0].split()[-1])
        self.assertIn('orphan clean', err.hint)
        self.assertFalse(_gone('works', oid))
        self.assertEqual(cli('orphan', 'clean', 'work', str(oid))['deleted'], True)
        self.assertTrue(_gone('works', oid))

    def test_r01_detach_applied_but_response_lost(self):
        item = cli('work', 'add', self.token, '--title', 'R01-摘除已生效')
        self.fc.inject_after('PUT', self.cv_path, 'unknown')
        self.assertEqual(wondercv.rm_entry(self.fc, self.token, 'work', item['id']), {'deleted': True})
        self.assertTrue(_gone('works', item['id']))
        self.assertEqual(cli('work', 'list', self.token), [])

    def test_r01_delete_failure_after_detach_is_partial_then_clean(self):
        item = cli('work', 'add', self.token, '--title', 'R01-删除失败')
        self.fc.inject('DELETE', f"/v3/works/{item['id']}", 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.rm_entry(self.fc, self.token, 'work', item['id'])
        self.assertEqual(cm.exception.state, 'partial')
        self.assertEqual(cli('work', 'list', self.token), [], '引用已解除')
        self.assertFalse(_gone('works', item['id']), '对象仍存在，需清理')
        self.assertEqual(cli('orphan', 'clean', 'work', str(item['id']))['deleted'], True)
        self.assertTrue(_gone('works', item['id']))

    def test_r01_orphan_clean_refuses_referenced_object(self):
        item = cli('work', 'add', self.token, '--title', 'R01-被引用')
        try:
            rc, err = cli_rc('orphan', 'clean', 'work', str(item['id']))
            self.assertEqual(rc, wondercv.EXIT_INPUT)
            self.assertIn('仍被', err)
            self.assertFalse(_gone('works', item['id']))
        finally:
            cli('work', 'rm', self.token, str(item['id']))

    def test_r01_extra_attach_failure_rolls_back(self):
        before = {x['kind'] for x in cli('extra', 'list', self.token)}
        self.fc.inject('PUT', self.cv_path, 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.set_extra(self.fc, self.token, 'other', 'R01-其他块')
        self.assertEqual(cm.exception.state, 'none')
        self.assertEqual({x['kind'] for x in cli('extra', 'list', self.token)}, before)
        self.assertIsNone(self.real.call('GET', f'/v3/cvs/{self.token}')['submodule_info']['other_id'])

    def test_r01_section_attach_failure_rolls_back(self):
        self.fc.inject('PUT', self.cv_path, 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_section(self.fc, self.token, 'R01-模块')
        self.assertEqual(cm.exception.state, 'none')
        self.assertNotIn('R01-模块', [s['title'] for s in cli('section', 'list', self.token)])

    def test_r01_item_attach_failure_rolls_back(self):
        sid = items_section(self.token)
        try:
            self.fc.inject('PUT', self.cv_path, 'none')
            with self.assertRaises(wondercv.WcvError) as cm:
                wondercv.add_item(self.fc, self.token, sid, {'title': 'R01-条目新增'})
            self.assertEqual(cm.exception.state, 'none')
            self.assertEqual(cli('item', 'list', self.token, str(sid)), [])
            pos = next(p for p in self.real.call('GET', f'/v3/cvs/{self.token}')['submodule_info']['sections_position']
                       if p['id'] == sid)
            self.assertEqual(pos['info_ids'], [], '挂载失败后模块引用不应变化')
        finally:
            pass  # 模块为模板自带，不得删除

    def test_r01_item_detach_failure_then_clean(self):
        sid = items_section(self.token)
        iid = cli('item', 'add', self.token, str(sid), '--title', 'R01-条目')['id']
        self.fc.inject('DELETE', re.escape(f'/v3/custom_sections/{sid}/infos/{iid}'), 'none')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.rm_item(self.fc, self.token, sid, iid)
        self.assertEqual(cm.exception.state, 'partial')
        self.assertEqual(cli('item', 'list', self.token, str(sid)), [])
        self.assertEqual(cli('orphan', 'clean', 'item', str(iid), '--sid', str(sid))['deleted'], True)
        # 模块为模板自带，不得删除


def iso_pair():
    """长期隔离夹具：源=main，副本=iso_copy（经 CLI copy 复制）。共享模块 iso_sid 同时存在于两者。"""
    c = client()
    main = fixture_token('main')
    store = _store()
    live = {r['token'] for r in wondercv.list_resumes(c)}
    if store.get('iso_copy') in live and store.get('iso_src') == main and store.get('iso_sid'):
        if store['iso_sid'] in wondercv.get_cv(c, store['iso_copy'])['submodule_info']['custom_sections_ids']:
            return main, store['iso_copy'], store['iso_sid']
    sid = cli('section', 'add', main, '--title', 'ISO-共享模块')['id']
    copy = cli('resume', 'copy', main, '--name', time.strftime('CLI-TEST-ISO-COPY-%m%d-%H%M%S'))['token']
    assert sid in wondercv.get_cv(c, copy)['submodule_info']['custom_sections_ids'], '复制后应共享自定义模块'
    store.update(iso_copy=copy, iso_sid=sid, iso_src=main)
    _save_store(store)
    return main, copy, sid


def _module_keys(token):
    return [m['id'] if m['id'] is not None else m['type'] for m in cli('resume', 'dump', token)['modules']]


@unittest.skipUnless(os.environ.get('WONDERCV_LIVE') == '1', '设置 WONDERCV_LIVE=1 才访问真实账号')
class LiveP1Test(unittest.TestCase):
    """P1（R07–R12）真实账号验证。所有写入只在夹具 main 与 iso_copy 上进行。"""

    token = None

    @classmethod
    def setUpClass(cls):
        cls.token = fixture_token('main')
        reset_fixture(cls.token)

    def test_p1_07_subtitles_of_extras(self):
        before = cli('module', 'list', self.token)
        cli('extra', 'set', self.token, 'skill', '--desc', '小标题测试内容')
        try:
            cli('module', 'title', self.token, 'skill', '--title', '  技能测试：  ')
            after = cli('module', 'list', self.token)
            self.assertEqual(after['skill'], '技能测试：', '保存前应去除首尾空白')
            self.assertEqual({k: v for k, v in after.items() if k != 'skill'},
                             {k: v for k, v in before.items() if k != 'skill'}, '其它标题不应变化')
            item = next(i for i in section(cli('resume', 'show', self.token), 'extras')['items'] if i['kind'] == 'skill')
            self.assertEqual(item['subtitle'], '技能测试：')
            before_fail = cli('module', 'list', self.token)
            self.assertIn('不能为空', cli_fails('module', 'title', self.token, 'skill', '--title', '   '))
            self.assertIn('长度', cli_fails('module', 'title', self.token, 'skill', '--title', '字' * 16))
            self.assertEqual(cli('module', 'list', self.token), before_fail, '非法标题不得写入')
        finally:
            cli('module', 'title', self.token, 'skill', '--title', before['skill'])
            cli('extra', 'rm', self.token, 'skill')

    def test_p1_08_theme_and_format_options(self):
        orig = cli('format', 'show', self.token)
        keys = ['title_color', 'top_line_color', 'title_select_color', 'top_line_select_color', 'top_format',
                'head_format', 'title_format', 'body_format', 'time_format', 'format_priority', 'page_size',
                'punctuation_format', 'page_margin', 'font_size', 'font']
        try:
            cli('format', 'theme', self.token, '--color', '#4183ff')
            f = cli('format', 'show', self.token)
            self.assertEqual((f['title_color'], f['top_line_color'], f['title_select_color'],
                              f['top_line_select_color']), ('#4183FF', '#4183FF', False, False))
            cli('format', 'theme', self.token, '--color', '#6DB557', '--custom')
            f = cli('format', 'show', self.token)
            self.assertEqual((f['title_color'], f['top_line_color'], f['title_select_color'],
                              f['top_line_select_color']), ('#6DB557', '#6DB557', True, True))
            cli('format', 'set', self.token, 'title_color=#CA3832')  # 标题颜色单独修改
            f = cli('format', 'show', self.token)
            self.assertEqual((f['title_color'], f['top_line_color']), ('#CA3832', '#6DB557'))

            cli('format', 'set', self.token, 'page_margin=5')
            for key, enum in [('top_format', wondercv.TOP_FORMATS), ('head_format', wondercv.HEAD_FORMATS),
                              ('title_format', wondercv.TITLE_FORMATS), ('body_format', wondercv.BODY_FORMATS),
                              ('time_format', wondercv.TIME_FORMATS), ('format_priority', wondercv.PRIORITY_FORMATS),
                              ('punctuation_format', wondercv.PUNCTUATION_FORMATS),
                              ('page_size', wondercv.PAGE_SIZES)]:
                for value in enum:
                    with self.subTest(key=key, value=value):
                        cli('format', 'set', self.token, f'{key}={value}')
                        self.assertEqual(cli('format', 'show', self.token)[key], value)
            for margin in wondercv.PAGE_MARGINS:
                cli('format', 'set', self.token, f'page_margin={margin}')
                self.assertEqual(cli('format', 'show', self.token)['page_margin'], margin)
            cli('format', 'set', self.token, 'top_format=left_block', 'page_margin=5')
            self.assertIn('仅支持页边距 5', cli_fails('format', 'set', self.token, 'page_margin=10'))
            self.assertEqual(cli('format', 'show', self.token)['page_margin'], 5, '被拒绝的组合不得写入')
            before_bad = cli('format', 'show', self.token)
            self.assertIn('可选值', cli_fails('format', 'set', self.token, 'top_format=unknown'))
            self.assertEqual(cli('format', 'show', self.token), before_bad)
            self.assertEqual(set(cli('format', 'options')), set(wondercv.format_options()))
        finally:
            restore = [f'{k}={orig[k]}' for k in ('page_margin', 'top_format', 'head_format', 'title_format',
                                                  'body_format', 'time_format', 'format_priority', 'page_size',
                                                  'punctuation_format', 'font_size', 'font')]
            cli('format', 'set', self.token, *restore)
            cli('format', 'theme', self.token, '--color', orig['title_color'])
            cli('format', 'set', self.token, f"title_color={orig['title_color']}")
            final = cli('format', 'show', self.token)
            self.assertEqual({k: final[k] for k in keys}, {k: orig[k] for k in keys}, '测试应恢复原版式')

    def test_p1_09_reorder(self):
        ids = [cli('work', 'add', self.token, '--title', f'R09-{t}', '--start', '2020-01')['id'] for t in 'ABC']
        try:
            listed = cli('work', 'reorder', self.token, *[str(i) for i in reversed(ids)])
            self.assertEqual([x['title'] for x in listed], ['R09-C', 'R09-B', 'R09-A'])
            self.assertEqual([x['title'] for x in cli('work', 'list', self.token)], ['R09-C', 'R09-B', 'R09-A'])
            shown = section(cli('resume', 'show', self.token), 'works')
            self.assertEqual([x['title'] for x in shown['items']], ['R09-C', 'R09-B', 'R09-A'])
            before = cli('work', 'list', self.token)
            self.assertIn('重复', cli_fails('work', 'reorder', self.token, str(ids[0]), str(ids[0]), str(ids[1])))
            self.assertIn('必须恰好', cli_fails('work', 'reorder', self.token, str(ids[0]), str(ids[1])))
            self.assertEqual(cli('work', 'list', self.token), before, '非法排列不得写入')
        finally:
            for i in ids:
                cli('work', 'rm', self.token, str(i))

        sid = items_section(self.token)  # 模板自带模块：不创建、不删除模块
        iids = [cli('item', 'add', self.token, str(sid), '--title', f'条{t}')['id'] for t in 'XYZ']
        try:
            cli('item', 'reorder', self.token, str(sid), *[str(i) for i in iids[::-1]])
            self.assertEqual([x['title'] for x in cli('item', 'list', self.token, str(sid))], ['条Z', '条Y', '条X'])
            self.assertEqual([x['title'] for x in section(cli('resume', 'show', self.token), sid=sid)['items']],
                             ['条Z', '条Y', '条X'])
            self.assertIn('必须恰好', cli_fails('item', 'reorder', self.token, str(sid), str(iids[0])))
        finally:
            for iid in iids:
                cli('item', 'rm', self.token, str(sid), str(iid))

        for kind in ('skill', 'language', 'interest'):
            cli('extra', 'set', self.token, kind, '--desc', f'R09-{kind}')
        try:
            cli('extra', 'reorder', self.token, 'interest', 'skill', 'language')
            kinds = [x['kind'] for x in cli('extra', 'list', self.token)]
            self.assertEqual([k for k in kinds if k in ('skill', 'language', 'interest')],
                             ['interest', 'skill', 'language'])
            self.assertIn('必须恰好', cli_fails('extra', 'reorder', self.token, 'skill'))
        finally:
            for kind in ('skill', 'language', 'interest'):
                cli('extra', 'rm', self.token, kind)

        original = _module_keys(self.token)
        try:
            cli('resume', 'reorder', self.token, *[str(k) for k in reversed(original)])
            self.assertEqual(_module_keys(self.token), list(reversed(original)))
            self.assertEqual([s['id'] if s['id'] is not None else s['type']
                              for s in cli('resume', 'show', self.token)['sections']], list(reversed(original)))
            self.assertIn('必须恰好', cli_fails('resume', 'reorder', self.token, 'edus'))
        finally:
            cli('resume', 'reorder', self.token, *[str(k) for k in original])
        self.assertEqual(_module_keys(self.token), original)

    def test_p1_10_school_and_display_options(self):
        eid = cli('edu', 'add', self.token, '--university', '显示测试大学', '--uni-school', '计算机学院',
                  '--tags', '985,211', '--start', '2016-09')['id']
        try:
            x = cli('edu', 'list', self.token)[0]
            self.assertEqual((x['uni_school'], x['show_tags']), ('计算机学院', ['985', '211']))
            cli('edu', 'set', self.token, str(eid), '--tags', '')
            self.assertEqual(cli('edu', 'list', self.token)[0]['show_tags'], [])
            cli('edu', 'set', self.token, str(eid), '--uni-school', '信息学院')
            self.assertEqual(cli('edu', 'list', self.token)[0]['uni_school'], '信息学院')
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                wondercv.main(['edu', 'add', self.token, '--second-major', '副修'])  # 网页未发现编辑入口，CLI 不提供
        finally:
            cli('edu', 'rm', self.token, str(eid))

        cli('profile', 'set', self.token, 'username=显示测试')
        cli('profile', 'set', self.token, 'age_birthday=age', 'show_attr_name=0')
        p = cli('profile', 'show', self.token)
        self.assertEqual((p['format_pref']['age_birthday']['chosen'], p['show_attr_name']), ('age', 0))
        self.assertEqual(p['username'], '显示测试', '显示选项写入不应影响其它字段')
        cli('profile', 'set', self.token, 'age_birthday=birthday', 'show_attr_name=1')
        p = cli('profile', 'show', self.token)
        self.assertEqual((p['format_pref']['age_birthday']['chosen'], p['show_attr_name']), ('birthday', 1))
        self.assertIn('show_attr_name', cli_fails('profile', 'set', self.token, 'show_attr_name=2'))

    def test_p1_11_templates(self):
        templates = cli('template', 'list')
        self.assertIn('rrEuiEA', [t['token'] for t in templates])
        stu = next(t['token'] for t in templates if t['cv_type'] == 'stu_cn')
        store = _store()
        live = {r['token'] for r in wondercv.list_resumes(client())}
        if store.get('tpl') not in live:
            store['tpl'] = cli('resume', 'create', '--name', time.strftime('CLI-TEST-TPL-%m%d-%H%M%S'),
                               '--template', stu)['token']
            _save_store(store)
        shown = cli('resume', 'show', store['tpl'])
        self.assertEqual(cli('resume', 'dump', store['tpl'])['basic_info']['cv_type'], 'stu_cn')
        self.assertGreater(len(shown['sections']), 0)
        rc, err = cli_rc('resume', 'create', '--name', 'x', '--template', 'NOTEMPLATE')
        self.assertEqual(rc, wondercv.EXIT_INPUT)

    def test_p1_12_copy_isolation(self):
        main, copy, sid = iso_pair()
        c = client()
        self.assertIn(sid, wondercv.get_cv(c, copy)['submodule_info']['custom_sections_ids'])
        # 清理旧夹具遗留的、原件已不再引用的自定义模块（只处理 custom 类型，不动模板模块）
        main_custom = set(wondercv.get_cv(c, main)['submodule_info']['custom_sections_ids'])
        for pos in wondercv.get_cv(c, copy)['submodule_info']['sections_position']:
            if pos['type'] == 'custom' and pos['id'] not in main_custom and not trash_count():
                wondercv.rm_section(c, copy, pos['id'])
        main_show = cli('resume', 'show', main)
        copy_show = cli('resume', 'show', copy)
        builtin = ('personal_summary', 'edus', 'project_experience', 'orgs', 'extras', 'works')
        self.assertEqual([s['type'] for s in main_show['sections'] if s['type'] in builtin],
                         [s['type'] for s in copy_show['sections'] if s['type'] in builtin],
                         '副本的内置模块顺序应与原件一致')

        # 1. 共享模块不能改名（改名会同时影响原件）
        before_title = section(main_show, sid=sid)['title']
        self.assertIn('共享', cli_fails('section', 'rename', copy, str(sid), '--title', 'ISO-不应生效'))
        self.assertEqual(section(cli('resume', 'show', main), sid=sid)['title'], before_title)

        # 2. 副本新增的工作经历不影响原件
        wid = cli('work', 'add', copy, '--title', 'ISO-副本工作', '--start', '2021-01')['id']
        try:
            self.assertIn('ISO-副本工作', [x['title'] for x in cli('work', 'list', copy)])
            self.assertNotIn('ISO-副本工作', [x['title'] for x in cli('work', 'list', main)])
        finally:
            cli('work', 'rm', copy, str(wid))

        # 3. 副本向共享模块添加条目：副本可见，原件的引用不变
        iid = cli('item', 'add', copy, str(sid), '--title', 'ISO-副本条目')['id']
        try:
            self.assertIn(iid, [x['id'] for x in cli('item', 'list', copy, str(sid))])
            self.assertNotIn(iid, [x['id'] for x in cli('item', 'list', main, str(sid))])
        finally:
            cli('item', 'rm', copy, str(sid), str(iid))

        # 4. 副本修改内置小标题不影响原件
        orig_title = cli('module', 'list', main)['edus']
        cli('module', 'title', copy, 'edus', '--title', 'ISO-副本教育')
        try:
            self.assertEqual(cli('module', 'list', main)['edus'], orig_title)
        finally:
            cli('module', 'title', copy, 'edus', '--title', orig_title)

        # 5. 从副本移除共享模块：服务端应拒绝删除对象（原件仍引用），只解除副本自身的引用。
        #    随后把引用恢复回副本，保证夹具保持共享状态供下次运行。
        copy_cv = wondercv.get_cv(c, copy)['submodule_info']
        pos_before = next(p for p in copy_cv['sections_position'] if p['id'] == sid)
        title_before = copy_cv['custom_sections_titles'][f'{sid}_title']
        if trash_count():  # 回收站非空：删除共享模块在任何写入前被拒绝，副本保持原样
            self.assertIn('回收站', cli_fails('section', 'rm', copy, str(sid)))
            self.assertIn(sid, wondercv.get_cv(c, copy)['submodule_info']['custom_sections_ids'])
            return
        try:
            result = cli('section', 'rm', copy, str(sid))
            self.assertFalse(result['deleted'])
            self.assertEqual(result['still_referenced_by'], [main], '原件仍引用，应保留模块对象')
            self.assertIn(sid, wondercv.get_cv(c, main)['submodule_info']['custom_sections_ids'])
        finally:
            def restore(sub):
                sub['custom_sections_ids'] = list(sub['custom_sections_ids']) + [sid]
                sub['custom_sections_titles'][f'{sid}_title'] = title_before
                sub['sections_position'].append(pos_before)
            wondercv.modify_submodule(c, copy, restore)
        self.assertEqual(section(cli('resume', 'show', copy), sid=sid)['title'], title_before)

    def test_p1_13_export(self):
        path = os.path.join(tempfile.mkdtemp(), 'main.pdf')
        try:
            url = client().call('GET', f'/v4/cvs/{self.token}/download_url',
                                {'watermark': 'no', 'showProfile': True, 'pageNum': 1, 'origin': 'pc',
                                 'platform': 'pc'})['url']
            self.assertTrue(url.startswith('https://') and '.aliyuncs.com/' in url, '应生成签名下载地址')
            with open(path, 'wb') as f:
                f.write(b'occupied')
            self.assertIn('已存在', cli_fails('resume', 'export', self.token, '--format', 'pdf', '--out', path))
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), b'occupied')
            rc, err = cli_rc('resume', 'export', self.token, '--format', 'pdf', '--out', path, '--force')
            if rc != 0:
                if rc == wondercv.EXIT_NETWORK and '证书' in err:
                    raise unittest.SkipTest('导出文件被阻塞：本机信任库缺少 OSS 证书链的受信任 CA，'
                                            '需设置 WONDERCV_CA_FILE 后重跑；文件校验未执行')
                self.fail(err)
            with open(path, 'rb') as f:
                self.assertTrue(f.read(8).startswith(b'%PDF-'))
        finally:
            if os.path.exists(path):
                os.unlink(path)
            if os.path.isdir(os.path.dirname(path)):
                os.rmdir(os.path.dirname(path))


@unittest.skipUnless(os.environ.get('WONDERCV_LIVE') == '1', '设置 WONDERCV_LIVE=1 才访问真实账号')
class LiveP2Test(unittest.TestCase):
    """P2（R13–R16）真实账号验证。只在夹具 main 上写入；结束时恢复原状态。"""

    token = None

    @classmethod
    def setUpClass(cls):
        cls.token = fixture_token('main')
        reset_fixture(cls.token)

    def test_p2_13_duplicate_category_refused(self):
        # 模板已自带“个人总结”“项目经历”，非自定义类别每份简历一个，应被拒绝重复创建（不写入）
        self.assertIn('已有', cli_fails('section', 'add', self.token, '--title', '重复', '--category', 'project_experience'))
        self.assertIn('未知模块类别', cli_fails('section', 'add', self.token, '--title', 'x', '--category', 'nosuch'))

    def test_p2_13_module_categories(self):
        n = trash_count()
        if n:
            raise unittest.SkipTest(f'回收站非空({n}份)：创建的测试模块无法删除（fail-closed），清理回收站后重跑')
        created = {}
        try:
            for cat, title in [('honor', 'P2-荣誉'), ('skill_list', 'P2-技能'), ('reasearch_experience', 'P2-研究'),
                               ('custom', 'P2-自定义')]:
                created[cat] = cli('section', 'add', self.token, '--title', title, '--category', cat)['id']
            listed = {x['id']: x for x in cli('section', 'list', self.token)}
            for cat, sid in created.items():
                self.assertEqual(listed[sid]['category'], cat, f'{cat} 应写入模块类别')
            self.assertIn('已有', cli_fails('section', 'add', self.token, '--title', '重复', '--category', 'honor'))
            sid = created['honor']
            iid = cli('item', 'add', self.token, str(sid), '--title', '荣誉条目', '--start', '2022-06')['id']
            self.assertEqual([x['title'] for x in section(cli('resume', 'show', self.token), sid=sid)['items']],
                             ['荣誉条目'])
            cli('section', 'rename', self.token, str(sid), '--title', 'P2-荣誉改')
            self.assertEqual({x['id']: x['title'] for x in cli('section', 'list', self.token)}[sid], 'P2-荣誉改')
            cli('item', 'rm', self.token, str(sid), str(iid))
        finally:
            for sid in created.values():
                result = cli('section', 'rm', self.token, str(sid))
                self.assertTrue(result['deleted'])
        remaining = [x['id'] for x in cli('section', 'list', self.token)]
        self.assertTrue(set(created.values()).isdisjoint(remaining))

    def test_p2_14_opus(self):
        n = trash_count()
        if n:  # 作品删除按 fail-closed 在回收站非空时拒绝，创建的测试作品将无法清理，因此不创建
            raise unittest.SkipTest(f'回收站非空({n}份)：作品删除被拒绝，不创建测试作品以免留下残留')
        a = cli('opus', 'add', self.token, '--title', 'P2-作品A', '--url', 'https://example.com/a')
        b = cli('opus', 'add', self.token, '--title', 'P2-作品B', '--url', 'https://example.com/b')
        ids = [a['id'], b['id']]
        try:
            self.assertEqual([x['title'] for x in cli('opus', 'list', self.token)], ['P2-作品A', 'P2-作品B'])
            cli('opus', 'reorder', self.token, str(b['id']), str(a['id']))
            self.assertEqual([x['title'] for x in cli('opus', 'list', self.token)], ['P2-作品B', 'P2-作品A'])
            self.assertEqual([x['title'] for x in section(cli('resume', 'show', self.token), 'opus')['items']],
                             ['P2-作品B', 'P2-作品A'])
            cli('opus', 'set', self.token, str(a['id']), '--title', 'P2-作品A改', '--url', 'https://example.com/a2')
            x = next(o for o in cli('opus', 'list', self.token) if o['id'] == a['id'])
            self.assertEqual((x['title'], x['url']), ('P2-作品A改', 'https://example.com/a2'))
            self.assertIn('最多 20', cli_fails('opus', 'add', self.token, '--title', '字' * 21))
            self.assertIn('不能为空', cli_fails('opus', 'set', self.token, str(a['id']), '--title', ''))
            self.assertIn('必须恰好', cli_fails('opus', 'reorder', self.token, str(a['id'])))
        finally:
            for oid in ids:
                rc, err = cli_rc('opus', 'rm', self.token, str(oid))
                self.assertEqual(rc, 0, err)
        self.assertEqual([x for x in cli('opus', 'list', self.token) if x['id'] in ids], [])

    def test_p2_15_school_logo(self):
        found = cli('school-logo', 'search', self.token, '--name', '清华大学')
        self.assertIn(3, [x['id'] for x in found])
        self.assertIn('不在', cli_fails('school-logo', 'set', self.token, '--name', '清华大学', '--id', '999999'))
        try:
            r = cli('school-logo', 'set', self.token, '--name', '清华大学', '--id', '3')
            self.assertEqual(r['school_logo_id'], 3)
            basic = wondercv.get_cv(client(), self.token)['basic_info']
            self.assertEqual((basic['school_logo_id'], bool(basic['school_logo_url'])), (3, True))
        finally:
            cli('school-logo', 'rm', self.token)
        basic = wondercv.get_cv(client(), self.token)['basic_info']
        self.assertEqual((basic['school_logo_id'], basic['school_logo_url']), (None, None))

    def test_p2_16_share_settings(self):
        status = cli('share', 'status', self.token)
        self.assertTrue(status['share_url'].startswith('https://www.wondercv.com/cvs/'))
        orig = status['settings']
        try:
            off = cli('share', 'set', self.token, '--public', 'off')
            self.assertFalse(off['settings']['public_visibility'], '关闭即撤销分享链接')
            cli('share', 'set', self.token, '--public', 'on', '--visibility', 'privacy', '--comment', 'off')
            st = cli('share', 'status', self.token)['settings']
            self.assertEqual((st['public_visibility'], st['visibility_type'], st['can_comment']),
                             (True, 10, False))
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                wondercv.main(['share', 'set', self.token, '--visibility', 'bogus'])  # 非法取值在请求前被拒绝
        finally:
            cli('share', 'set', self.token, '--public', 'on' if orig['public_visibility'] else 'off',
                '--visibility', 'full' if orig['visibility_type'] == 20 else 'privacy',
                '--comment', 'on' if orig['can_comment'] else 'off')
        self.assertEqual(cli('share', 'status', self.token)['settings'], orig, '测试应恢复原分享设置')


WCV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'wondercv.py')


def cli_proc(*argv):
    """用独立子进程运行 CLI（与测试进程无共享内存，只共享操作记录目录），返回 (rc, stdout, stderr)。"""
    import subprocess
    env = dict(os.environ, WONDERCV_STATE_DIR=os.environ['WONDERCV_STATE_DIR'])
    done = subprocess.run([sys.executable, WCV_PATH, '--json', *argv], capture_output=True, text=True,
                          env=env, timeout=600)
    return done.returncode, done.stdout, done.stderr


@unittest.skipUnless(os.environ.get('WONDERCV_LIVE') == '1', '设置 WONDERCV_LIVE=1 才访问真实账号')
class LiveCrossProcessRecoveryTest(unittest.TestCase):
    """A03（真实）：POST 响应丢失后，另一进程的 CLI 通过 recover 找到实际对象并按显式选择处理。"""

    token = None

    @classmethod
    def setUpClass(cls):
        cls.token = fixture_token('main')
        reset_fixture(cls.token)

    def setUp(self):
        self.real = client()
        self.fc = FaultyClient(self.real)

    def _lost_post(self, title, forward):
        """制造 POST 响应丢失：forward=True 表示服务端已真正创建对象。返回操作编号。"""
        if forward:
            self.fc.inject_after('POST', '/v3/works', 'unknown')
        else:
            self.fc.inject('POST', '/v3/works', 'unknown')
        with self.assertRaises(wondercv.WcvError) as cm:
            wondercv.add_entry(self.fc, self.token, 'work', {'title': title})
        self.assertEqual(cm.exception.state, 'unknown')
        return cm.exception.objects[0]

    def _ops(self):
        rc, out, err = cli_proc('recover')
        self.assertEqual(rc, 0, err)
        return [x['id'] for x in json.loads(out)]

    def _candidates(self, op):
        rc, out, err = cli_proc('recover', op)
        self.assertEqual(rc, 0, err)
        return json.loads(out)['candidates']

    def test_lost_response_attach_by_another_process(self):
        title = 'XPROC-挂载'
        op = self._lost_post(title, forward=True)
        self.assertIn(op, self._ops())
        cands = [c for c in self._candidates(op) if c['label'] == title]
        self.assertEqual(len(cands), 1, '候选应唯一地对应本次新增')
        rc, out, err = cli_proc('recover', op, '--attach', str(cands[0]['id']))
        self.assertEqual(rc, 0, err)
        titles = [x['title'] for x in cli('work', 'list', self.token)]
        self.assertEqual(titles.count(title), 1, '恢复后应只有一条，且已挂载')
        self.assertNotIn(op, self._ops())
        for x in cli('work', 'list', self.token):
            if x['title'] == title:
                cli('work', 'rm', self.token, str(x['id']))

    def test_lost_response_discard_by_another_process(self):
        title = 'XPROC-丢弃'
        op = self._lost_post(title, forward=True)
        cand = [c for c in self._candidates(op) if c['label'] == title][0]
        rc, out, err = cli_proc('recover', op, '--discard', str(cand['id']))
        self.assertEqual(rc, 0, err)
        self.assertFalse(any(o['id'] == cand['id'] for o in wondercv._lib_for(self.real, {'kind': 'work', 'lang': 'cn'})
                             if not o.get('hidden')), '丢弃后对象应已不存在')
        self.assertNotIn(title, [x['title'] for x in cli('work', 'list', self.token)])
        self.assertNotIn(op, self._ops())

    def test_request_never_arrived_is_confirmed_none(self):
        op = self._lost_post('XPROC-未到达', forward=False)
        self.assertEqual(self._candidates(op), [], '请求未到达服务端时不应有候选')
        rc, out, err = cli_proc('recover', op, '--none')
        self.assertEqual(rc, 0, err)
        self.assertNotIn(op, self._ops())


@unittest.skipUnless(os.environ.get('WONDERCV_LIVE') == '1', '设置 WONDERCV_LIVE=1 才访问真实账号')
class LiveStageBTest(unittest.TestCase):
    """阶段 B 真实验证：头像移除、五类小标题、三类经历排序、PNG 导出、头像上传（证书阻塞时记录真实报错）。"""

    token = None

    @classmethod
    def setUpClass(cls):
        cls.token = fixture_token('main')
        reset_fixture(cls.token)

    def test_b1_avatar_removal_takes_effect(self):
        """移除头像。测试用头像地址取自公开校徽图片（非用户数据），仅用于验证移除接口的真实效果。"""
        c = client()
        original = wondercv.get_profile(c, self.token).get('avatar_url')
        public_image = 'https://logo.wondercv.com/companies/93TFVtyvkHiq42e.png'
        c.call('PUT', f'/v3/cvs/{self.token}/membership_profile', body={'avatar_url': public_image})
        try:
            self.assertEqual(wondercv.get_profile(c, self.token)['avatar_url'], public_image)
            cli('profile', 'avatar', 'rm', self.token)
            self.assertFalse(wondercv.get_profile(c, self.token).get('avatar_url'), '移除后头像地址应为空')
        finally:
            c.call('PUT', f'/v3/cvs/{self.token}/membership_profile', body={'avatar_url': original or ''})

    def test_b1_avatar_upload_real_attempt(self):
        """真实上传头像一次。OSS 证书链不可信时，应明确报错且不改动头像。"""
        c = client()
        before = wondercv.get_profile(c, self.token).get('avatar_url')
        png = os.path.join(tempfile.mkdtemp(), 'avatar.png')
        with open(png, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + bytes(64))
        try:
            rc, err = cli_rc('profile', 'avatar', 'set', self.token, '--file', png)
            if rc == 0:
                self.assertTrue(wondercv.get_profile(c, self.token)['avatar_url'].startswith('https://'))
                cli('profile', 'avatar', 'rm', self.token)
            else:
                self.assertIn('图片上传失败', err)
                self.assertEqual(wondercv.get_profile(c, self.token).get('avatar_url'), before, '失败时不得改动头像')
                raise unittest.SkipTest('头像上传被阻塞（OSS 证书链不可信，需 WONDERCV_CA_FILE）：' + err.strip().splitlines()[0])
        finally:
            os.unlink(png)
            os.rmdir(os.path.dirname(png))

    def test_b7_five_subtitles_rename_and_read_back(self):
        """R07：五类技能块的小标题（技能、语言、证书、兴趣、其他）逐一改名并重读。"""
        before = cli('module', 'list', self.token)
        kinds = ['skill', 'language', 'certificate', 'interest', 'other']
        for kind in kinds:
            cli('extra', 'set', self.token, kind, '--desc', f'小标题-{kind}')
        try:
            for kind in kinds:
                title = f'测试{kind}标题'
                cli('module', 'title', self.token, kind, '--title', title)
                self.assertEqual(cli('module', 'list', self.token)[kind], title)
            shown = {i['kind']: i for i in section(cli('resume', 'show', self.token), 'extras')['items']}
            for kind in kinds:
                self.assertEqual(shown[kind]['subtitle'], f'测试{kind}标题', f'{kind} 小标题应在 show 中读回')
            after = cli('module', 'list', self.token)
            self.assertEqual({k: v for k, v in after.items() if k not in kinds},
                             {k: v for k, v in before.items() if k not in kinds}, '其它标题不应变化')
        finally:
            for kind in kinds:
                cli('module', 'title', self.token, kind, '--title', before[kind])
                cli('extra', 'rm', self.token, kind)
        self.assertEqual({k: cli('module', 'list', self.token)[k] for k in kinds},
                         {k: before[k] for k in kinds})

    def test_b9_three_experience_types_reorder(self):
        """R09：教育、工作、组织经历各 3 条，重排后 list 与 show 均按新顺序输出。"""
        specs = {
            'edu': (['--university', '{t}大学', '--degree', '本科', '--start', '2018-09'], 'university'),
            'work': (['--company', '{t}公司', '--title', '岗位', '--start', '2020-01'], 'company'),
            'org': (['--name', '{t}社团', '--title', '成员', '--start', '2019-09'], 'name'),
        }
        for kind, (args, key) in specs.items():
            ids = [cli(kind, 'add', self.token, *[a.format(t=t) for a in args])['id'] for t in 'ABC']
            try:
                cli(kind, 'reorder', self.token, *[str(i) for i in reversed(ids)])
                labels = [x[key] for x in cli(kind, 'list', self.token)]
                self.assertEqual(labels, [f'C{"大学" if kind == "edu" else "公司" if kind == "work" else "社团"}',
                                          f'B{"大学" if kind == "edu" else "公司" if kind == "work" else "社团"}',
                                          f'A{"大学" if kind == "edu" else "公司" if kind == "work" else "社团"}'])
                path = {'edu': 'edus', 'work': 'works', 'org': 'orgs'}[kind]
                shown = section(cli('resume', 'show', self.token), path)['items']
                self.assertEqual([x[key] for x in shown], labels)
                self.assertIn('必须恰好', cli_fails(kind, 'reorder', self.token, str(ids[0])))
            finally:
                for i in ids:
                    cli(kind, 'rm', self.token, str(i))

    def test_b2_opus_qrcode_upload_real_attempt(self):
        """作品二维码真实上传一次（不创建作品，因此不留下需要删除的作品）。证书阻塞时记录真实报错。"""
        png = os.path.join(tempfile.mkdtemp(), 'qr.png')
        with open(png, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + bytes(64))
        try:
            try:
                url = wondercv.upload_image(client(), 'opus', png)
            except wondercv.WcvError as e:
                self.assertIn('上传失败', str(e))
                raise unittest.SkipTest('作品二维码上传被阻塞（OSS 证书链不可信，需 WONDERCV_CA_FILE）：' + str(e))
            self.assertTrue(url.startswith('https://') and '.aliyuncs.com/cv/opus/' in url)
        finally:
            os.unlink(png)
            os.rmdir(os.path.dirname(png))

    def test_b5_png_export_real_or_blocked(self):
        """R12：PNG 与 PDF 同样走真实下载，校验文件头与非空。证书阻塞时明确跳过。"""
        d = tempfile.mkdtemp(prefix='wcv-png-')
        path = os.path.join(d, 'main.png')
        try:
            rc, err = cli_rc('resume', 'export', self.token, '--format', 'png', '--out', path)
            if rc != 0:
                if '证书' in err:
                    raise unittest.SkipTest('PNG 下载被阻塞：OSS 证书链不可信（需 WONDERCV_CA_FILE），文件校验未执行')
                self.fail(err)
            with open(path, 'rb') as f:
                head = f.read(8)
            self.assertEqual(head, b'\x89PNG\r\n\x1a\n')
            self.assertGreater(os.path.getsize(path), 100)
        finally:
            if os.path.exists(path):
                os.unlink(path)
            os.rmdir(d)


@unittest.skipUnless(os.environ.get('WONDERCV_LIVE') == '1', '设置 WONDERCV_LIVE=1 才访问真实账号')
class LiveHeadModuleTest(unittest.TestCase):
    """头部模块（13 项）与模块样式（49 项）：前端禁用的组合应被拒绝且不写入；其余逐一写入并重读。"""

    token = None

    @classmethod
    def setUpClass(cls):
        cls.token = fixture_token('main')
        reset_fixture(cls.token)

    def test_head_and_module_values_round_trip(self):
        c = client()
        original = wondercv.get_format(c, self.token)
        profile = wondercv.get_profile(c, self.token) or {}
        show = profile.get('show_attr_name')
        accepted, refused = [], []
        try:
            cli('format', 'set', self.token, 'top_format=default', 'page_margin=5', 'head_format=center')
            for head in wondercv.HEAD_MODULE_FORMATS:
                fmt = wondercv.get_format(c, self.token)
                if wondercv._head_module_disabled(fmt, show, head):
                    refused.append(head)
                    before = wondercv.get_format(c, self.token)
                    self.assertIn('不可用', cli_fails('format', 'set', self.token, f'head_module_format={head}'))
                    self.assertEqual(wondercv.get_format(c, self.token), before, f'{head} 被拒绝时不得写入')
                    continue
                patch = wondercv._head_module_patch(fmt, head)
                cli('format', 'set', self.token, f'head_module_format={head}')
                now = wondercv.get_format(c, self.token)
                self.assertEqual(now['head_module_format'], head)
                for key in ('head_format', 'head_module_color'):
                    if key in patch:
                        self.assertEqual(now.get(key), patch[key], f'{head}：{key} 应与前端副作用一致')
                accepted.append(head)
            self.assertTrue(accepted, '至少应有可用的头部模块')
            for module in wondercv.MODULE_FORMATS:
                fmt = wondercv.get_format(c, self.token)
                if wondercv._module_disabled(fmt, module):
                    self.assertIn('不可用', cli_fails('format', 'set', self.token, f'module_format={module}'))
                    refused.append(module)
                    continue
                cli('format', 'set', self.token, f'module_format={module}')
                self.assertEqual(wondercv.get_format(c, self.token)['module_format'], module)
                accepted.append(module)
        finally:
            c.call('PUT', f'/v3/cvs/{self.token}', body={'operation_type': 'update_format_info',
                                                          'format_info': original})
        self.assertEqual(wondercv.get_format(c, self.token), original, '测试应恢复原版式')
        print(f'\n[head/module] 已写入并读回 {len(accepted)} 项，按规则拒绝 {len(refused)} 项')

    def test_disabled_head_refused_when_name_hidden(self):
        """真实拒绝：信息名称设为“不显示”（0）时，前端禁用 clear，CLI 应拒绝且不写入；随后恢复。"""
        c = client()
        before_show = (wondercv.get_profile(c, self.token) or {}).get('show_attr_name')
        before_fmt = wondercv.get_format(c, self.token)
        try:
            cli('profile', 'set', self.token, 'show_attr_name=0')
            self.assertIn('不可用', cli_fails('format', 'set', self.token, 'head_module_format=clear'))
            self.assertEqual(wondercv.get_format(c, self.token), before_fmt, '被拒绝时版式不得变化')
        finally:
            cli('profile', 'set', self.token, f'show_attr_name={1 if before_show is None else before_show}')


if __name__ == '__main__':
    unittest.main()

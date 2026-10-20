"""测试用内存假服务端：只模拟 CLI 用到的接口，用于确定性地验证失败分支。

仅用于 tests/test_unit.py；真实写入验证在 tests/test_live.py 中进行。
"""
import copy
import itertools
import re
import uuid

import wondercv
import wondercv as w


class FakeAPI:
    """实现 Client.call(method, path, params, body) 的内存版简历存储。"""

    def __init__(self, cv_tokens=('AAAA0001',)):
        self.token = f'fake-{uuid.uuid4().hex}'  # 每个假账号独立的操作记录命名空间（不能用 id()，会被复用）
        self.ids = itertools.count(1000)
        self.objects = {}           # (path_kind, id) -> dict，path_kind 如 'edus'、'custom_info'
        self.cvs = {}               # token -> {'basic_info', 'submodule_info', 'format_info'}
        self.trash = set()
        self.calls = []             # 记录 (method, path)
        self.fail_next = []         # 待注入的故障：(method_regex, path_regex, state)
        self.cvs['rrEuiEA'] = self._blank_cv('rrEuiEA')  # 零基础模板
        self.cvs['TPLSTU'] = self._blank_cv('TPLSTU')    # 另一个模板（学生类）
        for t in cv_tokens:
            self.cvs[t] = self._blank_cv(t)

    @staticmethod
    def _blank_cv(token):
        return {
            'basic_info': {'token': token, 'cv_name': token, 'lang': 'cn', 'cv_type': 'zero_cn'},
            'submodule_info': {
                'edus_ids': [], 'works_ids': [], 'orgs_ids': [], 'custom_sections_ids': [],
                'sections_position': [{'id': None, 'type': 'edus', 'info_ids': []},
                                      {'id': None, 'type': 'orgs', 'info_ids': []},
                                      {'id': None, 'type': 'extras', 'info_ids': []}],
                'extras_position': [], 'edus_title': '教育经历', 'works_title': '工作经历',
                'orgs_title': '社团和组织经历', 'extras_title': '技能/证书及其他',
                'custom_sections_titles': {}, 'opus_ids': [], 'opus_title': '作品集',
                **{f'{k}_id': None for k in wondercv.EXTRA_FIELDS},
                **{f'{k}_title': '标题' for k in wondercv.EXTRA_FIELDS},
            },
            'format_info': {'title_color': '#CA3832', 'font': 'font-1', 'font_size': 13,
                            'line_spacing': 15, 'page_margin': 5, 'top_format': 'neat'},
        }

    # ---- 故障注入 ----
    def inject(self, method, path, state):
        """下一次匹配 method+path 的调用抛出 WcvError(state)，且不转发（模拟服务端未处理）。"""
        self.fail_next.append((method, path, state))

    def inject_after(self, method, path, state):
        """下一次匹配的调用先真正执行，再抛出 WcvError(state)（模拟“已生效但响应丢失”）。"""
        self.fail_next.append((method, path, state, 'after'))

    def _maybe_fail(self, method, path, run):
        for i, rule in enumerate(self.fail_next):
            m, p, state = rule[0], rule[1], rule[2]
            if m == method and re.fullmatch(p, path):
                del self.fail_next[i]
                if len(rule) == 4:
                    run()
                raise wondercv.WcvError(f'注入故障 {method} {path}', state=state,
                                        exit_code=wondercv.EXIT_NETWORK)
        return run()

    def _referenced(self, kind, oid):
        """与服务端一致：被任何简历（含回收站）引用时拒绝删除。"""
        field = f'{kind}_ids'
        return any(oid in (cv['submodule_info'][field] or []) for cv in self.cvs.values())

    # ---- Client 接口 ----
    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        return self._maybe_fail(method, path, lambda: self._handle(method, path, params or {}, body))

    def _handle(self, method, path, params, body):
        if method == 'GET' and path == '/v3/template_cvs':
            return [{'token': 'rrEuiEA', 'cv_type': 'zero_cn'}, {'token': 'TPLSTU', 'cv_type': 'stu_cn'}]
        if method == 'POST' and path == '/v3/cvs':
            token = 'NEW%05d' % next(self.ids)
            self.cvs[token] = copy.deepcopy(self.cvs['rrEuiEA'])
            self.cvs[token]['basic_info'].update(token=token, cv_name=body['basic_info']['cv_name'])
            return {'basic_info': copy.deepcopy(self.cvs[token]['basic_info'])}
        if method == 'POST' and path == '/v3/duplicate_cvs':
            src = self.cvs[body['cv_token']]
            token = 'DUP%05d' % next(self.ids)
            self.cvs[token] = copy.deepcopy(src)
            self.cvs[token]['basic_info'].update(token=token, cv_name=src['basic_info']['cv_name'] + '(复制)')
            return {'basic_info': copy.deepcopy(self.cvs[token]['basic_info'])}
        m = re.fullmatch(r'/v3/cvs/([^/]+)(/profile)?', path)
        if m and m.group(2) is None:
            cv = self.cvs[m.group(1)]
            if method == 'GET':
                return copy.deepcopy(cv)
            if method == 'PUT' and body['operation_type'] == 'update_submodule_info':
                cv['submodule_info'] = copy.deepcopy(body['submodule_info'])
                return {'submodule_info': copy.deepcopy(cv['submodule_info'])}
            if method == 'PUT' and body['operation_type'] == 'update_format_info':
                cv['format_info'] = copy.deepcopy(body['format_info'])
                return {'format_info': copy.deepcopy(cv['format_info'])}
            if method == 'PUT' and body['operation_type'] == 'update_basic_info':
                cv['basic_info'] = copy.deepcopy(body['basic_info'])
                return {'basic_info': copy.deepcopy(cv['basic_info'])}
        if method == 'GET' and path == '/v3/cvs':
            cvs = [{'token': t, 'cv_name': cv['basic_info']['cv_name'], 'cv_type': 'zero_cn', 'lang': 'cn',
                    'score': 0, 'update_time': ''} for t, cv in self.cvs.items() if t not in self.trash]
            return {'total_count': len(cvs), 'cvs': cvs}
        if method == 'GET' and path == '/v3/hidden_cvs':
            return {'cvs': [{'token': t} for t in sorted(self.trash)]}
        for kind, base in [('edus', '/v3/edus'), ('works', '/v3/works'), ('orgs', '/v3/orgs')]:
            if path == base and method == 'POST':
                oid = next(self.ids)
                self.objects[(kind, oid)] = dict(body, id=oid)
                return {kind: copy.deepcopy(self.objects[(kind, oid)])}
            if path == base and method == 'GET' and 'ids' not in params:  # 账号对象库（全量列表）
                items = [copy.deepcopy(v) for (k, _), v in sorted(self.objects.items()) if k == kind]
                return {kind: items, 'total_count': len(items), 'page_count': 1}
            if path == base and method == 'GET':
                ids = [int(x) for x in params.get('ids', '').split(',') if x]
                return {kind: [copy.deepcopy(self.objects[(kind, i)]) for i in ids if (kind, i) in self.objects]}
            m = re.fullmatch(base + r'/(\d+)', path)
            if m:
                key = (kind, int(m.group(1)))
                if method == 'PUT':
                    self.objects[key].update(body)
                    return {kind: copy.deepcopy(self.objects[key])}
                if method == 'DELETE':
                    if key not in self.objects:
                        raise wondercv.WcvError('不存在', code=2000)
                    if self._referenced(kind, key[1]):  # 与服务端一致：被任何简历（含回收站）引用则拒绝
                        raise wondercv.WcvError('模块被引用', code=80144)
                    del self.objects[key]
                    return None
        raise AssertionError(f'FakeAPI 未实现 {method} {path}')


class FaultyClient:
    """包装真实 Client：按规则注入故障（用于真实账号的失败恢复验证）。

    inject(method, path, state)：不转发，直接抛出 WcvError(state)（服务端未处理）。
    inject_after(method, path, state)：先真实转发（已生效），再抛出 WcvError(state)（响应丢失）。
    path 为正则，需要精确匹配时用 re.escape。
    """

    def __init__(self, real):
        self.real = real
        self.rules = []
        self.calls = []

    @property
    def token(self):  # 与真实 Client 相同的账号标识（操作记录按账号隔离）
        return self.real.token

    def inject(self, method, path, state):
        self.rules.append((method, path, state, False))

    def inject_after(self, method, path, state):
        self.rules.append((method, path, state, True))

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        for i, (m, p, state, after) in enumerate(self.rules):
            if m == method and re.fullmatch(p, path):
                del self.rules[i]
                if after:
                    self.real.call(method, path, params, body)
                raise wondercv.WcvError(f'注入故障 {method} {path}', state=state,
                                        exit_code=wondercv.EXIT_NETWORK)
        return self.real.call(method, path, params, body)


class RichFake(FakeAPI):
    """在 FakeAPI 上补充自定义模块、条目、作品、技能块的内存实现（仅供单元测试）。"""

    def __init__(self, cv_tokens=('AAAA0001',)):
        super().__init__(cv_tokens=cv_tokens)
        self.sections = {}   # sid -> {'id','title','category','hidden','info_ids'}
        self.infos = {}      # iid -> {'id','title','name','description','sid','hidden'}
        self.opus_lib = {}   # oid -> {'id','title','url','qrcode','language','hidden'}
        self.extra_lib = {}  # eid -> {'id','name','description','kind','hidden'}
        self.hide_section_list = False

    def _section_refs(self, sid):
        return any(sid in (cv['submodule_info']['custom_sections_ids'] or []) for cv in self.cvs.values())

    def _handle(self, method, path, params, body):
        if path == '/v3/custom_sections' and method == 'GET':
            items = [dict(s, infos=[i for i in self.infos.values() if i['sid'] == s['id'] and not i['hidden']])
                     for s in self.sections.values() if not s['hidden']]
            return {'custom_sections': copy.deepcopy(items), 'page_count': 1}
        if path == '/v3/custom_sections' and method == 'POST':
            sid = next(self.ids)
            self.sections[sid] = {'id': sid, 'title': body['section_title'],
                                  'category': body.get('category') or 'custom', 'hidden': False}
            return {'custom_section': dict(self.sections[sid], infos=[])}
        m = re.fullmatch(r'/v3/custom_sections/(\d+)', path)
        if m:
            sid = int(m.group(1))
            if sid not in self.sections or self.sections[sid]['hidden']:
                raise w.WcvError('找不到对应的自定义模块', code=80141)
            if method == 'PUT':
                self.sections[sid]['title'] = body['section_title']
                return None
            if method == 'DELETE':
                if self._section_refs(sid):
                    raise w.WcvError('模块被引用', code=80144)
                self.sections[sid]['hidden'] = True
                return None
        m = re.fullmatch(r'/v3/custom_sections/(\d+)/infos', path)
        if m:
            sid = int(m.group(1))
            if sid not in self.sections or self.sections[sid]['hidden']:
                raise w.WcvError('找不到对应的自定义模块', code=80141)
            if method == 'GET':
                infos = [dict(i) for i in self.infos.values() if i['sid'] == sid]
                return {'kind': None, 'infos': copy.deepcopy(infos)}
            if method == 'POST':
                iid = next(self.ids)
                self.infos[iid] = dict(body, id=iid, sid=sid, hidden=False)
                return {'info': copy.deepcopy(self.infos[iid])}
        m = re.fullmatch(r'/v3/custom_sections/(\d+)/infos/(\d+)', path)
        if m:
            iid = int(m.group(2))
            if method == 'PUT':
                self.infos[iid].update(body)
                return {'info': copy.deepcopy(self.infos[iid])}
            if method == 'DELETE':
                self.infos[iid]['hidden'] = True
                return None
        if path == '/v3/opus' and method == 'GET':
            if 'ids' in params:
                ids = [int(x) for x in params['ids'].split(',') if x]
                return {'opus': [copy.deepcopy(self.opus_lib[i]) for i in ids if i in self.opus_lib]}
            return {'opus': [copy.deepcopy(o) for o in self.opus_lib.values() if not o['hidden']]}
        if path == '/v3/opus' and method == 'POST':
            oid = next(self.ids)
            self.opus_lib[oid] = dict(title=body.get('title'), url=body.get('url'), qrcode=body.get('qrcode'),
                                      language=body.get('lang'), id=oid, hidden=False)
            return {'opus': copy.deepcopy(self.opus_lib[oid])}
        m = re.fullmatch(r'/v3/opus/(\d+)', path)
        if m:
            oid = int(m.group(1))
            if method == 'PUT':
                self.opus_lib[oid].update(title=body['title'], url=body['url'], qrcode=body['qrcode'])
                return {'opus': copy.deepcopy(self.opus_lib[oid])}
            if method == 'DELETE':  # 与真实接口一致：不检查引用
                self.opus_lib[oid]['hidden'] = True
                return None
        if path == '/v3/extras' and method == 'GET':
            out = {}
            for kind, key in [('skill', 'skills'), ('language', 'languages'), ('certificate', 'certificates'),
                              ('interest', 'interests'), ('other', 'others')]:
                out[key] = [copy.deepcopy(e) for e in self.extra_lib.values() if e['kind'] == kind and not e['hidden']]
                out[f'{key}_page_count'] = 1
            return {'others': out, 'others_page_count': 1}
        if path == '/v3/extras' and method == 'POST':
            kind = next(k for k in ('skill', 'language', 'certificate', 'interest', 'other') if k in body)
            eid = next(self.ids)
            self.extra_lib[eid] = dict(body[kind], id=eid, kind=kind, hidden=False)
            return {'others': {kind: copy.deepcopy(self.extra_lib[eid])}}
        if path == '/v3/extras' and method == 'DELETE':
            field, eid = next(iter(params.items()))
            self.extra_lib[int(eid)]['hidden'] = True
            return None
        if path == '/v3/extra' and method == 'GET':
            field, eid = next((k, v) for k, v in params.items() if k.endswith('_id'))
            obj = self.extra_lib[int(eid)]
            return {'others': {obj['kind']: copy.deepcopy(obj)}}
        return super()._handle(method, path, params, body)

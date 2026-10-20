#!/usr/bin/env python3
"""超级简历 WonderCV 命令行：简历及其各模块的增删改查（仅用标准库）。

凭证：环境变量 WONDERCV_TOKEN，或 ~/.config/wondercv/token（浏览器 Cookie `_t` 的值）。
输出：默认可读文本，加 --json 输出结构化数据。
退出码：0 成功；1 接口返回业务错误；2 命令行用法错误；3 输入或本地配置错误；4 网络或服务端故障。
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.wondercv.com/cv'
TOKEN_FILE = os.path.expanduser('~/.config/wondercv/token')
BLANK_TEMPLATE = 'rrEuiEA'  # 官方“零基础”模板（cv_type=zero_cn），新建简历即复制它
EXIT_API, EXIT_USAGE, EXIT_INPUT, EXIT_NETWORK = 1, 2, 3, 4
CONFIRM_ATTEMPTS, CONFIRM_DELAY = 2, 1.0  # 写入结果未知时，重新读取核对的次数与间隔
ORPHAN_HINT = '可先执行 wondercv resume show 核对；若对象仍存在且未挂载，执行 wondercv orphan clean 清理'
STATE_TEXT = {'none': '未写入', 'partial': '部分完成', 'unknown': '结果未知'}

DEGREE_IDS = {'高中': 7, '大专': 1, '本科': 2, '硕士': 3, '博士': 4, 'MBA': 5}
EXTRA_FIELDS = {'skill': 'skill_id', 'language': 'language_id', 'certificate': 'certificate_id',
                'interest': 'interest_id', 'other': 'other_id'}  # 技能块 kind -> submodule 字段
PROFILE_KEYS = {'username', 'gender', 'phone', 'email', 'address', 'exp_job', 'exp_city', 'birthday',
                'current_job', 'expected_salary', 'political_status', 'hometown', 'marriage', 'height',
                'weight', 'wechat', 'homepage', 'linked_in', 'ethnic', 'star_sign', 'mbti',
                'age_birthday', 'show_attr_name'}  # 后两项为显示选项，写入 format_pref（见 set_profile）
ENTRY_FIELDS = {  # 教育/工作/组织经历：命令行参数名 == 接口字段名
    'edu': ['university', 'uni_school', 'major', 'degree', 'edu_type', 'city', 'description'],
    'work': ['company', 'title', 'dept', 'city', 'description'],
    'org': ['name', 'title', 'dept', 'city', 'description'],
}
ENTRY_PATH = {'edu': 'edus', 'work': 'works', 'org': 'orgs'}  # 接口路径 == 简历模块 type
ITEM_FIELDS = ['title', 'name', 'dept', 'city', 'description']
# 读取时展示的字段：各类型中可写入的字段，加上 id
SHOW_FIELDS = {
    'edus': ['id', 'university', 'uni_school', 'major', 'second_major', 'degree', 'edu_type', 'city', 'start_at',
             'end_at', 'show_tags', 'description'],
    'works': ['id', 'company', 'title', 'dept', 'city', 'start_at', 'end_at', 'present', 'description'],
    'orgs': ['id', 'name', 'title', 'dept', 'city', 'start_at', 'end_at', 'description'],
    'custom': ['id', 'title', 'name', 'dept', 'city', 'start_at', 'end_at', 'present', 'description'],
    'extras': ['kind', 'id', 'subtitle', 'description'],
    'opus': ['id', 'title', 'url', 'language'],
}
MODULE_TYPES = ('edus', 'works', 'orgs', 'extras')  # 内置模块，标题存于 submodule_info["<type>_title"]
MODULE_TARGETS = MODULE_TYPES + tuple(EXTRA_FIELDS)  # 另有技能/语言/证书/兴趣/其他 五个小标题
MODULE_TITLE_MAX = 15  # 网页端 editListTitle 的 inputData.length
SCHOOL_TAGS = ('985', '211', '双一流', '海外前100')  # 网页端 mixins 中筛选学校标签所用的列表
FONT_IDS = {'font-1', 'font-1004', 'font-2', 'font-3', 'font-1005', 'font-4', 'font-5', 'font-7',
            'font-2005', 'font-2006'}  # 网页端中英文字体列表
# 版式枚举：取值与中文名均来自网页前端（top_format 为 a0 列表；head/title/body 等为 composing 与按钮文案）
TOP_FORMATS = {'default': '普通', 'line': '正式', 'block': '商务', 'left_block': '简约', 'circle': '沉稳',
               'left_form': '精简', 'fancy': '花样', 'transparent': '清透', 'card': '卡片', 'neat': '规整',
               'highlight': '突出', 'professional': '专业', 'personalized': '个性'}
HEAD_FORMATS = {'left': '居左', 'center': '居中', 'body_center': '大头像', 'right': '居右'}
TITLE_FORMATS = {'default': '专业', 'strip': '清新', 'tag': '突出', 'icon': '图标'}
BODY_FORMATS = {'one_line': '单行标题', 'two_line': '双行标题'}
TIME_FORMATS = {'default': '文字格式', 'number': '数字格式'}
PRIORITY_FORMATS = {'time': '时间优先', 'city': '地点优先'}
PUNCTUATION_FORMATS = {'show_punctuation': '使用分隔符号', 'not_show_punctuation': '不使用分隔符号'}
PAGE_SIZES = {'a4': 'A4', 'letter_size': 'Letter Size'}
PAGE_MARGINS = (5, 10, 15, 20, 25)  # 网页端 PAGE_PADDING
FONT_SIZES = tuple(range(12, 29))  # 网页端 FONT_SIZE 选项
# 头部模块与模块样式：取值与中文名来自前端 index-DEiBoukc.js 的选项表（head 13 项，module 49 项）
HEAD_MODULE_FORMATS = {'business': '商务', 'highlight': '突出', 'clear': '清晰', 'basic': '基础', 'line': '正式',
                       'work': '工作', 'left_block': '简约', 'circle': '沉稳', 'card': '卡片', 'column': '分栏',
                       'professional': '专业', 'neatly': '规整', 'compactness': '简洁'}
MODULE_FORMATS = {
    'default-stereo': '通用-立体', 'default-table': '通用-表格', 'default-avantgarde': '通用-前卫',
    'strip-flat': '专业-扁平', 'strip-stereo': '专业-立体', 'strip-table': '专业-表格', 'strip-avantgarde': '专业-前卫',
    'tag-flat': '突出-扁平', 'tag-stereo': '突出-立体', 'default-flat': '通用-扁平', 'tag-table': '突出-表格',
    'tag-avantgarde': '突出-前卫', 'icon-flat': '简约-扁平', 'icon-stereo': '简约-立体', 'icon-table': '简约-表格',
    'icon-avantgarde': '简约-前卫', 'concise-flat': '精简-扁平', 'table-table': '表格-表格', 'wave-flat': '波浪-扁平',
    'wave-stereo': '波浪-立体', 'wave-table': '波浪-表格', 'wave-avantgarde': '波浪-前卫', 'neat-flat': '规整-扁平',
    'neat-stereo': '规整-立体', 'neat-table': '规整-表格', 'neat-avantgarde': '规整-前卫', 'premium-flat': '高级-扁平',
    'premium-stereo': '高级-立体', 'premium-table': '高级-表格', 'premium-avantgarde': '高级-前卫',
    'arrow-flat': '箭头-扁平', 'arrow-stereo': '箭头-立体', 'arrow-table': '箭头-表格', 'arrow-avantgarde': '箭头-前卫',
    'stereo-flat': '立体圆角-扁平', 'stereo-stereo': '立体圆角-立体', 'stereo-table': '立体圆角-表格',
    'stereo-avantgarde': '立体圆角-前卫', 'left-flat': '通用左对齐-扁平', 'left-stereo': '通用左对齐-立体',
    'left-table': '通用左对齐-表格', 'left-avantgarde': '通用左对齐-前卫', 'center-flat': '通用居中-扁平',
    'center-stereo': '通用居中-立体', 'center-table': '通用居中-表格', 'center-avantgarde': '通用居中-前卫',
    'left_form-flat': '线条-扁平', 'left_form-stereo': '线条-立体', 'left_form-table': '线条-table'}
ENUM_FORMATS = {'top_format': TOP_FORMATS, 'head_format': HEAD_FORMATS, 'title_format': TITLE_FORMATS,
                'body_format': BODY_FORMATS, 'time_format': TIME_FORMATS, 'format_priority': PRIORITY_FORMATS,
                'punctuation_format': PUNCTUATION_FORMATS, 'page_size': PAGE_SIZES,
                'head_module_format': HEAD_MODULE_FORMATS, 'module_format': MODULE_FORMATS}
STATE_DIR = os.environ.get('WONDERCV_STATE_DIR') or os.path.join(os.path.dirname(TOKEN_FILE), 'state')
DATE_RE = re.compile(r'^\d{4}-\d{2}(-\d{2})?$')
PRESENT_TEXT = '至今'
# 新增模块的类别（网页 新增模块 菜单的 type 值）；非 custom 类别每份简历一个
MODULE_CATEGORIES = {'custom': '自定义', 'project_experience': '项目经历', 'reasearch_experience': '研究经历',
                     'honor': '荣誉奖项', 'personal_summary': '个人总结', 'skill_list': '专业技能'}
OPUS_TITLE_MAX = 20  # 网页 作品名称 输入框 maxlength
SHARE_VISIBILITY = {'full': 20, 'privacy': 10}  # 网页 visibilityOptions：20 完整可见，10 保护隐私


class WcvError(Exception):
    """可预期的错误。

    state 为写入状态：none=未写入，partial=部分完成（见 objects），unknown=结果未知，None=只读或输入校验失败。
    """

    def __init__(self, message, *, code=None, state=None, objects=(), hint=None, exit_code=EXIT_API):
        super().__init__(message)
        self.code, self.state, self.objects, self.hint = code, state, list(objects), hint
        self.exit_code = exit_code

    def describe(self):
        lines = [f'错误: {self}']
        if self.state:
            lines.append(f'  写入状态: {STATE_TEXT[self.state]}')
        if self.objects:
            lines.append(f"  涉及对象: {'、'.join(self.objects)}")
        if self.hint:
            lines.append(f'  建议: {self.hint}')
        return '\n'.join(lines)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """认证 API 不跟随重定向：凭证只会发送到请求中写明的地址（A05）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, '重定向已拒绝', headers, io.BytesIO(b''))


def _check_download_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.aliyuncs.com'):
        raise WcvError('下载地址不在允许范围内（仅接受 https 的阿里云 OSS 地址），已拒绝', exit_code=EXIT_NETWORK)


def _install_transport():
    """全局 opener 禁止自动跟随重定向（认证 API 与下载都不会把凭证带到其它地址）。

    证书路径在导入时读取：WONDERCV_CA_FILE 若设置则作为额外信任的 CA。传入 urlopen 的 context 参数会绕过此
    opener，因此不使用该参数。
    """
    urllib.request.install_opener(urllib.request.build_opener(_NoRedirect,
                                                              urllib.request.HTTPSHandler(context=_ssl_context())))




class Client:
    def __init__(self, token, timeout=30):
        if not token:
            raise WcvError('缺少凭证：设置环境变量 WONDERCV_TOKEN，或写入 ~/.config/wondercv/token',
                           exit_code=EXIT_INPUT)
        self.token = token
        self.timeout = timeout

    def call(self, method, path, params=None, body=None):
        """调用接口并返回 data。不会自动重试：写请求可能已生效，重试会产生重复数据。"""
        write = method != 'GET'
        url = API + path + ('?' + urllib.parse.urlencode(params) if params else '')
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': self.token, 'Content-Type': 'application/json',
            'Referer': 'https://www.wondercv.com/', 'Origin': 'https://www.wondercv.com',
            'User-Agent': 'Mozilla/5.0 Chrome/145'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            server = e.code >= 500
            if 300 <= e.code < 400:
                raise WcvError(f'{method} {path} 返回重定向 HTTP {e.code}，已拒绝跟随（凭证未发送到其它地址）',
                               state='none' if write else None, exit_code=EXIT_API) from e
            raise WcvError(f'{method} {path} HTTP {e.code}',
                           state=('unknown' if server else 'none') if write else None,
                           exit_code=EXIT_NETWORK if server else EXIT_API) from e
        except (urllib.error.URLError, OSError) as e:  # 超时、断网、DNS 等
            reason = getattr(e, 'reason', e)
            not_sent = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            raise WcvError(f'网络错误：{method} {path}（{type(reason).__name__}）',
                           state=(None if not write else 'none' if not_sent else 'unknown'),
                           exit_code=EXIT_NETWORK) from e
        try:
            payload = json.loads(raw or b'null')
        except ValueError as e:
            raise WcvError(f'{method} {path} 返回了无法解析的内容', state='unknown' if write else None,
                           exit_code=EXIT_NETWORK) from e
        if not isinstance(payload, dict) or 'code' not in payload:
            raise WcvError(f'{method} {path} 返回了非预期内容', state='unknown' if write else None,
                           exit_code=EXIT_NETWORK)
        if payload['code'] != 1000:
            hint = '凭证失效，请重新登录网页并更新 token' if payload['code'] == 2001 else None
            raise WcvError(f"{method} {path} 失败 [{payload['code']}] {payload.get('message')}",
                           code=payload['code'], state='none' if write else None, hint=hint)
        return payload.get('data')


def _ssl_context():
    """默认使用系统信任库；可用 WONDERCV_CA_FILE 指定额外信任的 CA 证书（PEM）。从不关闭证书校验。"""
    ca = os.environ.get('WONDERCV_CA_FILE')
    return ssl.create_default_context(cafile=ca) if ca else None


def _date(s):
    """校验 YYYY-MM 或 YYYY-MM-DD，且为真实存在的年月日。"""
    if s is None:
        return None
    if not DATE_RE.match(s):
        raise WcvError(f'日期格式应为 YYYY-MM 或 YYYY-MM-DD：{s}', exit_code=EXIT_INPUT)
    parts = [int(x) for x in s.split('-')]
    try:
        datetime.date(parts[0], parts[1], parts[2] if len(parts) == 3 else 1)
    except ValueError as e:
        raise WcvError(f'日期不存在：{s}', exit_code=EXIT_INPUT) from e
    return s


def _short_date(s):
    return s[:7] if isinstance(s, str) else s


def _input_error(message):
    return WcvError(message, exit_code=EXIT_INPUT)


# ---------- 简历本体 ----------

def get_cv(c, token):
    return c.call('GET', f'/v3/cvs/{token}')


def put_submodule(c, token, sub):
    return c.call('PUT', f'/v3/cvs/{token}',
                  body={'operation_type': 'update_submodule_info', 'submodule_info': sub})


def modify_submodule(c, token, fn):
    sub = get_cv(c, token)['submodule_info']
    fn(sub)
    return put_submodule(c, token, sub)


def ensure_section(sub, typ):
    if not any(p['type'] == typ and p['id'] is None for p in sub['sections_position']):
        sub['sections_position'].append({'id': None, 'type': typ, 'info_ids': []})


def _list_all_resumes(c):
    """分页取全部未删除的简历（引用检查必须覆盖全部简历）。"""
    cvs, page = [], 1
    while True:
        data = c.call('GET', '/v3/cvs', {'page_size': 100, 'page': page})
        batch = data['cvs']
        cvs.extend(batch)
        if not batch or len(cvs) >= data['total_count']:
            return cvs
        page += 1


def list_resumes(c):
    return [{k: cv.get(k) for k in ('token', 'cv_name', 'cv_type', 'lang', 'score', 'update_time')}
            for cv in _list_all_resumes(c)]


def create_resume(c, name):
    tpl = get_cv(c, BLANK_TEMPLATE)
    bi = dict(tpl['basic_info'], token=None, kind=None, created_at=None, updated_at=None, cv_name=name)
    data = c.call('POST', '/v3/cvs', body={'basic_info': bi, 'submodule_info': tpl['submodule_info'],
                                           'format_info': tpl['format_info'], 'operation_type': ''})
    return data['basic_info']['token']


def rename_resume(c, token, name):
    bi = dict(get_cv(c, token)['basic_info'], cv_name=name)
    c.call('PUT', f'/v3/cvs/{token}', body={'operation_type': 'update_basic_info', 'basic_info': bi})


def trash_resume(c, token):
    c.call('DELETE', f'/v3/cvs/{token}')


def list_trash(c):
    return c.call('GET', '/v3/hidden_cvs')['cvs']


def restore_resume(c, token):
    c.call('PUT', f'/v3/hidden_cvs/{token}', body={'operation_type': 'recovery'})


def purge_resume(c, token):
    c.call('DELETE', f'/v3/hidden_cvs/{token}', body={})


# ---------- 写入结果核对与引用检查（多步操作的恢复基础） ----------

def _confirm(c, token, pred):
    """写入结果未知时，重新读取核对 pred(submodule_info) 是否成立。"""
    for i in range(CONFIRM_ATTEMPTS):
        if pred(get_cv(c, token)['submodule_info']):
            return True
        if i + 1 < CONFIRM_ATTEMPTS:
            time.sleep(CONFIRM_DELAY)
    return False


def _safe_confirm(c, token, pred):
    try:
        return _confirm(c, token, pred)
    except WcvError:
        return False


def _referenced_by(c, pred, exclude=None):
    """返回引用了某对象的活动简历 token。活动简历读取失败会抛错，绝不假定无引用。

    回收站中的简历读取接口不可用（返回 80120），无法在此检查；由服务端删除时的引用检查（80144）兜底。
    """
    holders = []
    for r in _list_all_resumes(c):
        t = r['token']
        if t != exclude and pred(get_cv(c, t)['submodule_info']):
            holders.append(t)
    return holders


def _commit_attach(c, token, mutate, is_attached, rollback, objects, label):
    """新对象已创建，挂载到简历。确定未挂载则回滚删除该对象；结果未知则核对，核对不到也不回滚。"""
    try:
        modify_submodule(c, token, mutate)
        return
    except WcvError as e:
        if e.state != 'unknown':
            try:
                rollback()
            except WcvError as rb:
                raise WcvError(f'{label}失败，且回滚失败：{e}；{rb}', state='partial', objects=objects,
                               hint=ORPHAN_HINT, exit_code=e.exit_code) from e
            raise WcvError(f'{label}失败，已回滚删除新建对象：{e}', state='none', objects=objects,
                           exit_code=e.exit_code) from e
        if _safe_confirm(c, token, is_attached):
            return  # 挂载实际已生效，无需处理
        raise WcvError(f'{label}结果未知，核对未见挂载；为避免误删未回滚', state='unknown', objects=objects,
                       hint=ORPHAN_HINT, exit_code=EXIT_NETWORK) from e


def _commit_detach(c, token, mutate, is_attached, delete, refs_pred, objects, label, shared=False, absent=None):
    """先从简历解除引用，再删除对象；对象仍被其它活动简历引用时只解除本简历的引用。

    shared=True 表示对象可能与副本共享（或服务端不检查引用）：回收站非空时在任何写入前拒绝。
    absent 为核对“对象是否已不存在”的函数，用于删除响应未知时的收束。
    """
    if shared:
        _trash_guard(c, label)
    try:
        modify_submodule(c, token, mutate)
    except WcvError as e:
        if e.state != 'unknown':
            raise
        if not _safe_confirm(c, token, lambda sub: not is_attached(sub)):
            raise WcvError(f'{label}结果未知，核对仍见引用；请先执行 resume show 核对后再重试', state='unknown',
                           objects=objects, exit_code=EXIT_NETWORK) from e
    try:
        holders = _referenced_by(c, refs_pred, exclude=token)
    except WcvError as e:
        raise WcvError(f'{label}已从本简历移除，但无法核对其他简历引用，已保留对象', state='partial',
                       objects=objects, hint=ORPHAN_HINT, exit_code=e.exit_code) from e
    if holders:
        return {'deleted': False, 'still_referenced_by': holders}
    try:
        delete()
    except WcvError as e:
        if e.code == 80144:  # 服务端检测到仍被引用（如回收站中的简历），保留对象
            return {'deleted': False, 'still_referenced_by': 'server'}
        if absent is not None and _try_absent(absent):  # 删除结果未知或失败，但对象已不存在：收束为成功
            return {'deleted': True, 'note': '删除响应未确认，核对后对象已不存在'}
        if e.state == 'unknown':
            raise WcvError(f'{label}已从本简历移除，删除结果未知且无法确认对象已不存在', state='unknown',
                           objects=objects, hint=ORPHAN_HINT, exit_code=EXIT_NETWORK) from e
        raise WcvError(f'{label}已从本简历移除，但删除对象失败：{e}', state='partial', objects=objects,
                       hint=ORPHAN_HINT, exit_code=e.exit_code) from e
    return {'deleted': True}


def _try_absent(absent):
    try:
        return bool(absent())
    except WcvError:
        return False


# ---------- 个人信息 ----------

def get_profile(c, token):
    try:
        return c.call('GET', f'/v3/cvs/{token}/profile')['profile']
    except WcvError as e:
        if e.code == 80142:  # 找不到基本信息模块：该简历还没有个人信息
            return None
        raise


def set_profile(c, token, values):
    """个人信息部分更新，按整条命令报告结果（A04）。

    实测：同一次请求中同时提交 age_birthday 与 show_attr_name 时，服务端会忽略 age_birthday；
    format_pref 整体替换。因此分步提交：基本字段 → format_pref.age_birthday → 顶层 show_attr_name。
    任一步失败时，报告已完成、失败、未执行与结果未知的步骤。
    """
    values = dict(values)
    age = values.pop('age_birthday', None)
    show = values.pop('show_attr_name', None)
    cur = get_profile(c, token)
    steps = []
    if values:
        steps.append(('基本信息 ' + '、'.join(values), dict(values)))
    if age is not None:
        fp = {k: v for k, v in dict((cur or {}).get('format_pref') or {}).items() if k != 'show_attr_name'}
        fp['age_birthday'] = {'chosen': age, 'date_format': 'yyyy_d' if age == 'birthday' else ''}
        steps.append(('生日/年龄显示', {'format_pref': fp}))
    if show is not None:
        steps.append(('信息名称显示', {'show_attr_name': show}))
    done = []
    for index, (name, body) in enumerate(steps):
        try:
            if cur is None:  # 首次写入：创建个人信息模块，之后都是部分更新
                c.call('POST', f'/v3/cvs/{token}/profile', body=body)
                cur = get_profile(c, token)
            else:
                c.call('PUT', f'/v3/cvs/{token}/profile', body=body)
            done.append(name)
        except WcvError as e:
            rest = [n for n, _ in steps[index + 1:]]
            failed = f'失败 {name}（结果未知：{e}）' if e.state == 'unknown' else f'失败 {name}（{e}）'
            message = f'个人信息未全部写入：已完成 {done or "无"}；{failed}；未执行 {rest or "无"}'
            state = 'partial' if done else ('unknown' if e.state == 'unknown' else 'none')
            raise WcvError(message, code=e.code, state=state, objects=done,
                           hint='可用 profile show 核对；重新执行 profile set 是幂等的' if state != 'none' else None,
                           exit_code=e.exit_code) from e


# ---------- 时间段（经历与条目共用） ----------

def _period_body(args, has_present):
    """开始/结束时间与“至今”。has_present 为 False 的类型（教育经历）没有 present 字段。"""
    start, end = getattr(args, 'start', None), getattr(args, 'end', None)
    present = bool(getattr(args, 'present', False))
    if present and end is not None:
        raise _input_error('--present（至今）与 --end（结束时间）不能同时使用')
    if present and not has_present:
        raise _input_error('该类型不支持 --present')
    body = {}
    if start is not None:
        body['start_at'] = _date(start)
    if end is not None:
        body['end_at'] = _date(end)
        if has_present:
            body['present'] = None  # 给出结束时间即不再“至今”
    if present:
        body.update(present=PRESENT_TEXT, end_at=None)
    return body


# ---------- 教育 / 工作 / 组织经历 ----------

def _entry_body(kind, args):
    body = {f: getattr(args, f) for f in ENTRY_FIELDS[kind] if getattr(args, f, None) is not None}
    body.update(_period_body(args, has_present=(kind == 'work')))
    if kind == 'edu' and args.degree is not None:
        if args.degree not in DEGREE_IDS:
            raise _input_error(f'未知学历 {args.degree}，可选：{"、".join(DEGREE_IDS)}')
        body['degree_id'] = DEGREE_IDS[args.degree]
    if kind == 'edu' and getattr(args, 'tags', None) is not None:
        tags = [t for t in args.tags.split(',') if t]
        bad = [t for t in tags if t not in SCHOOL_TAGS]
        if bad:
            raise _input_error(f'未知学校标签 {bad}，可选：{"、".join(SCHOOL_TAGS)}')
        body['show_tags'] = tags
    return body


def _ordered(objs, ids):
    """按 ids 的保存顺序排列，并剔除不在 ids 中的对象。"""
    by_id = {o['id']: o for o in objs}
    return [by_id[i] for i in ids if i in by_id]


def list_entries(c, token, kind):
    cv = get_cv(c, token)
    path = ENTRY_PATH[kind]
    ids = cv['submodule_info'][f'{path}_ids'] or []
    if not ids:
        return []
    data = c.call('GET', f'/v3/{path}', {'ids': ','.join(map(str, ids)),
                                         'lang': cv['basic_info']['lang'], 'only_desc': 'false'})
    return _ordered(data[path], ids)


def add_entry(c, token, kind, body):
    path = ENTRY_PATH[kind]
    lang = get_cv(c, token)['basic_info']['lang']
    rec = _track_begin(c, kind, lang, body, target=token)
    try:
        item = c.call('POST', f'/v3/{path}', body=body)[path]
    except WcvError as e:
        _track_post_failed(c, rec, e)
    _create_attach(c, rec, item['id'], label='新增经历')
    return item


def set_entry(c, token, kind, eid, body):
    path = ENTRY_PATH[kind]
    _check_member(c, token, f'{path}_ids', eid)
    return c.call('PUT', f'/v3/{path}/{eid}', body=body)[path]


def rm_entry(c, token, kind, eid):
    path = ENTRY_PATH[kind]
    _check_member(c, token, f'{path}_ids', eid)
    rec = {'kind': kind}

    def detach(sub):
        sub[f'{path}_ids'] = [i for i in sub[f'{path}_ids'] if i != eid]
    return _commit_detach(
        c, token, detach,
        is_attached=lambda sub: eid in (sub[f'{path}_ids'] or []),
        delete=lambda: c.call('DELETE', f'/v3/{path}/{eid}'),
        refs_pred=lambda sub: eid in (sub[f'{path}_ids'] or []),
        objects=[f'{path} {eid}'], label='删除经历', absent=lambda: not _present_any_lang(c, rec, eid))


def _check_member(c, token, ids_key, eid):
    if eid not in (get_cv(c, token)['submodule_info'][ids_key] or []):
        raise _input_error(f'{eid} 不在该简历的 {ids_key} 中（若是失败遗留的孤立对象，见 orphan clean）')


# ---------- 技能 / 语言 / 证书 / 兴趣 / 其他（每类一个文本块） ----------

def _extra_position(kind):
    return f'{kind}s'  # extras_position 中的名称：skills / languages / certificates / interests / others


def _ordered_extra_kinds(sub):
    pos = [p[:-1] for p in sub.get('extras_position') or []]
    return [k for k in pos if k in EXTRA_FIELDS] + [k for k in EXTRA_FIELDS if k not in pos]


def list_extras(c, token):
    cv = get_cv(c, token)
    sub, lang, out = cv['submodule_info'], cv['basic_info']['lang'], []
    for kind in _ordered_extra_kinds(sub):
        field = EXTRA_FIELDS[kind]
        if sub[field] is None:
            continue
        obj = c.call('GET', '/v3/extra', {field: sub[field], 'section_lang': lang})['others'][kind]
        out.append(dict(obj, kind=kind, subtitle=sub.get(f'{kind}_title')))
    return out


def set_extra(c, token, kind, text):
    cv = get_cv(c, token)
    field, lang = EXTRA_FIELDS[kind], cv['basic_info']['lang']
    sub = cv['submodule_info']
    if sub[field] is None:  # 新建：先记录，再 POST，再挂载
        rec = _track_begin(c, 'extra', lang, {'description': text}, target=token, extra_kind=kind)
        try:
            others = c.call('POST', '/v3/extras', body={'section_lang': lang, kind: {'description': text}})['others']
        except WcvError as e:
            _track_post_failed(c, rec, e)
        eid = others[kind]['id']
        _create_attach(c, rec, eid, label='新增技能块')
        return dict(others[kind], kind=kind)
    cur = c.call('GET', '/v3/extra', {field: sub[field], 'section_lang': lang})['others'][kind]
    cur = dict(cur, description=text)  # 更新接口要求完整对象（缺 name/kind 会返回 2000）
    c.call('PUT', '/v3/extras', body={'section_lang': lang, kind: cur})
    return dict(cur, kind=kind)


def rm_extra(c, token, kind):
    field = EXTRA_FIELDS[kind]
    eid = get_cv(c, token)['submodule_info'][field]
    if eid is None:
        raise _input_error(f'该简历没有 {kind} 模块')
    rec = {'kind': 'extra', 'extra_kind': kind}

    def detach(sub):
        sub[field] = None
    return _commit_detach(c, token, detach,
                          is_attached=lambda sub: sub[field] == eid,
                          delete=lambda: c.call('DELETE', '/v3/extras', {field: eid}),
                          refs_pred=lambda sub: sub[field] == eid,
                          objects=[f'extras {kind} {eid}'], label='删除技能块',
                          absent=lambda: not _present_any_lang(c, rec, eid))


# ---------- 自定义模块（section）与其中的条目（item） ----------

def _custom_sections(sub):
    return [p for p in sub['sections_position'] if p['id'] is not None]


def add_section(c, token, title, category='custom'):
    """新增模块。category 决定模块类型（实测：传入 category 后模块位的 type 即为该类别）。"""
    if category not in MODULE_CATEGORIES:
        raise _input_error(f'未知模块类别 {category}，可选：{"、".join(MODULE_CATEGORIES)}')
    cv = get_cv(c, token)
    if category != 'custom' and any(p['type'] == category for p in cv['submodule_info']['sections_position']):
        raise _input_error(f'该简历已有「{MODULE_CATEGORIES[category]}」模块（非自定义类别每份简历一个）')
    body = {'section_title': title}
    if category != 'custom':
        body['category'] = category
    rec = _track_begin(c, 'section', cv['basic_info']['lang'], {'title': title, 'category': category}, target=token)
    try:
        sid = c.call('POST', '/v3/custom_sections', body=body)['custom_section']['id']
    except WcvError as e:
        _track_post_failed(c, rec, e)
    _create_attach(c, rec, sid, label='新增模块')
    return sid


def rename_section(c, token, sid, title):
    _check_section(c, token, sid)
    holders = _referenced_by(c, lambda sub: sid in (sub['custom_sections_ids'] or []), exclude=token)
    if holders:  # 自定义模块标题是共享对象：实测在副本中改名会同时改动原件，因此拒绝
        raise WcvError(f'模块 {sid} 与 {len(holders)} 份其他简历共享，改名会同时影响它们，已拒绝',
                       exit_code=EXIT_INPUT)
    _trash_guard(c, '修改模块名称')  # 回收站简历可能与之共享，无法检查时同样拒绝
    c.call('PUT', f'/v3/custom_sections/{sid}', body={'section_title': title})


def rm_section(c, token, sid):
    _check_section(c, token, sid)
    rec = {'kind': 'section'}

    def detach(sub):
        sub['custom_sections_ids'] = [i for i in sub['custom_sections_ids'] if i != sid]
        sub['custom_sections_titles'].pop(f'{sid}_title', None)
        sub['sections_position'] = [p for p in sub['sections_position'] if p['id'] != sid]
    return _commit_detach(c, token, detach,
                          is_attached=lambda sub: sid in (sub['custom_sections_ids'] or []),
                          delete=lambda: c.call('DELETE', f'/v3/custom_sections/{sid}'),
                          refs_pred=lambda sub: sid in (sub['custom_sections_ids'] or []),
                          objects=[f'section {sid}'], label='删除模块', shared=True,
                          absent=lambda: not _present_any_lang(c, rec, sid))


def _check_section(c, token, sid):
    if sid not in get_cv(c, token)['submodule_info']['custom_sections_ids']:
        raise _input_error(f'模块 {sid} 不属于该简历')


def _section_pos(sub, sid):
    return next(p for p in sub['sections_position'] if p['id'] == sid)


def list_items(c, token, sid):
    sub = get_cv(c, token)['submodule_info']
    _check_section(c, token, sid)
    ids = _section_pos(sub, sid)['info_ids'] or []
    if not ids:
        return []
    infos = c.call('GET', f'/v3/custom_sections/{sid}/infos', {'only_desc': 'false'})['infos']
    return _ordered(infos, ids)


def add_item(c, token, sid, body):
    _check_section(c, token, sid)
    rec = _track_begin(c, 'item', 'cn', body, target=token, sid=sid)
    try:
        info = c.call('POST', f'/v3/custom_sections/{sid}/infos', body=body)['info']
    except WcvError as e:
        _track_post_failed(c, rec, e)
    _create_attach(c, rec, info['id'], label='新增条目')
    return info


def set_item(c, token, sid, iid, body):
    _check_item(c, token, sid, iid)
    return c.call('PUT', f'/v3/custom_sections/{sid}/infos/{iid}', body=body)


def rm_item(c, token, sid, iid):
    _check_item(c, token, sid, iid)
    rec = {'kind': 'item', 'sid': sid}

    def detach(sub):
        pos = _section_pos(sub, sid)
        pos['info_ids'] = [i for i in pos['info_ids'] if i != iid]
    return _commit_detach(c, token, detach,
                          is_attached=lambda sub: iid in (_section_pos(sub, sid)['info_ids'] or []),
                          delete=lambda: c.call('DELETE', f'/v3/custom_sections/{sid}/infos/{iid}'),
                          refs_pred=_item_ref(iid),
                          objects=[f'item {iid}（模块 {sid}）'], label='删除条目',
                          absent=lambda: not _present_any_lang(c, rec, iid))


def _item_ref(iid):
    return lambda sub: any(iid in (p['info_ids'] or []) for p in _custom_sections(sub))


def _check_item(c, token, sid, iid):
    sub = get_cv(c, token)['submodule_info']
    _check_section(c, token, sid)
    if iid not in (_section_pos(sub, sid)['info_ids'] or []):
        raise _input_error(f'条目 {iid} 不在模块 {sid} 中')


def orphan_clean(c, typ, obj_id, sid=None, kind=None):
    """清理失败遗留、未被任何活动简历引用的对象。共享类别在回收站非空时拒绝（fail-closed）。"""
    if typ not in ENTRY_PATH and typ not in ('extra', 'section', 'item', 'opus'):
        raise _input_error(f'未知对象类型 {typ}')
    if typ == 'extra' and kind is None:
        raise _input_error('清理技能块需要 --kind')
    if typ == 'item' and sid is None:
        raise _input_error('清理条目需要 --sid（所在模块 id）')
    rec = {'kind': typ, 'sid': sid, 'extra_kind': kind}
    _shared_guard(c, None, rec, obj_id, '清理孤立对象')
    if not _present_any_lang(c, rec, obj_id):
        _resolve_pending(c, obj_id)
        return {'deleted': False, 'already_absent': True, 'id': obj_id}
    try:
        _delete_obj(c, rec, obj_id)
    except WcvError as e:
        if e.code == 80144:
            raise WcvError(f'对象 {obj_id} 仍被其他简历（服务端检测）引用，拒绝删除', exit_code=EXIT_INPUT) from e
        if not _absent_after(c, rec, obj_id):
            raise
    _resolve_pending(c, obj_id)  # 对象已确定不存在：指向它的未决记录同步解决，避免之后被误判为“未核对”
    return {'deleted': True, 'id': obj_id}


def _resolve_pending(c, oid):
    for rec in _pending_load(c):
        if rec.get('created') == oid:
            _pending_drop(c, rec['id'])


# ---------- 作品集（R14） ----------

def list_opus(c, token):
    cv = get_cv(c, token)
    ids = cv['submodule_info']['opus_ids'] or []
    if not ids:
        return []
    data = c.call('GET', '/v3/opus', {'ids': ','.join(map(str, ids)), 'lang': cv['basic_info']['lang'],
                                      'only_desc': 'false'})
    return _ordered(data['opus'], ids)


def _opus_body(title, url, lang, token):
    if title is not None and len(title) > OPUS_TITLE_MAX:
        raise _input_error(f'作品名称最多 {OPUS_TITLE_MAX} 个字符')
    return {'title': title, 'url': url, 'qrcode': '', 'lang': lang, 'cv_token': token}


def add_opus(c, token, title, url, qrcode_path=None):
    cv = get_cv(c, token)
    if not title:
        raise _input_error('作品名称不能为空')
    qrcode = upload_image(c, 'opus', qrcode_path) if qrcode_path else ''  # 先上传；上传失败不创建作品
    body = _opus_body(title, url, cv['basic_info']['lang'], token)
    body['qrcode'] = qrcode
    rec = _track_begin(c, 'opus', cv['basic_info']['lang'], {'title': title, 'url': url}, target=token)
    try:
        oid = c.call('POST', '/v3/opus', body=body)['opus']['id']
    except WcvError as e:
        _track_post_failed(c, rec, e)
    _create_attach(c, rec, oid, label='新增作品')
    return next(o for o in list_opus(c, token) if o['id'] == oid)


def set_opus(c, token, oid, title=None, url=None, remove_qrcode=False, qrcode_path=None):
    """修改作品：只改传入的字段，二维码与其它字段保留服务器上的当前值（A02）。"""
    _check_member(c, token, 'opus_ids', oid)
    cur = next(o for o in list_opus(c, token) if o['id'] == oid)
    new_title = cur['title'] if title is None else title
    if not new_title:
        raise _input_error('作品名称不能为空')
    if len(new_title) > OPUS_TITLE_MAX:
        raise _input_error(f'作品名称最多 {OPUS_TITLE_MAX} 个字符')
    if qrcode_path:
        qrcode = upload_image(c, 'opus', qrcode_path)
    elif remove_qrcode:
        qrcode = ''
    else:
        qrcode = cur.get('qrcode') or ''
    body = {'title': new_title, 'url': cur.get('url') if url is None else url,
            'qrcode': qrcode, 'lang': cur.get('language') or 'cn', 'cv_token': token}
    return c.call('PUT', f'/v3/opus/{oid}', body=body)['opus']


def rm_opus(c, token, oid):
    _check_member(c, token, 'opus_ids', oid)
    rec = {'kind': 'opus'}

    def detach(sub):
        sub['opus_ids'] = [i for i in sub['opus_ids'] if i != oid]
        if not sub['opus_ids']:  # 最后一个作品删除后，去掉空的作品集模块位（网页删除模块时同样移除位置）
            sub['sections_position'] = [p for p in sub['sections_position']
                                        if not (p['type'] == 'opus' and p['id'] is None and not p['info_ids'])]
    # 实测：服务端删除作品不检查引用（会留下悬空引用）。CLI 仅在无活动简历引用、且回收站为空时删除对象。
    return _commit_detach(c, token, detach,
                          is_attached=lambda sub: oid in (sub['opus_ids'] or []),
                          delete=lambda: c.call('DELETE', f'/v3/opus/{oid}'),
                          refs_pred=lambda sub: oid in (sub['opus_ids'] or []),
                          objects=[f'opus {oid}'], label='删除作品', shared=True,
                          absent=lambda: not _present_any_lang(c, rec, oid))


def reorder_opus(c, token, ids):
    cur = get_cv(c, token)['submodule_info']['opus_ids'] or []
    _check_permutation(cur, ids, 'opus_ids')
    modify_submodule(c, token, lambda sub: sub.__setitem__('opus_ids', list(ids)))
    return list_opus(c, token)


# ---------- 图片上传（R14 二维码、R15 头像与自定义校徽：阿里云 OSS 直传） ----------

UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # 网页 maxImgSize = 2*1024*1024
UPLOAD_PREFIX = {'avatar': 'cv/cv_accounts/avatars/', 'opus': 'cv/opus/', 'school_logo': 'cv/school_logs/'}  # 前端
IMAGE_MAGIC = ((b'\x89PNG\r\n\x1a\n', 'png', 'image/png'), (b'\xff\xd8\xff', 'jpg', 'image/jpeg'))
OSS_BUCKET_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')


def read_image(path):
    """读取本地图片：只接受 PNG/JPEG（按文件头判断，不看扩展名），且不超过 2MB。"""
    try:
        with open(path, 'rb') as f:
            data = f.read(UPLOAD_MAX_BYTES + 1)
    except OSError as e:
        raise _input_error(f'无法读取图片文件：{e.strerror}') from e
    if len(data) > UPLOAD_MAX_BYTES:
        raise _input_error('图片超过 2MB 上限')
    for magic, ext, ctype in IMAGE_MAGIC:
        if data.startswith(magic):
            return data, ext, ctype
    raise _input_error('仅支持 PNG 或 JPEG 图片')


def upload_credentials(c):
    """获取 OSS 上传凭证（GET /v3/file_upload_token）。端点与桶名先校验，避免把签名发往意外地址。"""
    d = c.call('GET', '/v3/file_upload_token')
    endpoint, bucket = d.get('endpoint') or '', d.get('bucket_name') or ''
    if not endpoint.endswith('.aliyuncs.com') or '/' in endpoint or ':' in endpoint:
        raise WcvError('上传凭证返回的端点不可信，已拒绝上传', exit_code=EXIT_NETWORK)
    if not OSS_BUCKET_RE.match(bucket):
        raise WcvError('上传凭证返回的桶名不合法，已拒绝上传', exit_code=EXIT_NETWORK)
    return d


def oss_put_request(creds, key, data, content_type, date=None):
    """构造签名后的 OSS PUT 请求（V1 签名：HMAC-SHA1 over 方法、Content-Type、Date、x-oss-* 与资源路径）。"""
    import base64
    import email.utils
    import hashlib
    import hmac
    bucket, endpoint = creds['bucket_name'], creds['endpoint']
    date = date or email.utils.formatdate(usegmt=True)
    token = creds['security_token']
    resource = f'/{bucket}/{key}'
    canon_headers = f'x-oss-security-token:{token}\n'
    to_sign = f'PUT\n\n{content_type}\n{date}\n{canon_headers}{resource}'
    sig = base64.b64encode(hmac.new(creds['access_key_secret'].encode(), to_sign.encode(), hashlib.sha1).digest())
    headers = {'Date': date, 'Content-Type': content_type, 'x-oss-security-token': token,
               'Authorization': f"OSS {creds['access_key_id']}:{sig.decode()}"}
    url = f'https://{bucket}.{endpoint}/{key}'
    return url, headers


def upload_image(c, kind, path):
    """上传图片到 OSS，返回对象地址。失败时不返回地址（调用方不会挂载）。"""
    data, ext, ctype = read_image(path)
    creds = upload_credentials(c)
    key = f"{UPLOAD_PREFIX[kind]}{secrets.token_hex(16)}.{ext}"
    url, headers = oss_put_request(creds, key, data, ctype)
    _check_upload_url(url)
    req = urllib.request.Request(url, data=data, method='PUT', headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != 200:
                raise WcvError(f'图片上传失败：HTTP {resp.status}', exit_code=EXIT_NETWORK)
    except urllib.error.HTTPError as e:
        raise WcvError(f'图片上传失败：HTTP {e.code}', exit_code=EXIT_NETWORK) from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, 'reason', e)
        hint = None
        if isinstance(reason, ssl.SSLError) or 'CERTIFICATE' in str(reason).upper():
            hint = '证书校验失败：请设置 WONDERCV_CA_FILE 指向受信任的 CA 证书（PEM）；CLI 不会关闭证书校验'
        raise WcvError(f'图片上传失败：{type(reason).__name__}', exit_code=EXIT_NETWORK, hint=hint) from e
    return url


def _check_upload_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.aliyuncs.com'):
        raise WcvError('上传地址不在允许范围内（仅 https 的阿里云 OSS 地址），已拒绝', exit_code=EXIT_NETWORK)


def set_avatar(c, token, path):
    """头像：上传后写入个人信息的 avatar_url（网页 upImgToCvFunc → membership_profile）。"""
    url = upload_image(c, 'avatar', path)
    c.call('PUT', f'/v3/cvs/{token}/membership_profile', body={'avatar_url': url})
    return get_profile(c, token)


def remove_avatar(c, token):
    """移除头像（网页 deleteAvatar：avatar_url 置空）。"""
    c.call('PUT', f'/v3/cvs/{token}/membership_profile', body={'avatar_url': ''})
    return get_profile(c, token)


def set_school_logo_file(c, token, path):
    """自定义校徽：上传后以 school_logo_url 写入，school_logo_id 为空（网页 uploadFileToOSS 分支）。"""
    url = upload_image(c, 'school_logo', path)
    c.call('PUT', f'/v3/cvs/{token}/membership_cv', body={'school_logo_url': url, 'school_logo_id': None})
    return {'school_logo_url': url}


# ---------- 校徽与分享（R15、R16） ----------

def search_school_logos(c, token, name):
    return c.call('GET', f'/v3/cvs/{token}/school_logo', {'school_name': name})['logos']


def set_school_logo(c, token, name, logo_id):
    logos = search_school_logos(c, token, name)
    logo = next((x for x in logos if x['id'] == logo_id), None)
    if logo is None or not logo.get('logo_url'):
        raise _input_error(f'校徽 {logo_id} 不在「{name}」的搜索结果中（可用 school-logo search 查看）')
    # 与网页 updateVipMembershipInfo 一致：PUT membership_cv
    c.call('PUT', f'/v3/cvs/{token}/membership_cv',
           body={'school_logo_url': logo['logo_url'], 'school_logo_id': logo_id})
    return {'school_logo_id': logo_id, 'school_logo_url': logo['logo_url']}


def rm_school_logo(c, token):
    c.call('DELETE', f'/v3/cvs/{token}/school_logo', body={})


def share_status(c, token):
    enc = c.call('GET', f'/v3/share_cvs/{token}/encoded_token', {'token': token, 'from_admin': False})['encoded_token']
    settings = c.call('GET', f'/v3/share_cvs/{enc}/settings')
    return {'share_url': f'https://www.wondercv.com/cvs/{enc}/share', 'settings': settings}


def set_share(c, token, public=None, visibility=None, comment=None):
    enc = c.call('GET', f'/v3/share_cvs/{token}/encoded_token', {'token': token, 'from_admin': False})['encoded_token']
    body = {}
    if public is not None:
        body['public_visibility'] = public
    if visibility is not None:
        body['visibility_type'] = SHARE_VISIBILITY[visibility]
    if comment is not None:
        body['can_comment'] = comment
    if body:
        c.call('PUT', f'/v3/share_cvs/{enc}/settings', body=body)
    return share_status(c, token)


# ---------- 模块小标题与版式/主题 ----------

def _check_module_title(target, title):
    """校验并规范化小标题：去首尾空白，非空，不超过网页端 15 字限制。"""
    if target not in MODULE_TARGETS:
        raise _input_error(f'{target} 不是可修改的标题，可选：{"、".join(MODULE_TARGETS)}')
    title = (title or '').strip()
    if not title:
        raise _input_error('标题不能为空')
    if len(title) > MODULE_TITLE_MAX:
        raise _input_error(f'标题长度应为 1~{MODULE_TITLE_MAX} 个字符')
    return title


def list_module_titles(c, token):
    sub = get_cv(c, token)['submodule_info']
    return {t: sub[f'{t}_title'] for t in MODULE_TARGETS}


def set_module_title(c, token, target, title):
    title = _check_module_title(target, title)
    modify_submodule(c, token, lambda sub: sub.__setitem__(f'{target}_title', title))
    return title


def _check_format_value(key, raw):
    if key == 'title_color':
        if not re.match(r'^#[0-9A-Fa-f]{6}$', raw):
            raise _input_error(f'颜色应为 #RRGGBB：{raw}')
        return raw.upper()
    if key == 'font':
        if raw not in FONT_IDS:
            raise _input_error(f'未知字体 {raw}，可选：{"、".join(sorted(FONT_IDS))}')
        return raw
    if key == 'font_size':
        if not (raw.isdigit() and int(raw) in FONT_SIZES):
            raise _input_error(f'字号应为 {FONT_SIZES[0]}~{FONT_SIZES[-1]} 的整数：{raw}')
        return int(raw)
    if key == 'page_margin':
        if not (raw.isdigit() and int(raw) in PAGE_MARGINS):
            raise _input_error(f'页边距应为 {"、".join(map(str, PAGE_MARGINS))}：{raw}')
        return int(raw)
    if key in ENUM_FORMATS:
        if raw not in ENUM_FORMATS[key]:
            raise _input_error(f'{key} 可选值：{"、".join(ENUM_FORMATS[key])}（见 format options）')
        return raw
    # line_spacing：网页端行距常量未能定位，仅校验为正整数（见 docs/structure.md）
    if not raw.isdigit() or int(raw) <= 0:
        raise _input_error(f'{key} 应为正整数：{raw}')
    return int(raw)


FORMAT_KEYS = ('title_color', 'font', 'font_size', 'line_spacing', 'page_margin') + tuple(ENUM_FORMATS)


def parse_format(pairs):
    values = {}
    for pair in pairs:
        key, sep, raw = pair.partition('=')
        if not sep or key not in FORMAT_KEYS:
            raise _input_error(f'无效版式字段 {key!r}，可选：{"、".join(FORMAT_KEYS)}')
        values[key] = _check_format_value(key, raw)
    return values


def get_format(c, token):
    return get_cv(c, token)['format_info']


def _head_module_disabled(fmt, show_attr_name, s):
    """网页 computedHeadDis：某头部模块在当前组合下是否被禁用。"""
    top, module = fmt.get('top_format'), fmt.get('module_format') or ''
    hidden_name = show_attr_name in (0, 2, 3, 4, 5)
    return {
        'basic': module == 'stereo' or top == 'circle',
        'business': top == 'neat',
        'highlight': top == 'neat',
        'clear': hidden_name or 'premium' in module or 'left_form' in module or top == 'neat',
        'left_block': hidden_name or top in ('neat', 'personalized') or 'premium' in module,
        'neatly': top in ('neat', 'personalized'),
        'work': top in ('neat', 'personalized'),
        'circle': top in ('neat', 'personalized'),
        'card': top == 'neat',
        'professional': hidden_name or top == 'neat',
        'column': 'default' not in module or top == 'neat',
    }.get(s, False)


def _module_disabled(fmt, s):
    """网页 computedModuleDis：某模块样式在当前组合下是否被禁用。"""
    top, head = fmt.get('top_format'), fmt.get('head_module_format')
    if s == 'concise-flat':
        return top in ('personalized', 'fancy', 'transparent') or head == 'column'
    if s in ('left_form-stereo', 'premium-stereo'):
        return top == 'personalized' or head in ('left_block', 'column')
    if s in ('premium-flat', 'premium-table', 'premium-avantgarde', 'left_form-flat', 'left_form-table'):
        return head in ('left_block', 'column')
    if s in ('wave-stereo', 'neat-stereo', 'strip-stereo', 'tag-stereo', 'icon-stereo', 'arrow-stereo',
             'stereo-stereo', 'left-stereo', 'center-stereo'):
        return top == 'personalized' or head == 'column'
    if s == 'default-stereo':
        return top == 'personalized'
    if s in ('default-flat', 'default-table', 'default-avantgarde'):
        return False
    return head == 'column'


def _head_module_patch(fmt, s):
    """网页 setHeadModule 的副作用：选择头部模块时同时调整头部对齐与颜色（与前端逻辑一致）。"""
    e = {'head_module_format': s, 'template_type': None}
    if s == 'business':
        e.update(head_format='center')
    elif s in ('clear', 'highlight') and fmt.get('head_format') == 'center':
        e.update(head_format='left')
    elif s in ('circle', 'neatly', 'compactness'):
        e.update(head_format='body_center')
    if s in ('work', 'left_block', 'circle', 'card', 'column', 'professional') \
            and fmt.get('head_module_color') == '#000000' and fmt.get('profile_color') == '#000000':
        e.update(profile_color='#FFFFFF', profile_select_color=True)
    if s in ('basic', 'business', 'clear'):
        e['head_module_color'] = '#000000'
    if s not in ('circle', 'neatly', 'business', 'compactness'):
        e['head_format'] = 'left'
    return e


def set_format(c, token, values):
    """修改版式字段。与网页端联动保持一致：

    - 图标标题（title_format=icon）会清除自定义标题颜色（网页 changeTileFormat）。
    - 简约样式（top_format=left_block）只支持页边距 5（网页 page_margin 选项禁用其余值）。
    - 头部模块/模块样式：网页禁用的组合拒绝写入；选择头部模块时的副作用（对齐、颜色）一并写入，
      同一条命令中用户显式给出的字段优先。
    """
    cur = get_format(c, token)
    values = dict(values)
    explicit = set(values)
    if 'head_module_format' in values:
        profile = get_profile(c, token) or {}
        if _head_module_disabled(cur, profile.get('show_attr_name'), values['head_module_format']):
            raise _input_error(f"当前组合下不可用的头部模块：{values['head_module_format']}（网页端同样禁用）")
        for key, value in _head_module_patch(cur, values['head_module_format']).items():
            if key not in explicit:
                values[key] = value
    if 'module_format' in values and _module_disabled(cur, values['module_format']):
        raise _input_error(f"当前组合下不可用的模块样式：{values['module_format']}（网页端同样禁用）")
    if values.get('title_format') == 'icon' and cur.get('title_select_color'):
        values.update(title_select_color=False, title_color='#000000')
    fmt = dict(cur, **values)  # 与网页端一致：提交完整 format_info
    if fmt.get('top_format') == 'left_block' and fmt.get('page_margin') != 5:
        raise _input_error('简约样式（left_block）仅支持页边距 5，请先把 page_margin 设为 5')
    c.call('PUT', f'/v3/cvs/{token}', body={'operation_type': 'update_format_info', 'format_info': fmt})


def set_theme(c, token, color, custom=False):
    """整体主题色：与网页端一致，同时写入 title_color、top_line_color 及两个 *_select_color。

    色块点选：两个 select_color 为 False；自定义取色：为 True（网页 setFormatInfo 调用）。
    """
    if not re.match(r'^#[0-9A-Fa-f]{6}$', color or ''):
        raise _input_error(f'颜色应为 #RRGGBB：{color}')
    color = color.upper()
    cur = get_format(c, token)
    fmt = dict(cur, title_color=color, top_line_color=color,
               title_select_color=bool(custom), top_line_select_color=bool(custom))
    c.call('PUT', f'/v3/cvs/{token}', body={'operation_type': 'update_format_info', 'format_info': fmt})
    return {k: fmt[k] for k in ('title_color', 'top_line_color', 'title_select_color', 'top_line_select_color')}


def format_options():
    """列出可用的版式取值（供 format options 与 README 使用）。"""
    out = {k: dict(v) for k, v in ENUM_FORMATS.items()}
    out['page_margin'] = {str(x): f'页边距 {x}' for x in PAGE_MARGINS}
    out['font'] = {f: '' for f in sorted(FONT_IDS)}
    out['font_size'] = {str(x): '' for x in FONT_SIZES}
    out['title_color'] = {'#RRGGBB': '主题色（同时联动 top_line_color，见 format theme）'}
    out['line_spacing'] = {'正整数': '网页端取值范围未核实'}
    return out


# ---------- 账号对象库：完整列举（存在性核对、恢复候选） ----------

EXTRA_LIST_KEY = {'skill': 'skills', 'language': 'languages', 'certificate': 'certificates',
                  'interest': 'interests', 'other': 'others'}  # GET /v3/extras 中的分组键
EXTRA_PAGE_KEY = {'skills': 'skills_page_count', 'languages': 'langs_page_count',
                  'certificates': 'certificates_page_count', 'interests': 'interests_page_count',
                  'others': 'others_page_count'}
SHARED_KINDS = ('section', 'opus')  # 实测：副本与原件共享模块 id；作品删除服务端不检查引用
LIBRARY_LANGS = ('cn', 'en')
MATCH_KEYS = ('title', 'name', 'company', 'university', 'major', 'dept', 'city', 'url', 'category',
              'description', 'cv_name')  # 恢复时用于匹配候选对象的内容字段（不含日期，服务端会规范化日期）


def _incomplete(path, got=None, total=None):
    detail = f'（已取得 {got}/{total}）' if got is not None else ''
    return WcvError(f'无法完整列出 {path} 的对象{detail}，为安全起见已停止', exit_code=EXIT_NETWORK)


def _lib_paged(c, path, params, key):
    """按 total_count 翻页，取不全就报错（绝不把不完整的列表当作“不存在”）。"""
    out, page = [], 1
    while True:
        data = c.call('GET', path, dict(params, page=page))
        batch = data.get(key) or []
        out.extend(batch)
        total = data.get('total_count')
        if total is None:  # 无总数字段的接口（作品集）：只能按单次响应处理，且只允许单页
            if data.get('page_count', 1) > 1:
                raise _incomplete(path)
            return out
        if not batch or len(out) >= total:
            break
        page += 1
    if len(out) < total:
        raise _incomplete(path, len(out), total)
    return out


def _union(lists):
    """按 id 去重合并多个列表（保留首次出现的对象）。"""
    seen, out = set(), []
    for items in lists:
        for obj in items:
            if obj.get('id') not in seen:
                seen.add(obj.get('id'))
                out.append(obj)
    return out


def _lib_for(c, rec):
    """账号库中该类对象的完整列表（含 hidden 标记）。

    实测：经历、作品、自定义模块的列表按 lang 精确过滤，不带 lang 时只返回语言为空的对象；
    CLI 新建的对象通常语言为空，因此必须同时查询简历语言与“不带语言”两个范围再合并（否则会漏掉对象）。
    """
    kind, lang = rec['kind'], rec.get('lang') or 'cn'
    if kind in ENTRY_PATH:
        path = ENTRY_PATH[kind]
        return _union(_lib_paged(c, f'/v3/{path}', dict(p, only_desc='false'), path)
                      for p in ({'lang': lang}, {}))
    if kind == 'extra':
        key = EXTRA_LIST_KEY[rec['extra_kind']]
        data = c.call('GET', '/v3/extras', {'section_lang': lang})
        if data.get(EXTRA_PAGE_KEY[key], 1) > 1:
            raise _incomplete('/v3/extras')
        return data['others'].get(key) or []
    if kind == 'section':
        return _union(_section_list(c, p) for p in ({'lang': lang, 'only_desc': 'false'}, {'only_desc': 'false'}))
    if kind == 'item':
        try:
            return c.call('GET', f"/v3/custom_sections/{rec['sid']}/infos", {'only_desc': 'false'})['infos']
        except WcvError as e:
            if e.code == 80141:  # 模块已删除，其条目不存在
                return []
            raise
    if kind == 'opus':
        return _union(c.call('GET', '/v3/opus', p)['opus'] for p in ({'lang': lang, 'only_desc': 'false'},
                                                                     {'only_desc': 'false'}))
    return [dict(r, id=r['token']) for r in _list_all_resumes(c)]  # cv


def _section_list(c, params):
    data = c.call('GET', '/v3/custom_sections', params)
    if data.get('page_count', 1) > 1:
        raise _incomplete('/v3/custom_sections')
    return data['custom_sections']


def _present(c, rec, oid):
    return any(o.get('id') == oid and not o.get('hidden') for o in _lib_for(c, rec))


def _present_any_lang(c, rec, oid):
    """对象是否仍存在。账号库查询已覆盖简历语言与空语言两个范围；读取失败抛错，由调用方按“无法确认”处理。"""
    return _present(c, rec, oid)


def _absent_after(c, rec, oid):
    """写入结果未知后的核对：能证明对象已不存在才返回 True；读取失败视为无法证明。"""
    try:
        return not _present_any_lang(c, rec, oid)
    except WcvError:
        return False


def _ref_pred(rec, oid):
    """判断一份简历是否引用了该对象的谓词（作用于 submodule_info）。"""
    kind = rec['kind']
    if kind in ENTRY_PATH:
        key = f'{ENTRY_PATH[kind]}_ids'
        return lambda sub: oid in (sub[key] or [])
    if kind == 'extra':
        field = EXTRA_FIELDS[rec['extra_kind']]
        return lambda sub: sub[field] == oid
    if kind == 'section':
        return lambda sub: oid in (sub['custom_sections_ids'] or [])
    if kind == 'item':
        return lambda sub: any(oid in (p['info_ids'] or []) for p in _custom_sections(sub))
    if kind == 'opus':
        return lambda sub: oid in (sub['opus_ids'] or [])
    return lambda sub: False  # cv：简历本身不被其它简历引用


def _delete_obj(c, rec, oid):
    kind = rec['kind']
    if kind in ENTRY_PATH:
        return c.call('DELETE', f'/v3/{ENTRY_PATH[kind]}/{oid}')
    if kind == 'extra':
        return c.call('DELETE', '/v3/extras', {EXTRA_FIELDS[rec['extra_kind']]: oid})
    if kind == 'section':
        return c.call('DELETE', f'/v3/custom_sections/{oid}')
    if kind == 'item':
        return c.call('DELETE', f"/v3/custom_sections/{rec['sid']}/infos/{oid}")
    if kind == 'opus':
        return c.call('DELETE', f'/v3/opus/{oid}')
    return trash_resume(c, oid)


def _attach_mutate(rec, oid, sub):
    """把对象挂到简历的 submodule_info 上（与各 add 命令的挂载步骤一致）。"""
    kind = rec['kind']
    if kind in ENTRY_PATH:
        path = ENTRY_PATH[kind]
        sub[f'{path}_ids'] = list(sub[f'{path}_ids'] or []) + [oid]
        ensure_section(sub, path)
    elif kind == 'extra':
        sub[EXTRA_FIELDS[rec['extra_kind']]] = oid
        ensure_section(sub, 'extras')
        if _extra_position(rec['extra_kind']) not in (sub.get('extras_position') or []):
            sub['extras_position'] = list(sub.get('extras_position') or []) + [_extra_position(rec['extra_kind'])]
    elif kind == 'section':
        sub['custom_sections_ids'] = list(sub['custom_sections_ids'] or []) + [oid]
        sub['custom_sections_titles'][f'{oid}_title'] = rec['payload']['title']
        sub['sections_position'].append({'id': oid, 'type': rec['payload']['category'], 'info_ids': []})
    elif kind == 'item':
        pos = _section_pos(sub, rec['sid'])
        pos['info_ids'] = list(pos['info_ids']) + [oid]
    elif kind == 'opus':
        sub['opus_ids'] = list(sub['opus_ids'] or []) + [oid]
        ensure_section(sub, 'opus')


def _trash_guard(c, label):
    """回收站非空时无法检查回收站简历是否引用同一对象（或共享模块），因此拒绝危险操作（fail-closed）。"""
    n = len(list_trash(c))
    if n:
        raise WcvError(f'{label}：回收站中有 {n} 份简历无法检查其引用（可能与之共享），为保护其原件已拒绝。'
                       '请先处理回收站（永久删除受 24 小时频控）', exit_code=EXIT_INPUT)


def _shared_guard(c, token, rec, oid, label):
    """删除或改名共享对象前的检查：活动简历仍引用则拒绝；共享类别在回收站非空时拒绝。"""
    holders = _referenced_by(c, _ref_pred(rec, oid), exclude=token)
    if holders:
        raise WcvError(f'{label}：仍被 {len(holders)} 份其它简历引用，已拒绝', exit_code=EXIT_INPUT)
    if rec['kind'] in SHARED_KINDS:
        _trash_guard(c, label)


# ---------- 未决操作记录（首个非幂等写请求结果未知时，用于跨进程恢复） ----------

def _now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def _pending_dir(c):
    """按账号（凭证的哈希，不保存凭证本身）分目录，权限 700；记录文件权限 600。"""
    return os.path.join(STATE_DIR, hashlib.sha256(c.token.encode()).hexdigest()[:24])


def _pending_save(c, rec):
    d = _pending_dir(c)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.op-', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(d, rec['id'] + '.json'))


def _pending_load(c):
    d = _pending_dir(c)
    if not os.path.isdir(d):
        return []
    recs = []
    for name in sorted(os.listdir(d)):
        if name.endswith('.json'):
            with open(os.path.join(d, name), encoding='utf-8') as f:
                recs.append(json.load(f))
    return sorted(recs, key=lambda r: r['created_at'])


def _pending_get(c, op_id):
    for rec in _pending_load(c):
        if rec['id'] == op_id:
            return rec
    raise _input_error(f'没有未决操作 {op_id}（可用 wondercv recover 查看）')


def _pending_drop(c, op_id):
    path = os.path.join(_pending_dir(c), op_id + '.json')
    if os.path.exists(path):
        os.unlink(path)


def _fingerprint(payload):
    return {k: v for k, v in payload.items() if k in MATCH_KEYS and v not in (None, '')}


def _same_payload(obj, fp):
    return all(_scalar(obj.get(k)) == _scalar(v) for k, v in fp.items())


def _track_begin(c, kind, lang, payload, target=None, sid=None, extra_kind=None):
    """在首个写请求之前记录基线（账号库中已有的对象 id）。同类操作未解决时拒绝，防止重复创建。"""
    key = (kind, target, sid, extra_kind)
    busy = [r for r in _pending_load(c) if (r['kind'], r.get('target'), r.get('sid'), r.get('extra_kind')) == key]
    if busy:
        raise WcvError(f"同类操作 {busy[0]['id']} 的结果尚未核对，为避免重复创建已拒绝。"
                       f"请先执行 wondercv recover {busy[0]['id']}", objects=[busy[0]['id']],
                       exit_code=EXIT_INPUT)
    rec = {'id': secrets.token_hex(6), 'created_at': _now(), 'kind': kind, 'lang': lang, 'target': target,
           'sid': sid, 'extra_kind': extra_kind, 'payload': _fingerprint(payload), 'phase': 'post',
           'created': None}
    rec['baseline'] = [o.get('id') for o in _lib_for(c, rec)]
    _pending_save(c, rec)
    return rec


def _track_post_failed(c, rec, e):
    if e.state == 'unknown':  # 服务端可能已创建：保留记录，不删除、不重试
        raise WcvError(f"新增结果未知：服务端可能已创建对象。操作 {rec['id']} 已保留，请执行 "
                       f"wondercv recover {rec['id']} 核对后再继续，不要直接重试。",
                       state='unknown', objects=[rec['id']], hint=f"wondercv recover {rec['id']}",
                       exit_code=EXIT_NETWORK) from e
    _pending_drop(c, rec['id'])  # 确定未创建
    raise e


def _create_attach(c, rec, oid, label):
    """对象已创建：记录 id，挂载到目标简历。确定失败则回滚删除；结果未知则保留记录供 recover。"""
    rec.update(phase='created', created=oid)
    _pending_save(c, rec)
    target = rec['target']
    try:
        _commit_attach(c, target, lambda sub: _attach_mutate(rec, oid, sub),
                       _ref_pred(rec, oid), rollback=lambda: _delete_obj(c, rec, oid),
                       objects=[f"{rec['kind']} {oid}"], label=label)
    except WcvError as e:
        if e.state == 'none':
            _pending_drop(c, rec['id'])  # 已回滚，无遗留
        else:
            hint = f"可执行 wondercv recover {rec['id']}（候选确认后挂载或丢弃）"
            if rec['kind'] in ENTRY_PATH or rec['kind'] == 'opus':
                hint += f"；或 wondercv orphan clean {rec['kind']} {oid}（仅当对象未被引用时）"
            raise WcvError(f"{e}；操作 {rec['id']} 已保留", state=e.state, objects=e.objects, hint=hint,
                           exit_code=e.exit_code) from e
        raise
    _pending_drop(c, rec['id'])


def recover_list(c):
    return [{k: r.get(k) for k in ('id', 'kind', 'phase', 'created', 'target', 'created_at')}
            for r in _pending_load(c)]


def recover_candidates(c, rec):
    """候选对象：阶段 post 为“基线之后新出现且内容匹配”的对象；阶段 created 为已记录的 id（若仍存在）。"""
    visible = {o['id']: o for o in _lib_for(c, rec) if not o.get('hidden')}
    if rec['phase'] == 'created':
        return [visible[rec['created']]] if rec['created'] in visible else []
    base = set(rec['baseline'])
    return [o for oid, o in visible.items() if oid not in base and _same_payload(o, rec['payload'])]


def _candidate(cands, obj_id):
    found = next((o for o in cands if str(o['id']) == str(obj_id)), None)
    if found is None:
        raise _input_error(f'候选对象中没有 {obj_id}（可用 wondercv recover <操作> 查看候选）')
    return found


def recover_run(c, op_id, action=None, obj_id=None):
    """恢复未决操作。action：attach 挂载候选；discard 丢弃候选；none 确认未创建；adopt 保留新简历。

    所有动作都需要显式给出候选 id，不会自动挂载或删除。
    """
    rec = _pending_get(c, op_id)
    cands = recover_candidates(c, rec)
    if action is None:
        return {'operation': {k: rec.get(k) for k in ('id', 'kind', 'phase', 'created', 'target')},
                'candidates': [{'id': o['id'], 'label': _scalar(o.get('title') or o.get('name')
                                                             or o.get('cv_name') or o.get('url'))} for o in cands]}
    if action == 'none':
        if cands:
            raise _input_error('存在候选对象，不能确认“未创建”；请用 --attach 或 --discard 指定其中一个')
        _pending_drop(c, op_id)
        return {'resolved': 'none'}
    if action == 'adopt':
        if rec['kind'] != 'cv':
            raise _input_error('adopt 只用于新建/复制的简历')
        cand = _candidate(cands, obj_id)
        _pending_drop(c, op_id)
        return {'resolved': 'adopt', 'token': cand['id']}
    cand = _candidate(cands, obj_id)
    oid = cand['id']
    target = rec['target']
    if action == 'attach':
        if rec['kind'] == 'cv':
            raise _input_error('简历不能挂载，只能 adopt 或 discard')
        if _confirm(c, target, _ref_pred(rec, oid)):
            _pending_drop(c, op_id)
            return {'attached': True, 'note': '对象已挂载到目标，已清除记录'}
        holders = _referenced_by(c, _ref_pred(rec, oid), exclude=target)
        if holders:
            raise WcvError(f'对象 {oid} 已被 {len(holders)} 份其它简历引用，拒绝挂载', exit_code=EXIT_INPUT)
        try:
            modify_submodule(c, target, lambda sub: _attach_mutate(rec, oid, sub))
        except WcvError as e:
            if e.state == 'unknown' and _safe_confirm(c, target, _ref_pred(rec, oid)):
                pass
            else:
                raise
        _pending_drop(c, op_id)
        return {'attached': True, 'id': oid}
    if action == 'discard':
        if rec['kind'] != 'cv' and _confirm(c, target, _ref_pred(rec, oid)):
            raise _input_error('该对象已挂载到目标简历，丢弃请使用对应的 rm 命令')
        _shared_guard(c, target, rec, oid, '丢弃候选对象')
        try:
            _delete_obj(c, rec, oid)
        except WcvError as e:
            if e.code == 80144:
                raise WcvError(f'对象 {oid} 仍被其它简历引用（服务端检测），拒绝删除', exit_code=EXIT_INPUT) from e
            if not _absent_after(c, dict(rec), oid):
                raise WcvError(f'丢弃候选对象结果未知，对象仍可能存在：{e}', state='unknown',
                               objects=[str(oid)], hint=f'wondercv recover {op_id}', exit_code=EXIT_NETWORK) from e
        _pending_drop(c, op_id)
        return {'discarded': True, 'id': oid}
    raise _input_error(f'未知动作 {action}')


# ---------- 排序（R09） ----------

def _check_permutation(current, given, label):
    """given 必须恰好是 current 的一个排列：不重复、不缺失、不混入外来项。写入前校验。"""
    if len(set(given)) != len(given):
        raise _input_error(f'{label} 中存在重复项')
    if set(given) != set(current):
        missing = [x for x in current if x not in given]
        extra = [x for x in given if x not in current]
        raise _input_error(f'{label} 必须恰好包含全部 {len(current)} 项（缺少 {missing}，多出 {extra}）')


def reorder_entries(c, token, kind, ids):
    path = ENTRY_PATH[kind]
    cur = get_cv(c, token)['submodule_info'][f'{path}_ids'] or []
    _check_permutation(cur, ids, f'{path}_ids')
    modify_submodule(c, token, lambda sub: sub.__setitem__(f'{path}_ids', list(ids)))
    return list_entries(c, token, kind)


def reorder_items(c, token, sid, iids):
    sub = get_cv(c, token)['submodule_info']
    _check_section(c, token, sid)
    cur = _section_pos(sub, sid)['info_ids'] or []
    _check_permutation(cur, iids, f'模块 {sid} 的 info_ids')

    def fn(s):
        _section_pos(s, sid)['info_ids'] = list(iids)
    modify_submodule(c, token, fn)
    return list_items(c, token, sid)


def reorder_extras(c, token, kinds):
    sub = get_cv(c, token)['submodule_info']
    present = [k for k in EXTRA_FIELDS if sub[EXTRA_FIELDS[k]] is not None]
    _check_permutation(present, kinds, 'extras（已设置的技能块）')
    order = list(kinds) + [k for k in _ordered_extra_kinds(sub) if k not in kinds]

    def fn(s):
        s['extras_position'] = [_extra_position(k) for k in order]
    modify_submodule(c, token, fn)
    return [x['kind'] for x in list_extras(c, token)]


def _section_key(pos):
    return pos['id'] if pos['id'] is not None else pos['type']


def reorder_sections(c, token, keys):
    """整体模块顺序。键：内置模块用类型名（edus/works/orgs/extras），自定义模块用模块 id。"""
    positions = get_cv(c, token)['submodule_info']['sections_position']
    given = [int(k) if str(k).isdigit() else k for k in keys]
    _check_permutation([_section_key(p) for p in positions], given, 'sections_position')
    by_key = {_section_key(p): p for p in positions}

    def fn(s):
        s['sections_position'] = [by_key[k] for k in given]
    modify_submodule(c, token, fn)
    return [{'key': _section_key(p), 'type': p['type']} for p in get_cv(c, token)['submodule_info']['sections_position']]


# ---------- 模板与复制（R11） ----------

def list_templates(c):
    return [{'token': t['token'], 'cv_type': t['cv_type']} for t in c.call('GET', '/v3/template_cvs')]


def create_resume(c, name, template=BLANK_TEMPLATE):
    """按模板新建：复制模板的内容与版式，清空模板标识（与网页端 createCv 的 template 分支一致）。"""
    if template != BLANK_TEMPLATE and template not in {t['token'] for t in list_templates(c)}:
        raise _input_error(f'{template} 不是可用模板（见 template list）')
    tpl = get_cv(c, template)
    bi = dict(tpl['basic_info'], token=None, kind=None, created_at=None, updated_at=None, cv_name=name)
    rec = _track_begin(c, 'cv', 'cn', {'cv_name': name})
    try:
        data = c.call('POST', '/v3/cvs', body={'basic_info': bi, 'submodule_info': tpl['submodule_info'],
                                               'format_info': tpl['format_info'], 'operation_type': ''})
    except WcvError as e:
        _track_post_failed(c, rec, e)
    _pending_drop(c, rec['id'])
    return data['basic_info']['token']


def copy_resume(c, token, name):
    """复制已有简历（服务端 duplicate_cvs，与网页“复制”一致），再改名。

    实测：复制后条目/技能块/工作经历为新对象；用户自建的自定义模块与原件共享同一模块 id。
    """
    rec = _track_begin(c, 'cv', 'cn', {})
    try:
        data = c.call('POST', '/v3/duplicate_cvs', body={'cv_token': token})
    except WcvError as e:
        _track_post_failed(c, rec, e)
    new = data['basic_info']['token']
    _pending_drop(c, rec['id'])  # 副本已确定存在（token 已返回），之后的改名失败按 partial 报告
    try:
        rename_resume(c, new, name)
    except WcvError as e:
        raise WcvError(f'已复制为 {new}，但改名失败：{e}', state='partial', objects=[f'resume {new}'],
                       hint='可用 resume rename 重命名该副本', exit_code=e.exit_code) from e
    return new


# ---------- 导出（R12） ----------

EXPORT_PATHS = {'pdf': '/v4/cvs/{t}/download_url', 'png': '/v4/cvs/{t}/img_download_url'}  # 网页 savePdfOrImage
EXPORT_SIGNATURES = {'pdf': b'%PDF-', 'png': b'\x89PNG\r\n\x1a\n'}
EXPORT_MAX_BYTES = 50 * 1024 * 1024


def _fetch_file(url):
    """下载签名地址。不携带凭证；每一跳（含重定向）都必须是 https 的阿里云 OSS 地址（A05）。"""
    for _ in range(4):
        _check_download_url(url)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 Chrome/145'})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read(EXPORT_MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            location = e.headers.get('Location') if 300 <= e.code < 400 else None
            if location:
                url = urllib.parse.urljoin(url, location)
                continue
            raise WcvError(f'下载失败：HTTP {e.code}', exit_code=EXIT_NETWORK) from e
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, 'reason', e)
            hint = None
            if isinstance(reason, ssl.SSLError) or 'CERTIFICATE' in str(reason).upper():
                hint = ('证书校验失败：本机信任库缺少该链路的 CA。请设置 WONDERCV_CA_FILE 指向受信任的 CA 证书（PEM）；'
                        'CLI 不会关闭证书校验')
            raise WcvError(f'下载失败：{type(reason).__name__}', state=None, hint=hint, exit_code=EXIT_NETWORK) from e
        if len(data) > EXPORT_MAX_BYTES:
            raise WcvError('下载文件超过大小上限，已放弃', exit_code=EXIT_NETWORK)
        return data
    raise WcvError('下载重定向次数过多，已放弃', exit_code=EXIT_NETWORK)


def export_resume(c, token, fmt, out, force=False, fetch=None):
    """导出 PDF/PNG 到本地路径。默认不覆盖；先写临时文件，校验签名后原子替换。"""
    if fmt not in EXPORT_PATHS:
        raise _input_error('格式应为 pdf 或 png')
    out = os.path.abspath(out)
    if os.path.isdir(out):
        raise _input_error(f'输出路径是目录：{out}')
    if os.path.exists(out) and not force:
        raise _input_error(f'文件已存在：{out}（确需覆盖请加 --force）')
    try:
        get_cv(c, token)  # 80120 在此表示简历不存在或在回收站；下载接口的 80120 才表示信息未完善
    except WcvError as e:
        if e.code == 80120:
            raise WcvError('简历不存在或已在回收站', code=e.code, exit_code=EXIT_INPUT) from e
        raise
    params = {'watermark': 'no', 'showProfile': True, 'pageNum': 1, 'origin': 'pc', 'platform': 'pc'}
    try:
        url = (c.call('GET', EXPORT_PATHS[fmt].format(t=token), params) or {}).get('url')
    except WcvError as e:
        if e.code == 80120:
            raise WcvError('导出失败：请先完善简历信息', code=e.code, exit_code=EXIT_INPUT) from e
        if e.code in (80160, 80161):
            raise WcvError('导出需要付费或升级会员，CLI 不会自动购买', code=e.code, exit_code=EXIT_INPUT) from e
        raise
    if not url:
        raise WcvError('导出失败：接口未返回下载地址')
    blob = (fetch or _fetch_file)(url)
    if not blob or not blob.startswith(EXPORT_SIGNATURES[fmt]):
        raise WcvError(f'下载内容不是有效的 {fmt} 文件，已丢弃', exit_code=EXIT_NETWORK)
    if fmt == 'pdf' and b'%%EOF' not in blob[-1024:]:
        raise WcvError('PDF 文件不完整（缺少结束标记），已丢弃', exit_code=EXIT_NETWORK)
    directory = os.path.dirname(out) or '.'
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.wcv-', suffix='.part')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        _publish(tmp, out, force)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    return {'path': out, 'format': fmt, 'bytes': len(blob)}


def _publish(tmp, out, force):
    """原子发布。不覆盖时用 link（目录项已存在则失败），不做“先检查再替换”（A06）。"""
    try:
        if force:
            os.replace(tmp, out)
        else:
            os.link(tmp, out)
    except FileExistsError as e:
        raise WcvError(f'文件已存在：{out}（确需覆盖请加 --force）', exit_code=EXIT_INPUT) from e
    except OSError as e:
        raise WcvError(f'无法原子发布到 {out}：{e.strerror}（文件系统不支持不覆盖发布时可改用 --force）',
                       exit_code=EXIT_INPUT) from e


# ---------- 读取 ----------

def _module_items(c, token, cv, pos):
    """返回某个模块位置对应的完整对象列表（按保存顺序）。"""
    typ, sid = pos['type'], pos['id']
    if typ in ENTRY_PATH.values():
        return list_entries(c, token, next(k for k, v in ENTRY_PATH.items() if v == typ)), f'{typ}_title', typ
    if typ == 'extras':
        return list_extras(c, token), 'extras_title', 'extras'
    if typ == 'opus':
        return list_opus(c, token), 'opus_title', 'opus'
    items = list_items(c, token, sid) if pos['info_ids'] else []
    return items, None, 'custom'


def show_resume(c, token):
    """可读摘要：只包含可编辑字段（日期到月）。完整数据见 dump_resume。"""
    cv = get_cv(c, token)
    sub = cv['submodule_info']
    sections = []
    for pos in sub['sections_position']:
        items, title_key, kind = _module_items(c, token, cv, pos)
        if title_key:
            title = sub[title_key]
        else:
            title = sub['custom_sections_titles'].get(f"{pos['id']}_title", pos['type'])
        keys = SHOW_FIELDS[kind]
        sections.append({'id': pos['id'], 'type': pos['type'], 'title': title,
                         'items': [{k: (_short_date(i[k]) if k.endswith('_at') else i[k])
                                    for k in keys if k in i} for i in items]})
    return {'token': token, 'cv_name': cv['basic_info']['cv_name'], 'lang': cv['basic_info']['lang'],
            'cv_type': cv['basic_info']['cv_type'], 'profile': get_profile(c, token), 'sections': sections}


def dump_resume(c, token):
    """完整数据：接口返回的各对象不做裁剪，并附带模块顺序与引用。"""
    cv = get_cv(c, token)
    sub = cv['submodule_info']
    modules = []
    for pos in sub['sections_position']:
        items, title_key, _ = _module_items(c, token, cv, pos)
        title = sub[title_key] if title_key else sub['custom_sections_titles'].get(f"{pos['id']}_title")
        modules.append({'type': pos['type'], 'id': pos['id'], 'title': title,
                        'info_ids': pos['info_ids'], 'objects': items})
    return {'token': token, 'basic_info': cv['basic_info'], 'format_info': cv['format_info'],
            'submodule_info': sub, 'cv_page_cross_config': cv.get('cv_page_cross_config'),
            'profile': get_profile(c, token), 'modules': modules}


# ---------- 命令行 ----------

def _add_entry_args(p, kind):
    p.add_argument('--start', help='开始时间 YYYY-MM 或 YYYY-MM-DD')
    p.add_argument('--end', help='结束时间 YYYY-MM 或 YYYY-MM-DD')
    for f in ENTRY_FIELDS[kind]:
        if f == 'degree':
            p.add_argument('--degree', help='、'.join(DEGREE_IDS))
        else:
            p.add_argument(f'--{f.replace("_", "-")}', dest=f)
    if kind == 'work':
        p.add_argument('--present', action='store_true', help='至今在职（与 --end 互斥）')
    if kind == 'edu':
        p.add_argument('--tags', help='学校标签，逗号分隔：' + '、'.join(SCHOOL_TAGS))


def build_parser():
    ap = argparse.ArgumentParser(prog='wondercv', description='超级简历 WonderCV 命令行')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    top = ap.add_subparsers(dest='group', required=True)

    res = top.add_parser('resume', help='简历').add_subparsers(dest='cmd', required=True)
    res.add_parser('list', help='列出简历')
    res.add_parser('show', help='查看可读摘要').add_argument('token')
    res.add_parser('dump', help='导出完整数据（不裁剪字段）').add_argument('token')
    p = res.add_parser('create', help='新建简历（默认空白；--template 指定模板）')
    p.add_argument('--name', required=True)
    p.add_argument('--template', help='模板 token，见 template list')
    p = res.add_parser('copy', help='复制已有简历并命名')
    p.add_argument('token'), p.add_argument('--name', required=True)
    p = res.add_parser('export', help='导出 PDF 或 PNG 到本地文件')
    p.add_argument('token'), p.add_argument('--format', required=True, choices=['pdf', 'png'])
    p.add_argument('--out', required=True, help='输出路径')
    p.add_argument('--force', action='store_true', help='允许覆盖已存在的文件')
    p = res.add_parser('reorder', help='调整整体模块顺序（内置用类型名，自定义用模块 id）')
    p.add_argument('token'), p.add_argument('keys', nargs='+')
    p = res.add_parser('rename', help='重命名简历')
    p.add_argument('token'), p.add_argument('name')
    res.add_parser('rm', help='移入回收站').add_argument('token')
    res.add_parser('trash', help='查看回收站')
    res.add_parser('restore', help='从回收站恢复').add_argument('token')
    res.add_parser('purge', help='从回收站永久删除').add_argument('token')

    prof = top.add_parser('profile', help='个人信息').add_subparsers(dest='cmd', required=True)
    prof.add_parser('show', help='查看').add_argument('token')
    p = prof.add_parser('set', help='设置字段，如 username=张三')
    p.add_argument('token'), p.add_argument('pairs', nargs='+', metavar='key=value')
    av = prof.add_parser('avatar', help='头像').add_subparsers(dest='sub', required=True)
    p = av.add_parser('set', help='上传并设置头像（PNG/JPEG，不超过 2MB）')
    p.add_argument('token'), p.add_argument('--file', required=True, metavar='PATH')
    av.add_parser('rm', help='移除头像').add_argument('token')

    for kind, name in [('edu', '教育经历'), ('work', '工作经历'), ('org', '社团/组织经历')]:
        grp = top.add_parser(kind, help=name).add_subparsers(dest='cmd', required=True)
        grp.add_parser('list', help='列出').add_argument('token')
        p = grp.add_parser('add', help='新增')
        p.add_argument('token'), _add_entry_args(p, kind)
        p = grp.add_parser('set', help='修改（只改传入的字段）')
        p.add_argument('token'), p.add_argument('id', type=int), _add_entry_args(p, kind)
        p = grp.add_parser('rm', help='删除')
        p.add_argument('token'), p.add_argument('id', type=int)
        p = grp.add_parser('reorder', help='调整顺序（按给定 id 顺序排列，须包含全部）')
        p.add_argument('token'), p.add_argument('ids', nargs='+', type=int)

    ext = top.add_parser('extra', help='技能/语言/证书/兴趣/其他').add_subparsers(dest='cmd', required=True)
    ext.add_parser('list', help='列出').add_argument('token')
    p = ext.add_parser('set', help='新建或修改一类内容')
    p.add_argument('token'), p.add_argument('kind', choices=list(EXTRA_FIELDS))
    p.add_argument('--desc', required=True, help='文本内容，多条可用换行或分号')
    p = ext.add_parser('rm', help='删除一类内容')
    p.add_argument('token'), p.add_argument('kind', choices=list(EXTRA_FIELDS))
    p = ext.add_parser('reorder', help='调整已设置的技能块顺序')
    p.add_argument('token'), p.add_argument('kinds', nargs='+', choices=list(EXTRA_FIELDS))

    sec = top.add_parser('section', help='自定义模块').add_subparsers(dest='cmd', required=True)
    sec.add_parser('list', help='列出').add_argument('token')
    p = sec.add_parser('add', help='新增模块（--category 选择类别）')
    p.add_argument('token'), p.add_argument('--title', required=True)
    p.add_argument('--category', default='custom', help='、'.join(f'{k}({v})' for k, v in MODULE_CATEGORIES.items()))
    p = sec.add_parser('rename', help='重命名模块')
    p.add_argument('token'), p.add_argument('sid', type=int), p.add_argument('--title', required=True)
    p = sec.add_parser('rm', help='删除模块（连同其条目）')
    p.add_argument('token'), p.add_argument('sid', type=int)

    mod = top.add_parser('module', help='内置模块标题（教育/工作/组织/技能）').add_subparsers(dest='cmd', required=True)
    mod.add_parser('list', help='列出标题').add_argument('token')
    p = mod.add_parser('title', help='修改标题')
    p.add_argument('token'), p.add_argument('target', choices=list(MODULE_TARGETS)), p.add_argument('--title', required=True)

    fmt = top.add_parser('format', help='版式与主题').add_subparsers(dest='cmd', required=True)
    fmt.add_parser('show', help='查看版式').add_argument('token')
    fmt.add_parser('options', help='列出可用的版式取值')
    p = fmt.add_parser('set', help='设置单项版式，如 top_format=neat（标题颜色 title_color 只改标题）')
    p.add_argument('token'), p.add_argument('pairs', nargs='+', metavar='key=value')
    p = fmt.add_parser('theme', help='整体主题色（同时联动标题与顶栏颜色）')
    p.add_argument('token'), p.add_argument('--color', required=True, help='#RRGGBB')
    p.add_argument('--custom', action='store_true', help='按自定义取色写入（对应网页取色器）')

    tpl = top.add_parser('template', help='模板').add_subparsers(dest='cmd', required=True)
    tpl.add_parser('list', help='列出可用模板')

    opus = top.add_parser('opus', help='作品集').add_subparsers(dest='cmd', required=True)
    opus.add_parser('list', help='列出作品').add_argument('token')
    p = opus.add_parser('add', help='新增作品')
    p.add_argument('token'), p.add_argument('--title', required=True, help=f'最多 {OPUS_TITLE_MAX} 字')
    p.add_argument('--url', default='', help='作品链接')
    p.add_argument('--qrcode', metavar='PATH', help='二维码图片（PNG/JPEG，不超过 2MB）')
    p = opus.add_parser('set', help='修改作品（只改传入的字段；二维码保留，除非 --remove-qrcode）')
    p.add_argument('token'), p.add_argument('id', type=int)
    p.add_argument('--title'), p.add_argument('--url')
    grp = p.add_mutually_exclusive_group()
    grp.add_argument('--qrcode', metavar='PATH', help='替换二维码图片（PNG/JPEG，不超过 2MB）')
    grp.add_argument('--remove-qrcode', action='store_true', help='移除作品二维码')
    p = opus.add_parser('rm', help='删除作品')
    p.add_argument('token'), p.add_argument('id', type=int)
    p = opus.add_parser('reorder', help='调整作品顺序（须包含全部）')
    p.add_argument('token'), p.add_argument('ids', nargs='+', type=int)

    logo = top.add_parser('school-logo', help='校徽').add_subparsers(dest='cmd', required=True)
    p = logo.add_parser('search', help='按学校名搜索校徽')
    p.add_argument('token'), p.add_argument('--name', required=True)
    p = logo.add_parser('set', help='设置校徽（先按学校名搜索，再按 id 选择）')
    p.add_argument('token'), p.add_argument('--name', required=True), p.add_argument('--id', type=int, required=True)
    p = logo.add_parser('upload', help='上传自定义校徽图片（PNG/JPEG）')
    p.add_argument('token'), p.add_argument('--file', required=True, metavar='PATH')
    logo.add_parser('rm', help='移除校徽').add_argument('token')

    share = top.add_parser('share', help='分享设置').add_subparsers(dest='cmd', required=True)
    share.add_parser('status', help='查看分享链接与设置').add_argument('token')
    p = share.add_parser('set', help='修改分享设置')
    p.add_argument('token')
    p.add_argument('--public', choices=['on', 'off'], help='on 开启链接分享；off 关闭（即撤销链接）')
    p.add_argument('--visibility', choices=list(SHARE_VISIBILITY), help='full 完整可见；privacy 保护隐私')
    p.add_argument('--comment', choices=['on', 'off'], help='是否允许评论')

    item = top.add_parser('item', help='自定义模块中的条目').add_subparsers(dest='cmd', required=True)
    p = item.add_parser('list', help='列出模块内条目')
    p.add_argument('token'), p.add_argument('sid', type=int)
    for name in ('add', 'set'):
        p = item.add_parser(name, help='新增条目' if name == 'add' else '修改条目（只改传入的字段）')
        p.add_argument('token'), p.add_argument('sid', type=int)
        if name == 'set':
            p.add_argument('iid', type=int)
        p.add_argument('--start', help='YYYY-MM 或 YYYY-MM-DD'), p.add_argument('--end', help='YYYY-MM 或 YYYY-MM-DD')
        p.add_argument('--present', action='store_true', help='至今（与 --end 互斥）')
        for f in ITEM_FIELDS:
            p.add_argument(f'--{f}')
    p = item.add_parser('rm', help='删除条目')
    p.add_argument('token'), p.add_argument('sid', type=int), p.add_argument('iid', type=int)
    p = item.add_parser('reorder', help='调整模块内条目顺序（须包含全部条目）')
    p.add_argument('token'), p.add_argument('sid', type=int), p.add_argument('iids', nargs='+', type=int)

    rcv = top.add_parser('recover', help='恢复结果未知的新增操作（列出未决操作、查看候选、显式挂载或丢弃）')
    rcv.set_defaults(cmd=None)
    rcv.add_argument('op', nargs='?', help='操作编号（不填则列出全部未决操作）')
    grp = rcv.add_mutually_exclusive_group()
    grp.add_argument('--attach', metavar='ID', help='把候选对象挂载到原目标')
    grp.add_argument('--discard', metavar='ID', help='丢弃候选对象（未挂载且未被引用）')
    grp.add_argument('--adopt', metavar='ID', help='保留新建/复制的简历并清除记录')
    grp.add_argument('--none', action='store_true', help='确认未创建任何对象（仅当候选为空）')

    orp = top.add_parser('orphan', help='清理失败遗留的孤立对象').add_subparsers(dest='cmd', required=True)
    p = orp.add_parser('clean', help='删除未被任何简历引用的对象')
    p.add_argument('type', choices=['edu', 'work', 'org', 'extra', 'section', 'item', 'opus'])
    p.add_argument('id', type=int)
    p.add_argument('--kind', choices=list(EXTRA_FIELDS), help='type=extra 时指定技能块类型')
    p.add_argument('--sid', type=int, help='type=item 时指定所在模块 id')
    return ap


def _item_body(args):
    body = {f: getattr(args, f) for f in ITEM_FIELDS if getattr(args, f) is not None}
    body.update(_period_body(args, has_present=True))
    return body


def _profile_body(pairs):
    body = {}
    for pair in pairs:
        key, sep, value = pair.partition('=')
        if not sep or key not in PROFILE_KEYS:
            raise _input_error(f'无效字段 {key!r}，可选：{"、".join(sorted(PROFILE_KEYS))}')
        if key == 'birthday':
            _date(value)
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', value):
                raise _input_error('birthday 应为 YYYY-MM-DD')
        if key == 'age_birthday' and value not in ('birthday', 'age'):
            raise _input_error('age_birthday 应为 birthday（显示生日）或 age（显示年龄）')
        if key == 'show_attr_name':
            if value not in ('0', '1'):
                raise _input_error('show_attr_name 应为 1（显示信息名称）或 0（不显示）')
            value = int(value)
        body[key] = value
    return body


def run(args, c):
    g, cmd = args.group, args.cmd
    if g == 'resume':
        if cmd == 'list':
            return list_resumes(c)
        if cmd == 'show':
            return show_resume(c, args.token)
        if cmd == 'dump':
            return dump_resume(c, args.token)
        if cmd == 'create':
            return {'token': create_resume(c, args.name, args.template or BLANK_TEMPLATE)}
        if cmd == 'copy':
            return {'token': copy_resume(c, args.token, args.name)}
        if cmd == 'export':
            return export_resume(c, args.token, args.format, args.out, force=args.force)
        if cmd == 'reorder':
            return reorder_sections(c, args.token, args.keys)
        if cmd == 'rename':
            rename_resume(c, args.token, args.name)
        if cmd == 'rm':
            trash_resume(c, args.token)
        if cmd == 'trash':
            return list_trash(c)
        if cmd == 'restore':
            restore_resume(c, args.token)
        if cmd == 'purge':
            purge_resume(c, args.token)
        return {'ok': True}
    if g == 'profile':
        if cmd == 'avatar':
            if args.sub == 'set':
                return set_avatar(c, args.token, args.file)
            return remove_avatar(c, args.token)
        if cmd == 'show':
            return get_profile(c, args.token)
        set_profile(c, args.token, _profile_body(args.pairs))
        return get_profile(c, args.token)
    if g in ENTRY_PATH:
        kind = g
        if cmd == 'list':
            return list_entries(c, args.token, kind)
        if cmd == 'add':
            return add_entry(c, args.token, kind, _entry_body(kind, args))
        if cmd == 'set':
            return set_entry(c, args.token, kind, args.id, _entry_body(kind, args))
        if cmd == 'rm':
            return rm_entry(c, args.token, kind, args.id)
        if cmd == 'reorder':
            return reorder_entries(c, args.token, kind, args.ids)
    if g == 'extra':
        if cmd == 'list':
            return list_extras(c, args.token)
        if cmd == 'set':
            return set_extra(c, args.token, args.kind, args.desc)
        if cmd == 'reorder':
            return reorder_extras(c, args.token, args.kinds)
        return rm_extra(c, args.token, args.kind)
    if g == 'section':
        if cmd == 'list':
            cv = get_cv(c, args.token)
            return [{'id': p['id'], 'category': p['type'],
                     'title': cv['submodule_info']['custom_sections_titles'].get(f"{p['id']}_title"),
                     'info_ids': p['info_ids']} for p in _custom_sections(cv['submodule_info'])]
        if cmd == 'add':
            return {'id': add_section(c, args.token, args.title, category=args.category)}
        if cmd == 'rename':
            rename_section(c, args.token, args.sid, args.title)
            return {'ok': True}
        return rm_section(c, args.token, args.sid)
    if g == 'module':
        if cmd == 'list':
            return list_module_titles(c, args.token)
        set_module_title(c, args.token, args.target, args.title)
        return list_module_titles(c, args.token)
    if g == 'template':
        return list_templates(c)
    if g == 'opus':
        if cmd == 'list':
            return list_opus(c, args.token)
        if cmd == 'add':
            return add_opus(c, args.token, args.title, args.url, qrcode_path=args.qrcode)
        if cmd == 'set':
            return set_opus(c, args.token, args.id, title=args.title, url=args.url,
                            remove_qrcode=args.remove_qrcode, qrcode_path=args.qrcode)
        if cmd == 'reorder':
            return reorder_opus(c, args.token, args.ids)
        return rm_opus(c, args.token, args.id)
    if g == 'school-logo':
        if cmd == 'search':
            return search_school_logos(c, args.token, args.name)
        if cmd == 'set':
            return set_school_logo(c, args.token, args.name, args.id)
        if cmd == 'upload':
            return set_school_logo_file(c, args.token, args.file)
        rm_school_logo(c, args.token)
        return {'ok': True}
    if g == 'share':
        if cmd == 'status':
            return share_status(c, args.token)
        return set_share(c, args.token,
                         public=None if args.public is None else args.public == 'on',
                         visibility=args.visibility,
                         comment=None if args.comment is None else args.comment == 'on')
    if g == 'format':
        if cmd == 'show':
            return get_format(c, args.token)
        if cmd == 'options':
            return format_options()
        if cmd == 'theme':
            return set_theme(c, args.token, args.color, custom=args.custom)
        set_format(c, args.token, parse_format(args.pairs))
        return get_format(c, args.token)
    if g == 'item':
        if cmd == 'list':
            return list_items(c, args.token, args.sid)
        if cmd == 'add':
            return add_item(c, args.token, args.sid, _item_body(args))
        if cmd == 'set':
            return set_item(c, args.token, args.sid, args.iid, _item_body(args))
        if cmd == 'reorder':
            return reorder_items(c, args.token, args.sid, args.iids)
        return rm_item(c, args.token, args.sid, args.iid)
    if g == 'orphan':
        return orphan_clean(c, args.type, args.id, sid=args.sid, kind=args.kind)
    if g == 'recover':
        if args.op is None:
            return recover_list(c)
        if args.none:
            return recover_run(c, args.op, action='none')
        for action in ('attach', 'discard', 'adopt'):
            if getattr(args, action):
                return recover_run(c, args.op, action=action, obj_id=getattr(args, action))
        return recover_run(c, args.op)
    raise _input_error(f'未知命令 {g} {cmd}')


def to_text(obj, indent=0):
    pad = '  ' * indent
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f'{pad}{k}:')
                lines.append(to_text(v, indent + 1))
            elif isinstance(v, (dict, list)):
                lines.append(f'{pad}{k}: (空)')
            else:
                lines.append(f'{pad}{k}: {_scalar(v)}')
        return '\n'.join(lines)
    if isinstance(obj, list):
        if not obj:
            return f'{pad}(空)'
        out = []
        for x in obj:
            if isinstance(x, (dict, list)):
                out.append(f'{pad}-\n{to_text(x, indent + 1)}')
            else:
                out.append(f'{pad}- {_scalar(x)}')
        return '\n'.join(out)
    return f'{pad}{_scalar(obj)}'


def _scalar(v):
    if v is None:
        return '-'
    return re.sub(r'<[^>]+>', '', str(v)).replace('&nbsp;', ' ').strip() or '-'


def load_token():
    env = os.environ.get('WONDERCV_TOKEN', '').strip()
    if env:
        return env
    if not os.path.exists(TOKEN_FILE):
        return ''
    try:
        with open(TOKEN_FILE, encoding='utf-8') as f:
            token = f.read().strip()
    except (OSError, UnicodeDecodeError) as e:
        raise WcvError('无法读取凭证文件，请检查权限与编码（内容未输出）', exit_code=EXIT_INPUT) from e
    if os.stat(TOKEN_FILE).st_mode & 0o077:
        print(f'警告: 凭证文件 {TOKEN_FILE} 权限过宽，建议 chmod 600', file=sys.stderr)
    return token


_install_transport()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run(args, Client(load_token()))
    except WcvError as e:
        print(e.describe(), file=sys.stderr)
        return e.exit_code
    except Exception as e:  # 未预料的错误：默认不输出堆栈，避免泄露细节
        if os.environ.get('WONDERCV_DEBUG') == '1':
            raise
        print(f'内部错误：{type(e).__name__}（设置 WONDERCV_DEBUG=1 可查看堆栈）', file=sys.stderr)
        return EXIT_API
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else to_text(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""头部模块与模块样式的规则测试（来自前端 computedHeadDis / computedModuleDis / setHeadModule）。

运行：python3 -m unittest tests.test_headmodule -v
"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wondercv as w  # noqa: E402
from tests.fakes import RichFake  # noqa: E402


class HeadRules(unittest.TestCase):
    def test_head_disabled_by_name_and_top_format(self):
        self.assertTrue(w._head_module_disabled({'top_format': 'default'}, 0, 'clear'))
        self.assertFalse(w._head_module_disabled({'top_format': 'default'}, 1, 'clear'))
        self.assertTrue(w._head_module_disabled({'top_format': 'neat'}, 1, 'card'))
        self.assertTrue(w._head_module_disabled({'top_format': 'circle'}, 1, 'basic'))
        self.assertTrue(w._head_module_disabled({'top_format': 'default', 'module_format': 'premium-flat'}, 1, 'left_block'))
        self.assertTrue(w._head_module_disabled({'top_format': 'personalized'}, 1, 'circle'))
        self.assertFalse(w._head_module_disabled({'top_format': 'default'}, 1, 'circle'))

    def test_module_disabled_rules(self):
        self.assertTrue(w._module_disabled({'head_module_format': 'column'}, 'strip-flat'))
        self.assertFalse(w._module_disabled({'head_module_format': 'column'}, 'default-flat'))
        self.assertTrue(w._module_disabled({'top_format': 'personalized'}, 'default-stereo'))
        self.assertTrue(w._module_disabled({'head_module_format': 'left_block'}, 'premium-flat'))
        self.assertFalse(w._module_disabled({'head_module_format': 'work'}, 'premium-flat'))

    def test_head_side_effects_follow_setHeadModule(self):
        p = w._head_module_patch({'head_format': 'center'}, 'business')
        self.assertEqual((p['head_format'], p['head_module_color']), ('center', '#000000'))
        p = w._head_module_patch({'head_format': 'center'}, 'clear')
        self.assertEqual(p['head_format'], 'left')
        p = w._head_module_patch({'head_format': 'left'}, 'circle')
        self.assertEqual(p['head_format'], 'body_center')
        p = w._head_module_patch({'head_format': 'body_center', 'head_module_color': '#000000',
                                  'profile_color': '#000000'}, 'work')
        self.assertEqual((p['profile_color'], p['profile_select_color'], p['head_format']), ('#FFFFFF', True, 'left'))
        p = w._head_module_patch({'head_format': 'body_center', 'head_module_color': '#FF0000'}, 'work')
        self.assertNotIn('profile_color', p)


class ProfileRichFake(RichFake):
    show_attr_name = 1

    def _handle(self, method, path, params, body):
        if method == 'GET' and path.endswith('/profile'):
            return {'profile': {'show_attr_name': self.show_attr_name}}
        return super()._handle(method, path, params, body)


class HeadWriteTest(unittest.TestCase):
    def setUp(self):
        self.api = ProfileRichFake()
        self.api.cvs['AAAA0001']['format_info'].update(top_format='default', head_format='center',
                                                       module_format='default-flat')

    @property
    def fmt(self):  # 每次从假服务端读取（PUT 会替换整个 format_info）
        return self.api.cvs['AAAA0001']['format_info']

    def test_head_write_applies_side_effects(self):
        w.set_format(self.api, 'AAAA0001', {'head_module_format': 'business'})
        self.assertEqual((self.fmt['head_module_format'], self.fmt['head_format'], self.fmt['head_module_color']),
                         ('business', 'center', '#000000'))

    def test_explicit_field_overrides_side_effect(self):
        w.set_format(self.api, 'AAAA0001', {'head_module_format': 'circle', 'head_format': 'left'})
        self.assertEqual((self.fmt['head_module_format'], self.fmt['head_format']), ('circle', 'left'))

    def test_disabled_combination_is_refused_without_write(self):
        self.api.show_attr_name = 0
        before = copy.deepcopy(self.fmt)
        with self.assertRaises(w.WcvError) as cm:
            w.set_format(self.api, 'AAAA0001', {'head_module_format': 'clear'})
        self.assertIn('不可用', str(cm.exception))
        self.assertEqual(self.fmt, before)
        self.assertFalse([c for c in self.api.calls if c[0] == 'PUT'])

    def test_module_format_disabled_refused(self):
        self.fmt['head_module_format'] = 'column'
        with self.assertRaises(w.WcvError):
            w.set_format(self.api, 'AAAA0001', {'module_format': 'strip-flat'})
        w.set_format(self.api, 'AAAA0001', {'module_format': 'default-flat'})
        self.assertEqual(self.fmt['module_format'], 'default-flat')

    def test_values_are_limited_to_frontend_lists(self):
        self.assertEqual(len(w.HEAD_MODULE_FORMATS), 13)
        self.assertEqual(len(w.MODULE_FORMATS), 49)
        for bad in ('head_module_format=neatish', 'module_format=strip-foil'):
            with self.subTest(bad=bad), self.assertRaises(w.WcvError):
                w.parse_format([bad])


if __name__ == '__main__':
    unittest.main(verbosity=2)

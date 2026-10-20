"""测试包：测试期间把 CLI 的操作记录目录（STATE_DIR）指向临时目录，不写入用户配置。"""
import atexit
import os
import shutil
import tempfile

if 'WONDERCV_STATE_DIR' not in os.environ:
    _state = tempfile.mkdtemp(prefix='wcv-state-test-')
    os.environ['WONDERCV_STATE_DIR'] = _state
    atexit.register(shutil.rmtree, _state, True)

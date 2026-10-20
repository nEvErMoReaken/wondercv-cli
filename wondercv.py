#!/usr/bin/env python3
"""Compatibility entry point; the CLI ships with the installable WonderCV skill."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

_name = __name__ if __name__ != '__main__' else 'wondercv'
_spec = spec_from_file_location(_name, Path(__file__).parent / 'skills/wondercv/scripts/wondercv.py')
_cli = module_from_spec(_spec)
sys.modules[_name] = _cli
_spec.loader.exec_module(_cli)

if __name__ == '__main__':
    sys.exit(_cli.main())

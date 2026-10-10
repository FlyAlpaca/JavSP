"""兼容旧导入路径；实现位于 javsp.scan.file。"""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("javsp.scan.file")

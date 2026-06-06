"""获取当前版本的脚本，供 CI 打包时使用"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from javsp.__version__ import __version__

print(__version__)

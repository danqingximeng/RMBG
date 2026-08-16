import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 让 `import standalone.*` 在任意 rootdir 下都可用
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 坑：仓库根有 ComfyUI 的 __init__.py（含 NODE_CLASS_MAPPINGS），pytest 会把根目录
# 当 Package 收集并在 setup 时 import 它 —— 其 load_nodes() 会 rglob 执行全仓库
# 所有 .py（包括 .venv，曾把 numba 的 CLI 拿 pytest argv 跑崩）。这里预先往
# sys.modules 塞一个无害的同名假包顶掉这次 import；standalone 代码并不依赖根包。
if "RMBG" not in sys.modules:
    fake = types.ModuleType("RMBG")
    fake.__file__ = str(REPO_ROOT / "__init__.py")
    fake.__path__ = [str(REPO_ROOT)]
    fake.NODE_CLASS_MAPPINGS = {}
    fake.NODE_DISPLAY_NAME_MAPPINGS = {}
    sys.modules["RMBG"] = fake

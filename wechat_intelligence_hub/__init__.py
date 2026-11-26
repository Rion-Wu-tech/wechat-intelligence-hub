"""WeChat Intelligence Hub Engine Wrapper."""
import sys
from pathlib import Path

_proj_dir = Path(__file__).resolve().parent / "projects" / "wechat-intelligence-hub"
if not _proj_dir.exists():
    _proj_dir = Path(__file__).resolve().parent.parent / "projects" / "wechat-intelligence-hub"

if str(_proj_dir) not in sys.path:
    sys.path.insert(0, str(_proj_dir))

_hub_py = _proj_dir / "wechat_intelligence_hub.py"
if _hub_py.exists():
    import importlib.util
    _spec = importlib.util.spec_from_file_location("wechat_intelligence_hub", _hub_py)
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["wechat_intelligence_hub"] = _mod
        _spec.loader.exec_module(_mod)
        for _k, _v in _mod.__dict__.items():
            if not _k.startswith("__"):
                globals()[_k] = _v


try:
    from engine import whitelist
    from engine import contact_resolver
except ImportError:
    pass



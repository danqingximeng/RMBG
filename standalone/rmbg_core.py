"""Core wrapper: loads the RMBG / BiRefNet node modules with the folder_paths
shim and exposes a simple PIL-in / PIL-out API.

Intended for standalone CLI and WebUI use, no ComfyUI required.

Node instances are process-level singletons so a running daemon keeps model
weights loaded across requests. warmup()/unload()/unload_all() let the daemon
manage the weights' lifetime (idle unload).
"""

import importlib.util
import os
import sys
import threading
import time
import warnings

from PIL import Image

from standalone.model_names import MODEL_ALIASES, aliases

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM_DIR = os.path.dirname(os.path.abspath(__file__))
NODE_DIR = os.path.join(REPO_ROOT, "py")

_lock = threading.Lock()

# Noise from third-party libs / node code, filtered before any node import.
warnings.filterwarnings("ignore", message="Failed to import flet.*")
warnings.filterwarnings("ignore", message=".*torch.meshgrid.*")
warnings.filterwarnings("ignore", category=FutureWarning, module="timm")
warnings.filterwarnings("ignore", message="local_dir_use_symlinks.*")

_RMBG_ORIG = ("RMBG-2.0", "INSPYRENET", "BEN", "BEN2")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_shim():
    if SHIM_DIR not in sys.path:
        sys.path.insert(0, SHIM_DIR)
    if NODE_DIR not in sys.path:
        sys.path.insert(0, NODE_DIR)


_rmbg_module = None
_birefnet_module = None
_rmbg_node = None
_birefnet_node = None


def _get_rmbg_module():
    """AILab_RMBG hosts RMBG-2.0/INSPYRENET/BEN/BEN2 (incl. transparent_background)."""
    global _rmbg_module
    if _rmbg_module is None:
        _ensure_shim()
        _rmbg_module = _load_module("AILab_RMBG", os.path.join(NODE_DIR, "AILab_RMBG.py"))
    return _rmbg_module


def _get_birefnet_module():
    """AILab_BiRefNet hosts all BiRefNet variants (torch + transformers, no transparent_background)."""
    global _birefnet_module
    if _birefnet_module is None:
        _ensure_shim()
        _birefnet_module = _load_module("AILab_BiRefNet", os.path.join(NODE_DIR, "AILab_BiRefNet.py"))
    return _birefnet_module


def _get_rmbg_node():
    global _rmbg_node
    if _rmbg_node is None:
        _rmbg_node = _get_rmbg_module().RMBG()
    return _rmbg_node


def _get_birefnet_node():
    global _birefnet_node
    if _birefnet_node is None:
        _birefnet_node = _get_birefnet_module().BiRefNetRMBG()
    return _birefnet_node


def _resolve(model):
    return MODEL_ALIASES.get(model, model)


def _loader_for(model):
    """Return (model_loader, original_name) for a model alias or original name."""
    orig = _resolve(model)
    if orig in _RMBG_ORIG:
        return _get_rmbg_node().models[orig], orig
    return _get_birefnet_node().model, orig


def available_models():
    return aliases()


def warmup(model):
    """Load model weights without inference (downloads on first use)."""
    loader, orig = _loader_for(model)
    with _lock:
        ok, msg = loader.check_model_cache(orig)
        if not ok:
            print(f"Downloading {orig} model files...")
            loader.download_model(orig)
        loader.load_model(orig)


def unload(model):
    """Release a single model's weights (idempotent)."""
    loader, _ = _loader_for(model)
    with _lock:
        loader.clear_model()


def unload_all():
    """Release all loaded weights across both families (idempotent)."""
    with _lock:
        if _rmbg_node is not None:
            for loader in _rmbg_node.models.values():
                loader.clear_model()
        if _birefnet_node is not None:
            _birefnet_node.model.clear_model()


def any_model_loaded():
    """True if any model weights are currently resident (for /health)."""
    if _rmbg_node is not None:
        for loader in _rmbg_node.models.values():
            if loader.model is not None:
                return True
    if _birefnet_node is not None and _birefnet_node.model.model is not None:
        return True
    return False


def _pil2tensor(image):
    import numpy as np
    import torch
    return torch.from_numpy(np.array(image).astype(np.float32) / 255.0).unsqueeze(0)


def _tensor2pil(tensor):
    import numpy as np
    return Image.fromarray(np.clip(255.0 * tensor.cpu().numpy().squeeze(), 0, 255).astype(np.uint8))


def remove_bg(image, model, process_res=1024, sensitivity=1.0, mask_blur=0,
              mask_offset=0, refine_foreground=False):
    """Remove background from a PIL image, return RGBA PIL image.

    Model aliases: rmbg2, inspyrenet, ben, ben2, birefnet, biref-lite, ...
    (see model_names.MODEL_ALIASES). Original node names also work.
    Models are auto-downloaded to models/RMBG/ on first use.
    """
    import torch

    orig = _resolve(model)
    params = {
        "process_res": process_res,
        "sensitivity": sensitivity,
        "mask_blur": mask_blur,
        "mask_offset": mask_offset,
        "refine_foreground": refine_foreground,
        "invert_output": False,
        "background": "Alpha",
        "background_color": "#222222",
    }
    tensor = _pil2tensor(image.convert("RGB"))
    with _lock:
        torch.set_num_threads(os.cpu_count())
        t0 = time.time()
        if orig in _RMBG_ORIG:
            out, _, _ = _get_rmbg_node().process_image(tensor, orig, **params)
        else:
            out, _, _ = _get_birefnet_node().process_image(tensor, orig, **params)
        elapsed = time.time() - t0
    return _tensor2pil(out), elapsed
"""Model alias registry. Zero dependencies — importable without torch.

CLI-friendly lowercase aliases -> original node model names.
Original names still work via the fallback in rmbg_core.remove_bg().
"""

MODEL_ALIASES = {
    "rmbg2": "RMBG-2.0",
    "inspyrenet": "INSPYRENET",
    "ben": "BEN",
    "ben2": "BEN2",
    "birefnet": "BiRefNet-general",
    "biref-512": "BiRefNet_512x512",
    "biref-hr": "BiRefNet-HR",
    "biref-portrait": "BiRefNet-portrait",
    "biref-matting": "BiRefNet-matting",
    "biref-hr-matting": "BiRefNet-HR-matting",
    "biref-lite": "BiRefNet_lite",
    "biref-lite2k": "BiRefNet_lite-2K",
    "biref-dynamic": "BiRefNet_dynamic",
    "biref-lite-matting": "BiRefNet_lite-matting",
    "biref-toon": "BiRefNet_toonout",
    "lucida": "Lucida",
}

RMBG_MODELS = {"rmbg2", "inspyrenet", "ben", "ben2"}
BIREFNET_MODELS = set(MODEL_ALIASES) - RMBG_MODELS

DEFAULT_MODEL = "inspyrenet"


def aliases():
    return sorted(MODEL_ALIASES)

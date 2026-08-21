"""RMBG daemon / WebUI. Usage:

    rmbg serve [--host] [--port] [--preload-model MODEL] [--no-preload]
               [--idle-kill-min 5] [--idle-unload-min 5] [-c CONFIG]
    python -m standalone.rmbg_web [--managed] ...   # CLI 内部 spawn 用

- Manual serve (default): preloads the default model at startup, keeps
  running, and unloads model weights after `--idle-unload-min` minutes
  without a request. If a daemon already runs on the port, serve does NOT
  start a second one: a CLI-spawned (managed) daemon is promoted to manual
  (POST /api/managed) so it stops self-killing and only unloads weights
  when idle.
- `--no-preload` disables the startup preload (default is to preload).
- Managed (CLI-spawned, --managed): exits entirely after `--idle-kill-min`
  minutes without a request.
- 未指定的参数回落 ~/.config/rmbg/config.yaml（优先级 CLI > config > 内建，
  见 rmbg_config.py）。

HTTP API (programmable; OpenAI-style envelope shared with upscayl-py):
    GET  /health        {"status":"ok", "service": "rmbg-daemon", managed, busy, ...}
    GET  /api/config    生效配置（serve 时含 CLI 合并结果；否则为文件+默认）
    GET  /api/models    {"data":[{"id":...,"default":bool}], "default":...}
    POST /api/managed   {"managed": bool} -> promote to manual (false) at runtime
    POST /api/rmbg      multipart (`file`, 1..n) or JSON ({"image": data-URI|path|URL})
                        + model/process_res/sensitivity/mask_blur/mask_offset/refine
                        -> {"created","model","data":[{"filename","b64_json","format",
                        "width","height","size"}],"usage":{"elapsed_ms"}}
                        Errors: {"error":{"message","type"}} + 400/404/500.
"""

import argparse
import base64
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from standalone import rmbg_core
from standalone.model_names import DEFAULT_MODEL, MODEL_ALIASES
from standalone.rmbg_config import Config, ConfigError
from standalone.rmbg_core import available_models, remove_bg
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="RMBG Standalone")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_state = {
    "t": time.monotonic(),
    "busy": False,
    "managed": False,
    "idle_kill_min": 5.0,
    "idle_unload_min": 5.0,
    # serve() 时写入；未 serve（TestClient 直接挂 app）时用内建默认
    "model_default": DEFAULT_MODEL,
    "config": None,
}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "rmbg-daemon",
        "managed": _state["managed"],
        "idle_kill_min": _state["idle_kill_min"],
        "idle_unload_min": _state["idle_unload_min"],
        "busy": _state["busy"],
        "model_loaded": rmbg_core.any_model_loaded(),
    }


@app.get("/api/config")
def api_config():
    if _state["config"] is not None:
        return _state["config"]
    try:
        return Config.load().to_dict()
    except ConfigError as e:
        return JSONResponse(*_err_payload(f"bad config: {e}", 500))


@app.get("/api/models")
def models():
    default = _state["model_default"]
    return {
        "data": [{"id": m, "default": m == default} for m in available_models()],
        "default": default,
    }


@app.post("/api/managed")
async def set_managed(request: Request):
    """Promote a CLI-spawned daemon to manual at runtime (idempotent)."""
    body = await request.json()
    _state["managed"] = bool(body.get("managed", False))
    _state["t"] = time.monotonic()
    return {
        "managed": _state["managed"],
        "idle_kill_min": _state["idle_kill_min"],
        "idle_unload_min": _state["idle_unload_min"],
    }


def _parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value):
    return value is True or value == "true"


OPTION_FIELDS = (
    "model",
    "process_res",
    "sensitivity",
    "mask_blur",
    "mask_offset",
    "refine",
)


def _err_payload(msg, status=400):
    error_type = {400: "invalid_request_error", 404: "not_found_error"}.get(
        status, "server_error"
    )
    return {"error": {"message": msg, "type": error_type}}, status


def _valid_models():
    """别名 + 原始节点名（_resolve 对原名是透传的，两者都合法）。受 allowed_models 白名单约束。"""
    # Serve 已运行时优先用 _state 中的生效配置（支持 -c 自定义路径）
    if _state.get("config") is not None:
        am = _state["config"].get("allowed_models")
        if am is not None:
            allowed = set(am)
            allowed_originals = {
                MODEL_ALIASES[a] for a in allowed if a in MODEL_ALIASES
            }
            return allowed | allowed_originals
        return set(MODEL_ALIASES) | set(MODEL_ALIASES.values())
    allowed = rmbg_core._allowed_set()
    if allowed is None:
        return set(MODEL_ALIASES) | set(MODEL_ALIASES.values())
    allowed_originals = {MODEL_ALIASES[a] for a in allowed if a in MODEL_ALIASES}
    return allowed | allowed_originals


def _decode_data_uri(uri):
    """'data:image/png;base64,xxxx' -> (bytes, filename)。失败抛 ValueError。"""
    if "," not in uri:
        raise ValueError("malformed data-URI: no ',' separator")
    meta, payload = uri.split(",", 1)
    meta = meta[5:].lower()  # 去掉 "data:"
    if not meta.startswith("image/"):
        raise ValueError(f"malformed data-URI: unsupported mime '{meta}'")
    subtype = meta.split(";", 1)[0][len("image/") :]
    ext = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "webp": "webp", "bmp": "bmp"}.get(
        subtype
    )
    if ext is None:
        raise ValueError(f"malformed data-URI: unsupported image mime '{subtype}'")
    try:
        return base64.b64decode(payload, validate=True), f"image.{ext}"
    except Exception as e:
        raise ValueError(f"malformed data-URI: bad base64 ({e})") from e


def _fetch_url(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def _process(images, opts):
    """同步推理（线程池里跑，别阻塞事件循环）。images: [(filename, bytes)]。"""
    try:
        model = opts.get("model") or _state["model_default"]
        valid = _valid_models()
        if model not in valid:
            allowed = rmbg_core._allowed_set()
            hint = sorted(allowed) if allowed is not None else sorted(MODEL_ALIASES)
            return _err_payload(
                f"unknown model '{model}'. available: {', '.join(hint)}",
                400,
            )
        data_items = []
        total = 0.0
        for filename, raw in images:
            try:
                image = Image.open(io.BytesIO(raw))
                image.load()
            except Exception as e:
                return _err_payload(f"cannot decode image '{filename}': {e}", 400)
            out, elapsed = remove_bg(
                image,
                model,
                process_res=_parse_int(opts.get("process_res"), 1024),
                sensitivity=_parse_float(opts.get("sensitivity"), 1.0),
                mask_blur=_parse_int(opts.get("mask_blur"), 0),
                mask_offset=_parse_int(opts.get("mask_offset"), 0),
                refine_foreground=_parse_bool(opts.get("refine")),
            )
            total += elapsed
            buf = io.BytesIO()
            out.save(buf, format="PNG")
            png = buf.getvalue()
            data_items.append(
                {
                    "filename": Path(filename).stem + ".png",
                    "b64_json": base64.b64encode(png).decode(),
                    "format": "png",
                    "width": out.width,
                    "height": out.height,
                    "size": len(png),
                }
            )
        return {
            "created": int(time.time()),
            "model": model,
            "data": data_items,
            "usage": {"elapsed_ms": int(total * 1000)},
        }, 200
    except Exception as e:  # noqa: BLE001  # any failure must become JSON 500, not crash the daemon
        return _err_payload(f"server error: {e}", 500)


@app.post("/api/rmbg")
async def rmbg(request: Request):
    ctype = request.headers.get("content-type", "").lower()
    images = []  # [(filename, bytes)]
    opts = {}
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            for f in form.getlist("file"):
                images.append((f.filename or "image.png", await f.read()))
            if not images:
                return JSONResponse(*_err_payload("no file uploaded"))
            for k in OPTION_FIELDS:
                v = form.get(k)
                if v is not None and v != "":
                    opts[k] = v
        else:
            body = await request.json()
            for k in OPTION_FIELDS:
                if k in body:
                    opts[k] = body[k]
            image = body.get("image")
            if not image:
                return JSONResponse(
                    *_err_payload("'image' required (data-URI, local path or URL)")
                )
            if image.startswith("data:"):
                raw, name = _decode_data_uri(image)
                images.append((name, raw))
            elif image.startswith(("http://", "https://")):
                images.append(
                    (
                        Path(image.split("?")[0]).name or "image.png",
                        await run_in_threadpool(_fetch_url, image),
                    )
                )
            else:
                path = Path(image).expanduser()
                if not path.is_file():
                    return JSONResponse(*_err_payload(f"image not found: {path}", 404))
                images.append((path.name, await run_in_threadpool(path.read_bytes)))
    except Exception as e:  # noqa: BLE001  # malformed multipart/JSON input -> JSON 400
        return JSONResponse(*_err_payload(f"bad request: {e}"))

    _state["busy"] = True
    _state["t"] = time.monotonic()
    try:
        payload, status = await run_in_threadpool(_process, images, opts)
        return JSONResponse(payload, status_code=status)
    finally:
        _state["busy"] = False
        _state["t"] = time.monotonic()


# 根挂载放最后：API 路由已注册完毕，未被匹配的路径交给静态 WebUI
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def _probe(port):
    """Return ("rmbg", health) if an RMBG daemon answers, ("other", _) if the
    port is occupied by something else, ("closed", _) if nothing listens."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError:
        return "closed", None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            h = json.loads(r.read())
            if h.get("service") == "rmbg-daemon":
                return "rmbg", h
    except Exception:
        pass
    return "other", None


def _set_managed(port, managed):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/managed",
        data=json.dumps({"managed": managed}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _idle_thread():
    while True:
        time.sleep(5)
        if _state["busy"]:
            continue
        idle = time.monotonic() - _state["t"]
        if _state["managed"]:
            if _state["idle_kill_min"] > 0 and idle > _state["idle_kill_min"] * 60:
                print(f"[idle {idle:.0f}s] managed daemon exiting (idle-kill)")
                os._exit(0)
        elif _state["idle_unload_min"] > 0 and idle > _state["idle_unload_min"] * 60:
            if rmbg_core.any_model_loaded():
                print(f"[idle {idle:.0f}s] unloading model weights")
                try:
                    rmbg_core.unload_all()
                except Exception as e:
                    print(f"[idle] unload failed: {e}")
            _state["t"] = time.monotonic()


def serve(
    host=None,
    port=None,
    preload_model=None,
    no_preload=False,
    managed=False,
    idle_kill_min=None,
    idle_unload_min=None,
    config_path=None,
):
    """Start the daemon. None 的参数按 CLI > config > 内建默认解析。

    main() 和 rmbg_cli 的 serve 子命令都走这里。
    """
    import uvicorn

    try:
        cfg = Config.load(Path(config_path).expanduser() if config_path else None)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    host = host if host is not None else cfg.host
    port = port if port is not None else cfg.port
    model_default = cfg.resolve_model()
    preload_model = preload_model if preload_model is not None else model_default
    preload = not no_preload and (cfg.preload or preload_model is not None)
    idle_kill_min = idle_kill_min if idle_kill_min is not None else cfg.idle_kill_min
    idle_unload_min = (
        idle_unload_min if idle_unload_min is not None else cfg.idle_unload_min
    )

    if preload and preload_model not in _valid_models():
        allowed = rmbg_core._allowed_set()
        hint = sorted(allowed) if allowed is not None else sorted(MODEL_ALIASES)
        print(
            f"error: unknown model '{preload_model}'. available: {', '.join(hint)}",
            file=sys.stderr,
        )
        sys.exit(1)

    _state["managed"] = managed
    _state["idle_kill_min"] = idle_kill_min
    _state["idle_unload_min"] = idle_unload_min
    _state["model_default"] = model_default
    # 生效配置（CLI 合并后）直接落 dict，/api/config 原样返回
    _state["config"] = {
        "default_model": cfg.default_model,
        "allowed_models": cfg.allowed_models,
        "host": host,
        "port": port,
        "idle_unload_min": idle_unload_min,
        "idle_kill_min": idle_kill_min,
        "preload": preload,
        "config_path": str(cfg.config_path) if cfg.config_path else None,
    }

    if not managed:
        status, existing = _probe(port)
        if status != "closed":
            if status == "other":
                print(
                    f"Port {port} is used by a non-RMBG service; not starting.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if existing["managed"]:
                _set_managed(port, False)
                print(
                    f"Existing CLI-spawned daemon on {port} promoted to manual: "
                    f"it will no longer self-kill, only unload weights when idle."
                )
            else:
                print(f"Daemon already running on {port} (manual); nothing to start.")
            return

    if preload:
        print(f"Preloading model: {preload_model}")
        rmbg_core.warmup(preload_model)
        _state["t"] = time.monotonic()

    threading.Thread(target=_idle_thread, daemon=True).start()

    print(
        f"RMBG daemon: http://{host}:{port}  "
        f"(managed={managed}, idle-kill={idle_kill_min}m, idle-unload={idle_unload_min}m)"
    )
    uvicorn.run(app, host=host, port=port)


def main():
    parser = argparse.ArgumentParser(description="RMBG daemon / WebUI")
    parser.add_argument("--host", help="bind address (default from config)")
    parser.add_argument("--port", type=int, help="port (default from config)")
    parser.add_argument(
        "--preload-model",
        help="Model alias to preload at startup (default: config model or inspyrenet)",
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="Do not preload any model at startup (default is to preload)",
    )
    parser.add_argument(
        "--managed",
        action="store_true",
        help="CLI-spawned: exit after idle-kill-min without requests",
    )
    parser.add_argument(
        "--idle-kill-min",
        type=float,
        help="Managed daemon: exit after N idle minutes (0=never)",
    )
    parser.add_argument(
        "--idle-unload-min",
        type=float,
        help="Manual daemon: unload weights after N idle minutes (0=never)",
    )
    parser.add_argument("-c", "--config", help="config file path")
    args = parser.parse_args()

    serve(
        host=args.host,
        port=args.port,
        preload_model=args.preload_model,
        no_preload=args.no_preload,
        managed=args.managed,
        idle_kill_min=args.idle_kill_min,
        idle_unload_min=args.idle_unload_min,
        config_path=args.config,
    )


if __name__ == "__main__":
    main()

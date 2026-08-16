"""RMBG daemon / WebUI. Usage:

    python -m standalone.rmbg_web [--port 8123] [--preload-model MODEL]
                                  [--no-preload] [--managed] [--idle-kill-min 5]
                                  [--idle-unload-min 5]

- Manual serve (default): preloads the default model (`--preload-model`,
  default inspyrenet) at startup, keeps running, and unloads model weights
  after `--idle-unload-min` minutes without a request. If a daemon already
  runs on the port, serve does NOT start a second one: a CLI-spawned
  (managed) daemon is promoted to manual (POST /api/managed) so it stops
  self-killing and only unloads weights when idle.
- `--no-preload` disables the startup preload (default is to preload).
- Managed (CLI-spawned, --managed): exits entirely after `--idle-kill-min`
  minutes without a request.

HTTP API (programmable; OpenAI-style envelope shared with upscayl-py):
    GET  /health        {"status":"ok", "service": "rmbg-daemon", managed, busy, ...}
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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from starlette.concurrency import run_in_threadpool

from standalone import rmbg_core
from standalone.model_names import DEFAULT_MODEL, MODEL_ALIASES
from standalone.rmbg_core import available_models, remove_bg

app = FastAPI(title="RMBG Standalone")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

_state = {
    "t": time.monotonic(),
    "busy": False,
    "managed": False,
    "idle_kill_min": 5.0,
    "idle_unload_min": 5.0,
}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()
    )


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


@app.get("/api/models")
def models():
    return {
        "data": [{"id": m, "default": m == DEFAULT_MODEL} for m in available_models()],
        "default": DEFAULT_MODEL,
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
    """别名 + 原始节点名（_resolve 对原名是透传的，两者都合法）。"""
    return set(MODEL_ALIASES) | set(MODEL_ALIASES.values())


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
        model = opts.get("model") or DEFAULT_MODEL
        if model not in _valid_models():
            return _err_payload(
                f"unknown model '{model}'. available: {', '.join(sorted(MODEL_ALIASES))}",
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
    finally:
        _state["busy"] = False
        _state["t"] = time.monotonic()


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


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="RMBG daemon / WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--preload-model",
        default="inspyrenet",
        help="Model alias to preload at startup (default: inspyrenet)",
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
        default=5.0,
        help="Managed daemon: exit after N idle minutes (0=never)",
    )
    parser.add_argument(
        "--idle-unload-min",
        type=float,
        default=5.0,
        help="Manual daemon: unload weights after N idle minutes (0=never)",
    )
    args = parser.parse_args()

    _state["managed"] = args.managed
    _state["idle_kill_min"] = args.idle_kill_min
    _state["idle_unload_min"] = args.idle_unload_min

    if not args.managed:
        status, existing = _probe(args.port)
        if status != "closed":
            if status == "other":
                print(
                    f"Port {args.port} is used by a non-RMBG service; not starting.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if existing["managed"]:
                _set_managed(args.port, False)
                print(
                    f"Existing CLI-spawned daemon on {args.port} promoted to manual: "
                    f"it will no longer self-kill, only unload weights when idle."
                )
            else:
                print(
                    f"Daemon already running on {args.port} (manual); nothing to start."
                )
            return

    if not args.no_preload:
        print(f"Preloading model: {args.preload_model}")
        rmbg_core.warmup(args.preload_model)
        _state["t"] = time.monotonic()

    threading.Thread(target=_idle_thread, daemon=True).start()

    print(
        f"RMBG daemon: http://{args.host}:{args.port}  "
        f"(managed={args.managed}, idle-kill={args.idle_kill_min}m, idle-unload={args.idle_unload_min}m)"
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

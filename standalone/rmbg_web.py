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

HTTP API (programmable, self-documented at /docs):
    GET  /health        {"service": "rmbg-daemon", managed, busy, model_loaded, ...}
    GET  /api/models    JSON list of model aliases
    POST /api/managed   {"managed": bool} -> promote to manual (false) at runtime
    POST /api/rmbg      multipart: files (1..n) + model/process_res/sensitivity/
                        mask_blur/mask_offset/refine -> PNG (single) or zip (multi)
"""

import argparse
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from standalone import rmbg_core
from standalone.model_names import DEFAULT_MODEL
from standalone.rmbg_core import available_models, remove_bg

app = FastAPI(title="RMBG Standalone")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

_state = {"t": time.monotonic(), "busy": False,
          "managed": False, "idle_kill_min": 5.0, "idle_unload_min": 5.0}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read())


@app.get("/health")
def health():
    return {"service": "rmbg-daemon",
            "managed": _state["managed"],
            "idle_kill_min": _state["idle_kill_min"],
            "idle_unload_min": _state["idle_unload_min"],
            "busy": _state["busy"],
            "model_loaded": rmbg_core.any_model_loaded()}


@app.get("/api/models")
def models():
    return {"models": available_models(), "default": DEFAULT_MODEL}


@app.post("/api/managed")
async def set_managed(request: Request):
    """Promote a CLI-spawned daemon to manual at runtime (idempotent)."""
    body = await request.json()
    _state["managed"] = bool(body.get("managed", False))
    _state["t"] = time.monotonic()
    return {"managed": _state["managed"],
            "idle_kill_min": _state["idle_kill_min"],
            "idle_unload_min": _state["idle_unload_min"]}


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


@app.post("/api/rmbg")
async def rmbg(files: list[UploadFile] = File(...),
               model: str = Form(DEFAULT_MODEL),
               process_res: str = Form("1024"),
               sensitivity: str = Form("1.0"),
               mask_blur: str = Form("0"),
               mask_offset: str = Form("0"),
               refine: str = Form("false")):
    _state["busy"] = True
    _state["t"] = time.monotonic()
    total = 0.0
    try:
        results = []
        for f in files:
            image = Image.open(io.BytesIO(await f.read()))
            out, elapsed = remove_bg(image, model,
                                     process_res=_parse_int(process_res, 1024),
                                     sensitivity=_parse_float(sensitivity, 1.0),
                                     mask_blur=_parse_int(mask_blur, 0),
                                     mask_offset=_parse_int(mask_offset, 0),
                                     refine_foreground=refine == "true")
            total += elapsed
            buf = io.BytesIO()
            out.save(buf, format="PNG")
            results.append((f.filename, buf.getvalue()))

        if len(results) == 1:
            return Response(content=results[0][1], media_type="image/png",
                            headers={"X-Elapsed-Seconds": f"{total:.1f}"})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in results:
                zf.writestr(Path(name).stem + ".png", data)
        buf.seek(0)
        return Response(content=buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=rmbg_results.zip",
                                 "X-Elapsed-Seconds": f"{total:.1f}"})
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
        method="POST")
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
            print(f"[idle {idle:.0f}s] unloading model weights")
            rmbg_core.unload_all()
            _state["t"] = time.monotonic()


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="RMBG daemon / WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--preload-model", default="inspyrenet", help="Model alias to preload at startup (default: inspyrenet)")
    parser.add_argument("--no-preload", action="store_true", help="Do not preload any model at startup (default is to preload)")
    parser.add_argument("--managed", action="store_true", help="CLI-spawned: exit after idle-kill-min without requests")
    parser.add_argument("--idle-kill-min", type=float, default=5.0, help="Managed daemon: exit after N idle minutes (0=never)")
    parser.add_argument("--idle-unload-min", type=float, default=5.0, help="Manual daemon: unload weights after N idle minutes (0=never)")
    args = parser.parse_args()

    _state["managed"] = args.managed
    _state["idle_kill_min"] = args.idle_kill_min
    _state["idle_unload_min"] = args.idle_unload_min

    if not args.managed:
        status, existing = _probe(args.port)
        if status != "closed":
            if status == "other":
                print(f"Port {args.port} is used by a non-RMBG service; not starting.", file=sys.stderr)
                sys.exit(1)
            if existing["managed"]:
                _set_managed(args.port, False)
                print(f"Existing CLI-spawned daemon on {args.port} promoted to manual: "
                      f"it will no longer self-kill, only unload weights when idle.")
            else:
                print(f"Daemon already running on {args.port} (manual); nothing to start.")
            return

    if not args.no_preload:
        print(f"Preloading model: {args.preload_model}")
        rmbg_core.warmup(args.preload_model)
        _state["t"] = time.monotonic()

    threading.Thread(target=_idle_thread, daemon=True).start()

    print(f"RMBG daemon: http://{args.host}:{args.port}  "
          f"(managed={args.managed}, idle-kill={args.idle_kill_min}m, idle-unload={args.idle_unload_min}m)")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
"""CLI for background removal. Usage:

    python -m standalone.rmbg_cli <image_or_dir> -o <out_dir> [options]

Processes a single image or every image in a directory, saving PNGs with
transparent background. Models are auto-downloaded on first use.

Daemon integration: if a RMBG daemon is running on the default port (or
--port), requests are forwarded to it. Otherwise a managed daemon is spawned
(exit after 5 idle minutes) and used; if that fails, processing falls back to
in-process.

Keep this module import-light: -l and --help must not import torch or the
model nodes (see lazy import of rmbg_core / daemon helpers in main()).
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

from standalone.model_names import DEFAULT_MODEL, MODEL_ALIASES

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

DEFAULT_PORT = 8123
REPO_ROOT = Path(__file__).resolve().parent.parent


def iter_images(path):
    if path.is_file():
        yield path
    elif path.is_dir():
        for p in sorted(path.iterdir()):
            if p.suffix.lower() in SUPPORTED_EXTS:
                yield p


def daemon_alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3) as r:
            data = json.loads(r.read())
        return data.get("service") == "rmbg-daemon"
    except Exception:
        return False


def spawn_daemon(port, model):
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + pythonpath if pythonpath else "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "standalone.rmbg_web",
         "--port", str(port), "--managed", "--preload-model", model],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env,
    )
    for _ in range(60):
        time.sleep(1)
        if daemon_alive(port):
            return True
        if proc.poll() is not None:
            return False
    return False


def multipart_body(boundary, args, file_bytes, filename):
    parts = []
    fields = [
        ("model", args.model),
        ("process_res", str(args.process_res)),
        ("sensitivity", str(args.sensitivity)),
        ("mask_blur", str(args.mask_blur)),
        ("mask_offset", str(args.mask_offset)),
        ("refine", "true" if args.refine else "false"),
    ]
    for k, v in fields:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\n"
        f"Content-Type: image/png\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts)


def process_via_daemon(files, args, port):
    url = f"http://127.0.0.1:{port}/api/rmbg"
    total = 0.0
    for i, path in enumerate(files, 1):
        buf = io.BytesIO()
        Image.open(path).convert("RGB").save(buf, format="PNG")
        boundary = uuid.uuid4().hex
        body = multipart_body(boundary, args, buf.getvalue(), path.name)
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=3600) as r:
            out_bytes = r.read()
        elapsed = time.time() - t0
        total += elapsed
        out_path = args.output / (path.stem + ".png")
        out_path.write_bytes(out_bytes)
        print(f"[{i}/{len(files)}] {path.name}: {elapsed:.1f}s -> {out_path.name} (daemon)")
    print(f"Done. {len(files)} image(s), total {total:.1f}s, avg {total / len(files):.1f}s/image")


def process_local(files, args, remove_bg):
    total = 0.0
    for i, path in enumerate(files, 1):
        image = Image.open(path)
        t0 = time.time()
        result, _ = remove_bg(image, args.model,
                              process_res=args.process_res,
                              sensitivity=args.sensitivity,
                              mask_blur=args.mask_blur,
                              mask_offset=args.mask_offset,
                              refine_foreground=args.refine)
        elapsed = time.time() - t0
        total += elapsed
        out_path = args.output / (path.stem + ".png")
        result.save(out_path)
        print(f"[{i}/{len(files)}] {path.name}: {elapsed:.1f}s -> {out_path.name} (local)")
    print(f"Done. {len(files)} image(s), total {total:.1f}s, avg {total / len(files):.1f}s/image")


def main():
    parser = argparse.ArgumentParser(description="Remove image backgrounds (CPU-friendly)")
    parser.add_argument("input", type=Path, nargs="?", help="Image file or directory of images")
    parser.add_argument("-o", "--output", type=Path, help="Output directory")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL,
                        help=f"Model alias (see -l; default: {DEFAULT_MODEL})")
    parser.add_argument("-l", "--list-models", action="store_true", help="List available model aliases and exit")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Daemon port (default: {DEFAULT_PORT})")
    parser.add_argument("--process_res", type=int, default=1024, help="Processing resolution (default: 1024)")
    parser.add_argument("--sensitivity", type=float, default=1.0, help="Mask sensitivity 0-1 (default: 1.0)")
    parser.add_argument("--mask_blur", type=int, default=0, help="Mask edge blur 0-64 (default: 0)")
    parser.add_argument("--mask_offset", type=int, default=0, help="Mask expand/shrink -64..64 (default: 0)")
    parser.add_argument("--refine", action="store_true", help="Refine foreground edges (slower)")
    args = parser.parse_args()

    if args.list_models:
        for name in sorted(MODEL_ALIASES):
            print(name)
        return

    if args.input is None or args.output is None:
        parser.print_usage()
        sys.exit(1)

    if args.model not in MODEL_ALIASES:
        print(f"Unknown model '{args.model}'. Available models:", file=sys.stderr)
        for name in sorted(MODEL_ALIASES):
            print(f"  {name}", file=sys.stderr)
        sys.exit(1)

    files = list(iter_images(args.input))
    if not files:
        print(f"No supported images found in {args.input}")
        sys.exit(1)

    args.output.mkdir(parents=True, exist_ok=True)
    print(f"Model: {args.model} | processing {len(files)} image(s)")

    if daemon_alive(args.port):
        process_via_daemon(files, args, args.port)
    elif spawn_daemon(args.port, args.model):
        print(f"Daemon spawned on port {args.port}")
        process_via_daemon(files, args, args.port)
    else:
        print("No daemon available, processing locally")
        from standalone.rmbg_core import remove_bg
        process_local(files, args, remove_bg)


if __name__ == "__main__":
    main()
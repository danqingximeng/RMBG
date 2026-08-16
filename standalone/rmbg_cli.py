"""CLI for background removal. Usage:

    rmbg [run] <image_or_dir> -o <out_dir> [options]
    rmbg serve [--host] [--port] [--preload-model] [--no-preload] ...
    rmbg list
    rmbg completion zsh|bash

`run` (default when the first arg is not a subcommand) processes a single
image or every image in a directory, saving PNGs with transparent
background. Models are auto-downloaded on first use. `-l/--list-models`
still works as a compat alias for `list`.

Daemon integration: if a RMBG daemon is running on the configured port
(or --port), requests are forwarded to it. Otherwise a managed daemon is
spawned (exit after idle-kill-min idle minutes) and used; if that fails,
processing falls back to in-process.

Precedence: CLI args > ~/.config/rmbg/config.yaml > built-in defaults
(see rmbg_config.py). `run`'s -m and --port, `serve`'s host/port/idle
settings all fall back to config when omitted.

Keep this module import-light: list/--help must not import torch or the
model nodes (rmbg_web/rmbg_core are imported lazily).
"""

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from standalone.model_names import DEFAULT_MODEL, MODEL_ALIASES
from standalone.rmbg_config import Config, ConfigError

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

REPO_ROOT = Path(__file__).resolve().parent.parent

COMMANDS = ("run", "serve", "list", "completion")


def iter_images(path):
    if path.is_file():
        yield path
    elif path.is_dir():
        for p in sorted(path.iterdir()):
            if p.suffix.lower() in SUPPORTED_EXTS:
                yield p


def daemon_alive(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=0.3
        ) as r:
            data = json.loads(r.read())
        return data.get("service") == "rmbg-daemon"
    except Exception:
        return False


def spawn_daemon(port, model, config_path=None):
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + pythonpath if pythonpath else "")
    cmd = [
        sys.executable,
        "-m",
        "standalone.rmbg_web",
        "--port",
        str(port),
        "--managed",
        "--preload-model",
        model,
    ]
    # 转发 -c：拉起的守护要读到同一份 config 的 idle/preload 等设置
    if config_path:
        cmd += ["-c", str(config_path)]
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    for _ in range(60):
        time.sleep(1)
        if daemon_alive(port):
            return True
        if proc.poll() is not None:
            return False
    return False


def _daemon_error_message(e):
    """HTTPError -> 服务端 {"error":{"message"}} 信封里的 message，解析失败回落 str(e)。"""
    try:
        body = json.loads(e.read())
        return body.get("error", {}).get("message", str(e))
    except Exception:  # noqa: BLE001  # 解析失败回落 str(e)，宽捕获否则畸形响应会崩
        return str(e)


def _print_summary(files, ok, failed, total):
    """汇总行：全部成功走 stdout，有失败走 stderr（ok=0 时不能除 0）。"""
    if failed:
        if ok:
            print(
                f"Done. {ok}/{len(files)} image(s), {failed} failed, total {total:.1f}s, "
                f"avg {total / ok:.1f}s/image",
                file=sys.stderr,
            )
        else:
            print(
                f"Done. {failed}/{len(files)} failed, nothing written",
                file=sys.stderr,
            )
    else:
        print(
            f"Done. {len(files)} image(s), total {total:.1f}s, "
            f"avg {total / len(files):.1f}s/image"
        )


def process_via_daemon(files, args, port):
    """逐张 POST /api/rmbg 的 JSON data-URI 输入，无需手拼 multipart。

    单张失败（daemon 4xx/5xx、网络错误、响应缺字段）打印报错并跳过，
    返回非 0 表示有失败。
    """
    url = f"http://127.0.0.1:{port}/api/rmbg"
    total = 0.0
    failed = 0
    for i, path in enumerate(files, 1):
        try:
            buf = io.BytesIO()
            Image.open(path).convert("RGB").save(buf, format="PNG")
            payload = {
                "image": "data:image/png;base64,"
                + base64.b64encode(buf.getvalue()).decode(),
                "model": args.model,
                "process_res": args.process_res,
                "sensitivity": args.sensitivity,
                "mask_blur": args.mask_blur,
                "mask_offset": args.mask_offset,
                "refine": args.refine,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=3600) as r:
                result = json.loads(r.read())
            out_bytes = base64.b64decode(result["data"][0]["b64_json"])
            out_path = args.output / (path.stem + ".png")
            out_path.write_bytes(out_bytes)
        except urllib.error.HTTPError as e:
            failed += 1
            print(
                f"[{i}/{len(files)}] {path.name}: daemon error {e.code}: "
                f"{_daemon_error_message(e)}",
                file=sys.stderr,
            )
            continue
        except Exception as e:  # noqa: BLE001  # 网络错误/坏文件/响应缺字段都跳过继续
            failed += 1
            print(f"[{i}/{len(files)}] {path.name}: {e}", file=sys.stderr)
            continue
        elapsed = time.time() - t0
        total += elapsed
        print(
            f"[{i}/{len(files)}] {path.name}: {elapsed:.1f}s -> {out_path.name} (daemon)"
        )
    ok = len(files) - failed
    _print_summary(files, ok, failed, total)
    return 1 if failed else 0


def process_local(files, args, remove_bg):
    """单张失败（坏图/推理异常）打印报错并跳过，返回非 0 表示有失败。"""
    total = 0.0
    failed = 0
    for i, path in enumerate(files, 1):
        try:
            image = Image.open(path)
            t0 = time.time()
            result, _ = remove_bg(
                image,
                args.model,
                process_res=args.process_res,
                sensitivity=args.sensitivity,
                mask_blur=args.mask_blur,
                mask_offset=args.mask_offset,
                refine_foreground=args.refine,
            )
            elapsed = time.time() - t0
            total += elapsed
            out_path = args.output / (path.stem + ".png")
            result.save(out_path)
        except Exception as e:  # noqa: BLE001  # 坏图/推理/保存失败都跳过继续
            failed += 1
            print(f"[{i}/{len(files)}] {path.name}: {e}", file=sys.stderr)
            continue
        print(
            f"[{i}/{len(files)}] {path.name}: {elapsed:.1f}s -> {out_path.name} (local)"
        )
    ok = len(files) - failed
    _print_summary(files, ok, failed, total)
    return 1 if failed else 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="rmbg", description="Remove image backgrounds (CPU-friendly)"
    )
    sub = p.add_subparsers(dest="command", metavar="run|serve|list|completion")

    pr = sub.add_parser("run", help="process an image or a directory of images")
    pr.add_argument("input", type=Path, help="image file or directory of images")
    pr.add_argument("-o", "--output", type=Path, required=True, help="output directory")
    pr.add_argument(
        "-m",
        "--model",
        help=f"model alias (default: config model or {DEFAULT_MODEL})",
    )
    pr.add_argument("--port", type=int, help="daemon port (default from config)")
    pr.add_argument(
        "--process_res",
        type=int,
        default=1024,
        help="processing resolution (default: 1024)",
    )
    pr.add_argument(
        "--sensitivity",
        type=float,
        default=1.0,
        help="mask sensitivity 0-1 (default: 1.0)",
    )
    pr.add_argument(
        "--mask_blur", type=int, default=0, help="mask edge blur 0-64 (default: 0)"
    )
    pr.add_argument(
        "--mask_offset",
        type=int,
        default=0,
        help="mask expand/shrink -64..64 (default: 0)",
    )
    pr.add_argument(
        "--refine", action="store_true", help="refine foreground edges (slower)"
    )
    pr.add_argument("-c", "--config", help="config file path")

    ps = sub.add_parser("serve", help="start daemon / WebUI (manual)")
    ps.add_argument("--host", help="bind address (default from config)")
    ps.add_argument("--port", type=int, help="port (default from config)")
    ps.add_argument(
        "--preload-model", help="model alias to preload (default: config model)"
    )
    ps.add_argument(
        "--no-preload",
        action="store_true",
        help="do not preload any model at startup (default is to preload)",
    )
    ps.add_argument(
        "--idle-kill-min",
        type=float,
        help="managed daemon: exit after N idle minutes (0=never)",
    )
    ps.add_argument(
        "--idle-unload-min",
        type=float,
        help="manual daemon: unload weights after N idle minutes (0=never)",
    )
    ps.add_argument("-c", "--config", help="config file path")

    pl = sub.add_parser("list", help="list available model aliases")
    pl.add_argument("-c", "--config", help="config file path")

    pc = sub.add_parser("completion", help="print shell completion script")
    pc.add_argument("shell", choices=("zsh", "bash"))
    return p


def _load_config(config_path: str | None) -> Config:
    try:
        return Config.load(Path(config_path).expanduser() if config_path else None)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_run(args, cfg: Config) -> int:
    model = args.model or cfg.resolve_model()
    if model not in MODEL_ALIASES:
        print(f"Unknown model '{model}'. Available models:", file=sys.stderr)
        for name in sorted(MODEL_ALIASES):
            print(f"  {name}", file=sys.stderr)
        return 1
    if not args.input.exists():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 1

    args.model = model
    args.port = args.port or cfg.port

    files = list(iter_images(args.input))
    if not files:
        print(f"No supported images found in {args.input}")
        return 1

    args.output.mkdir(parents=True, exist_ok=True)
    print(f"Model: {model} | processing {len(files)} image(s)")

    if daemon_alive(args.port):
        return process_via_daemon(files, args, args.port)
    elif spawn_daemon(args.port, model, config_path=cfg.config_path):
        print(f"Daemon spawned on port {args.port}")
        return process_via_daemon(files, args, args.port)
    else:
        print("No daemon available, processing locally")
        from standalone.rmbg_core import remove_bg

        return process_local(files, args, remove_bg)


def cmd_serve(args) -> int:
    from standalone.rmbg_web import serve

    serve(
        host=args.host,
        port=args.port,
        preload_model=args.preload_model,
        no_preload=args.no_preload,
        managed=False,
        idle_kill_min=args.idle_kill_min,
        idle_unload_min=args.idle_unload_min,
        config_path=args.config,
    )
    return 0


def cmd_list(args) -> int:
    for name in sorted(MODEL_ALIASES):
        print(name)
    return 0


def cmd_completion(args) -> int:
    name = "_rmbg" if args.shell == "zsh" else "rmbg.bash"
    p = REPO_ROOT / "completion" / name
    if not p.is_file():
        print(f"error: completion file not found: {p}", file=sys.stderr)
        return 1
    sys.stdout.write(p.read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-l" in argv or "--list-models" in argv:
        # 顶层 -l/--list-models 兼容别名 -> list
        argv = ["list"] + [a for a in argv if a not in ("-l", "--list-models")]
    elif not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "run")
    args = build_parser().parse_args(argv)
    if args.command == "completion":
        return cmd_completion(args)
    cfg = _load_config(args.config)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "serve":
        return cmd_serve(args)
    return cmd_run(args, cfg)


if __name__ == "__main__":
    sys.exit(main())

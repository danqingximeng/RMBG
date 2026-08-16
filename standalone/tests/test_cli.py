"""CLI 子命令化：默认 run、-l 兼容、list/completion、config 优先级。

cmd_run 的 daemon/local 路径全部打桩，不加载 torch。
"""

from pathlib import Path

import pytest

from standalone import rmbg_cli
from standalone.rmbg_config import Config


@pytest.fixture
def run_stub(monkeypatch):
    seen = {}

    def fake_run(args, cfg):
        seen["args"] = args
        seen["cfg"] = cfg
        return 0

    monkeypatch.setattr(rmbg_cli, "cmd_run", fake_run)
    return seen


def test_default_run_without_subcommand(run_stub):
    assert rmbg_cli.main(["img.png", "-o", "out"]) == 0
    assert run_stub["args"].command == "run"
    assert run_stub["args"].input == Path("img.png")
    assert run_stub["args"].output == Path("out")


def test_flags_before_input_still_run(run_stub):
    assert rmbg_cli.main(["-o", "out", "img.png"]) == 0
    assert run_stub["args"].command == "run"


def test_explicit_run(run_stub):
    rc = rmbg_cli.main(["run", "img.png", "-o", "out", "-m", "biref-lite"])
    assert rc == 0
    assert run_stub["args"].model == "biref-lite"


def test_run_model_from_config(run_stub, tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("model: biref-lite\n")
    assert rmbg_cli.main(["img.png", "-o", "out", "-c", str(f)]) == 0
    assert run_stub["cfg"].model == "biref-lite"


def test_serve_forwards_to_web(monkeypatch):
    import standalone.rmbg_web as web

    seen = {}
    monkeypatch.setattr(web, "serve", lambda **kw: seen.update(kw))
    rc = rmbg_cli.main(
        ["serve", "--host", "0.0.0.0", "--port", "9001", "--no-preload"]
    )
    assert rc == 0
    assert seen["host"] == "0.0.0.0"
    assert seen["port"] == 9001
    assert seen["no_preload"] is True
    assert seen["managed"] is False


def test_list(capsys):
    assert rmbg_cli.main(["list"]) == 0
    out = capsys.readouterr().out.split()
    assert out == sorted(out)
    assert "inspyrenet" in out


def test_list_compat_dash_l(capsys):
    assert rmbg_cli.main(["-l"]) == 0
    assert "inspyrenet" in capsys.readouterr().out


def test_list_names(capsys):
    assert rmbg_cli.main(["list", "--names"]) == 0
    assert "biref-lite" in capsys.readouterr().out


def test_completion_zsh(capsys):
    assert rmbg_cli.main(["completion", "zsh"]) == 0
    assert "#compdef rmbg" in capsys.readouterr().out


def test_completion_bash(capsys):
    assert rmbg_cli.main(["completion", "bash"]) == 0
    assert "complete -F _rmbg rmbg" in capsys.readouterr().out


def test_cmd_run_unknown_model(capsys):
    args = rmbg_cli.build_parser().parse_args(["run", "x.png", "-o", "out"])
    assert rmbg_cli.cmd_run(args, Config(model="no-such")) == 1
    assert "no-such" in capsys.readouterr().err


def test_cmd_run_input_not_found(tmp_path, capsys):
    args = rmbg_cli.build_parser().parse_args(
        ["run", str(tmp_path / "none.png"), "-o", "out"]
    )
    assert rmbg_cli.cmd_run(args, Config()) == 1
    assert "not found" in capsys.readouterr().err


def test_cmd_run_resolves_config_model_and_port(tmp_path, monkeypatch, capsys):
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    args = rmbg_cli.build_parser().parse_args(
        ["run", str(img), "-o", str(tmp_path / "out")]
    )
    seen = {}

    def fake_alive(port):
        seen["port"] = port
        return True

    monkeypatch.setattr(rmbg_cli, "daemon_alive", fake_alive)

    def fake_via_daemon(files, args, port):
        seen["model"] = args.model
        seen["files"] = [f.name for f in files]

    monkeypatch.setattr(rmbg_cli, "process_via_daemon", fake_via_daemon)

    assert rmbg_cli.cmd_run(args, Config(model="biref-lite", port=9123)) == 0
    assert seen["port"] == 9123
    assert seen["model"] == "biref-lite"
    assert seen["files"] == ["a.png"]
    assert (tmp_path / "out").is_dir()

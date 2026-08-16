"""API 层测试：models/health 形状、OpenAI 错误对象、data-URI 输入、成功信封。

remove_bg 用 monkeypatch 替换，全程不加载 torch / 模型节点。
"""

import base64
import io

import pytest
import standalone.rmbg_web as web
from fastapi.testclient import TestClient
from PIL import Image
from standalone.model_names import DEFAULT_MODEL, MODEL_ALIASES


@pytest.fixture
def client(monkeypatch):
    def fake_remove_bg(
        image,
        model,
        process_res=1024,
        sensitivity=1.0,
        mask_blur=0,
        mask_offset=0,
        refine_foreground=False,
    ):
        # 回传调用参数供断言；返回固定 8x8 RGBA
        fake_remove_bg.calls.append(
            dict(
                model=model,
                process_res=process_res,
                sensitivity=sensitivity,
                mask_blur=mask_blur,
                mask_offset=mask_offset,
                refine_foreground=refine_foreground,
            )
        )
        return Image.new("RGBA", (8, 8), (255, 0, 0, 128)), 0.25

    fake_remove_bg.calls = []
    monkeypatch.setattr(web, "remove_bg", fake_remove_bg)
    return TestClient(web.app), fake_remove_bg


def png_bytes(size=(8, 8)):
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, format="PNG")
    return buf.getvalue()


def test_model_names():
    assert DEFAULT_MODEL in MODEL_ALIASES
    assert len(MODEL_ALIASES) >= 16
    # 原始节点名也合法（_resolve 透传）
    valid = web._valid_models()
    assert "RMBG-2.0" in valid and "inspyrenet" in valid


def test_health(client):
    c, _ = client
    body = c.get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "rmbg-daemon"  # CLI daemon_alive 靠它识别


def test_models_shape(client):
    c, _ = client
    body = c.get("/api/models").json()
    ids = [m["id"] for m in body["data"]]
    assert ids == sorted(ids)
    assert body["default"] == DEFAULT_MODEL
    marked = [m["id"] for m in body["data"] if m["default"]]
    assert marked == [DEFAULT_MODEL]


def test_error_missing_image(client):
    c, _ = client
    r = c.post("/api/rmbg", json={})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_error_image_not_found(client):
    c, _ = client
    r = c.post("/api/rmbg", json={"image": "/nonexistent/x.png"})
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "not_found_error"


def test_error_unknown_model(client):
    c, _ = client
    r = c.post(
        "/api/rmbg",
        json={
            "image": "data:image/png;base64," + base64.b64encode(png_bytes()).decode(),
            "model": "no-such-model",
        },
    )
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "no-such-model" in msg and "inspyrenet" in msg


def test_error_bad_data_uri(client):
    c, _ = client
    r = c.post("/api/rmbg", json={"image": "data:image/png;base64,!!!"})
    assert r.status_code == 400
    assert "data-URI" in r.json()["error"]["message"]


def test_error_multipart_no_file(client):
    c, _ = client
    r = c.post("/api/rmbg", files={})
    assert r.status_code == 400


def test_success_multipart(client):
    c, fake = client
    r = c.post(
        "/api/rmbg",
        files=[
            ("file", ("a.png", png_bytes(), "image/png")),
            ("file", ("b.jpg", png_bytes(), "image/png")),
        ],
        data={
            "model": "inspyrenet",
            "process_res": "512",
            "sensitivity": "0.5",
            "refine": "true",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"created", "model", "data", "usage"}
    assert body["model"] == "inspyrenet"
    assert body["usage"] == {"elapsed_ms": 500}  # 2 张 x 0.25s
    assert len(body["data"]) == 2
    item = body["data"][0]
    assert item["filename"] == "a.png"
    assert item["format"] == "png"
    assert (item["width"], item["height"]) == (8, 8)
    img = Image.open(io.BytesIO(base64.b64decode(item["b64_json"])))
    assert img.format == "PNG" and img.mode == "RGBA"
    # 表单字符串被正确解析成类型
    call = fake.calls[0]
    assert call["process_res"] == 512 and call["sensitivity"] == 0.5
    assert call["refine_foreground"] is True


def test_success_json_data_uri(client):
    c, fake = client
    payload = base64.b64encode(png_bytes()).decode()
    r = c.post(
        "/api/rmbg",
        json={
            "image": f"data:image/png;base64,{payload}",
            "model": "biref-lite",
            "refine": True,  # JSON 里可以直接给 bool
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "biref-lite"
    assert len(body["data"]) == 1
    assert base64.b64decode(body["data"][0]["b64_json"])
    assert fake.calls[0]["refine_foreground"] is True
    assert fake.calls[0]["model"] == "biref-lite"


def test_error_undecodable_image(client):
    c, _ = client
    r = c.post("/api/rmbg", files={"file": ("a.png", b"not-an-image", "image/png")})
    assert r.status_code == 400
    assert "decode" in r.json()["error"]["message"]


def test_api_config_fallback(client, monkeypatch, tmp_path):
    """未经过 serve()（TestClient 直接挂 app）时回落 Config.load()。"""
    from standalone import rmbg_config

    c, _ = client
    missing = tmp_path / "none.yaml"
    monkeypatch.setattr(rmbg_config, "DEFAULT_CONFIG_FILE", missing)
    body = c.get("/api/config").json()
    assert body["model"] is None
    assert body["host"] == "127.0.0.1"
    assert body["port"] == 8123
    assert body["preload"] is True
    assert body["config_path"] == str(missing)


def test_api_config_served_state(client, monkeypatch):
    c, _ = client
    served = {"model": "biref-lite", "port": 9000}
    monkeypatch.setitem(web._state, "config", served)
    assert c.get("/api/config").json() == served


def test_models_default_from_served_state(client, monkeypatch):
    c, _ = client
    monkeypatch.setitem(web._state, "model_default", "biref-lite")
    body = c.get("/api/models").json()
    assert body["default"] == "biref-lite"
    assert [m["id"] for m in body["data"] if m["default"]] == ["biref-lite"]


def test_rmbg_uses_served_default_model(client, monkeypatch):
    c, fake = client
    monkeypatch.setitem(web._state, "model_default", "biref-lite")
    payload = base64.b64encode(png_bytes()).decode()
    r = c.post("/api/rmbg", json={"image": f"data:image/png;base64,{payload}"})
    assert r.status_code == 200
    assert fake.calls[0]["model"] == "biref-lite"

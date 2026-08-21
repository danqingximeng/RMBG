# standalone/ — 独立 CLI 与 WebUI（无 ComfyUI）

本目录把 `py/` 下的 RMBG / BiRefNet 节点魔改成可独立运行的去背景工具，纯 CPU，供 SillyTavern 角色表情图批量透明化。守护/WebUI 与 upscayl-py（`~/projects/upscayl-py`）保持同构约定：OpenAI 信封、config 文件、CLI 子命令、completion。

## 常用命令

```bash
# 1. 建环境（standalone/requirements.txt 已含 CPU 版 torch，文件内声明了
#    PyTorch CPU 源；torch/torchvision 必须成对，镜像里没有 +cpu 构建）
uv venv .venv
uv pip install --python .venv/bin/python -r standalone/requirements.txt
uv pip install --python .venv/bin/python pytest httpx   # 跑测试用（TestClient 需 httpx）

# 2. 测试（37 项全过；remove_bg 全程 monkeypatch，不加载 torch）
.venv/bin/python -m pytest standalone/tests -q
uv run python -m pytest standalone/tests -q  # agent 沙箱里直接调 .venv/bin/python 可能失败，用这个

# 3. CLI（默认 run；模型用别名，`rmbg list` 查看，-m 指定）
./rmbg <图片或目录> -o <输出目录> -m inspyrenet   # 仓库根 bash 包装脚本，任何 cwd 都能用
.venv/bin/python -m standalone.rmbg_cli <图片或目录> -o <输出目录>   # 等价上一行
./rmbg list                                        # 模型别名（顶层 -l 是兼容别名）
./rmbg completion zsh|bash                         # 打印补全脚本（completion/ 目录）
alias rmbg='/path/to/本仓库/rmbg'                  # 可选：加进 shell 配置

# 4. WebUI / 常驻守护（默认端口 8123）
./rmbg serve                                                  # 默认预载，http://127.0.0.1:8123
./rmbg serve --preload-model biref-lite --idle-unload-min 5   # 指定预载模型 + 闲置卸载
./rmbg serve --no-preload                                     # 不预载（首次请求才加载）
.venv/bin/python -m standalone.rmbg_web --managed --idle-kill-min 5  # 仅 CLI spawn 用，勿手动跑
```

CLI run 会自动复用/拉起守护：端口有 daemon 就 HTTP 转发（输出标 `(daemon)`），没有就 spawn managed 守护（闲置自杀），拉起失败回退本地处理（标 `(local)`）。批量时单张失败（daemon 4xx/5xx、网络错误、坏图、推理异常）打印报错并跳过继续，结束汇总 `Done. {ok}/{n}...`，有失败退出码 1。

## 配置文件（`~/.config/rmbg/config.yaml`，全可选，不主动创建）

优先级 **CLI 参数 > config > 内建默认**；`-c/--config` 可覆盖路径。加载在 `standalone/rmbg_config.py`（`Config.load()`，风格与 upscayl-py 的 `upscayl/config.py` 一致）：

```yaml
default_model: biref-toon # 默认模型：run 的 -m 缺省、serve 的预载模型、/api/models 的 default 三处联动
allowed_models: [inspyrenet, rmbg2, ben2, biref-toon, lucida] # 白名单：仅允许的模型（缺省全部允许），白名单前置校验，越权请求 400 且不触发下载
host: 127.0.0.1 # serve 绑定地址
port: 8123 # serve 端口，也是 run 的 daemon 端口
idle_unload_min: 5 # 手动守护闲置 N 分钟后卸载权重（0=永不）
idle_kill_min: 5 # 托管守护闲置 N 分钟后自杀（0=永不）
preload: true # serve 启动时预载
```

## 守护生命周期

`_state` 里的 `managed` 标志 + `_idle_thread` 每 5s 轮询：

- `serve`（manual）：闲置超 `--idle-unload-min` **只卸载模型权重**（`unload_all()`），进程永不自杀。
- CLI 自动拉起（`--managed`）：闲置超 `--idle-kill-min` 直接 `os._exit(0)` 自杀。
- **晋升**：先 CLI 拉起、后运行 `serve` 时，serve `_probe(port)` 发现已存在的 managed 守护，`POST /api/managed {"managed": false}` 实时翻转为 manual——此后只卸载不自杀（幂等；对已 manual 的守护打印 "nothing to start" 退出 0）。
- CLI 复用：`daemon_alive` 只做健康检查，绝不杀任何已运行进程。
- 预载在 serve 探测**之后**（避免探测到已有守护时白预载）。`--preload-model` 缺省 = config `model`；显式给 `--preload-model` 时即使 config `preload: false` 也预载；与 `--no-preload` 同给时后者生效。
- 探测到非 RMBG 服务占用端口 → 友好报错 exit 1（不走 uvicorn 裸 bind 失败）。
- busy/活动时间的复位只在 handler 的 `finally` 一处；预载完成后也重置计时，防 `/health` 探测/长请求误触发卸载。
- `_idle_thread` 整体 try/except：`unload_all()` 异常只打印不退出循环；卸载经 `any_model_loaded()` 门控，空模型闲置周期不刷日志。

## 模型别名（`-m` 传入，`rmbg list` 列出）

| 别名                                                                                  | 原模型名                                                       |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `rmbg2`                                                                               | RMBG-2.0                                                       |
| `inspyrenet`                                                                          | INSPYRENET                                                     |
| `ben` / `ben2`                                                                        | BEN / BEN2                                                     |
| `birefnet`                                                                            | BiRefNet-general                                               |
| `biref-512` / `biref-hr` / `biref-portrait` / `biref-matting` / `biref-hr-matting`    | BiRefNet_512x512 / -HR / -portrait / -matting / -HR-matting    |
| `biref-lite` / `biref-lite2k` / `biref-dynamic` / `biref-lite-matting` / `biref-toon` | BiRefNet_lite / _lite-2K / _dynamic / _lite-matting / _toonout |
| `lucida`                                                                              | Lucida                                                         |

别名表在 `standalone/model_names.py`（**零依赖**，`list`/`--help` 不加载 torch）；**新增模型必须在此登记**，原模型名经 `remove_bg` 的 fallback 仍可用。`DEFAULT_MODEL = "inspyrenet"` 是 CLI 与 WebUI/守护的统一默认模型（唯一定义点，勿在别处硬编码）。`rmbg_core.available_models()` 返回别名列表（CLI 与 WebUI 共用）。

## 文件职责

- `folder_paths.py` — ComfyUI `folder_paths` 垫片：`models_dir`（指向仓库根 `models/`）+ 空操作 `add_model_folder_path`。修改节点用到新的 `folder_paths` 属性时需同步补垫片。
- `model_names.py` — 模型别名表，零依赖，`list`/`--help` 快速输出用。
- `rmbg_config.py` — `Config.load()` / `ConfigError` / `to_dict()`，依赖仅 pyyaml + model_names（轻量，CLI 顶层可安全 import）。
- `rmbg_core.py` — 核心。按文件路径加载 `py/AILab_RMBG.py`、`py/AILab_BiRefNet.py`（**不能 import 仓库根 `__init__.py`**——它会扫描加载全部节点含 SAM2/SAM3 等未装依赖）。对外 API：`remove_bg(pil_image, model, **params) -> (rgba_pil, elapsed_seconds)`、`available_models()`、`warmup(alias)`、`unload_all()`。内部 `_lock` 串行化推理；模型实例是**进程级单例**（勿每请求新建，否则每请求重载权重）；**单模型驻留**——加载任一模型前先卸其他 loader 的权重（节点内部 RMBG 4 个 + BiRefNet 1 个 loader 互不知晓，不这样做跨 loader 切模型会累积驻留），`unload*` 后调 glibc `malloc_trim` 把已释放但扣留在 arena 的权重页归还 OS；节点模块按模型家族懒加载（inspyrenet/rmbg2/ben/ben2 → 只加载 AILab_RMBG，biref-* → 只加载 AILab_BiRefNet）；模块顶部有 warnings 过滤（flet/torch.meshgrid/timm）。
- `rmbg_cli.py` — 子命令化 CLI（镜像 upscayl 的 cli.py 结构）：`run`（默认，首参不是子命令时自动插入）/ `serve`（解析后调 `rmbg_web.serve()`，自身不实现 daemon）/ `list` / `completion`；顶层 `-l`/`--list-models` 兼容别名在 main() 里改写成 list。run 三条路径：`daemon_alive(port)`（校验 `/health` 返回 `service=="rmbg-daemon"`）→ `spawn_daemon`（60s 内轮询存活；`-c` 会转发给守护）→ `process_via_daemon`（**JSON 转发**：`POST /api/rmbg` + `{"image": "data:image/png;base64,..."}`，不手拼 multipart；解析 `data[0].b64_json` 解码落盘）→ 全失败 `process_local`。`-m` 缺省 = config `model` 或 `DEFAULT_MODEL`；`--port` 缺省 = config `port`。`rmbg_web`/`rmbg_core` 懒导入，`list`/`--help` 不加载 torch、秒出。
- `rmbg_web.py` — FastAPI 守护/WebUI，见 HTTP API 一节。启动逻辑 `serve(host, port, preload_model, no_preload, managed, idle_kill_min, idle_unload_min, config_path)`：**None 的参数按 CLI > config > 内建默认解析**（`main()` 的 argparse 壳和 `rmbg_cli serve` 都调它；spawn 依赖的 `python -m standalone.rmbg_web --managed` 仍可直接跑）。
- `web/` — 单图 WebUI，见 WebUI 一节。
- `completion/`（仓库根，非本目录）— `_rmbg`（zsh）+ `rmbg.bash`。**只补模型名**：`-m/--model/--preload-model` 后出 `rmbg list` 别名，其余一律兜底原生文件补全（zsh 显式 `_files`，bash `complete -o default`）——不补子命令/选项。zsh 补全经 `~/.oh-my-zsh/custom/completions/_rmbg` 符号链接安装（改仓库文件即同步；装后需新开终端）。**坑**：注册 compdef 后若无显式兜底，原生文件补全会被接管且不自动回退。
- `tests/` — pytest，跑法见常用命令。**conftest.py 里的 sys.modules 假包补丁别删**：仓库根 ComfyUI `__init__.py` 的 `load_nodes()` 会 rglob 执行全仓库 .py（含 `.venv`），假包顶掉 pytest 的 Package 收集，否则可能直接 SystemExit。
- `requirements.txt` — 精简直依赖：CPU 版 torch/torchvision（钉死成对版本，文件内声明 PyTorch CPU extra index）+ fastapi/uvicorn/python-multipart。装完可再装上游 `requirements.txt`，顺序不限——本文件会把 torch 钉回 CPU 版。

## HTTP API（rmbg_web.py）

字段与 upscayl-py 同构（OpenAI 风格信封，snake_case）：

```
GET  /health        -> {"status":"ok","service":"rmbg-daemon",managed,idle_kill_min,idle_unload_min,busy,model_loaded}
                       （CLI 靠 service 字段识别守护）
GET  /api/config    -> 生效配置（serve() 已跑时取 _state["config"]（含 CLI 合并结果）；未 serve 的 TestClient 场景回落 Config.load()）
GET  /api/models    -> {"data":[{"id":别名,"default":bool}],"default":...}
                       （default 取 _state["model_default"] = config model 或 DEFAULT_MODEL；前端设默认模型勿用 data[0]——排序后是 ben）
POST /api/managed   -> {"managed": bool}，运行时晋升/降级（serve 晋升用它）
POST /api/rmbg      -> 去背景，见下
```

`POST /api/rmbg`：

- 输入两种：multipart（字段名 `file`，可多个）或 JSON（`{"image": data-URI | 服务器本地路径 | URL}`，选项字段同名）
- 成功：`{"created","model","data":[{"filename","b64_json","format":"png","width","height","size"}],"usage":{"elapsed_ms"}}`（多文件即 `data` 多项，无 zip）
- 失败：`{"error":{"message","type":"invalid_request_error|not_found_error|server_error"}}` + 400/404/500
- 未知模型在进 torch 前就 400（`_valid_models()` 校验别名+原名，缺省模型也走 `_state["model_default"]`）
- 解析/推理整体在 `run_in_threadpool` 里跑（推理 30s+，不能阻塞事件循环卡死 `/health`）

静态服务与 upscayl 同构：`app.mount("/", StaticFiles(directory=WEB_DIR, html=True))` 放在**所有 API 路由之后**（挂根目录，`/index.html` 直访也 200，无 `/static` 前缀）。

## WebUI（`web/`）

- **单图**（多图用 CLI）；`index.html` + `style.css` + `app.js` + `icon.svg`，与 upscayl 的 `web/` 同构拆分。
- 布局对齐 upscayl：`.panel` 是 `<main>`（`display:flex`）里的流内 flex 列——桌面(≥901px) 宽 300px、右缘 border、无 transform，页头永远完整可见；手机(≤900px) 退化成 fixed 覆盖式抽屉（`body.open` + `translateX` + 遮罩，☰/✕/遮罩关闭、左缘右滑呼出，`matchMedia` 断点切换，不记 localStorage），桌面端 ☰/✕/遮罩 `display:none`。
- 对比**只有滑块视图**（无原图/结果切换按钮）：beforeImg/afterImg 绝对定位叠加 + `#divider`（2px 蓝线带圆点把手，拖动时把手放大）；after `clip-path: inset(0 0 0 X%)`、before `inset(0 (100-X)% 0 0)`，**左原图右结果、两图都裁**（只裁 after 的话结果图的透明区会透出下层未裁原图，看起来像没去背景）；无结果时 before 不裁、全幅可见，afterImg/divider 隐藏。Pointer Events + `setPointerCapture`，`#cmp{touch-action:none}`；`fitCmp()` 按图片宽高比缩进 viewer（48px 边距，resize 重算）。
- RMBG 特有：`#cmp` 底纹是 `repeating-conic-gradient` 棋盘格，透明区直接可见。
- 拖拽/点击共用 `pickFile` 存 `fileObj`（拖拽时 `<input type=file>` 的 `.files` 为空，勿用它）。
- 计时显示在 viewer 底部 bar（`#time`，读响应 `usage.elapsed_ms` + 宽高 + 模型）。
- CSS 用与 upscayl 同名的 `:root` 变量（--bg/--panel/--accent…）；交互基线同 upscayl：`:hover`/`:active` 反馈、`:focus-visible` 描边、range `accent-color`、空态内联 SVG、字阶 12/13/14/16。
- `icon.svg`：圆角深底 + 灰阶棋盘格 + 蓝色人物剪影（#3b82f6），favicon 与页头 logo 共用（根路径 `/icon.svg`）。

## 关键约束与坑

- **代理**：首次跑会从 HF 下载模型，需 `export http_proxy=http://localhost:2082 https_proxy=http://localhost:2082`。模型统一落地 `models/RMBG/`（含 INSPYRENET 的 `inspyrenet.pth`；transparent-background 的 config.yaml 也经 `TRANSPARENT_BACKGROUND_FILE_PATH` 重定向到 `models/RMBG/.transparent-background/`，不写 `~/.`）。
- **CPU 补丁别撤**：`py/AILab_BiRefNet.py` 两处 `.half()`（模型和输入）已条件化为仅 CUDA 生效（fp16 在此 CPU 更慢，且模型 fp32 + 输入 fp16 直接类型报错）。
- **hasattr 防护别撤**：`py/AILab_RMBG.py` `clear_model()` 的 `.cpu()` 有 `hasattr` 防护——INSPYRENET 的 `self.model` 是 `transparent_background.Remover()`（无 `.cpu()`），撤掉会让闲置卸载 `AttributeError` 崩掉 `_idle_thread`。上游也有此坑，日后可提 PR。
- **启动期低 CPU 是正常的**：启动到推理前约 15s 为导入 + 模型加载（351MB ckpt 反序列化；`Remover(ckpt=...)` 显式传权重路径，跳过库自身的 md5 校验/下载逻辑），单线程 30-40% 占用；之后才是 100% 推理。批量/常驻进程只付一次启动成本。
- **调节点 `process_image` 必须给全参数**：`invert_output`、`background`、`background_color`（节点内部 `params["..."]` 直取，不兜底，缺了 KeyError）。
- **INSPYRENET 的 process_res 无效**：transparent-background 库内部固定 1024 输入。
- 推理需要 `einops`（BiRefNet 模型文件 import），不在原 requirements.txt 里。
- 噪音清理：`AILab_RMBG.py` 的 `Remover()` 构造包了 `redirect_stdout`（消掉 "Settings ->" 打印）；`AILab_BiRefNet.py` 的下载不用废弃参数 `local_dir_use_symlinks`。
- **测守护进程时**：`pgrep/pkill -f` 会匹配执行命令的 shell 自身——用括号模式 `pgrep -f "[s]tandalone.rmbg_web"`；验证常驻进程优先用启动日志 `Started server process [PID]` 的真实 pid（wrapper 下 `$!` 是 shell 包装 pid）。
- **看守护实时日志**：`> log 2>&1` 下 python stdout 块缓冲（~8KB）不落盘，加 `PYTHONUNBUFFERED=1`；验证行为用 `/health` 的 `model_loaded` 更可靠。
- **serve 与预载竞态（未处理，真实路径不触发）**：serve 探测端口时若另一进程正处预载（未 bind），会被当空闲抢占端口，先占者 bind 失败退出。

## 性能参考（i5-8250U 8 线程，1600x708 图）

| 模型                        | 单张耗时 |
| --------------------------- | -------- |
| `inspyrenet` / `biref-lite` | ~30-32s  |
| `rmbg2`                     | ~52-58s  |
| `birefnet`                  | ~72s     |

CLI 三路径：daemon 复用 ~23s、含拉起 ~24s、本地回退 ~29s。内存：CPU 版 torch 导入基线 ≈ 294MB（CUDA 版构建 ≈ 500MB，且带 2.7GB nvidia 库）；`unload_all()` 含 `malloc_trim`，权重页基本即时归还 OS（实测大模型卸载直降 0.85-1.8GB）；切模型单模型驻留不累积。ben2 已实测加载（权重 ≈ 363MB），推理未实测，与 ben 走同一代码路径。

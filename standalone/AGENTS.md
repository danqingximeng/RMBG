# standalone/ — 独立 CLI 与 WebUI（无 ComfyUI）

本目录把 `py/` 下的 RMBG / BiRefNet 节点魔改成可独立运行的去背景工具，纯 CPU，供 SillyTavern 角色表情图批量透明化。

## 快速上手

```bash
# 1. 建 venv 并装依赖（CPU 版 torch 必须走专用索引）
uv venv .venv
uv pip install --python .venv/bin/python torch torchvision --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv/bin/python -r standalone/requirements-standalone.txt

# 2. CLI：单张或目录批量（模型用别名，-l 查看，-m 指定）
.venv/bin/python -m standalone.rmbg_cli <图片或目录> -o <输出目录> -m inspyrenet
./rmbg -l                # 仓库根的 bash 包装脚本，任何 cwd 都能用
alias rmbg='/path/to/本仓库/rmbg'   # 可选：加进 shell 配置

# 3. WebUI / 常驻守护（默认端口 8123）
.venv/bin/python -m standalone.rmbg_web                     # 手动常驻：默认预载 inspyrenet，http://127.0.0.1:8123
.venv/bin/python -m standalone.rmbg_web --preload-model biref-lite --idle-unload-min 5  # 预载 biref-lite，闲置 5 分钟卸载权重
.venv/bin/python -m standalone.rmbg_web --no-preload                                    # 不预载（首次请求才加载模型）
.venv/bin/python -m standalone.rmbg_web --managed --idle-kill-min 5              # CLI 拉起模式：闲置 5 分钟自杀
```

**serve 默认预载**（模型取 `--preload-model`，默认 `inspyrenet`）；`--no-preload` 关闭预载（两者同给时 `--no-preload` 生效）。CLI 自动拉起的守护固定传 `--preload-model <请求的模型>`。

**守护生命周期**（`_state` 里的 `managed` 标志 + `_idle_thread` 每 5s 轮询）：
- `serve`（manual）：闲置超 `--idle-unload-min` **只卸载模型权重**，进程永不自杀。
- CLI 自动拉起（`--managed`）：闲置超 `--idle-kill-min` 直接 `os._exit(0)` 自杀。
- **晋升**：先 CLI 拉起、后运行 `serve` 时，serve 会 `_probe(port)` 发现已存在的 managed 守护，`POST /api/managed {"managed": false}` 把它实时翻转为 manual——此后只卸载不自杀（幂等；对已 manual 的守护打印 "nothing to start" 并退出）。
- CLI 复用：`daemon_alive` 只做健康检查、绝不杀任何已运行进程。
- 预载在 serve 探测**之后**（避免探测到已有守护时白预载）。
- 探测到非 RMBG 服务占用端口 → 友好报错 exit 1（不走 uvicorn 裸 bind 失败）。

```bash
# CLI 会自动复用/拉起守护：端口有 daemon 就 HTTP 转发，没有就 --managed --preload-model 拉起（闲置 5 分钟自杀），拉起失败则本地处理
./rmbg 图 -o 输出 -m biref-lite

## 模型别名（`-m` 传入，`rmbg -l` 列出）

| 别名 | 原模型名 |
|---|---|
| `rmbg2` | RMBG-2.0 |
| `inspyrenet` | INSPYRENET |
| `ben` / `ben2` | BEN / BEN2 |
| `birefnet` | BiRefNet-general |
| `biref-512` / `biref-hr` / `biref-portrait` / `biref-matting` / `biref-hr-matting` | BiRefNet_512x512 / -HR / -portrait / -matting / -HR-matting |
| `biref-lite` / `biref-lite2k` / `biref-dynamic` / `biref-lite-matting` / `biref-toon` | BiRefNet_lite / _lite-2K / _dynamic / _lite-matting / _toonout |
| `lucida` | Lucida |

别名表在 `standalone/model_names.py`（**零依赖**，`-l`/`--help` 不加载 torch），新增模型须在此登记；原模型名经 `remove_bg` 的 fallback 仍可用。`DEFAULT_MODEL = "inspyrenet"` 是 CLI 与 WebUI/守护的统一默认模型（都在此定义，勿在别处硬编码）。`rmbg_core.available_models()` 返回别名列表（CLI 与 WebUI 共用）。

## 文件职责

- `folder_paths.py` — ComfyUI `folder_paths` 垫片。节点模块顶部 `import folder_paths` 且用 `folder_paths.models_dir`，垫片提供 `models_dir`（指向仓库根 `models/`）+ 空操作 `add_model_folder_path`。修改节点时若用到新的 `folder_paths` 属性需同步补垫片。
- `model_names.py` — 模型别名表，零依赖，仅用于 `-l`/`--help` 快速输出。
- `rmbg_core.py` — 核心。按文件路径加载 `py/AILab_RMBG.py`、`py/AILab_BiRefNet.py`（**不能 import 仓库根 `__init__.py`**，它扫描加载全部节点含 SAM2/SAM3 等未安装依赖）。对外 API：`remove_bg(pil_image, model, **params) -> (rgba_pil, elapsed_seconds)`、`available_models()`、`warmup(alias)`、`unload_all()`（清空两家族模型权重并释放内存）。内部有 `_lock` 串行化推理，模型实例是**进程级单例**（`_get_rmbg_node()`/`_get_birefnet_node()`，勿在每请求新建，否则每请求重载权重）。**节点模块按模型家族懒加载**（inspyrenet/rmbg2/ben/ben2 → 只加载 AILab_RMBG；biref-* → 只加载 AILab_BiRefNet），模块顶部已设 warnings 过滤（flet/torch.meshgrid/timm）。
- `rmbg_cli.py` — 批量 CLI，输出同名透明 PNG。三条处理路径：`daemon_alive(port)`（校验 `/health` 返回 `service=="rmbg-daemon"`）→ `spawn_daemon`（60s 内轮询存活）→ `process_via_daemon`（HTTP 转发，multipart 表单）；全失败则 `process_local`。输出行带 `(daemon)`/`(local)` 标记。`-m` 默认 `DEFAULT_MODEL`。`remove_bg` 在 `main()` 内懒导入，保证 `-l`/`--help` 秒出（实测 0.1s，此前 8.1s/2.5s）。
- `rmbg_web.py` + `web/index.html` — FastAPI 守护/WebUI。`GET /health` 返回 `{"service": "rmbg-daemon", managed, idle_kill_min, idle_unload_min, busy, model_loaded}`（CLI 靠 `service` 识别）；`GET /api/models` 列别名 + `default`（前端用它设默认模型，**勿用 `models[0]`**——排序后是 ben）；`POST /api/managed {"managed": bool}` 运行时晋升/降级（serve 晋升用它）；`POST /api/rmbg` 单文件返回 PNG、多文件返回 zip，响应头 `X-Elapsed-Seconds` 带总推理秒数（前端计时显示）；参数经表单字符串传入，`_parse_int/_parse_float` 兜底。`main()` 支持 `--host/--port`(默认 8123)/`--preload-model`(默认 `DEFAULT_MODEL`)/`--no-preload`/`--managed`/`--idle-kill-min`/`--idle-unload-min`（默认均 5 分钟）。**默认预载**（`--preload-model` 指定的模型），`--no-preload` 关闭。`managed`/`idle_*` 存进 `_state`，`_idle_thread()`（无参）每 5s 读 `_state` 实时判断：managed 超时 `os._exit(0)`，manual 超时调 `unload_all()` 并重置计时。handler 用 busy/`finally` 更新活动时间，预载完成后重置计时，避免 health 检查/长请求误触发卸载。serve 启动前 `_probe(port)`（TCP 探测 + /health）区分 已跑 rmbg（晋升或 no-op）/非 rmbg（报错 exit 1）/空闲（启动）。
- `web/index.html` — **单图** WebUI（多图去掉，用 CLI）。viewer 永远独占剩余全部空间（`object-fit:contain`），**参数面板是左抽屉**（全尺寸统一，`width:min(85vw,320px)`，`body.open` 类 + `translateX` 控制）：桌面(>900px)默认展开、折叠后 `main` 的 `margin-left` 过渡为 0（viewer 全宽，适合横向图）；手机(≤900px)默认收起、覆盖式弹出（带遮罩），viewer 保持全屏。头部 ☰ 开关 + 抽屉内 ✕ + 遮罩点击关闭。`matchMedia("(min-width:901px)")` 初始化默认态并在断点切换时重置（不记 localStorage）。**手机端左边缘右滑呼出侧边栏**（`touchstart` 起手 x<32px、`touchmove` dx>60 且横向为主时 `setOpen(true)`；**不实现右滑关闭**——侧边栏有滑块会冲突）。手机端点 run 自动收起抽屉。计时显示在 viewer 底部 bar（`#time`，读 `X-Elapsed-Seconds`）。拖拽/点击共用 `pickFile` 保存 `fileObj`（拖拽时 `<input type=file>` 的 `.files` 为空，勿用它）。
- `requirements-standalone.txt` — 裁剪后的依赖（不含 SAM/SDMatte/GroundingDINO 等重型包）。

## 关键约束与坑

- **代理**：首次跑会从 HF 下载模型，需 `export http_proxy=http://localhost:2082 https_proxy=http://localhost:2082`。模型落地 `models/RMBG/`，INSPYRENET 在 transparent-background 库自身缓存。
- **CPU 补丁**：`py/AILab_BiRefNet.py` 有两处 `.half()`（模型和输入）已条件化为仅 CUDA 生效。原因：fp16 在该 CPU 上更慢，且模型 fp32 + 输入 fp16 会直接类型报错。改这个文件时别把 `if device == "cuda"` 撤掉。
- **启动期低 CPU 是正常的**：进程启动到推理前约 15s 为导入 + 模型加载（torch 1.6s + transformers 1.2s + transparent_background 5.6s + `Remover()` 构造 6.4s，含 367MB ckpt 的全文件 md5 校验和反序列化），单线程工作所以只有 30-40% 占用；之后才是 100% 的推理。md5 校验通过则不会重复下载。批量目录 / WebUI 常驻进程只付一次启动成本。
- **缺少的参数会 KeyError**：调节点 `process_image` 必须给全 `invert_output`、`background`、`background_color`（节点内部用 `params["..."]` 直取，不兜底）。
- **INSPYRENET 的 process_res 无效**：transparent-background 库内部固定 1024 输入。
- 推理需要 `einops`（BiRefNet 模型文件 import），不在原 requirements.txt 里。
- 噪音清理：`AILab_RMBG.py` 的 `Remover()` 构造包了 `redirect_stdout`（消掉 "Settings ->" 打印）；`AILab_BiRefNet.py` 的下载已删废弃参数 `local_dir_use_symlinks`。

## 实测参考（i5-8250U 8 线程，1024px，1600x708 图）

| 模型 | 单张耗时 |
|---|---|
| `inspyrenet` / `biref-lite` | ~30-32s |
| `rmbg2` | ~52-58s |
| `birefnet` | ~72s |

已实测：inspyrenet、rmbg2、biref-lite、birefnet、CLI 批量、WebUI 单图（原图/结果切换、下载、计时）、守护生命周期（预载→闲置卸载→自动重载、managed 自杀、serve 晋升）、CLI 三路径（复用/拉起/回退）。ben/ben2/其余 BiRefNet 变体未实测，走同一代码路径。

## 守护生命周期实测（8123/8126/8127/8131-8134 端口，biref-lite）

- 预载模型 RSS ≈ 766MB；闲置超时 `unload_all()` 后降到 ≈ 490-500MB（大模型如 birefnet 回落到 486MB 更明显；小模型因 glibc 不归还内存 RSS 可能降幅小，但权重已释放可复用）。
- `[idle 30s] unloading model weights` 只打印一次（第二次闲置时模型已空，幂等跳过）。
- managed 自杀：`--idle-kill-min 0.2` 启动后 `[idle 15s] managed daemon exiting (idle-kill)`，~15s 进程自行退出（`os._exit(0)`）。
- **serve 晋升**：CLI 拉起 managed 守护 → `./rmbg serve --port X` 打印 "promoted to manual" 退出 0；`/health` 的 `managed` 从 true 翻 false；进程存活过原自杀阈值（证明晋升生效）；日志见 `POST /api/managed 200`。
- serve 各分支：已 manual → "nothing to start" 退出 0；非 RMBG 服务占端口 → 报错退出 1；空闲 → 正常启动。
- **pgrep/pkill 坑**：`pkill -f "standalone.rmbg_web"` 会匹配执行命令的 shell 自身导致挂起超时；`pgrep -f` 也会匹配 shell 包装进程（误报另一 pid）。务必用不自匹配的括号模式 `pgrep -f "[s]tandalone.rmbg_web"`。测试验证后台常驻进程时，**优先用启动时记下的真实 pid**（wrapper 下 `$!` 是 shell 包装 pid，`exec python` 后真实 python pid 不同，日志 "Started server process [PID]" 才是真实 pid）。
- **serve 与预载竞态**：`serve` 探测端口时若另一进程正处预载（未 bind），会被当空闲并抢占端口，先占者后 bind 失败退出。真实场景（CLI 拉起时 spawn_daemon 已等 daemon_alive；serve 在前则进程已就绪）不会触发，未做特殊处理。
- CLI 转发实测：CLI 24.1s（含拉起 ~5s），复用 23.1s，本地回退 28.6s（输出分别标记 daemon/daemon/local）。
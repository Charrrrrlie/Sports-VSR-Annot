# CLAUDE.md

本文件给 Claude Code（以及未来重新进入这个仓库的你自己）提供工程级别的上下文。用户面向的说明见 `README.md`。

## 项目本质

一个**最小化**的本地视频关键帧标注工具。明确的设计约束：

1. **简单优先**：组件越少越好。Flask + OpenCV + 单文件 HTML，没有 React/Vue/Webpack，没有数据库，没有 Celery，没有 Docker。
2. **本地为主**：单机起服务，最多内网少量人协作（≤5 人）。不为大并发/多租户做设计。
3. **物理文件即数据**：视频在 `videos/`，标注在 `annotations/<video>.json`，可读、可 grep、可 git。

## 文件职责（每个文件一句话）

| 文件                  | 职责                                                                 |
| --------------------- | -------------------------------------------------------------------- |
| `app.py`              | 单文件 Flask 后端。视频索引、按需解码、登录、所有 REST API。         |
| `static/index.html`   | 单页前端。UI、键盘绑定、隐式关键帧逻辑、200ms debounce 自动保存。    |
| `config.json`         | 运行时配置（密码、端口、secret_key、LRU 大小）。                     |
| `persons.json`        | 全局预定义 person ID 列表。                                          |
| `videos/`             | 用户拷视频进来；启动时扫描。                                         |
| `annotations/`        | 每个视频一个 JSON；后端自动创建该目录。                              |
| `requirements.txt`    | 仅 `flask` 和 `opencv-python`。**不要随意加依赖**。                  |

## 关键设计决策（与可能的反直觉之处）

### 关键帧是"隐式"的，不是状态

前端没有"标记为关键帧"按钮。**勾选任一 person ID → 自动 upsert 当前帧；取消全部 → 自动删除**。这是用户明确要求的简化，**改动时请保留这个语义**。涉及代码：

- `static/index.html` 中 `onPersonToggle()` 函数
- `app.py` 中 `api_annotations` POST 路由的过滤逻辑：`if not ids: continue`（空 ID 列表的条目会被丢弃，不写入磁盘）

### 关键帧的醒目视觉提示

为了避免误标注，是关键帧时同时有 3 处视觉变化（也是用户要求）：
1. 帧图像加 4px 金色边框（CSS `body.keyframe .frame-wrap`）
2. 图像左上角浮 "★ KEYFRAME" 角标（`#kf-overlay`）
3. 顶栏帧计数后追加红色 "★ KEYFRAME" 徽章（`#kf-badge`）
4. 右栏列表对应行加 `.current` 类高亮 + ▶ 标记

任何修改 UI 的改动都要确保这 3-4 处依然正常切换。

### 每个视频共享一个 VideoCapture + 锁

`VideoHandle` 类持有常驻 `cv2.VideoCapture` 和 `threading.Lock`。OpenCV 不是线程安全的，并发请求帧时必须串行 seek。短视频场景下 seek 很快，瓶颈不在这里。
**不要为了"并发"改成每请求 open/close**，那样反而更慢。

### LRU 缓存键是 `(video_name, frame_idx)`

`FrameLRU` 用 `OrderedDict + Lock` 手写实现。**不要换成 `functools.lru_cache`**，因为：
- 我们需要在 rescan 时清理特定视频的所有帧（虽然现在没实现，但容易加）
- 需要可配置 maxsize（来自 config.json）
- `functools.lru_cache` 对 bytes 值序列化也没意义

### 标注 POST 是"整体覆盖"

前端每次变更都 POST 完整的 `keyframes` 数组。**没有 diff/PATCH**。这让前后端逻辑都简单，代价是网络包略大。短视频标注规模下完全够用。

后端 `save_anno_atomic()` 用 `tmp + os.replace` 原子写，避免崩溃损坏 JSON。

### 端口不用 5000

macOS 的 AirPlay Receiver 占用 5000。`config.json` 里改成了非 5000 的端口避免开箱即报 "Address already in use"。

### Ctrl+C 用 `os._exit(0)` 强制退出

Werkzeug 内置开发服务器在 `threaded=True` 下，遇到浏览器留着 keep-alive 连接时，第一次 SIGINT 会被忽略——表现为 Ctrl+C 没反应、端口不释放、进程被孤儿化到 PPID=1。

修复：`__main__` 块顶部装了 `SIGINT/SIGTERM → os._exit(0)`。这跳过了线程清理但对本应用没影响（标注用 `os.replace` 原子写，没有需要 flush 的状态）。

**不要把这一段删掉**，否则 Ctrl+C 又会失效。

### 多级目录与 video name 即相对路径

`videos/` 下支持任意层级子目录。视频的"名字"即它相对 `VIDEO_DIR` 的 POSIX 路径（如 `alice/nested/deep.mp4`），同时也用作：
- `VIDEOS` 字典的 key
- URL 路径参数 `<path:name>`（Flask 接受含 `/` 的路径）
- `annotations/` 下镜像保存路径（`annotations/alice/nested/deep.mp4.json`）

**前端 URL 编码必须按段处理**：用 `encName()` 把每段单独 `encodeURIComponent` 再用 `/` 拼回，避免把分隔符也编码掉。直接 `encodeURIComponent(fullPath)` 是错的。

**`anno_path()` 必须做路径穿越防御**：用 `resolve()` 后检查结果在 `ANNO_DIR` 下。虽然 `name` 是白名单（只有 `VIDEOS` 里的 key），但加这一层防御无成本。

### 目录筛选是前端做的，后端不分页

`/api/videos` 返回全量列表（含 `dir`、`basename` 字段），前端按目录前缀过滤。`videos/` 规模就算几百个也没问题；如果未来真到几千个再考虑分页。

筛选规则是**前缀匹配**（选 `alice` 包含 `alice/nested/...`），不是精确匹配——多人协作时这通常是想要的行为。如果需要改成精确匹配，只改 `videosInDir()` 一个函数。

## 不要做的事

- ❌ 引入数据库（SQLite/Postgres）。当前 JSON 文件已满足需求。
- ❌ 引入前端框架（React/Vue/Svelte）。当前 HTML 一个文件够了。
- ❌ 引入构建工具（webpack/vite/npm）。零构建是核心特性。
- ❌ 为多用户、归属、权限做大改造。当前是"共享密码 + 协议层信任"。
- ❌ 加 SocketIO/WebSocket 做实时同步。冲突由"分配不同视频"在流程层解决。
- ❌ 把帧预抽到磁盘。按需解码 + LRU 已够快，且不污染文件系统。

## 安全注意

- 当前认证仅"共享密码 + Flask session"。**不要直接公网暴露**。
- `secret_key` 在 `config.json` 里默认值 `please-change-this-random-string`，部署前必须改。
- `<path:name>` 路由参数可能含 `..`。Flask 的 `send_from_directory` 已防穿越，但其他路由中我们用 `if name not in VIDEOS: abort(404)` 来约束（只允许扫描出的视频名）。新增涉及文件名的路由时务必沿用这个白名单检查。

## 常见修改指引

### 增加一个视频格式
改 `app.py` 顶部的 `VIDEO_EXTS` 集合。OpenCV 支持的就支持。

### 改变默认 person ID
直接改 `persons.json`；不要硬编码。

### 加一个键盘快捷键
在 `static/index.html` 的 `document.addEventListener("keydown", ...)` 里加 case。注意检查 `tag === "input"` 等元素聚焦时跳过。

### 增加导出（如 CSV）
推荐新增一个 `/api/export/<name>?format=csv` 路由，**不要**改变 JSON 主存储格式。

### 改用生产 WSGI 服务器
`pip install waitress`，把启动改成 `waitress-serve` 或在 `app.py` 末尾用 `from waitress import serve; serve(app, ...)`。但**不要默认依赖 waitress**，保持 stdlib + flask + opencv 的最小依赖。

## 验证 checklist（修改后）

- [ ] `python3 -c "import app"` 不报错
- [ ] 启动服务后浏览器能登录、看到视频列表
- [ ] 翻帧、Shift/Ctrl 组合键、Home/End、数字键、Esc 都正常
- [ ] 勾选 ID 后图像 + 顶栏 + 右栏 3 处高亮同时出现，取消勾选后全部消失
- [ ] `annotations/*.json` 文件按 frame 升序、updated_at 字段存在
- [ ] 越界帧请求返回 404，未登录 API 返回 401

## 历史决策日志

- **2026-05-27**：初版。Flask + 原生前端 + per-video JSON 存储。端口默认 5009 规避 macOS AirPlay。

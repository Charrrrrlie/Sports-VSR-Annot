# 视频关键帧标注工具

一个最小化的本地视频逐帧标注工具：用浏览器逐帧浏览短视频，勾选每个关键帧出现的人物 ID，结果保存为本地 JSON 文件。适用于团队内网/局域网协作（少量并发用户）。

## 特性

- **逐帧浏览**：±1 / ±5 / ±10 帧跳转，首尾帧，直跳指定帧号，键盘快捷键
- **隐式关键帧**：勾选任一 person ID 即把当前帧标为关键帧，全部取消则删除该标注
- **醒目高亮**：关键帧时图像金色边框 + ★KEYFRAME 角标 + 顶栏红色徽章，避免误操作
- **本地存储**：每个视频一个 JSON 文件，按帧号升序，原子写入
- **共享密码**：单一密码登录（Flask session）
- **按需解码**：OpenCV seek + 内存 LRU 缓存，无预处理开销
- **零前端框架**：单文件 HTML + 原生 JS

## 技术栈

- 后端：Python 3.9+ / Flask / opencv-python
- 前端：原生 HTML/CSS/JS（无构建步骤）
- 存储：本地 JSON 文件

## 目录结构

```
annotation/
├── app.py              # Flask 服务（单文件后端）
├── requirements.txt    # flask, opencv-python
├── config.json         # 密码、端口、缓存大小
├── persons.json        # 全局预定义 person ID 列表
├── videos/             # 输入：把视频拷进这里
├── annotations/        # 输出：每个视频一个 JSON
└── static/
    └── index.html      # 单页前端
```

## 安装

```bash
cd /path/to/annotation
pip3 install -r requirements.txt
```

依赖很少：`flask` + `opencv-python`（含 numpy）。

## 启动

```bash
python3 app.py
```

默认监听 `0.0.0.0:5009`。浏览器打开 **http://localhost:5009** 或 **http://<本机IP>:5009**，输入密码即可。

> macOS 用户：5000 端口被 AirPlay Receiver 占用，默认改为 5009。如需 5000 请先在 *系统设置 → 通用 → AirDrop 与接力 → AirPlay 接收器* 关闭。

## 使用流程

### 1. 准备视频

把 `.mp4` / `.mov` / `.avi` / `.mkv` / `.webm` 文件拷进 `videos/`，重启服务（或 `POST /api/rescan`）。

**支持多级目录**。例如：

```
videos/
├── alice/
│   ├── clip_a.mp4
│   └── nested/
│       └── deep.mp4
├── bob/
│   └── clip_b.mp4
└── shared.mp4
```

页面顶栏会出现"目录"下拉框，可按目录筛选视频，方便分工：每个人选择自己负责的目录即可（前缀匹配，选 `alice/` 会同时显示 `alice/clip_a.mp4` 和 `alice/nested/deep.mp4`）。选择会通过 localStorage 记住，下次打开自动恢复。

标注 JSON 也会按相同的目录结构镜像保存到 `annotations/alice/nested/deep.mp4.json`，便于按目录归档或单独打包发送。

### 2. 配置 Person ID

编辑 `persons.json`：

```json
{
  "person_ids": ["alice", "bob", "carol", "dave"]
}
```

刷新页面生效。最多支持任意多个 ID，但前 9 个可用数字键 1-9 快速切换。

### 3. 修改密码

编辑 `config.json`：

```json
{
  "password": "your-strong-password",
  "host": "0.0.0.0",
  "port": 5009,
  "lru_size": 256,
  "secret_key": "random-string-for-session-cookie"
}
```

- `password`：登录密码
- `host`：`0.0.0.0` 允许局域网访问，`127.0.0.1` 仅本机
- `port`：服务端口
- `lru_size`：帧缓存大小（单位：帧），256 帧约占 50-200MB 内存
- `secret_key`：Flask session 签名密钥，**生产环境请改成随机字符串**

### 4. 浏览器标注

打开页面，输入密码：

- **切换视频**：顶栏下拉框
- **翻帧**：左右方向键 ±1，Shift+方向 ±5，Ctrl/Cmd+方向 ±10，Home/End 首尾，"Jump" 输入框跳转
- **打点**：勾选当前帧出现的 person ID。**勾选任一即自动保存为关键帧**，取消全部勾选即删除该关键帧。数字键 1-9 快速切换前 9 个 ID。Esc 清空当前帧所有勾选。
- **跳到已标注帧**：右栏点击关键帧列表项
- **删除关键帧**：列表项右侧 × 按钮，或取消所有勾选

### 5. 标注结果

保存在 `annotations/<video_name>.json`：

```json
{
  "video": "clip01.mp4",
  "fps": 30.0,
  "frame_count": 500,
  "keyframes": [
    {
      "frame": 42,
      "person_ids": ["alice", "bob"],
      "updated_at": "2026-05-27T15:30:00"
    },
    {
      "frame": 87,
      "person_ids": ["bob"],
      "updated_at": "2026-05-27T15:31:12"
    }
  ]
}
```

按 `frame` 升序排列，原子写入（不会因中断写坏）。

## 键盘快捷键速查

| 操作         | 快捷键                                |
| ------------ | ------------------------------------- |
| 上一帧/下一帧 | `←` / `→`                             |
| ±5 帧        | `Shift+←` / `Shift+→`                 |
| ±10 帧       | `Ctrl+←/→`（Mac 上 `Cmd+←/→` 也可）   |
| 首帧/末帧    | `Home` / `End`                        |
| 跳到帧号 N   | 顶栏 "Jump" 输入框                    |
| 切换 ID 1-9  | 数字键 `1` - `9`                      |
| 清空当前勾选 | `Esc`                                 |

## 多人远程访问

### 局域网（最简）

服务绑定 `0.0.0.0` 后，团队成员浏览器输入 `http://<服务器局域网IP>:5009` 即可。所有人共享同一密码。

### 公网访问

当前设计只有共享密码 + 无 HTTPS，**不建议直接暴露公网**。如需远程协作，推荐其中之一：

- **Cloudflare Tunnel**：`cloudflared tunnel --url http://localhost:5009`，自带 HTTPS，无需公网 IP
- **Tailscale**：所有成员加入同一 Tailnet，只在私网可见，零信任最稳
- 路由器端口转发：仅在你**确实有公网 IPv4**、并且自行架设 HTTPS 反向代理时才考虑

## API 参考

所有 `/api/*` 需要登录态（session cookie）。

| 方法 | 路径                                | 说明                       |
| ---- | ----------------------------------- | -------------------------- |
| GET  | `/`                                 | 主页（未登录跳 `/login`）  |
| POST | `/login`                            | `password=xxx` 表单登录    |
| GET  | `/logout`                           | 清除 session               |
| GET  | `/api/videos`                       | 视频列表与元数据           |
| GET  | `/api/video/<name>/frame/<n>`       | 第 n 帧 JPEG               |
| GET  | `/api/persons`                      | 预定义 person ID 列表      |
| GET  | `/api/annotations/<name>`           | 读取标注                   |
| POST | `/api/annotations/<name>`           | 整体覆盖写入标注           |
| POST | `/api/rescan`                       | 重扫 `videos/` 目录        |

## 并发与限制

- Flask 内置 dev server 已开 `threaded=True`，**少量并发用户（≤5）够用**
- 每个视频共用一个 `cv2.VideoCapture` 对象 + 一把锁，并发请求帧时串行解码（短视频 seek 很快）
- **两人同时编辑同一视频**：后写覆盖先写，不做合并；多人请分配不同视频
- 想要更稳的生产部署，可改用：`pip install waitress && waitress-serve --host 0.0.0.0 --port 5009 app:app`

## 常见问题

**Q: 视频在 `videos/` 里但页面看不到？**
A: 重启服务，或 `curl -X POST -b cookies.txt http://localhost:5009/api/rescan`。注意扩展名必须是 `.mp4/.avi/.mov/.mkv/.webm`。

**Q: 帧号显示和 ffprobe 不一致？**
A: OpenCV 的 `CAP_PROP_FRAME_COUNT` 在部分编码（特别是变帧率 / 含 B 帧的 H.264）下可能略有偏差。短视频通常没问题，长视频建议先用 ffmpeg 转成 CFR mp4。

**Q: 切帧偶发卡顿？**
A: 第一次 seek 到某帧需要解码，相邻帧已自动预取；调大 `lru_size` 可缓存更多帧（代价是内存占用）。

**Q: 怎么清空所有标注重来？**
A: 删除 `annotations/<video>.json` 文件即可。

**Q: 想换密码但已经有人在用？**
A: 改 `config.json` 后重启，所有已登录会话失效，需重新登录。

## 后续可拓展（本仓库未实现）

- 多用户独立账号 + 标注归属
- 关键帧区间（起止帧 + 中间所有帧自动标记）
- 导出 CSV / COCO 格式
- 网页上传视频
- 锁定机制（同视频独占编辑）

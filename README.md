# read-video

> WorkBuddy skill：让 AI 读懂一个视频，但**不下载整片**。

给一个视频链接，它只取目标时刻附近的那一小段字节 —— 取某一秒的画面，或截一段音频转成文字。
整片 99% 的字节从头到尾不碰，磁盘上不会出现任何视频文件。

## 为什么这么做

朴素做法是把视频下载下来再处理：慢、占存储、有版权风险，而且往往只需要其中几秒。

这套东西用 HTTP Range + ffmpeg 输入侧 seek 只取必需的字节，实测基准
（249 MB / 9 分 56 秒的 MP4）：

| 动作 | 上游取字节 | 占整片 |
|---|---|---|
| 取第 30 秒一帧 | 2.0 MB | 0.839% |
| 取第 8 分钟一帧 | 1.7 MB | 0.707% |
| 取 480s 起 20 秒音频 | 6.6 MB | 2.784% |

占比随视频变长而下降 —— 视频越长，按需取的优势越大。

## 安装

```bash
git clone https://github.com/RaganaBloodedge/read-video-skill.git
cp -r read-video-skill ~/.workbuddy/skills/read-video
```

装依赖（Python 3.10+）：

```bash
pip install httpx imageio-ffmpeg sherpa-onnx numpy yt-dlp
```

`imageio-ffmpeg` 自带静态 ffmpeg，不需要系统安装。ASR 模型（约 240 MB）首次运行时
自动从 hf-mirror 下载到 `~/.workbuddy/cache/read-video/models`。

## 用法

### 一条命令拿到文字稿

```bash
python scripts/transcribe.py --url "<网页链接>" --seconds 90
```

支持 **1700+ 站点**（bilibili 走手写适配器，其余走 yt-dlp），直接给网页链接即可，
脚本自己解析出带签名的直链并带上 CDN 要求的请求头。

长视频分段取几段就够覆盖主线：

```bash
python scripts/transcribe.py --url "<链接>" --segments 0:90 320:90 630:97
```

### 定点取帧

```bash
python scripts/grab.py "<链接>" --times 14 29 44 58 73 --prefix shot --outdir ./out
python scripts/grab.py "<链接>" --time 183.5 --out shot.jpg      # 指定某一秒
```

### 先看能不能跳读

```bash
python scripts/resolve.py "<链接>"          # 网页链接 → 直链 + 标题/时长/建议抽帧点
python scripts/probe.py <直链> --json       # 探测 Range 支持与索引位置
```

## 三个设计要点

1. **`-ss` 必须在 `-i` 之前**。放后面是输出侧 seek，等于先顺序解到目标点再丢，退化成拉全片。
2. **字节计量默认开**。本地起一个流式计数代理，记录真正从上游转发了多少字节 ——
   这是"确实没下整片"的唯一硬证据，不是自说自话。
3. **转写会自我检查**。正常口播约 4~6 字/秒；脚本发现低于 1 字/秒会主动提示
   "这个视频可能没有口播"，避免把一句噪音当成视频内容。

## 目录

```
SKILL.md                     技能定义（触发条件、工作流、边界）
scripts/rvcore.py            共享库：流式计数代理、ffmpeg 封装、探测
scripts/resolve.py           网页链接 → 媒体直链（CLI）
scripts/resolvers.py         平台适配（bilibili 手写 + yt-dlp 通用）
scripts/probe.py             URL 可跳读性探测
scripts/grab.py              定点取帧 / 截取音频
scripts/transcribe.py        本地离线转写（SenseVoice）
references/methodology.md    原理、四个坑、实测数据
references/dsh-dual-form.md  与 dsh 插件形态的关系
spike/                       最初的可行性验证脚本（保留作证据）
```

## 已知边界

- **平台覆盖面 = yt-dlp 的覆盖面**，需要定期 `pip install -U yt-dlp`（站点改版会让旧版失效）。
- **需要登录 / Cookie 的站点会失败**。
- **ASR 同音字**：中文人名、地名、古词容易错（实测"阪泉之野"→"版圈之也"），
  引用专有名词前要按上下文校正。
- **HLS / DASH**：不是单文件，`probe.py` 的 box 解剖会失败（不阻塞取数）。

## 相关

dsh 插件形态（TypeScript，供 DeepSeek Harness 里的模型调用）：
[github.com/RaganaBloodedge/dsh-read-video](https://github.com/RaganaBloodedge/dsh-read-video)
— npm 包名 `dsh-readvideo`。

## License

MIT

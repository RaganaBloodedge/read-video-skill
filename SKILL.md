---
name: read-video
description: 读取远程视频内容而不下载整片。支持 1700+ 视频站点的网页链接（bilibili 走手写适配器，其余走 yt-dlp 通用后端，自动解析出带签名的直链与所需请求头）与视频直链；先探测 HTTP Range 支持与 MP4 索引位置，再用 ffmpeg 输入侧 seek 定点取帧或截取音频片段；音频用本地 SenseVoice 离线转写成文字，帧交给视觉模型识别。当用户给出视频链接、要求"看看这个视频""这个视频讲了什么""第几分钟画面是什么"、需要从视频提取某一时刻的视觉证据或语音文稿时使用；也可用于验证某个 URL 是否支持按需取字节。
agent_created: true
---

# read-video —— 按需取字节地"读"视频

## 这条技能解决什么问题

用户丢来一个视频链接，想理解内容。朴素做法是把视频下载下来再处理 —— 慢、占存储、
有版权风险，而且往往只需要其中几秒。本技能的做法是：**只取目标时刻附近的那一小段字节**。
音频轨在本地离线转成文字（回答"讲了什么"的主力），视频轨解出关键帧交给视觉模型补画面。

实测基准（249 MB / 9 分 56 秒的 MP4，非 faststart）：

| 动作 | 上游取字节 | 占整片 |
|---|---|---|
| 取第 30 秒一帧 | 2.0 MB | 0.839% |
| 取第 8 分钟一帧 | 1.7 MB | 0.707% |
| 取 480s 起 20 秒音频 | 6.6 MB | 2.784% |

## 何时用 / 何时不用

用：用户给了视频直链，或给了含视频的网页链接（bilibili 等平台由 `resolve.py` 自动解析，不必手写 API 调用）。
不用：本地文件（直接读）、需要逐秒完整理解的长视频（成本仍高，先问用户要时间点）。

## 硬原则

1. **不下载整片。** 任何一步都不要 `wget`/`curl -O` 整个文件。
2. **先探测再动手。** 不探测就 seek，会在不支持 Range 的服务器上白等一场。
3. **`-ss` 必须放在 `-i` 之前。** 放后面是输出侧 seek，等于先顺序解到目标点再丢，
   退化成拉全片。放在前面才是输入侧 seek，解复用器直接跳。
4. **计量开着。** `--measure` 默认开：它用本地流式代理数上游字节，是"真的按需取了"
   的唯一硬证据，也是向用户证明没偷偷下整片的依据。
5. **网页链接交给脚本解析。** 页面 URL ≠ 直链，且直链带签名会过期。
   用 `resolve.py`，或直接把链接传给 `grab.py`/`transcribe.py`（它们会自动解析并补上请求头）。
   别在会话里现写站方 API 调用 —— 那种代码每次都长得不一样，也没人 review 过。

## 工作流

### 第 0 步：分清输入 —— 网页链接还是直链

- **网页链接**（bilibili、YouTube、抖音、微博、Vimeo…共 1700+ 站点）→ 先解析成直链。
  也可以直接把链接交给 `grab.py` / `transcribe.py`，它们认得出并自动解析、自动带上所需请求头。
- **直链**（`.mp4` / `.m3u8` 等媒体扩展名）→ 可跳过解析。

判定规则在 `resolvers.is_page_url()`：带媒体扩展名的一律当直链，其余 http 链接都送去解析层。

### 第一步：拿到可取的流

```bash
python scripts/resolve.py "<网页链接>"
```

输出 `video_url`（取帧用）、`audio_url`（转写用，与前者**不是同一条**）、`headers`
（CDN 要求的请求头），外加标题、时长，以及一组**建议抽帧时间点**。

`平台` 那行会标出用的是哪个后端：`native`（手写适配器，目前是 bilibili）还是
`yt-dlp`（通用，1700+ 站点）。要强制指定用 `--backend native|yt-dlp|auto`。

**一条命令走完全程**（解析与取数自动衔接，推荐）：

```bash
python scripts/transcribe.py --url "<网页链接>" --seconds 90
python scripts/grab.py "<网页链接>" --times 14 29 44 58 73 --prefix shot --outdir ./out
```

### 第二步：探测（直链建议先做，网页链接可跳过）

```bash
python scripts/probe.py <直链> --json
```

若直链来自 CDN，要带来源头：`--header 'Referer: https://www.bilibili.com'`。

看三个字段：

- `seekable` —— `false` 就停，服务器忽略 Range，定点取帧会退化成顺序拉流。
  改走"边下边解"（`-i <url>` 不加 `-ss`，取流最前面的帧）或直接告诉用户做不到。
- `container.moov_offset` / `faststart` —— 索引在头部（faststart）时固定开销几十 KB；
  在尾部时要先取文件尾一段（实测约 441 KB）。这是"取一帧"的固定成本，省不掉。
- `content_length` —— 后面算"占整片百分比"的分母。

### 第三步：按用户意图取数

**用户给了具体时间点 → 定点取证：**

```bash
python scripts/grab.py <url> --time 183.5 --out shot.jpg
```

**用户没给时间点 → 先抽几帧了解大概：**

```bash
python scripts/grab.py <url> --times 5 60 180 420 --prefix shot --outdir /tmp/rv
```

选点技巧：按内容密度取 4~6 帧，别均匀铺满全片（那等于把整片解了一遍）。
拿不准就先取 3 帧，看完了再决定要不要细看某一段。

**要理解讲了什么 → 直接转成文字（推荐先做这一步）：**

```bash
python scripts/transcribe.py --url <url> --segments 0:90 320:90 630:97
```

一条命令完成「截音频 → 本地识别」，输出带时间戳的中文文本，**不需要外部 API、不需要联网推理**。
首次运行自动下载 SenseVoice 模型（约 240 MB，从 hf-mirror 拉，仅一次）。

长视频取 3~4 段各 90 秒即可覆盖主线，不必整片转 —— 这比逐帧看画面快得多，
也更适合回答"讲了什么"这类问题。**先转文字，再用帧补充视觉信息**，是这个顺序。

**转写完先看产率。** 正常口播约 4~6 字/秒；脚本输出里带 `chars_per_second`，
低于 1 字/秒就会主动提示。遇到这种（纯音乐、游戏实况、无声演示）**不要拿那点文字当内容**，
直接改走更密的抽帧（8~10 帧）做画面分析 —— 声音里没信息，答案只在画面里。

只需要音频文件本身时（比如送去别处转写）：

```bash
python scripts/grab.py <url> --start 0 --seconds 60 --audio clip.wav
```

有内嵌字幕轨时优先读字幕（`ffmpeg -i <url>` 看有没有 `subtitle` 流），比 ASR 更准更快。

### 第四步：把帧交给视觉模型

`grab.py` 只负责把像素拿到本地，不理解内容。把输出的 jpg 路径交给视觉模型提问
（"这一帧里有什么""这段在讲什么"）。多帧时按时间顺序一起给，让模型串成叙述。

## 输出怎么读

`grab.py` 输出 JSON，每个动作带一份字节账：

```json
{
  "ok": true,
  "actions": [{
    "kind": "frame", "time": 30,
    "out_bytes": 48211,
    "upstream_bytes": 2097152,
    "requests": 4,
    "wall_seconds": 2.317
  }],
  "total_upstream_bytes": 2097152
}
```

- `upstream_bytes / content_length` 就是"占整片多少"。超过 5% 就要停下来复查：
  多半是 `-ss` 位置写错了，或者服务器不认 Range。
- `requests` 多是正常的：ffmpeg 要分几次取（moov、关键帧到目标帧之间、音频头）。
- `ok: false` 时看 `err_tail`，常见是超时和 5XX —— 脚本已内置重试，仍失败就换时间点再试。

`transcribe.py` 的输出：

```json
{
  "ok": true,
  "text": "这是一片被大河与群山环抱的土地……",
  "segments": [{"start": 0, "end": 25, "text": "…"}],
  "audio_seconds": 276.7,
  "model": "SenseVoiceSmall-int8"
}
```

`text` 是所有分段拼成的整段；`segments` 带时间戳，需要"第 X 秒说了什么"时查它。

## 环境

- **解释器**：本机托管 venv 的 python。Windows 上路径形如
  `~/.workbuddy/binaries/python/envs/default/Scripts/python.exe`。
  注意是 **`Scripts/`**，不是 Unix 风格的 `bin/`（照文档写 `bin/python` 会报文件不存在）。
- **依赖**：`httpx`、`imageio-ffmpeg`（取数）、`sherpa-onnx`、`numpy`（转写）、
  `yt-dlp`（通用平台解析），都已装在同一个 venv 里；缺哪个脚本会明确报错。
  没装 yt-dlp 时 bilibili 仍可用（走手写适配器），但其它站点会解析失败。
- **ffmpeg**：由 `imageio-ffmpeg` 自带静态构建，无需系统安装。
  要指定别的用环境变量 `RV_FFMPEG` 或 `--ffmpeg`。
- **ASR 模型缓存**：`~/.workbuddy/cache/read-video/models`（约 240 MB，首次自动下载）。
  文件存在且字节数匹配就跳过下载。

## 已知边界

- **CDN 常校验来源**：bilibili 等站点的视频 CDN 要求 `Referer`，不带就 403。
  用 `resolve.py` 或 `--page` 会自动带上；手动传直链时记得
  `--header 'Referer: https://www.bilibili.com'`（三个脚本都支持，可重复）。
- **直链会过期**：站方直链带签名与 deadline，同一批操作内用完；一旦报 403 就重新跑 `resolve.py`。
- **平台覆盖面 = yt-dlp 的覆盖面**（1700+ 站点），不是自己维护的。解析不了的站点先用
  `--backend yt-dlp` 看原始报错；需要登录 / Cookie 的站点会失败，这类只能放弃或手工提供 Cookie。
  yt-dlp 本身要定期升级（站点改版后旧版会失效）——`pip install -U yt-dlp`。
- **"解析通"≠"取得到"**：解析层只负责给 URL。取数失败时先分清是解析失败还是网络不可达 ——
  实测本机到 YouTube / Vimeo 不通（代理 502），而抖音 / 微博 / 西瓜视频可达。
- **HLS / m3u8**：不是单文件，`probe.py` 的 box 解剖会失败（有 `error` 字段）。
  此时 `grab.py` 仍可用，ffmpeg 自己会拉需要的分片；`content_length` 为空，
  算不出占比。
- **DASH 音视频分离**：不少站点（bilibili 等）视频流与音频流是两条独立 URL。
  取帧用视频流，转写用音频流 —— 用错了会静默拿到空结果。
- **`-ss` 精度受 GOP 限制**：要取某一秒的帧，必须从最近的关键帧解起，
  所以最小取数单位是"关键帧到目标帧"，不是"一个字节区间"。
- **ASR 同音字**：SenseVoiceSmall 对中文效果不错，但人名、地名、古词常见同音错字
  （实测"阪泉之野"→"版圈之也"、"涿鹿"→"涿陆"）。**引用专有名词前要按上下文校正**，
  别把转写文本原样当成事实。
- **依赖**：`httpx` + `imageio-ffmpeg`（取数）+ `sherpa-onnx` + `numpy`（转写）。
  缺少时脚本会明确报错并提示装哪个。

更完整的原理、踩坑记录与实测数据见 `references/methodology.md`。
本技能与 dsh 插件 `dsh-readvideo` 的关系见 `references/dsh-dual-form.md`。

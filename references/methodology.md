# 按需取字节：方法论、坑与实测数据

这份文档记录"不下载整片就读视频"背后的原理，以及实现时四个**少了就错**的细节。
SKILL.md 只留操作步骤，原理和踩坑都在这里。

## 1. 为什么不能下载整片

- 慢：一个 10 分钟 1080p 视频几百 MB，用户往往只要其中 20 秒。
- 占存储：临时文件清不干净就烂在磁盘上。
- 版权：整片落地和"取一帧看看"在法律上是两件事。

所以目标是：**给定时间点 t，只取解出该帧所必需的那段字节。**

## 2. HTTP Range：一切的前提

```
客户端: GET /movie.mp4
        Range: bytes=2097152-3145727
服务端: 206 Partial Content
        Content-Range: bytes 2097152-3145727/249224577
```

三个事实：

1. `Accept-Ranges: bytes` 是**声明**，不等于真的支持。必须再手工发一次 Range GET
   看回的是 206 还是 200 —— 有些服务器声明了却忽略 Range 回全量。
   `probe.py` 因此同时看 `declared_range` 和 `effective_range`。
2. `bytes=-N`（只给长度）表示"文件最后 N 字节"，用来取尾部不需要知道总长。
3. 不支持 Range 时，**任何定点取帧都会退化成顺序拉流**。此时不要重试 seek，
   要么取流最前面几帧，要么直接告诉用户做不到。

## 3. MP4 容器：索引在哪，决定固定开销

MP4 由一串 box 组成，`moov` 是索引（sample table：`stts` 时间→样本、`stsc`、
`stco` chunk 偏移、`stsz` 样本大小）。"第 183 秒对应的字节区间是多少"只能查 moov。

- **faststart**：moov 在文件头附近。读几十 KB 就能算出目标区间，固定开销极小。
  这是给网络播放优化的标准做法。
- **非 faststart**：moov 在文件尾。必须先取尾部一段才能建索引 —— 这是**固定开销，
  省不掉**。实测 bunny/movie.mp4 的 moov 在 248773029 处（整文件 249224577 B），
  取尾部 640 KB 才拿到。

⇒ `probe.py` 报告 `moov_offset` 和 `faststart`，就是在提前告知这个固定成本。

## 4. GOP 与 `-ss`：最小取数单位不是"一个字节区间"

视频是帧间编码：I 帧（关键帧，独立可解）→ P/B 帧（依赖前面的帧）。
要解第 183 秒的那一帧，必须从**它前面最近的那个 I 帧**开始顺序解。

所以"取一帧"实际取的是「最近 I 帧 → 目标帧」这一整段，不是单点。
GOP 越长，这一段越大。这是编解码决定的，无法靠请求技巧绕过。

`-ss` 的位置决定这这段从哪儿开始：

| 写法 | 语义 | 后果 |
|---|---|---|
| `ffmpeg -ss 183 -i url ...` | **输入侧 seek**：解复用器直接跳到 183 秒附近的字节 | 只取需要的段 ✅ |
| `ffmpeg -i url -ss 183 ...` | 输出侧 seek：先完整解码到 183 秒再丢 | 退化成拉全片 ❌ |

⇒ **`-ss` 永远放在 `-i` 前面。** 这是本技能最重要的一条命令纪律。

## 5. 流式计数代理：四个坑

ffmpeg 不会报告它拉了多少字节。"有没有真的按需取"只能自己数：起一个本地代理，
ffmpeg 指向它，它把请求转发给真实 CDN 并记账。这个代理有四个坑：

### 坑 1：必须先流式转发，不能先缓冲

真实 CDN 不会等整个 range 下完才回数据；而 ffmpeg 拿到目标帧后会**主动断开连接**。
若代理"收完整段再回给 ffmpeg"，就会把"取一帧"放大成"拉完整个开放式 range"——
那是代理自己造成的失真，不是 ffmpeg 的行为。

实现：`for chunk in resp.iter_bytes(CHUNK): wfile.write(chunk)`，边收边发。

### 坑 2：取快照前必须等在途请求归零

ffmpeg 提前断开后，代理的处理线程还要一点时间才能把这次请求落账。
若调用方立刻读 records，就会**漏记目标请求** —— 偏偏漏的是最关键的那次。

实现：`wait_idle()`，连续 0.4 秒无在途请求才认为落账完成。

### 坑 3：每次操作独占一个代理实例

共用一个长命代理时，上一次操作被提前断开的请求可能在**下一次操作开始后**才落账，
字节数记到别人头上（"记录串档"）。

实现：`ByteMeter` 每次 `start()`/`stop()` 现造现毁，`snapshot()` 只读这一轮的。

### 坑 4：Windows 上断开抛的不是 BrokenPipeError

ffmpeg 主动断开时：

- POSIX：`BrokenPipeError`
- **Windows：`ConnectionAbortedError`（WinError 10053）**

只捕获前者，在 Windows 上会当成未知异常炸掉，把"预期行为"变成"脚本失败"。

实现：`except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)` 三个一起抓，
并标记 `aborted=True` —— 这个标记本身就是"没拉完"的正面证据。

## 6. 网络不稳：重试与超时

本机出口代理会间歇返回 5XX。两处重试：

- `retrying()`：所有 HTTP 探测带 4 次指数退避重试。
- `run_ffmpeg()`：遇到 `Error opening input` / `Server disconnected` / `Connection reset`
  / `5XX` / `timed out` 整体重跑，**并先删掉上一次的残留输出** —— ffmpeg 失败时也可能
  留下 0 字节文件，不清掉会让调用方误以为成功。

超时默认 120 s。取帧通常 2~3 s，超时多半是网络问题而不是计算问题。

## 7. 实测数据

目标：`https://media.w3.org/2010/05/bunny/movie.mp4`
249224577 B（237.7 MB），00:09:56.48，非 faststart，moov @ 248773029。

| 操作 | 上游字节 | 占整片 | 请求数 | 墙钟 |
|---|---|---|---|---|
| 取第 30 秒一帧 | 2.0 MB | 0.839% | 4 | — |
| 取第 8 分钟（480s）一帧 | 1.7 MB | 0.707% | 4 | — |
| 取 480s 起 20 秒音频 | 6.6 MB | 2.784% | 4 | — |

固定开销约 1.3 MB（moov + 流头）。**占比随视频时长增长而下降** —— 视频越长，
按需取的优势越大。

## 8. HLS（m3u8）简注

HLS 把视频切成一个个小分片，`m3u8` 清单列出每个分片的时间范围与 URL。
没有单文件、没有 `Content-Length`，`probe.py` 的 box 解剖必然失败。

处理方式：直接让 ffmpeg 读 m3u8 URL，它自己按需拉分片（播放器行为，本身就高效）。
代价是算不出"占整片多少"，也拿不到 moov 那一套分析。

## 9. 平台适配：从页面 URL 到直链

用户给的往往是**页面 URL**，不是直链。

> 这一节的逻辑已固化成 `scripts/resolvers.py`（CLI：`resolve.py`），
> `grab.py` / `transcribe.py` 也走它。下面是实现依据 —— 加平台或排查解析失败时看这里，
> **不要在会话里重新手写这些请求**（写了两次都不长一样，也没人 review）。

### 9.1 通用后端：为什么不自己写"全平台"

"支持所有视频网站"不该手写：每站的页面结构、签名参数、反爬都在变，一站一套代码
就是维护地狱。yt-dlp 是这件事的事实标准（1752 个 extractor），而且支持只取 URL
不下载（`--skip-download`），与本 skill"不下载整片"的原则一致。

集成要点：

- 走**子进程**（`python -m yt_dlp -J --skip-download --no-playlist <url>`）而不是 Python API ——
  API 字段在不同版本间会变，JSON 输出稳定得多，也更好排查。
- **音视频分流**：DASH 站点返回 vcodec-only 与 acodec-only 两族格式，必须分别挑。
  视频流取 `height <= 720` 里最高的一档（取帧是为看清内容，1080P 会让每帧字节数翻几倍）；
  音频流取**码率最低**的一档（ASR 不需要高保真）。
- **请求头必须透传**：`formats[].http_headers` 里有站点要求的东西（Referer / UA / Cookie），
  要交给 ffmpeg，否则 CDN 403 —— 这是"能解析却取不到数"最常见的原因。
- 解析失败**不能静默当直链**：是直链就回退，看着像网页却解不出来就直接报错，
  否则 ffmpeg 会在更晚的环节报一个更难懂的错。
- yt-dlp 需要定期升级：站点改版后旧版 extractor 会失效（`pip install -U yt-dlp`）。

### 9.2 手写适配器：bilibili（保留）

bilibili 是最常见的输入，两条 API 就能拿到精确的 DASH 分流，比拉起 yt-dlp 轻得多，
所以单独实现，`auto` 模式下优先用它。细节如下：

三步拿到流：

1. 取 cid
   ```
   GET https://api.bilibili.com/x/web-interface/view?bvid=<BV号>
   → data.title / data.duration / data.pages[0].cid
   ```
2. 取流地址
   ```
   GET https://api.bilibili.com/x/player/playurl?bvid=<BV>&cid=<cid>&qn=80&fnval=16&fnver=0&fourk=1
   → data.dash.video[]（视频流）/ data.dash.audio[]（音频流）
   ```
3. **两个请求都必须带 `Referer: https://www.bilibili.com`**，否则 CDN 返回 403。
   ffmpeg 取数时同样要带（用 `--header`）。

四个容易踩的点：

- **DASH = 音视频分离**：取帧用 `dash.video[0].baseUrl`，转写用 `dash.audio[0].baseUrl`。
  拿错不会报错，只会静默拿到空音轨 —— 表现为"转写输出为空"。
- 未登录只给到 480P（`id=32`），认画面够用。
- 流 URL 带签名与 deadline，**会过期**。同一批操作内用完；报 403 就重新调一次 playurl。
- 实测这个 12 分钟的视频：6 帧共取 3.27 MB，3 段 90 秒音频共取约 3.2 MB。
  全程没有碰视频主体，也没有任何完整文件落地。

## 10. ASR 选型：为什么用 SenseVoice

- **模型**：SenseVoiceSmall int8（约 239 MB）+ tokens.txt，用 `sherpa-onnx` 跑 CPU 推理。
  与 dsh 语音输入用的是同一个模型、同一份 SHA256 清单，两边口径一致。
- **相比 Whisper**：模型小一个量级、中文更准、CPU 上更快（实测 90 秒音频数秒出结果）。
- **下载**：默认 hf-mirror.com，失败回退 huggingface.co，缓存到
  `~/.workbuddy/cache/read-video/models`。**用字节数校验完整性**（239233841），
  不校验 SHA256 —— 读 239 MB 太慢，字节数足够判断下载是否截断。
- **分片**：SenseVoice 最佳输入约 30 秒。长音频按 25 秒固定窗口切分，逐段识别再拼接。
  切在词中间的风险存在但可接受；要更准可接 Silero VAD（模型已下载但尚未启用）。
- **同音字是硬伤**：中文人名、地名、古词常错。实测"阪泉之野"→"版圈之也"、
  "涿鹿"→"涿陆"、"尧舜"→"尧顺"、"禅让"→"善绕"。
  **引用专有名词前必须按上下文校正，不要把转写原文直接当成事实。**
- **字幕优先**：源站若有内嵌字幕轨，读字幕比 ASR 又准又快。先 `ffmpeg -i <url>`
  看看有没有 `subtitle` 流。

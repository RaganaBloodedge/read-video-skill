"""平台适配：把"页面 URL"解析成可以直接取数的媒体直链。

两类后端，各司其职：

  * **手写适配器**（bilibili）—— 无额外依赖、两条 API 就拿到精确的 DASH 音视频分流。
    这是最常用的路径，所以单独实现，不必每次都拉起 yt-dlp。
  * **yt-dlp 通用后端** —— 覆盖 1700+ 站点（YouTube / Vimeo / 微博 / 抖音……）。
    全平台这件事**不该手写**：每站的页面结构、签名参数、反爬都在变，手写适配器
    是维护地狱。yt-dlp 是这件事的事实标准，而且支持"只取 URL 不下载"
    （--skip-download），与本 skill"不下载整片"的原则一致。

关键输出：
  video_url —— 取帧用
  audio_url —— 转写用（DASH 站点音视频分离，两者不是同一条）
  headers   —— CDN 要求的请求头（很多站点校验 Referer / User-Agent，不带就 403）
"""

from __future__ import annotations

import json as _json
import re
import subprocess
import sys
from typing import Any

import httpx

BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# bilibili 的 CDN 与 API 都校验来源，两个请求和后续 ffmpeg 取数都要带这组头
BILI_HEADERS = {
    "User-Agent": BILI_UA,
    "Referer": "https://www.bilibili.com",
}

# 取帧是为了"看清内容"，不是出片：默认封顶 720P。选 1080P 会让每帧字节数翻好几倍，
# 而按需取字节的意义就在于把这块成本压住。要更高画质可显式传 max_height。
DEFAULT_MAX_HEIGHT = 720

YTDLP_TIMEOUT = 120.0

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_MEDIA_EXT_RE = re.compile(
    r"\.(mp4|m4v|mov|webm|mkv|flv|m3u8|mpd|ts|mp3|m4a|aac|wav|flac|ogg)(?:$|[?#])", re.I
)


# --------------------------------------------------------------------------
# 判断输入类型
# --------------------------------------------------------------------------
def detect_platform(url: str) -> str | None:
    """有手写适配器的平台。返回 None 不代表不支持 —— 只是要靠 yt-dlp。"""
    if _BV_RE.search(url):
        return "bilibili"
    if re.search(r"bilibili\.com/video/|b23\.tv/", url):
        return "bilibili"
    return None


def is_page_url(url: str) -> bool:
    """判断输入是"网页"还是"媒体直链"。

    规则：
      * 带媒体扩展名（.mp4/.m3u8/…）→ 直链，不必解析；
      * 其余 http(s) 链接 → 当页面处理，交给解析层。
        yt-dlp 认不出来时会回退成直链，所以这里宽松一点是安全的。
    """
    if detect_platform(url) is not None:
        return True
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    if _MEDIA_EXT_RE.search(path):
        return False
    return url.lower().startswith(("http://", "https://"))


# --------------------------------------------------------------------------
# 后端一：bilibili 手写适配器
# --------------------------------------------------------------------------
def _get_json(url: str, headers: dict) -> Any:
    with httpx.Client(timeout=30.0, follow_redirects=True, trust_env=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


def resolve_bilibili(url: str, part: int = 1, quality: int = 80) -> dict[str, Any]:
    """两步拿到 DASH 流：view 拿 cid → playurl 拿流地址。

    quality 是期望清晰度（80=1080P），**未登录会被降级**（实测只能拿到 32=480P）。
    认画面够用；需要更高清晰度得带登录 Cookie。
    """
    m = _BV_RE.search(url)
    if not m:
        raise ValueError(f"从 {url} 里找不到 BV 号")
    bvid = m.group(1)

    view = _get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", BILI_HEADERS)
    if view.get("code") != 0:
        raise RuntimeError(f"view API 失败：code={view.get('code')} {view.get('message')}")
    d = view["data"]
    pages = d.get("pages") or []
    if not pages:
        raise RuntimeError("该视频没有分P信息")
    if not 1 <= part <= len(pages):
        raise ValueError(f"分P {part} 不存在（共 {len(pages)} P）")
    cid = pages[part - 1]["cid"]

    play = _get_json(
        "https://api.bilibili.com/x/player/playurl"
        f"?bvid={bvid}&cid={cid}&qn={quality}&fnval=16&fnver=0&fourk=1",
        BILI_HEADERS,
    )
    if play.get("code") != 0:
        raise RuntimeError(f"playurl API 失败：code={play.get('code')} {play.get('message')}")
    dash = (play.get("data") or {}).get("dash")
    if not dash:
        raise RuntimeError("该视频没有 DASH 流（可能是付费/番剧内容，或需要登录）")

    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    if not videos:
        raise RuntimeError("DASH 里没有视频流")

    return {
        "platform": "bilibili",
        "resolver": "native",
        "title": d.get("title", ""),
        "uploader": (d.get("owner") or {}).get("name", ""),
        "duration": d.get("duration", 0),
        "bvid": bvid,
        "cid": cid,
        "part": part,
        "parts": len(pages),
        "video_quality_id": videos[0].get("id"),
        "audio_quality_id": audios[0].get("id") if audios else None,
        "video_url": videos[0].get("baseUrl", ""),
        "audio_url": audios[0].get("baseUrl", "") if audios else "",
        "headers": dict(BILI_HEADERS),
        "note": "DASH 音视频分离：取帧用 video_url，转写用 audio_url",
    }


# --------------------------------------------------------------------------
# 后端二：yt-dlp 通用后端（1700+ 站点）
# --------------------------------------------------------------------------
def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def _pick_formats(info: dict, max_height: int) -> tuple[str, str, dict]:
    """从 yt-dlp 的 formats 里挑视频流与音频流，返回 (video_url, audio_url, headers)。

    DASH 站点（YouTube 等）音视频分离，要分别挑 vcodec-only 与 acodec-only；
    单流站点（普通 mp4）两者指向同一条。
    """
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    base_headers = dict(info.get("http_headers") or {})

    def headers_of(fmt: dict) -> dict:
        h = dict(base_headers)
        h.update(fmt.get("http_headers") or {})
        return h

    video_only = [f for f in formats
                  if f.get("vcodec") not in (None, "none")
                  and f.get("acodec") in (None, "none")]
    audio_only = [f for f in formats
                  if f.get("acodec") not in (None, "none")
                  and f.get("vcodec") in (None, "none")]
    merged = [f for f in formats
              if f.get("vcodec") not in (None, "none")
              and f.get("acodec") not in (None, "none")]

    def best_video(cands: list[dict]) -> dict | None:
        with_h = [f for f in cands if f.get("height")]
        if with_h:
            within = [f for f in with_h if f["height"] <= max_height] or with_h
            # 取封顶高度里最高的那档：看得清，又不至于字节数失控
            return max(within, key=lambda f: f["height"])
        return cands[-1] if cands else None

    if video_only:
        v = best_video(video_only)
        # 音频只用来转写，取码率最低那档就够（ASR 不需要高保真）
        a = min(audio_only, key=lambda f: f.get("abr") or 1e9) if audio_only else None
        return (
            v.get("url", "") if v else info.get("url", ""),
            a.get("url", "") if a else (v.get("url", "") if v else ""),
            headers_of(v or {}),
        )

    pick = merged[-1] if merged else None
    single = (pick or info).get("url", "") or info.get("url", "")
    return single, single, headers_of(pick or {})


def resolve_ytdlp(url: str, max_height: int = DEFAULT_MAX_HEIGHT) -> dict[str, Any]:
    """通用后端。只取信息与直链，绝不下载。"""
    cmd = [sys.executable, "-m", "yt_dlp", "-J", "--skip-download",
           "--no-warnings", "--no-playlist", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=YTDLP_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"yt-dlp 解析超时（{YTDLP_TIMEOUT:g}s）") from None

    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        raise RuntimeError("yt-dlp 解析失败：" + (lines[-1] if lines else "未知原因"))

    try:
        info = _json.loads(proc.stdout.decode("utf-8", "replace"))
    except _json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp 输出不是合法 JSON：{exc}") from None

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("这是个播放列表，但里面没有可用条目")
        info = entries[0]

    video_url, audio_url, headers = _pick_formats(info, max_height)
    if not video_url:
        raise RuntimeError("yt-dlp 没有给出可用的流地址")

    return {
        "platform": info.get("extractor_key") or info.get("extractor") or "unknown",
        "resolver": "yt-dlp",
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator") or "",
        "duration": int(info.get("duration") or 0),
        "video_url": video_url,
        "audio_url": audio_url or video_url,
        "headers": headers,
        "note": "通用后端（yt-dlp）；DASH 站点音视频分离，取帧与转写用不同 URL",
    }


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------
def _direct(url: str, note: str) -> dict[str, Any]:
    return {
        "platform": "direct",
        "resolver": "direct",
        "title": "",
        "uploader": "",
        "duration": 0,
        "video_url": url,
        "audio_url": url,
        "headers": {},
        "note": note,
    }


def resolve(url: str, part: int = 1, quality: int = 80,
            backend: str = "auto",
            max_height: int = DEFAULT_MAX_HEIGHT) -> dict[str, Any]:
    """把任意输入解析成 {video_url, audio_url, headers, ...}。

    backend：
      auto（默认）—— 有手写适配器就用它，否则走 yt-dlp
      native      —— 只用手写适配器
      yt-dlp      —— 强制通用后端（排查"手写适配器是不是过时了"时有用）
    """
    platform = detect_platform(url)

    if backend in ("auto", "native") and platform is not None:
        return resolve_bilibili(url, part=part, quality=quality)

    if backend == "native":
        return _direct(url, "未识别平台，按直链处理")

    last_error = ""
    if ytdlp_available():
        try:
            return resolve_ytdlp(url, max_height=max_height)
        except Exception as exc:  # noqa: BLE001 - 失败要回退，不能直接炸
            last_error = str(exc)
    else:
        last_error = "未安装 yt-dlp（pip install yt-dlp 可支持 1700+ 站点）"

    # 是直链就走直链；看起来是网页却解析不出来，必须明确报错 ——
    # 静默当直链会让 ffmpeg 在更晚的环节报一个更难懂的错。
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    if _MEDIA_EXT_RE.search(path):
        return _direct(url, f"按直链处理（解析层：{last_error}）")
    raise RuntimeError(f"没有解析后端能处理这个链接：{last_error}")


def resolve_input(url: str, force_page: bool, headers: dict[str, str],
                  want: str = "video", part: int = 1,
                  backend: str = "auto") -> tuple[str, dict[str, Any]]:
    """给 grab/transcribe 用的统一入口：页面就解析，直链就原样返回。

    headers 是**就地补齐**的（setdefault，不覆盖调用方显式给的值）——
    这样调用方只要传了页面 URL，就不会因为忘带 Referer 而吃 403。

    返回 (媒体直链, 解析信息)；直链时第二项为空 dict。
    """
    if not (force_page or is_page_url(url)):
        return url, {}
    info = resolve(url, part=part, backend=backend)
    for key, value in (info.get("headers") or {}).items():
        headers.setdefault(key, value)
    target = info.get("video_url" if want == "video" else "audio_url") or ""
    if not target:
        label = "视频" if want == "video" else "音频"
        raise RuntimeError(
            f"解析后没有可用的{label}流（该视频可能音视频分离不完整，或需要登录）"
        )
    return target, info

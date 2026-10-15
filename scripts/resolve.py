"""把网页链接解析成可取的媒体直链。

支持范围：bilibili 走手写适配器；其余 1700+ 站点（YouTube / Vimeo / 微博 / 抖音…）
走 yt-dlp 通用后端。两者都只取 URL 与元信息，**不下载**。

用法：
    python resolve.py <网页链接>            # 人类可读摘要
    python resolve.py <网页链接> --json     # 完整 JSON（给后续脚本/agent 用）
    python resolve.py <链接> --backend yt-dlp   # 强制走通用后端

输出里的 video_url 给 grab.py（取帧），audio_url 给 transcribe.py（转写），
headers 是 CDN 要求的请求头 —— 三个一起用，别只拿 URL。
更省事的做法是直接把原始链接交给 grab.py / transcribe.py，它们会自己解析。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rvcore  # noqa: E402
import resolvers  # noqa: E402


def render(info: dict) -> str:
    if info["platform"] == "direct":
        return (f"未识别平台，按直链处理：\n  {info['video_url']}\n"
                f"原因：{info.get('note', '')}")
    lines = [
        f"平台      : {info['platform']}   （解析后端：{info.get('resolver', '?')}）",
        f"标题      : {info['title']}",
        f"UP 主     : {info['uploader']}",
        f"时长      : {info['duration']} 秒"
        f"（{info['duration'] // 60}:{info['duration'] % 60:02d}）",
    ]
    if info.get("parts", 1) > 1:
        lines.append(f"分 P      : 第 {info['part']} / {info['parts']} P")
    if info.get("video_quality_id") is not None:
        lines.append(f"清晰度 id : video={info.get('video_quality_id')} "
                     f"audio={info.get('audio_quality_id')}"
                     f"  (32=480P, 64=720P, 80=1080P；未登录会降级)")
    lines += [
        "",
        f"video_url : {info['video_url'][:120]}...",
        f"audio_url : {(info['audio_url'] or '(无)')[:120]}...",
        "",
        f"headers   : {json.dumps(info['headers'], ensure_ascii=False)[:220]}",
        "→ 取帧 : grab.py <原始链接> --times ...      (自动解析，不必手动传 URL)",
        "→ 转写 : transcribe.py --url <原始链接> --seconds 90",
    ]
    if info["duration"]:
        lines.insert(6, f"建议抽帧: {suggest_times(info['duration'])}")
    return "\n".join(lines)


def suggest_times(duration: int) -> str:
    """按视频长度给一组抽帧时间点：短片密一点，长片稀一点。"""
    if duration <= 0:
        return "-"
    if duration <= 120:
        n = 5
    elif duration <= 400:
        n = 5
    else:
        n = 6
    step = duration / (n + 1)
    return " ".join(str(int(step * (i + 1))) for i in range(n))


def main() -> int:
    ap = argparse.ArgumentParser(description="网页链接 → 媒体直链")
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--part", type=int, default=1, help="分 P 序号（默认 1）")
    ap.add_argument("--quality", type=int, default=80, help="期望清晰度（默认 80）")
    ap.add_argument("--backend", choices=["auto", "native", "yt-dlp"], default="auto",
                    help="解析后端：auto（默认）/ native（仅手写适配器）/ yt-dlp（强制通用后端）")
    ap.add_argument("--max-height", type=int, default=720,
                    help="yt-dlp 后端下视频流的高度上限（默认 720；调高会让每帧字节数变大）")
    args = ap.parse_args()

    try:
        info = resolvers.resolve(args.url, part=args.part, quality=args.quality,
                                 backend=args.backend, max_height=args.max_height)
    except Exception as exc:  # noqa: BLE001
        print(f"解析失败：{exc}", file=sys.stderr)
        return 1

    if args.json:
        info = dict(info)
        info["suggest_times"] = suggest_times(info["duration"]).split()
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        print(render(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

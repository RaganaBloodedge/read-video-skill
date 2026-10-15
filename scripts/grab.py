"""定点取帧 / 截取音频，并报告真正从上游取了多少字节。

用法：
    # 取某一时刻的一帧
    python grab.py <url> --time 30 --out shot_30.jpg

    # 直接给网页链接 —— 自动解析直链并带上 CDN 要求的头（推荐）
    python grab.py "https://www.bilibili.com/video/BV..." --times 14 29 44 --prefix shot

    # 一次取多帧（自动编号）
    python grab.py <url> --times 30 480 600 --prefix shot

    # 截一段音频转 16kHz 单声道 WAV（喂给 ASR）
    python grab.py <url> --start 480 --seconds 20 --audio clip.wav

    # 跳过字节计量（更快，但拿不到"拉了多少"的证据）
    python grab.py <url> --time 30 --out shot.jpg --no-measure

输出：JSON（含每个动作的 exit / out_bytes / upstream_bytes / requests / wall_seconds）。
--measure 是默认开启的：它通过本地流式代理数上游字节，是"真的没拉整片"的唯一硬证据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolvers  # noqa: E402
import rvcore  # noqa: E402


def parse_headers(items: list[str]) -> dict[str, str]:
    """把多个 'K: V' 形式的 --header 解析成 dict（实现统一在 rvcore）。"""
    return rvcore.parse_headers(items)


def main() -> int:
    ap = argparse.ArgumentParser(description="定点取帧 / 截取音频（按需取字节）")
    ap.add_argument("url", help="视频直链")
    ap.add_argument("--time", type=float, action="append", default=[],
                    help="要取帧的时刻（秒），可重复")
    ap.add_argument("--times", type=float, nargs="+", default=[],
                    help="要取帧的多个时刻（秒）")
    ap.add_argument("--out", help="单帧输出路径（配合单个 --time）")
    ap.add_argument("--prefix", default="frame", help="多帧输出前缀（配合 --times）")
    ap.add_argument("--outdir", default=".", help="多帧输出目录")
    ap.add_argument("--start", type=float, help="音频起始秒（配合 --audio）")
    ap.add_argument("--seconds", type=float, default=20.0, help="音频时长秒")
    ap.add_argument("--audio", help="音频输出路径（.wav）")
    ap.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径（默认自动发现）")
    ap.add_argument("--header", action="append", default=[], metavar="K: V",
                    help="附加 HTTP 头，可重复。CDN 校验来源时必须带，"
                         "如 --header 'Referer: https://www.bilibili.com'")
    ap.add_argument("--no-measure", action="store_true", help="跳过字节计量")
    ap.add_argument("--page", action="store_true",
                    help="输入是网页链接（如 bilibili），先解析成直链并自动带上 CDN 要求的请求头")
    ap.add_argument("--part", type=int, default=1, help="分 P 序号（网页链接有多 P 时用）")
    args = ap.parse_args()

    headers = parse_headers(args.header)
    url, info = resolvers.resolve_input(args.url, args.page, headers, want="video",
                                        part=args.part)
    if info:
        print(f"[resolve] {info['title']} · {info['uploader']} · {info['duration']}s"
              f" · video id={info.get('video_quality_id')}", file=sys.stderr)

    times = args.times + args.time
    if not times and not args.audio:
        ap.error("至少要给 --time/--times 或 --audio")

    measure = not args.no_measure
    actions: list[dict] = []

    if times:
        if len(times) == 1 and args.out:
            targets = [Path(args.out)]
        else:
            outdir = Path(args.outdir)
            outdir.mkdir(parents=True, exist_ok=True)
            targets = [outdir / f"{args.prefix}_{i + 1}_{int(t)}s.jpg"
                       for i, t in enumerate(times)]
        for t, target in zip(times, targets):
            actions.append({"kind": "frame", "time": t, **rvcore.grab_frame(
                url, t, target, ffmpeg=args.ffmpeg, measure=measure,
                headers=headers)})

    if args.audio:
        if args.start is None:
            ap.error("--audio 需要配 --start")
        actions.append({"kind": "audio", "start": args.start,
                        "seconds": args.seconds, **rvcore.grab_audio(
                            url, args.start, args.seconds, args.audio,
                            ffmpeg=args.ffmpeg, measure=measure, headers=headers)})

    ok = all(a["ok"] for a in actions)
    result = {
        "ok": ok,
        "url": url,
        "actions": actions,
        "total_out_bytes": sum(a["out_bytes"] for a in actions),
    }
    if measure:
        result["total_upstream_bytes"] = sum(a.get("upstream_bytes", 0) for a in actions)
        result["total_requests"] = sum(a.get("requests", 0) for a in actions)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

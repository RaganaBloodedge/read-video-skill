"""探测一条视频 URL：能不能按需取字节，索引在头还是在尾。

用法：
    python probe.py <url>              # 人类可读摘要
    python probe.py <url> --json       # 完整 JSON（给 agent 解析用）

    直链需要带来源头时（如 bilibili CDN）：
    python probe.py <直链> --header "Referer: https://www.bilibili.com"

先跑这个，再决定要不要 grab.py。若 seekable 为 false，说明服务器忽略 Range，
任何定点取帧都会退化成顺序拉流，此时应改走"边下边解"或直接放弃。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rvcore  # noqa: E402


def probe(url: str, tail_len: int = 640 * 1024,
          headers: dict[str, str] | None = None) -> dict:
    head = rvcore.probe_head(url, headers=headers)
    try:
        rng = rvcore.probe_range_get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        rng = {"status": 0, "content_range": "", "bytes": 0, "error": str(exc)}
    try:
        layout = rvcore.scan_boxes(url, tail_len=tail_len, headers=headers)
    except Exception as exc:  # noqa: BLE001 - 非 MP4 容器（如 HLS 清单）解不出 box
        layout = {"head_boxes": [], "moov_offset": None,
                  "tail_bytes_fetched": 0, "faststart": None, "error": str(exc)}

    declared = "bytes" in (head.get("accept_ranges") or "").lower()
    effective = rng.get("status") == 206
    return {
        "url": url,
        "final_url": head.get("final_url", url),
        "status": head["status"],
        "content_type": head["content_type"],
        "content_length": head["content_length"],
        "accept_ranges": head["accept_ranges"],
        "range_get": rng,
        "declared_range": declared,
        "effective_range": effective,
        "seekable": declared and effective,
        "container": layout,
    }


def render(r: dict) -> str:
    lines = [
        f"url            : {r['url']}",
        f"final url      : {r['final_url']}",
        f"status         : {r['status']}",
        f"content-type   : {r['content_type'] or '(未声明)'}",
        f"length         : {rvcore.human(r['content_length']) if r['content_length'] else '(未知)'}",
        f"accept-ranges  : {r['accept_ranges'] or '(未声明)'}",
        f"range 实测     : HTTP {r['range_get'].get('status')}"
        f"  content-range={r['range_get'].get('content_range') or '-'}",
    ]
    if r["seekable"]:
        lines.append("seekable       : 是 —— 可以定点取字节，不必拉整片")
    elif r["declared_range"]:
        lines.append("seekable       : 声明了 Range 但实测未回 206 —— 不可信，别按跳读设计")
    else:
        lines.append("seekable       : 否 —— 服务器不支持 Range，只能顺序读")

    c = r["container"]
    if c.get("error"):
        lines.append(f"container      : 解剖失败（{c['error']}）—— 可能不是 MP4（如 HLS/m3u8）")
    else:
        for b in c.get("head_boxes", [])[:8]:
            lines.append(f"  头部 box     : {b}")
        if c.get("moov_offset") is not None:
            tag = "faststart，索引在头部" if c["faststart"] else "非 faststart，索引在尾部"
            lines.append(f"moov 偏移      : {c['moov_offset']} ({rvcore.human(c['moov_offset'])}) → {tag}")
        else:
            lines.append("moov 偏移      : 尾部未找到（可能就在上面的头部 box 里）")
        lines.append(f"尾部取数       : {rvcore.human(c.get('tail_bytes_fetched', 0))}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="探测视频 URL 的按需取字节能力")
    ap.add_argument("url")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON 而非摘要")
    ap.add_argument("--tail-len", type=int, default=640 * 1024,
                    help="扫 moov 时取文件尾多少字节（默认 640KB）")
    ap.add_argument("--header", action="append", default=[], metavar="K: V",
                    help="附加 HTTP 头，可重复。CDN 校验来源时必须带，"
                         "如 --header 'Referer: https://www.bilibili.com'")
    args = ap.parse_args()

    r = probe(args.url, tail_len=args.tail_len,
              headers=rvcore.parse_headers(args.header))
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

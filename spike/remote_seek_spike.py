"""可行性验证（spike）：ffmpeg 远程 seek 到底拉了多少字节？

要证实的命题：
    「给定一条直链视频 URL，ffmpeg 能只取目标时间附近的一小段字节，
      而不必把整个文件拉下来。」

做法（两层证据）：
  证据 A（硬证据）—— 流式计数代理
    本地起一个转发代理，ffmpeg 指向它，它把请求原样转发给真实 CDN，
    并把**实际转发出去的字节数**精确记下来。
    必须流式转发：真实 CDN 不会等整个 range 下完才回数据，而 ffmpeg
    拿到目标帧后会主动断开连接。若代理先缓冲再回，就会把"取一帧"
    放大成"拉完整个开放式 range"——那是代理的失真，不是 ffmpeg 的行为。
  证据 B（行为证据）—— ffmpeg 的 trace 日志
    直接跑一次 -loglevel trace，从日志里捞出所有 `Range: bytes=...`
    请求头，证明它确实是"跳读"。

用法：
    python remote_seek_spike.py
    python remote_seek_spike.py --url <直链> --times 30 480
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import imageio_ffmpeg

SPIKE_DIR = Path(__file__).resolve().parent
OUT_DIR = SPIKE_DIR / "out"

# ffmpeg 由 imageio-ffmpeg 自带的静态构建提供，不依赖系统安装
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

DEFAULT_URL = "https://media.w3.org/2010/05/bunny/movie.mp4"

CHUNK = 64 * 1024


# --------------------------------------------------------------------------
# 证据 A：流式计数代理
# --------------------------------------------------------------------------
class CountingProxy:
    """把 ffmpeg 的请求转发给上游，并记录真正转发出去的字节数。

    关键点：
      * 流式转发（边收边发），不缓冲整个响应；
      * ffmpeg 提前断开连接时（BrokenPipe）如实记录已转发量并正常收尾；
      * 只处理 HEAD 与带 Range 的 GET。
    """

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.records: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        # 在途请求计数：ffmpeg 提前断开后，处理线程的落账可能晚于调用方快照，
        # 于是那一次请求的字节数会被丢掉（表现为"漏记目标区域"）。
        # 所以快照前必须等在途请求归零。
        self._inflight = 0
        self._lock = threading.Lock()

    def _enter(self) -> None:
        with self._lock:
            self._inflight += 1

    def _exit(self) -> None:
        with self._lock:
            self._inflight -= 1

    def wait_idle(self, timeout: float = 10.0, quiet: float = 0.4) -> bool:
        """等在途请求全部落账（连续 quiet 秒无在途请求）后再返回。"""
        deadline = time.monotonic() + timeout
        last_busy = time.monotonic()
        while time.monotonic() < deadline:
            with self._lock:
                busy = self._inflight
            if busy > 0:
                last_busy = time.monotonic()
            elif time.monotonic() - last_busy >= quiet:
                return True
            time.sleep(0.05)
        return False

    def start(self) -> None:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_one_request(self):
                proxy._enter()
                try:
                    super().handle_one_request()
                finally:
                    proxy._exit()

            def _upstream_headers(self) -> dict:
                h = {"Accept-Encoding": "identity"}
                rng = self.headers.get("Range")
                if rng:
                    h["Range"] = rng
                return h

            def _record(self, **kw) -> None:
                proxy.records.append(kw)

            def do_HEAD(self):  # noqa: N802
                started = time.perf_counter()
                try:
                    with httpx.Client(timeout=60.0, follow_redirects=True,
                                      trust_env=True) as client:
                        resp = client.head(proxy.upstream,
                                           headers=self._upstream_headers())
                except Exception as exc:
                    self._record(range=None, status=0, bytes=0, error=str(exc),
                                 seconds=round(time.perf_counter() - started, 3))
                    self.send_error(502, "upstream head failed")
                    return
                self._record(range=None, status=resp.status_code, bytes=0,
                             content_range="",
                             seconds=round(time.perf_counter() - started, 3))
                self.send_response(resp.status_code)
                for key in ("Content-Type", "Content-Range", "Accept-Ranges",
                            "Content-Length"):
                    if key in resp.headers:
                        self.send_header(key, resp.headers[key])
                self.end_headers()

            def do_GET(self):  # noqa: N802
                rng = self.headers.get("Range")
                started = time.perf_counter()
                relayed = 0
                try:
                    with httpx.Client(timeout=60.0, follow_redirects=True,
                                      trust_env=True) as client:
                        with client.stream("GET", proxy.upstream,
                                           headers=self._upstream_headers()) as resp:
                            self.send_response(resp.status_code)
                            for key in ("Content-Type", "Content-Range",
                                        "Accept-Ranges", "Content-Length"):
                                if key in resp.headers:
                                    self.send_header(key, resp.headers[key])
                            self.end_headers()
                            aborted = False
                            try:
                                for chunk in resp.iter_bytes(CHUNK):
                                    self.wfile.write(chunk)
                                    relayed += len(chunk)
                            except (BrokenPipeError, ConnectionResetError,
                                    ConnectionAbortedError):
                                # ffmpeg 拿到想要的帧后主动断开 —— 这是预期行为，
                                # 也正是"不必拉完"的证明。
                                # 注意：Windows 上抛的是 ConnectionAbortedError
                                # (WinError 10053)，不是 POSIX 的 BrokenPipeError。
                                aborted = True
                            self._record(
                                range=rng,
                                status=resp.status_code,
                                bytes=relayed,
                                aborted=aborted,
                                content_range=resp.headers.get("Content-Range", ""),
                                seconds=round(time.perf_counter() - started, 3),
                            )
                except Exception as exc:
                    self._record(range=rng, status=0, bytes=relayed, error=str(exc),
                                 seconds=round(time.perf_counter() - started, 3))
                    try:
                        self.send_error(502, "upstream fetch failed")
                    except Exception:
                        pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/stream"

    def mark(self) -> int:
        return len(self.records)

    def since(self, mark: int) -> list[dict]:
        return self.records[mark:]


# --------------------------------------------------------------------------
# 子进程封装
# --------------------------------------------------------------------------
def subprocess_env() -> dict:
    """让 ffmpeg 不走系统代理去访问我们自己的本地代理。"""
    env = os.environ.copy()
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def run_ffmpeg(args: list[str], timeout: float = 120.0, attempts: int = 3) -> tuple[int, str]:
    """本机出口代理会间歇性返回 5XX，对"打开输入失败"这类错误整体重试。"""
    code = -1
    err = ""
    for i in range(attempts):
        try:
            proc = subprocess.run(
                [FFMPEG, "-hide_banner", *args],
                capture_output=True, env=subprocess_env(), timeout=timeout,
            )
            code = proc.returncode
            err = proc.stderr.decode("utf-8", "replace")
            if code == 0:
                return 0, err
        except subprocess.TimeoutExpired as exc:
            code = -9
            err = (exc.stderr or b"").decode("utf-8", "replace")
            err += f"\n[spike] ffmpeg 超时（{timeout}s）被中断"

        transient = any(s in err for s in (
            "5XX", "Error opening input", "Server disconnected", "Connection reset",
        )) or "timed out" in err.lower()
        if not transient or i == attempts - 1:
            break
        time.sleep(1.5 * (i + 1))
        target = Path(args[-1])
        if target.is_file():
            try:
                target.unlink()
            except OSError:
                pass
    return code, err


def retrying(method: str, url: str, attempts: int = 4, **kw):
    """本机出口代理不稳定，网络探测统一带重试。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True,
                              trust_env=True) as client:
                return client.request(method, url, **kw)
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def probe_head(url: str) -> dict:
    resp = retrying("HEAD", url)
    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("Content-Type", ""),
        "content_length": int(resp.headers.get("Content-Length") or 0),
        "accept_ranges": resp.headers.get("Accept-Ranges", ""),
    }


def probe_range_get(url: str, start: int = 0, length: int = 1024) -> dict:
    """手工发一次 Range 请求，确认服务器回 206 而不是忽略 Range 回 200。"""
    resp = retrying(
        "GET", url,
        headers={"Range": f"bytes={start}-{start + length - 1}",
                 "Accept-Encoding": "identity"},
    )
    return {
        "status": resp.status_code,
        "content_range": resp.headers.get("Content-Range", ""),
        "bytes": len(resp.content),
    }


def scan_boxes(url: str, head_len: int = 65536, tail_len: int = 640 * 1024) -> dict:
    """扫一遍 MP4 的顶层 box，回答一个关键问题：moov（索引）在文件头还是文件尾？

    这决定"取一帧"的固定开销：
      * faststart（moov 在头部）→ 读几十 KB 就能算出目标样本的字节区间；
      * 非 faststart（moov 在尾部）→ 必须先取文件尾那一段（本例约 441 KB）。
    """
    rng = f"bytes=0-{head_len - 1}"
    head = retrying("GET", url, headers={"Range": rng, "Accept-Encoding": "identity"}).content
    boxes: list[str] = []
    off = 0
    while off + 8 <= len(head):
        size = int.from_bytes(head[off:off + 4], "big")
        typ = head[off + 4:off + 8].decode("latin1", "replace")
        boxes.append(f"{typ}({size}B)@ {off}")
        if size < 8:
            break
        off += size
        if off >= len(head):
            break

    tail_start = max(0, 0)  # 占位，下面按总长算
    r = retrying("GET", url, headers={"Range": f"bytes=-{tail_len}",
                                     "Accept-Encoding": "identity"})
    tail = r.content
    idx = tail.find(b"moov")
    moov_at = None
    content_range = r.headers.get("Content-Range", "")
    if idx >= 0 and content_range:
        total = int(content_range.split("/")[-1])
        moov_at = total - len(tail) + idx  # moov 在整文件中的偏移
    return {
        "head_boxes": boxes,
        "moov_offset": moov_at,
        "tail_bytes_fetched": len(tail),
        "faststart": moov_at is not None and moov_at < 1024 * 1024,
    }


def parse_duration(stderr: str) -> str:
    m = re.search(r"Duration:\s*(\d+:\d+:\d+\.\d+)", stderr)
    return m.group(1) if m else "?"


def parse_ranges(stderr: str) -> list[str]:
    seen: list[str] = []
    for line in stderr.splitlines():
        for m in re.finditer(r"Range:\s*bytes=\S+", line):
            if m.group(0) not in seen:
                seen.append(m.group(0))
    return seen


def parse_request_summary(stderr: str) -> list[str]:
    out: list[str] = []
    for line in stderr.splitlines():
        if "Request: GET" in line or "Redirect to" in line or "HTTP error" in line:
            out.append(re.sub(r"\s+", " ", line.strip())[-150:])
    return out


# --------------------------------------------------------------------------
# 两种取证动作
# --------------------------------------------------------------------------
@contextmanager
def measured(url: str):
    """每次操作独占一个计数代理。

    不共用一个长命代理：否则提前断开的那次请求可能在下一个操作开始后才
    落账，把字节数记到别人头上（表现为"记录串档"）。
    """
    proxy = CountingProxy(url)
    proxy.start()
    try:
        yield proxy
    finally:
        pass


def _snapshot(proxy: CountingProxy) -> list[dict]:
    """等在途请求全部落账后再取记录，避免漏记被提前断开的那次请求。"""
    proxy.wait_idle()
    return list(proxy.records)


def grab_frame(url: str, timestamp: float, out_name: str) -> dict:
    out_path = OUT_DIR / out_name
    with measured(url) as proxy:
        code, err = run_ffmpeg(
            [
                "-loglevel", "warning",
                # -ss 放在 -i 之前 = 输入侧 seek：让解复用器直接跳到目标位置
                "-ss", f"{timestamp}",
                "-i", proxy.url,
                "-frames:v", "1",
                "-q:v", "3",
                "-y", str(out_path),
            ]
        )
        recs = _snapshot(proxy)
        proxy.stop()
    return {
        "label": f"取帧 @ {timestamp:g}s",
        "exit": code,
        "out_bytes": out_path.stat().st_size if out_path.exists() else 0,
        "upstream_bytes": sum(r["bytes"] for r in recs),
        "requests": len(recs),
        "wall": sum(r["seconds"] for r in recs),
        "records": recs,
        "err_tail": [ln for ln in err.strip().splitlines() if ln][-3:] if code != 0 else [],
    }


def grab_audio(url: str, start: float, seconds: float, out_name: str) -> dict:
    out_path = OUT_DIR / out_name
    with measured(url) as proxy:
        code, err = run_ffmpeg(
            [
                "-loglevel", "warning",
                "-ss", f"{start}",
                "-t", f"{seconds}",
                "-i", proxy.url,
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-c:a", "pcm_s16le",
                "-f", "wav",
                "-y", str(out_path),
            ]
        )
        recs = _snapshot(proxy)
        proxy.stop()
    return {
        "label": f"音频 {start:g}s + {seconds:g}s",
        "exit": code,
        "out_bytes": out_path.stat().st_size if out_path.exists() else 0,
        "upstream_bytes": sum(r["bytes"] for r in recs),
        "requests": len(recs),
        "wall": sum(r["seconds"] for r in recs),
        "records": recs,
        "err_tail": [ln for ln in err.strip().splitlines() if ln][-3:] if code != 0 else [],
    }


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("SPIKE_URL", DEFAULT_URL))
    ap.add_argument("--times", nargs="*", type=float, default=[30.0, 480.0])
    ap.add_argument("--audio-seconds", type=float, default=20.0)
    args = ap.parse_args()
    url: str = args.url
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"目标: {url}")
    print(f"ffmpeg: {FFMPEG}")
    print("=" * 78)

    head = probe_head(url)
    total = head["content_length"]
    print("\n[1] HEAD 探测")
    print(f"    status          = {head['status']}")
    print(f"    content-type    = {head['content_type']}")
    print(f"    content-length  = {total} ({human(total)})")
    print(f"    accept-ranges   = {head['accept_ranges'] or '(未声明)'}")

    rng = probe_range_get(url)
    print("\n[2] 手工 Range 请求（验证服务器是否真支持跳读）")
    print(f"    status          = {rng['status']}   (206 = 支持部分内容)")
    print(f"    content-range   = {rng['content_range']}")
    print(f"    实收字节        = {rng['bytes']}")

    print("\n[2.5] 容器解剖：索引（moov）在文件头还是文件尾？")
    try:
        layout = scan_boxes(url)
        for b in layout["head_boxes"][:8]:
            print(f"    头部 box: {b}")
        if layout["moov_offset"] is None:
            print("    尾部未找到 moov（可能在上面的头部 box 列表里）")
        else:
            off = layout["moov_offset"]
            print(f"    moov 偏移       = {off} ({human(off)} 处)"
                  f"{'  → faststart' if layout['faststart'] else '  → 非 faststart，索引在尾部'}")
        print(f"    尾部取数        = {human(layout['tail_bytes_fetched'])}")
    except Exception as exc:
        print(f"    解剖失败（跳过）：{exc}")

    print("\n[3] 行为证据：-loglevel trace 下 ffmpeg 实际发了哪些 Range 请求")
    print("    （直连真实 URL，不经过我们的代理）")
    code, err = run_ffmpeg(
        ["-loglevel", "trace", "-ss", "480", "-i", url,
         "-frames:v", "1", "-f", "null", "-"],
        timeout=120,
    )
    print(f"    输入时长        = {parse_duration(err)}")
    ranges = parse_ranges(err)
    print(f"    共 {len(ranges)} 个 Range 请求:")
    for r in ranges[:12]:
        print(f"      {r}")
    if len(ranges) > 12:
        print(f"      ... 另有 {len(ranges) - 12} 个")
    for line in parse_request_summary(err)[:6]:
        print(f"    日志: {line}")

    results: list[dict] = [
        grab_frame(url, t, f"frame_{int(t)}s.jpg") for t in args.times
    ]
    results.append(grab_audio(url, args.times[-1], args.audio_seconds, "audio_20s.wav"))
    for r in results:
        print(f"    · 完成 {r['label']}")
    print("\n[4] 计数代理实测：每次操作真正从服务器拉了多少字节")
    print(f"    {'操作':<20}{'上游字节':>11}{'占整片':>9}{'请求':>6}{'耗时':>9}{'产出':>11}")
    for r in results:
        pct = f"{r['upstream_bytes'] / total * 100:.3f}%" if total else "?"
        print(f"    {r['label']:<20}{human(r['upstream_bytes']):>11}{pct:>9}"
              f"{r['requests']:>6}{r['wall']:>8.2f}s{human(r['out_bytes']):>11}")
        for rec in r["records"]:
            cr = f" {rec.get('content_range', '')}" if rec.get("content_range") else ""
            note = f" !! {rec['error']}" if rec.get("error") else ""
            cut = "  [提前断开]" if rec.get("aborted") else ""
            print(f"        └ {rec['status']} {rec['range']} -> {human(rec['bytes'])}"
                  f"{cut}{cr}{note}")
        for ln in r["err_tail"]:
            print(f"        !! exit={r['exit']} {ln}")

    print("\n[5] 落地文件")
    for path in sorted(OUT_DIR.glob("*")):
        print(f"    {path.name:<22}{human(path.stat().st_size)}")

    pulled = sum(r["upstream_bytes"] for r in results)
    print("\n" + "=" * 78)
    if total:
        print(f"结论：整片 {human(total)}；上面所有操作合计从服务器取 {human(pulled)}"
              f"，占 {pulled / total * 100:.3f}%")
    else:
        print(f"结论：上面所有操作合计从服务器取 {human(pulled)}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

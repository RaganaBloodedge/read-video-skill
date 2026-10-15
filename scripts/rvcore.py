"""read-video 共享库：不下载整片，按需取字节。

从 spike/remote_seek_spike.py 提炼而来，去掉报表打印，只留可复用能力。
四个"看起来多余、少了就错"的实现细节都保留着，理由见 references/methodology.md：

  1. 代理必须流式转发（边收边发），不能先缓冲再回；
  2. 取快照前必须等在途请求归零（wait_idle）；
  3. 每次操作独占一个代理实例，不共用长命代理；
  4. Windows 上客户端提前断开抛的是 ConnectionAbortedError（WinError 10053），
     不是 POSIX 的 BrokenPipeError —— 必须一起捕获。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

try:  # ffmpeg 由 imageio-ffmpeg 提供静态构建，不依赖系统安装
    import imageio_ffmpeg
except ImportError:  # pragma: no cover - 允许只装 httpx 做探测
    imageio_ffmpeg = None  # type: ignore[assignment]

CHUNK = 64 * 1024
DEFAULT_FFMPEG_TIMEOUT = 120.0
# 本机出口代理会间歇返回 5XX，这些字样出现时值得整体重试一次
TRANSIENT_MARKERS = (
    "5XX",
    "Error opening input",
    "Server disconnected",
    "Connection reset",
    "timed out",
)

_FORWARD_HEADERS = ("Content-Type", "Content-Range", "Accept-Ranges", "Content-Length")


# --------------------------------------------------------------------------
# ffmpeg 定位与执行
# --------------------------------------------------------------------------
def ffmpeg_exe(explicit: str | None = None) -> str:
    """定位 ffmpeg：显式参数 > RV_FFMPEG > imageio-ffmpeg 自带静态构建。

    桌面版 dsh（E:\\DeepSeek-Harness）自带 Python 3.12 + Pillow，但没有 ffmpeg；
    此时装一个 imageio-ffmpeg 比自己配系统 ffmpeg 省事。
    """
    for cand in (explicit, os.environ.get("RV_FFMPEG"), os.environ.get("FFMPEG")):
        if cand and Path(cand).exists():
            return cand
    if imageio_ffmpeg is None:
        raise RuntimeError(
            "未找到 ffmpeg：请 pip install imageio-ffmpeg，或用 RV_FFMPEG 指定可执行文件路径"
        )
    return imageio_ffmpeg.get_ffmpeg_exe()


def subprocess_env() -> dict:
    """让 ffmpeg 不走系统代理去访问我们自己的本地代理。"""
    env = os.environ.copy()
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def run_ffmpeg(
    ffmpeg: str,
    args: list[str],
    timeout: float = DEFAULT_FFMPEG_TIMEOUT,
    attempts: int = 3,
) -> tuple[int, str]:
    """跑 ffmpeg，对瞬时网络错误整体重试。

    重试前会删掉上一次的残留输出文件：ffmpeg 失败时也可能留下一个 0 字节文件，
    不清掉会让调用方误以为成功了。
    """
    code = -1
    err = ""
    for i in range(attempts):
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", *args],
                capture_output=True,
                env=subprocess_env(),
                timeout=timeout,
            )
            code = proc.returncode
            err = proc.stderr.decode("utf-8", "replace")
            if code == 0:
                return 0, err
        except subprocess.TimeoutExpired as exc:
            code = -9
            err = (exc.stderr or b"").decode("utf-8", "replace")
            err += f"\n[read-video] ffmpeg 超时（{timeout}s）被中断"

        transient = any(m in err for m in TRANSIENT_MARKERS)
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


# --------------------------------------------------------------------------
# HTTP 探测
# --------------------------------------------------------------------------
def retrying(method: str, url: str, attempts: int = 4, **kw) -> httpx.Response:
    """网络探测统一带重试（出口代理不稳定）。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True, trust_env=True) as client:
                return client.request(method, url, **kw)
        except Exception as exc:  # noqa: BLE001 - 重试结束后原样抛出
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def parse_headers(items: list[str]) -> dict[str, str]:
    """把多个 'K: V' 形式的 --header 参数解析成 dict。"""
    out: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            raise ValueError(f"--header 格式应为 'K: V'，收到: {item!r}")
        key, value = item.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def probe_head(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    resp = retrying("HEAD", url, headers=dict(headers or {}))
    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("Content-Type", ""),
        "content_length": int(resp.headers.get("Content-Length") or 0),
        "accept_ranges": resp.headers.get("Accept-Ranges", ""),
        "final_url": str(resp.url),
    }


def probe_range_get(url: str, start: int = 0, length: int = 1024,
                    headers: dict[str, str] | None = None) -> dict[str, Any]:
    """手工发一次 Range 请求，确认服务器回 206 而不是忽略 Range 回 200。

    有些服务器声明了 Accept-Ranges 却仍然回 200 全量，这一步才能验真。
    """
    merged = {"Range": f"bytes={start}-{start + length - 1}", "Accept-Encoding": "identity"}
    merged.update(headers or {})
    resp = retrying("GET", url, headers=merged)
    return {
        "status": resp.status_code,
        "content_range": resp.headers.get("Content-Range", ""),
        "bytes": len(resp.content),
    }


def scan_boxes(url: str, head_len: int = 65536, tail_len: int = 640 * 1024,
               headers: dict[str, str] | None = None) -> dict[str, Any]:
    """扫 MP4 顶层 box，回答关键问题：moov（索引）在文件头还是文件尾？

    这决定"取一帧"的固定开销：
      * faststart（moov 在头部）→ 读几十 KB 就能算出目标样本的字节区间；
      * 非 faststart（moov 在尾部）→ 必须先取文件尾那一段（实测约 441 KB）。
    """
    h1 = {"Range": f"bytes=0-{head_len - 1}", "Accept-Encoding": "identity"}
    h1.update(headers or {})
    head = retrying("GET", url, headers=h1).content
    boxes: list[str] = []
    off = 0
    while off + 8 <= len(head):
        size = int.from_bytes(head[off : off + 4], "big")
        typ = head[off + 4 : off + 8].decode("latin1", "replace")
        boxes.append(f"{typ}({size}B)@ {off}")
        if size < 8:
            break
        off += size
        if off >= len(head):
            break

    r = retrying("GET", url, headers={**{"Range": f"bytes=-{tail_len}", "Accept-Encoding": "identity"},
                                      **(headers or {})})
    tail = r.content
    idx = tail.find(b"moov")
    moov_at: int | None = None
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


# --------------------------------------------------------------------------
# 流式计数代理
# --------------------------------------------------------------------------
class ByteMeter:
    """本地转发代理：记录真正从上游转发出多少字节。

    为什么需要它：ffmpeg 自己不会告诉你它拉了多少数据，而"有没有真的按需取"
    只能靠数上游字节来证明。四个关键实现见模块 docstring。
    """

    def __init__(self, upstream: str, timeout: float = 60.0,
                 extra_headers: dict[str, str] | None = None) -> None:
        self.upstream = upstream
        self.timeout = timeout
        # 需要透传给上游的附加头（如 bilibili CDN 强制校验的 Referer）。
        # ffmpeg 的请求经过本代理，代理默认只转发 Range，附加头会在这里补上。
        self.extra_headers = dict(extra_headers or {})
        self.records: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        # 在途请求计数：ffmpeg 提前断开后，处理线程的落账可能晚于调用方快照，
        # 那一次请求的字节数就会被丢掉（表现为"漏记目标区域"）。
        self._inflight = 0
        self._lock = threading.Lock()

    # -- 在途计数 --
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

    # -- 生命周期 --
    def start(self) -> None:
        meter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音，别把代理日志混进结果
                pass

            def handle_one_request(self):
                meter._enter()
                try:
                    super().handle_one_request()
                finally:
                    meter._exit()

            def _upstream_headers(self) -> dict:
                h = {"Accept-Encoding": "identity"}
                rng = self.headers.get("Range")
                if rng:
                    h["Range"] = rng
                # 附加头放在最后，允许调用方覆盖默认值
                h.update(meter.extra_headers)
                return h

            def do_HEAD(self):  # noqa: N802
                started = time.perf_counter()
                try:
                    with httpx.Client(
                        timeout=meter.timeout, follow_redirects=True, trust_env=True
                    ) as client:
                        resp = client.head(meter.upstream, headers=self._upstream_headers())
                except Exception as exc:  # noqa: BLE001
                    meter.records.append(
                        dict(range=None, status=0, bytes=0, error=str(exc),
                             seconds=round(time.perf_counter() - started, 3))
                    )
                    self.send_error(502, "upstream head failed")
                    return
                meter.records.append(
                    dict(range=None, status=resp.status_code, bytes=0, content_range="",
                         seconds=round(time.perf_counter() - started, 3))
                )
                self.send_response(resp.status_code)
                for key in _FORWARD_HEADERS:
                    if key in resp.headers:
                        self.send_header(key, resp.headers[key])
                self.end_headers()

            def do_GET(self):  # noqa: N802
                rng = self.headers.get("Range")
                started = time.perf_counter()
                relayed = 0
                try:
                    with httpx.Client(
                        timeout=meter.timeout, follow_redirects=True, trust_env=True
                    ) as client:
                        with client.stream("GET", meter.upstream,
                                           headers=self._upstream_headers()) as resp:
                            self.send_response(resp.status_code)
                            for key in _FORWARD_HEADERS:
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
                                # ffmpeg 拿到想要的帧后主动断开 —— 预期行为，
                                # 也正是"不必拉完"的证明。
                                aborted = True
                            meter.records.append(
                                dict(range=rng, status=resp.status_code, bytes=relayed,
                                     aborted=aborted,
                                     content_range=resp.headers.get("Content-Range", ""),
                                     seconds=round(time.perf_counter() - started, 3))
                            )
                except Exception as exc:  # noqa: BLE001
                    meter.records.append(
                        dict(range=rng, status=0, bytes=relayed, error=str(exc),
                             seconds=round(time.perf_counter() - started, 3))
                    )
                    try:
                        self.send_error(502, "upstream fetch failed")
                    except Exception:  # noqa: BLE001 - 客户端已断开，回什么都无所谓
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

    # -- 计量 --
    def mark(self) -> int:
        return len(self.records)

    def snapshot(self, mark: int | None = None) -> list[dict[str, Any]]:
        """取记录。先 wait_idle 再拷，避免漏记被提前断开的那次请求。"""
        self.wait_idle()
        return list(self.records if mark is None else self.records[mark:])


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把记录汇成一次操作的账：上游字节、请求数、墙钟时间。"""
    return {
        "upstream_bytes": sum(r["bytes"] for r in records),
        "requests": len(records),
        "wall_seconds": round(sum(r["seconds"] for r in records), 3),
        "records": records,
    }


# --------------------------------------------------------------------------
# 两个取证动作
# --------------------------------------------------------------------------
def _header_arg(headers: dict[str, str] | None) -> list[str]:
    """把附加头转成 ffmpeg 的 -headers 参数。

    注意分隔符必须是 CRLF（\\r\\n），只写 \\n ffmpeg 会解析失败。
    """
    if not headers:
        return []
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


def grab_frame(
    url: str,
    timestamp: float,
    out_path: str | Path,
    ffmpeg: str | None = None,
    measure: bool = True,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """取某一时刻的一帧。

    -ss 放在 -i 之前 = 输入侧 seek，解复用器直接跳到目标位置，
    不必从文件头顺序解到目标点。

    headers 用于 CDN 强制校验来源的场景（如 bilibili 必须带 Referer，否则 403）。
    """
    ffmpeg = ffmpeg_exe(ffmpeg)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    base = ["-loglevel", "warning", *_header_arg(headers),
            "-ss", f"{timestamp}", "-i", "{src}",
            "-frames:v", "1", "-q:v", "3", "-update", "1", "-y", str(out)]

    if not measure:
        code, err = run_ffmpeg(ffmpeg, [a.format(src=url) for a in base])
        return {"ok": code == 0, "exit": code, "output": str(out),
                "out_bytes": out.stat().st_size if out.exists() else 0,
                "err_tail": _tail(err) if code != 0 else []}

    meter = ByteMeter(url, extra_headers=headers)
    meter.start()
    try:
        code, err = run_ffmpeg(ffmpeg, [a.format(src=meter.url) for a in base])
        stats = summarize(meter.snapshot())
    finally:
        meter.stop()
    return {
        "ok": code == 0,
        "exit": code,
        "output": str(out),
        "out_bytes": out.stat().st_size if out.exists() else 0,
        "err_tail": _tail(err) if code != 0 else [],
        **stats,
    }


def grab_audio(
    url: str,
    start: float,
    seconds: float,
    out_path: str | Path,
    ffmpeg: str | None = None,
    measure: bool = True,
    sample_rate: int = 16000,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """截取一段音频并转成 16 kHz 单声道 WAV（ASR 的通用输入格式）。"""
    ffmpeg = ffmpeg_exe(ffmpeg)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    base = ["-loglevel", "warning", *_header_arg(headers),
            "-ss", f"{start}", "-t", f"{seconds}", "-i", "{src}",
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", "-f", "wav", "-y", str(out)]

    if not measure:
        code, err = run_ffmpeg(ffmpeg, [a.format(src=url) for a in base])
        return {"ok": code == 0, "exit": code, "output": str(out),
                "out_bytes": out.stat().st_size if out.exists() else 0,
                "err_tail": _tail(err) if code != 0 else []}

    meter = ByteMeter(url, extra_headers=headers)
    meter.start()
    try:
        code, err = run_ffmpeg(ffmpeg, [a.format(src=meter.url) for a in base])
        stats = summarize(meter.snapshot())
    finally:
        meter.stop()
    return {
        "ok": code == 0,
        "exit": code,
        "output": str(out),
        "out_bytes": out.stat().st_size if out.exists() else 0,
        "err_tail": _tail(err) if code != 0 else [],
        **stats,
    }


def _tail(err: str, n: int = 3) -> list[str]:
    lines = [ln for ln in err.strip().splitlines() if ln]
    return lines[-n:]


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} GB"

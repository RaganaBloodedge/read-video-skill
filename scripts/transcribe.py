"""把视频里的语音转成文字（本地离线，SenseVoiceSmall）。

用法：
    # 转写已有音频文件
    python transcribe.py clip.wav

    # 一步到位：直接给网页链接，自动解析出音频流
    python transcribe.py --url <网页链接> --seconds 90

    # 多段一起转（按先后顺序拼成完整文本）
    python transcribe.py --url <流地址> --segments 0:90 320:90 630:90 \
        --header "Referer: https://www.bilibili.com"

模型：SenseVoiceSmall int8（约 239 MB）+ tokens，首次运行自动下载到
~/.workbuddy/cache/read-video/models。默认从 hf-mirror.com 拉（国内快），
失败回退 huggingface.co。已存在且大小匹配就跳过下载。

为什么用 SenseVoice 而不是 Whisper：模型小一个量级、中文准、CPU 上快，
而且 dsh 的语音输入用的就是同一个模型，两边口径一致。
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolvers  # noqa: E402
import rvcore  # noqa: E402

MODEL_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
MODEL_REV = "2365baeacb507f821a0c8120fcee3d484dba7a07"
VAD_REPO = "csukuangfj/vad"
VAD_REV = "fba88cd2e921609e7675c3aaf51e0b9b295da4bc"

# 文件名 -> (相对仓库路径, 期望字节数)。字节数用来判断下载是否完整。
REQUIRED = {
    "model.int8.onnx": (f"{MODEL_REPO}/resolve/{MODEL_REV}/model.int8.onnx", 239233841),
    "tokens.txt": (f"{MODEL_REPO}/resolve/{MODEL_REV}/tokens.txt", 315894),
}
OPTIONAL = {
    "silero_vad.onnx": (f"{VAD_REPO}/resolve/{VAD_REV}/silero_vad.onnx", 1807522),
}
MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")

DEFAULT_MODEL_DIR = Path.home() / ".workbuddy" / "cache" / "read-video" / "models"
SAMPLE_RATE = 16000


# --------------------------------------------------------------------------
# 模型准备
# --------------------------------------------------------------------------
def _download(rel_path: str, dest: Path) -> bool:
    """按镜像顺序试下载。返回是否成功。"""
    import httpx

    for base in MIRRORS:
        url = f"{base}/{rel_path}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with httpx.Client(timeout=120.0, follow_redirects=True, trust_env=True) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        continue
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    with open(tmp, "wb") as fh:
                        for chunk in resp.iter_bytes(256 * 1024):
                            fh.write(chunk)
                            done += len(chunk)
                            if total and done % (16 * 1024 * 1024) < 256 * 1024:
                                pct = done * 100 // total
                                print(f"      {dest.name}: {pct}%", file=sys.stderr)
            tmp.replace(dest)
            return True
        except Exception as exc:  # noqa: BLE001 - 换镜像重试
            print(f"      {base} 失败：{exc}", file=sys.stderr)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    return False


def ensure_models(model_dir: Path, with_vad: bool = False) -> Path:
    """确保模型文件齐备；缺失则下载。返回模型目录。"""
    model_dir.mkdir(parents=True, exist_ok=True)
    wanted = dict(REQUIRED)
    if with_vad:
        wanted.update(OPTIONAL)

    for name, (rel, expect_bytes) in wanted.items():
        dest = model_dir / name
        # 大小对得上就当作完整（比对 SHA256 要读 239MB，太慢）
        if dest.exists() and dest.stat().st_size == expect_bytes:
            continue
        if dest.exists():
            print(f"    {name} 大小不符（{dest.stat().st_size} != {expect_bytes}），重新下载",
                  file=sys.stderr)
        else:
            print(f"    下载 {name}（{rvcore.human(expect_bytes)}）...", file=sys.stderr)
        if not _download(rel, dest):
            raise RuntimeError(
                f"无法下载 {name}。可手动放到 {model_dir}，或用 --model-dir 指定已有目录。"
            )
    return model_dir


# --------------------------------------------------------------------------
# 音频读取与识别
# --------------------------------------------------------------------------
def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """读 WAV 成 float32 单声道 [-1,1]。grab.py 产出的就是 16k 单声道 PCM16。"""
    with wave.open(str(path), "rb") as fh:
        channels = fh.getnchannels()
        width = fh.getsampwidth()
        rate = fh.getframerate()
        raw = fh.readframes(fh.getnframes())
    if width != 2:
        raise RuntimeError(f"只支持 16-bit PCM，收到 {width * 8}-bit（grab.py 的产物满足）")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def build_recognizer(model_dir: Path, language: str, threads: int):
    import sherpa_onnx

    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_dir / "model.int8.onnx"),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=threads,
        use_itn=True,          # 逆文本正则：数字、标点更像人写的
        language=language,     # auto / zh / en / ja / ko / yue
        debug=False,
    )


def transcribe_array(recognizer, samples: np.ndarray, rate: int,
                     window: float = 25.0) -> list[dict]:
    """按固定窗口切分后逐段识别。

    SenseVoice 单次最佳长度约 30 秒，长音频一次喂进去会掉效果，
    所以按 window 秒切片。切在词中间的风险存在，但比整段退化可控。
    """
    if rate != SAMPLE_RATE:
        raise RuntimeError(f"需要 {SAMPLE_RATE} Hz（grab.py 的产物满足），收到 {rate}")

    step = int(window * rate)
    segments: list[dict] = []
    total = len(samples)
    for start in range(0, total, step):
        chunk = samples[start : start + step]
        if len(chunk) < rate * 0.5:  # 尾巴不足半秒，丢弃
            break
        stream = recognizer.create_stream()
        stream.accept_waveform(rate, chunk)
        recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
        segments.append({
            "start": round(start / rate, 2),
            "end": round(min(start + step, total) / rate, 2),
            "text": text,
        })
    return segments


def parse_segments(items: list[str]) -> list[tuple[float, float]]:
    """把 '起始:时长' 解析成 [(start, seconds), ...]。"""
    out = []
    for it in items:
        if ":" in it:
            a, b = it.split(":", 1)
            out.append((float(a), float(b)))
        else:
            out.append((float(it), 90.0))
    return out


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="本地离线语音转文字（SenseVoice）")
    ap.add_argument("audio", nargs="?", help="待转写的 WAV 文件")
    ap.add_argument("--url", help="直接从该视频流地址截音频（免先落地）")
    ap.add_argument("--start", type=float, default=0.0, help="--url 模式的起始秒")
    ap.add_argument("--seconds", type=float, default=90.0, help="--url 模式的时长秒")
    ap.add_argument("--segments", nargs="+", default=[], metavar="起始:时长",
                    help="多段截取，如 --segments 0:90 320:90 630:90")
    ap.add_argument("--header", action="append", default=[], metavar="K: V",
                    help="附加 HTTP 头（CDN 校验来源时用，如 Referer）")
    ap.add_argument("--page", action="store_true",
                    help="输入是网页链接（如 bilibili），自动解析出音频流并带上所需请求头")
    ap.add_argument("--part", type=int, default=1, help="分 P 序号（网页链接有多 P 时用）")
    ap.add_argument("--model-dir", help=f"模型目录（默认 {DEFAULT_MODEL_DIR}）")
    ap.add_argument("--language", default="auto", help="auto/zh/en/ja/ko/yue")
    ap.add_argument("--window", type=float, default=25.0, help="分段窗口秒数")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()

    if not args.audio and not args.url:
        ap.error("要给 audio 文件路径，或 --url 直接截取")

    model_dir = Path(args.model_dir).expanduser() if args.model_dir else DEFAULT_MODEL_DIR
    print("[1/3] 准备模型...", file=sys.stderr)
    ensure_models(model_dir)

    tmp_files: list[Path] = []
    payload: dict = {"ok": False}

    try:
        print("[2/3] 准备音频...", file=sys.stderr)
        if args.url:
            headers = rvcore.parse_headers(args.header)
            media_url, info = resolvers.resolve_input(args.url, args.page, headers,
                                                      want="audio", part=args.part)
            if info:
                print(f"      [resolve] {info['title']} · {info['uploader']} · "
                      f"{info['duration']}s · audio id={info.get('audio_quality_id')}",
                      file=sys.stderr)
            spans = parse_segments(args.segments) if args.segments else [(args.start, args.seconds)]
            import tempfile
            tmpdir = Path(tempfile.mkdtemp(prefix="rv-asr-"))
            audios = []
            for i, (start, secs) in enumerate(spans, 1):
                dest = tmpdir / f"seg{i}.wav"
                res = rvcore.grab_audio(media_url, start, secs, dest,
                                        measure=True, headers=headers)
                if not res["ok"]:
                    raise RuntimeError(f"截取音频失败 @ {start}s: {res.get('err_tail')}")
                tmp_files.append(dest)
                audios.append((start, dest))
                print(f"      {start:g}s +{secs:g}s → {rvcore.human(res['out_bytes'])}"
                      f"（上游取 {rvcore.human(res['upstream_bytes'])}）", file=sys.stderr)
        else:
            audios = [(0.0, Path(args.audio))]  # type: ignore[list-item]

        print("[3/3] 识别中...", file=sys.stderr)
        recognizer = build_recognizer(model_dir, args.language, args.threads)

        all_segments: list[dict] = []
        offset = 0.0
        for start, path in audios:
            samples, rate = read_wav(path)
            segs = transcribe_array(recognizer, samples, rate, args.window)
            for s in segs:
                s["start"] = round(s["start"] + start, 2)
                s["end"] = round(s["end"] + start, 2)
            all_segments.extend(segs)
            offset += len(samples) / rate

        text = "".join(s["text"] for s in all_segments)
        chars = len([c for c in text if not c.isspace()])
        payload = {
            "ok": True,
            "text": text,
            "segments": all_segments,
            "audio_seconds": round(offset, 2),
            "chars": chars,
            "chars_per_second": round(chars / offset, 2) if offset else 0.0,
            "model": "SenseVoiceSmall-int8",
            "language": args.language,
        }
    finally:
        import shutil
        for f in tmp_files:
            try:
                f.unlink()
            except OSError:
                pass
        if tmp_files:
            shutil.rmtree(tmp_files[0].parent, ignore_errors=True)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if not payload["ok"]:
            print("识别失败", file=sys.stderr)
            return 1
        print(f"\n=== 转写结果（{payload['audio_seconds']:g} 秒音频）===\n")
        print(payload["text"])
        # 正常口播约 4~6 字/秒。低于 1 字/秒基本等于"没人说话" —— 此时转写没价值，
        # 该换策略去读画面，而不是把一句噪音当成视频内容。
        if payload["chars_per_second"] < 1.0:
            print(
                f"\n[提示] 每秒钟只识别出 {payload['chars_per_second']} 个字"
                f"（{payload['chars']} 字 / {payload['audio_seconds']:g} 秒）。\n"
                f"       这个视频很可能**没有口播**（纯音乐 / 游戏实况 / 无声演示）。\n"
                f"       转写对理解内容帮助有限，建议改用更密的抽帧做画面分析"
                f"（8~10 帧，见 SKILL.md 第三步）。"
            )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

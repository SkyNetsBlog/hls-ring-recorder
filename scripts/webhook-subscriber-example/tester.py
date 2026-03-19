#!/usr/bin/env python3
"""Subscribe to ntfy ring-segments topic, download segments, and transcribe to SRT."""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests not found. Install with: pip install requests")
    raise SystemExit(1)

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("faster-whisper not found. Install with: pip install faster-whisper")
    raise SystemExit(1)


def format_srt_time(seconds: float) -> str:
    """Convert float seconds to SRT timestamp format HH:MM:SS,mmm."""
    millis = int(round(seconds * 1000))
    h = millis // 3_600_000
    millis %= 3_600_000
    m = millis // 60_000
    millis %= 60_000
    s = millis // 1000
    ms = millis % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def download_segment(segment: str, nginx_url: str, output_dir: Path) -> Path:
    """Download a .ts segment from the nginx file server, normalise timestamps to
    start at zero (so VLC subtitle seek works), and return its local path."""
    url = f"{nginx_url}/{segment}"
    local_path = output_dir / segment
    local_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", url, "-c", "copy", "-f", "mpegts", str(local_path)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return local_path


def transcribe_segment(filepath: Path, model: WhisperModel) -> Path:
    """Transcribe a .ts file with faster-whisper and write an .srt alongside it."""
    print(filepath)
    segments_iter, _info = model.transcribe(str(filepath))
    srt_dir = filepath.parent
    srt_dir.mkdir(exist_ok=True)
    srt_path = srt_dir / filepath.with_suffix(".srt").name
    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, seg in enumerate(segments_iter, start=1):
            start = format_srt_time(seg.start)
            end = format_srt_time(seg.end)
            f.write(f"{idx}\n{start} --> {end}\n{seg.text.strip()}\n\n")
    return srt_path


def subscribe(args) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading Whisper model '{args.model}' on {args.device} ({args.compute_type})...")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)

    url = f"{args.ntfy_url}/ring-segments/json"
    print(f"Subscribing to {url} ...")

    with ThreadPoolExecutor(max_workers=2) as executor:
        while True:
            try:
                with requests.get(url, stream=True, timeout=None) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        raw = event.get("message", "")
                        if not raw:
                            continue
                        try:
                            payload = json.loads(raw)
                            segment = payload.get("segment", "?")
                            path = payload.get("path", "?")
                        except (json.JSONDecodeError, AttributeError):
                            segment = raw
                            path = ""
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        if path:
                            print(f"[{ts}] New segment: {segment}  ->  {path}")
                        else:
                            print(f"[{ts}] New segment: {segment}")

                        if segment == "?":
                            continue

                        nginx_url = args.nginx_url
                        future = executor.submit(
                            lambda s=segment: transcribe_segment(
                                download_segment(s, nginx_url, output_dir), model
                            )
                        )

                        def _done(fut, seg=segment):
                            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            exc = fut.exception()
                            if exc:
                                print(f"[{now}] Error processing {seg}: {exc}")
                            else:
                                print(f"[{now}] Transcribed: {fut.result().name}")

                        future.add_done_callback(_done)

            except KeyboardInterrupt:
                print("\nStopped.")
                return
            except Exception as exc:
                print(f"Connection error: {exc}. Reconnecting in 5s...")
                time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Subscribe to ntfy ring-segments and transcribe .ts segments to .srt"
    )
    parser.add_argument(
        "ntfy_url",
        nargs="?",
        default="http://192.168.178.96:30081",
        help="ntfy base URL (default: http://192.168.178.96:30081)",
    )
    parser.add_argument(
        "--nginx-url",
        default="http://192.168.178.96:30080",
        help="nginx base URL for segment downloads (default: http://192.168.178.96:30080)",
    )
    parser.add_argument(
        "--model",
        default="base",
        help="faster-whisper model size (default: base)",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="CTranslate2 device: cpu, cuda, auto (default: auto)",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="CTranslate2 compute type: int8, float16, float32 (default: int8, recommended for CPU)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).parent / "segments"),
        help="directory to save .ts and .srt files (default: <script-dir>/segments)",
    )
    subscribe(parser.parse_args())

# webhook-subscriber-example

A Python script that subscribes to the ntfy segment-completion event stream, downloads each new
`.ts` segment from nginx via ffmpeg, transcribes it with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), and writes a `.srt` subtitle file
alongside it.

This serves as a working example of how to consume the ring recorder's webhook notifications.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [ffmpeg](https://ffmpeg.org/download.html) on your `PATH`
- A running hls-ring-recorder deployment (see the root README)

## Setup

```bash
uv sync
```

## Usage

```bash
uv run tester.py [ntfy-url] [--nginx-url URL] [--model SIZE] [--output-dir DIR]
```

| Argument       | Default                       | Description                                                    |
| -------------- | ----------------------------- | -------------------------------------------------------------- |
| `ntfy_url`     | `http://192.168.178.96:30081` | ntfy base URL                                                  |
| `--nginx-url`  | `http://192.168.178.96:30080` | nginx base URL for segment downloads                           |
| `--model`      | `base`                        | Whisper model size: `tiny`, `base`, `small`, `medium`, `large` |
| `--device`     | `auto`                        | CTranslate2 device: `cpu`, `cuda`, `auto`                      |
| `--output-dir` | `./scripts/segments`          | Directory to save `.ts` and `.srt` files                       |

**Example:**

```bash
uv run tester.py http://<node-ip>:30081 --nginx-url http://<node-ip>:30080 --model small
```

## What it does

1. Connects to the ntfy event stream at `<ntfy-url>/ring-segments/json`
2. For each segment-completion event, downloads the `.ts` file from nginx using ffmpeg
   (which normalises timestamps to start at zero, so VLC subtitle seek works correctly)
3. Transcribes the segment in a background thread using faster-whisper
4. Writes a `.srt` file alongside the `.ts` in the output directory
5. Reconnects automatically if the stream drops

## Output

```
scripts/segments/
├── segment_042.ts
├── segment_042.srt
├── segment_043.ts
├── segment_043.srt
└── ...
```

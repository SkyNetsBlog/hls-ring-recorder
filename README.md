# HLS Ring Recorder

A Kubernetes pod that records an HLS stream into a circular buffer — capturing a fixed number of
segments and overwriting the oldest ones when the buffer is full. Think of it as a perpetual
dashcam for any HLS source.

The recorder uses ffmpeg's `-segment_wrap` to implement the ring:

```bash
ffmpeg -i "https://example.com/stream.m3u8" \
  -f segment \
  -segment_time 200 \
  -segment_wrap 400 \
  -segment_format mpegts \
  "segment_%03d.ts"
```

**Key options:**

- `-segment_time 200` — each segment is ~200 seconds long
- `-segment_wrap 400` — after segment 399, wraps back to segment 0, overwriting the oldest
- `-segment_format mpegts` — output stays as TS, lossless copy from the source

### Why segments instead of one big file?

The alternative would be a raw MPEG-TS stream written to a single file used as a ring buffer.
Keeping lots of smaller files is more useful here because random samples can be grabbed for
further analysis work, and it's simpler to serve over HTTP.

---

## Architecture

The pod runs three containers:

| Container | Image | Role |
|-----------|-------|------|
| `hls-ring-recorder` | custom (this repo) | ffmpeg recorder + webhook notifier |
| `ntfy` | `binwiederhier/ntfy` | segment-completion event bus |
| `nginx` | `openresty/openresty` | HTTP file server for recorded segments |

Segments are written to a PersistentVolumeClaim (`/data`) and served read-only by OpenResty.
When ffmpeg finishes writing a segment, the recorder POSTs a webhook to the ntfy sidecar, which
fans the notification out to any subscribers.

---

## Quick Start (Kubernetes)

### 1. First-time cluster setup

If you haven't already, install the local-path-provisioner storage class (required for PVCs to
bind on a bare-metal or single-node cluster):

```bash
make k8s-setup
```

This downloads the provisioner manifest into `k8s/vendor/`, applies it, and waits for it to
become ready.

### 2. Configure your image repo

In `Makefile`, replace `IMAGE_REPO` with your own Docker Hub repository. You'll need to create
the repository and set up a personal access token with read+write access.

### 3. Deploy

```bash
make deploy
```

`make deploy` builds and pushes the image, applies all Kubernetes manifests, and forces a pod
rollout so the new image is immediately pulled.

Run `make k8s-apply` alone if you only changed Makefile variables (no Dockerfile changes).
`make k8s-apply` auto-generates `k8s/deployment.yaml` from `k8s/deployment.tmpl.yaml` using
`envsubst` — the generated file is gitignored.

---

## Service Endpoints

Once deployed, the pod exposes two NodePorts. Replace `<node-ip>` with your cluster node's IP
(set via `HLS_STREAM` in the Makefile, e.g. `192.168.178.96`):

| Port | Service | URL |
|------|---------|-----|
| 30080 | OpenResty — recorded segments | `http://<node-ip>:30080/` |
| 30081 | ntfy — segment notifications | `http://<node-ip>:30081/` |

### Browsing recorded segments

Open `http://<node-ip>:30080/` in a browser to see an autoindexed directory of `.ts` files
written by ffmpeg. Files can be downloaded directly.

### Segment completion notifications (ntfy)

When ffmpeg finishes writing a segment, the recorder POSTs to the ntfy sidecar.
Subscribe to events from another machine:

```bash
# Stream events as newline-delimited JSON
curl -s http://<node-ip>:30081/ring-segments/json
```

Webhook payload:
```json
{"segment": "segment_001.ts", "path": "/data/segment_001.ts"}
```

To push notifications to your own service instead, set `WEBHOOK_URL` in
`k8s/deployment.tmpl.yaml` to your endpoint and redeploy.

### HLS source stream

The recorder consumes `http://<node-ip>:30900/hls/stream.m3u8` (configured via `HLS_STREAM` in
the Makefile). Change this to point at your own camera or HLS source.

---

## Tester Script (`scripts/webhook-subscriber-example/tester.py`)

A Python script that subscribes to the ntfy event stream, downloads each new segment from
nginx via ffmpeg, transcribes it using [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
and writes a `.srt` subtitle file alongside the downloaded `.ts`.

**Dependencies:**

```bash
pip install requests faster-whisper
```

(ffmpeg must also be on your `PATH`.)

**Usage:**

```bash
python scripts/webhook-subscriber-example/tester.py [ntfy-url] [--nginx-url URL] [--model SIZE] [--output-dir DIR]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `ntfy_url` | `http://192.168.178.96:30081` | ntfy base URL |
| `--nginx-url` | `http://192.168.178.96:30080` | nginx base URL for segment downloads |
| `--model` | `base` | faster-whisper model size (`tiny`, `base`, `small`, `medium`, `large`) |
| `--output-dir` | `./scripts/segments` | directory to save `.ts` and `.srt` files |

**Example:**

```bash
python scripts/webhook-subscriber-example/tester.py http://<node-ip>:30081 --nginx-url http://<node-ip>:30080 --model small
```

As each segment completes, the script downloads it, transcribes it in a background thread, and
prints the result. Downloaded segments and their `.srt` files land in `./scripts/segments/`.

---

## Configuration

Key variables and their defaults. Override on the command line: `make deploy SEGMENT_TIME=60`.

`FFMPEG_CFLAGS` is a build-time variable passed via `--build-arg`. The default (`-O3`) is safe
on any x86_64 CPU. To add CPU-specific tuning without editing the Makefile, create a gitignored
`config.local.mk` in the repo root:

```makefile
# config.local.mk  (not committed)
FFMPEG_CFLAGS = -O3 -march=skylake
```

The Makefile includes this file automatically if it exists (via `-include config.local.mk`).
You can also override on the command line: `make docker-build FFMPEG_CFLAGS="-O3 -march=x86-64-v3"`.

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_REPO` | `lbrtx01/hls-ring-recorder` | Docker Hub repository |
| `IMAGE_TAG` | git short SHA | Image tag (falls back to `latest` outside a git repo) |
| `HLS_STREAM` | `http://192.168.178.96:30900/hls/stream.m3u8` | HLS source URL |
| `SEGMENT_TIME` | `200` | Duration of each segment in seconds |
| `SEGMENT_WRAP` | `400` | Number of segments in the ring buffer — **max 999** (segment filenames use a 3-digit format) |
| `POLL_INTERVAL` | `15` | Seconds between stream availability checks |
| `BLANK_TIMEOUT` | `60` | Seconds of black screen before recording stops |
| `LUMA_THRESHOLD` | `15` | Minimum luma (0–255) for a frame to be considered non-black |
| `PVC_SIZE` | `10Gi` | Persistent volume size |
| `NODE_SELECTOR_KEY` | `kubernetes.io/hostname` | Node selector label key |
| `NODE_SELECTOR_VALUE` | `talos-k86-gbo` | Node selector label value |
| `TERMINATION_GRACE` | `15` | Seconds Kubernetes waits for the pod to exit cleanly before sending SIGKILL |

---

## Makefile Reference

| Target | Description |
|--------|-------------|
| `make deploy` | Build + push image, apply manifests, force rollout |
| `make k8s-apply` | Generate `k8s/deployment.yaml` and apply all manifests |
| `make k8s-setup` | Install local-path-provisioner storage class (first-time only) |
| `make k8s-rollout` | Restart the deployment and wait for rollout |
| `make k8s-logs` | Tail logs from all pod containers |
| `make k8s-uninstall` | Delete the `recorder` namespace and everything in it |
| `make docker-build` | Build image locally (no push, for local testing) |
| `make docker-push` | Build and push multi-arch image to Docker Hub |
| `make clean` | Remove generated `k8s/deployment.yaml` |

---

## Troubleshooting

### Force pod restart (pick up new image without rebuilding)

```bash
make k8s-rollout
```

### Check pod logs

```bash
make k8s-logs
```

### PVC not binding

Run `make k8s-setup` to ensure the local-path-provisioner is installed. On a single-node
cluster the provisioner needs a toleration for the control-plane taint — `k8s-setup` patches
this automatically.

---

## Uninstall

```bash
make k8s-uninstall
```

This deletes the entire `recorder` namespace. The PVC and its data are removed with it.

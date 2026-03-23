# HLS Ring Recorder

Always-on ring-buffer recorder for HLS streams — captures continuously, overwrites the oldest
segments automatically, and stops wasting disk on black screens. Point it at a camera, set a
buffer size, and forget it.

## Features

- **Ring buffer** — fixed segment count, oldest segment overwritten automatically; disk use is bounded forever
- **Black screen detection** — samples a single frame every poll interval; skips recording entirely when the stream is dark, resumes the moment content appears. No black `.ts` files, no wasted storage.
- **Atomic segment delivery** — segments land in `/data` only when fully written; nginx never serves a partial file
- **Content-MD5 on every segment** — sidecar `.md5` files written at close time; the sync script uses these for efficient incremental downloads without re-hashing
- **Webhook fan-out** — ntfy sidecar fires an event on each completed segment; plug in transcription, alerting, or anything else
- **Pre-built image** — `lbrtx01/hls-ring-recorder` on Docker Hub; no build step needed

---

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

| Container           | Image                 | Role                                   |
| ------------------- | --------------------- | -------------------------------------- |
| `hls-ring-recorder` | custom (this repo)    | ffmpeg recorder + webhook notifier     |
| `ntfy`              | `binwiederhier/ntfy`  | segment-completion event bus           |
| `nginx`             | `openresty/openresty` | HTTP file server for recorded segments |

Segments are written to a PersistentVolumeClaim (`/data`) and served read-only by OpenResty.
When ffmpeg finishes writing a segment, the recorder POSTs a webhook to the ntfy sidecar, which
fans the notification out to any subscribers.

---

## Quick Start (Docker Compose)

No checkout required — create a single `docker-compose.yml`, set one environment variable, and
start:

```yaml
services:
  recorder:
    image: lbrtx01/hls-ring-recorder:latest
    environment:
      HLS_STREAM: "${HLS_STREAM}"
      SEGMENT_TIME: "200"
      SEGMENT_WRAP: "400"
      WEBHOOK_URL: "http://ntfy:8081/ring-segments"
    volumes:
      - segments:/data
    depends_on:
      - ntfy

  ntfy:
    image: binwiederhier/ntfy:v2.19.2
    command: serve
    environment:
      NTFY_LISTEN_HTTP: ":8081"
    ports:
      - "8081:8081"

  nginx:
    image: openresty/openresty:1.29.2.1-noble
    ports:
      - "8080:8080"
    volumes:
      - segments:/data:ro
    configs:
      - source: nginx_config
        target: /etc/nginx/conf.d/default.conf
    depends_on:
      - recorder

volumes:
  segments:

configs:
  nginx_config:
    content: |
      server {
          listen 8080;
          root /data;
          access_log /dev/stdout;
          error_log /dev/stderr warn;
          autoindex on;
          autoindex_exact_size off;
          autoindex_localtime on;

          location ~* \.md5$ {
              return 404;
          }

          header_filter_by_lua_block {
              if ngx.req.get_method() ~= "HEAD" then
                  return
              end
              local path = ngx.var.request_filename
              local sf = io.open(path .. ".md5", "r")
              if sf then
                  local digest = sf:read("l")
                  sf:close()
                  if digest and #digest == 24 then
                      ngx.header["Content-MD5"] = digest
                      return
                  end
              end
              local f = io.open(path, "rb")
              if not f then return end
              local md5 = require "resty.md5"
              local m = md5:new()
              local ok, err = pcall(function()
                  while true do
                      local chunk = f:read(65536)
                      if not chunk then break end
                      m:update(chunk)
                  end
              end)
              f:close()
              if not ok then return end
              ngx.header["Content-MD5"] = ngx.encode_base64(m:final())
          }
      }
```

> The inline `configs.content` syntax requires Docker Compose v2.23.0 or later
> (`docker compose version`).

Then start:

```bash
export HLS_STREAM=http://<camera-ip>/hls/stream.m3u8
docker compose up -d
```

| Port | Service                          |
| ---- | -------------------------------- |
| 8080 | OpenResty — browse/download `.ts` segments |
| 8081 | ntfy — segment-completion events |

Subscribe to segment notifications:

```bash
curl -s http://localhost:8081/ring-segments/json
```

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

| Port  | Service                       | URL                       |
| ----- | ----------------------------- | ------------------------- |
| 30080 | OpenResty — recorded segments | `http://<node-ip>:30080/` |
| 30081 | ntfy — segment notifications  | `http://<node-ip>:30081/` |

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
{ "segment": "segment_001.ts", "path": "/data/segment_001.ts" }
```

To push notifications to your own service instead, set `WEBHOOK_URL` in
`k8s/deployment.tmpl.yaml` to your endpoint and redeploy.

### HLS source stream

The recorder consumes `http://<node-ip>:30900/hls/stream.m3u8` (configured via `HLS_STREAM` in
the Makefile). Change this to point at your own camera or HLS source.

---

## Scripts

Both scripts are managed as uv projects and run via Makefile targets. Set `NGINX_URL` and
`NTFY_URL` in `config.local.mk` so you don't have to pass them every time:

```makefile
# config.local.mk  (not committed)
NGINX_URL = http://<node-ip>:30080
NTFY_URL  = http://<node-ip>:30081
```

### Segment batch fetcher (`script-sync`)

Downloads all `.ts` segments currently on the nginx file server to a local directory, skipping
any files that are already up to date (compared by `Content-MD5`).

```bash
make script-sync                                  # saves to ./segments/
make script-sync SYNC_OUTPUT_DIR=/data/ring SYNC_WORKERS=4
```

| Variable          | Default                   | Description                               |
| ----------------- | ------------------------- | ----------------------------------------- |
| `NGINX_URL`       | _(required)_              | nginx base URL — `http://<node-ip>:30080` |
| `SYNC_OUTPUT_DIR` | `<project-root>/segments` | directory to save `.ts` files             |
| `SYNC_WORKERS`    | `1`                       | parallel download workers                 |

### Webhook subscriber / transcriber (`script-subscribe`)

Subscribes to the ntfy event stream, downloads each new segment from nginx, transcribes it
using [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and writes a `.srt` subtitle
file alongside the downloaded `.ts`. ffmpeg must be on your `PATH`.

```bash
make script-subscribe
make script-subscribe WHISPER_MODEL=small
```

| Variable        | Default      | Description                                                            |
| --------------- | ------------ | ---------------------------------------------------------------------- |
| `NTFY_URL`      | _(required)_ | ntfy base URL — `http://<node-ip>:30081`                               |
| `NGINX_URL`     | _(required)_ | nginx base URL — `http://<node-ip>:30080`                              |
| `WHISPER_MODEL` | `base`       | faster-whisper model size (`tiny`, `base`, `small`, `medium`, `large`) |

Output lands in `scripts/webhook-subscriber-example/segments/` by default.

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

**Deployment variables** — used by `make deploy` / `make k8s-apply`:

| Variable              | Default                                       | Description                                                                                  |
| --------------------- | --------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `IMAGE_REPO`          | `lbrtx01/hls-ring-recorder`                   | Docker Hub repository                                                                        |
| `IMAGE_TAG`           | git short SHA                                 | Image tag (falls back to `latest` outside a git repo)                                        |
| `HLS_STREAM`          | `http://192.168.178.96:30900/hls/stream.m3u8` | HLS source URL                                                                               |
| `SEGMENT_TIME`        | `200`                                         | Duration of each segment in seconds                                                          |
| `SEGMENT_WRAP`        | `400`                                         | Number of segments in the ring buffer — **max 999** (segment filenames use a 3-digit format) |
| `POLL_INTERVAL`       | `15`                                          | Seconds between stream availability checks                                                   |
| `BLANK_TIMEOUT`       | `60`                                          | Seconds of continuous black screen before recording stops (see Black screen detection)       |
| `LUMA_THRESHOLD`      | `15`                                          | Minimum luma (0–255) for a frame to be considered non-black; single pixel sampled per check  |
| `PVC_SIZE`            | `10Gi`                                        | Persistent volume size                                                                       |
| `NODE_SELECTOR_KEY`   | `kubernetes.io/hostname`                      | Node selector label key                                                                      |
| `NODE_SELECTOR_VALUE` | `talos-k86-gbo`                               | Node selector label value                                                                    |
| `TERMINATION_GRACE`   | `15`                                          | Seconds Kubernetes waits for the pod to exit cleanly before sending SIGKILL                  |

**Script variables** — used by `make script-sync` / `make script-subscribe`:

| Variable          | Default                   | Description                                 |
| ----------------- | ------------------------- | ------------------------------------------- |
| `NGINX_URL`       | _(required)_              | nginx base URL — `http://<node-ip>:30080`   |
| `NTFY_URL`        | _(required)_              | ntfy base URL — `http://<node-ip>:30081`    |
| `SYNC_OUTPUT_DIR` | `<project-root>/segments` | output directory for `script-sync`          |
| `SYNC_WORKERS`    | `1`                       | parallel download workers for `script-sync` |
| `WHISPER_MODEL`   | `base`                    | faster-whisper model for `script-subscribe` |

---

## Formatting & Linting

`make format` runs four formatters across the repo. `make lint` runs ruff and shellcheck.
The Python tools (ruff) are installed automatically via uv. The other system tools need to be
installed once:

**macOS**

```bash
brew install shfmt yamlfmt prettier shellcheck
```

**Linux** (Debian/Ubuntu)

```bash
sudo apt install shfmt shellcheck
npm install -g prettier
go install github.com/google/yamlfmt/cmd/yamlfmt@latest
```

| Formatter     | Scope                                                                          |
| ------------- | ------------------------------------------------------------------------------ |
| `ruff format` | `scripts/segment-batch-fetcher/` and `scripts/webhook-subscriber-example/`     |
| `shfmt`       | `docker/entrypoint.sh`                                                         |
| `yamlfmt`     | `k8s/*.tmpl.yaml` and static manifests (excludes generated and vendored files) |
| `prettier`    | all `*.md` files (respects `.gitignore`, so `.venv/` dirs are skipped)         |

---

## Makefile Reference

| Target                  | Description                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `make deploy`           | Build + push image, apply manifests, force rollout                                                                                   |
| `make k8s-apply`        | Generate `k8s/deployment.yaml` and apply all manifests                                                                               |
| `make k8s-setup`        | Install local-path-provisioner storage class (first-time only)                                                                       |
| `make k8s-rollout`      | Restart the deployment and wait for rollout                                                                                          |
| `make k8s-logs`         | Tail logs from all pod containers                                                                                                    |
| `make k8s-uninstall`    | Delete the `recorder` namespace and everything in it                                                                                 |
| `make docker-build`     | Build image locally (no push, for local testing)                                                                                     |
| `make docker-push`      | Build and push multi-arch image to Docker Hub                                                                                        |
| `make format`           | Auto-format Python (ruff), shell (shfmt), YAML (yamlfmt), and Markdown (prettier) — see [Formatting & Linting](#formatting--linting) |
| `make lint`             | Lint Python (ruff check) and shell (shellcheck)                                                                                      |
| `make clean`            | Remove generated `k8s/deployment.yaml`                                                                                               |
| `make script-sync`      | Download `.ts` segments from nginx to `SYNC_OUTPUT_DIR`                                                                              |
| `make script-subscribe` | Subscribe to ntfy and transcribe segments with faster-whisper                                                                        |
| `make script-test`      | Run unit tests for the segment batch fetcher                                                                                         |

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

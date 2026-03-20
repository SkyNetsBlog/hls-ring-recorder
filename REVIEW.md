# Code Review

Overall this is a well-structured, production-conscious project with good security hardening and a clean sidecar pattern. Issues below are organized by severity.

---

## High

**1. `atomize_segments` recovery writes wrong `.next_segment` for multiple partial files**
`docker/entrypoint.sh:22-29`

Shell glob order is not guaranteed to be ascending. If `/data/.tmp` has `segment_005.ts` and `segment_006.ts`, the loop might process `006` then `005`, leaving `.next_segment` containing `6` instead of `7`. The last file processed — not the largest — wins.

**2. `ntfy` container may fail to bind port 80 under `drop: ALL`**
`k8s/deployment.tmpl.yaml:83-90`

The ntfy container drops ALL capabilities and runs as non-root (uid 1000). Binding to port 80 (< 1024) requires either `NET_BIND_SERVICE` or `net.ipv4.ip_unprivileged_port_start=0` at the kernel level. On a hardened cluster this will fail silently at startup with a "permission denied" error, breaking the webhook pipeline. Consider configuring ntfy to listen on port 8080+ via `NTFY_LISTEN_HTTP=:8081` or restoring `NET_BIND_SERVICE` for that container only.

**3. Nginx MD5 cache key collision for same-size ring buffer rewrites**
`k8s/nginx-config.yaml:27-30`

The cache key is `path .. ":" .. size`. When the ring buffer wraps and overwrites `segment_001.ts` with new content that happens to be the same byte size as the previous recording, the cached MD5 will be stale for up to 600 seconds. `sync.py` will skip re-downloading a changed segment, silently diverging local copies from the server.

Using `mtime` (via `lfs`) or an incrementing write counter in the key would solve this. Alternatively, shortening the TTL reduces the window.

---

## Medium

**4. `trap` is re-registered inside the main loop**
`docker/entrypoint.sh:128`

The `trap` line runs on every iteration of `while true`. The trap captures `$PROGRESS_FILE` and `$LUMA_FILE` by value at the time of registration, so an early iteration's trap could reference stale paths. Move the trap before the loop, or update it each iteration with that caveat documented.

**5. Blocking MD5 computation in nginx `header_filter_by_lua_block`**
`k8s/nginx-config.yaml:19-49`

The Lua block runs synchronously in the nginx header filter phase. For a 200MB segment, computing MD5 can take hundreds of milliseconds, blocking that nginx worker for all concurrent requests. Under parallel `sync.py` downloads with `--workers > 1`, multiple HEAD requests could pile up. Worth noting as a known limitation.

**6. Lua file handle not closed on error**
`k8s/nginx-config.yaml:24-46`

If `m:update(chunk)` or `m:final()` throws (possible under memory pressure), `f:close()` is never reached. The MD5 computation should be wrapped in `pcall` with a `finally`-style close.

**7. `ThreadPoolExecutor` hangs on `KeyboardInterrupt` in `tester.py`**
`scripts/webhook-subscriber-example/tester.py:125-127`

Catching `KeyboardInterrupt` returns from `subscribe()`, triggering the `with ThreadPoolExecutor` context manager's `shutdown(wait=True)`. If an ffmpeg transcription subprocess is running, this blocks for up to 300 seconds (the `subprocess.run` timeout). Use `executor.shutdown(cancel_futures=True)` for a clean exit.

---

## Low

**8. Recovered segments are not webhook-notified**
`docker/entrypoint.sh:25`

Segments recovered from `.tmp` on startup are moved directly to `/data` without triggering `notify_on_segment`, which watches `moved_to` events on `/data`. If downstream consumers need to process recovered segments, they will miss them silently.

**9. Dockerfile missing `--no-install-recommends`**
`docker/Dockerfile:35-42`

The runtime stage's `apt-get install` does not pass `--no-install-recommends`, pulling in additional recommended packages and increasing image size unnecessarily.

**10. `-march=skylake` default crashes on other x86_64 CPUs**
`docker/Dockerfile:5`, `Makefile:14`

The default `FFMPEG_CFLAGS="-O3 -march=skylake"` produces an ffmpeg binary that raises `SIGILL` on any x86_64 machine without AVX2/skylake features (e.g., older Xeons, some VMs). This is intentional for the target hardware, but should be documented prominently in the README.

**11. Unbounded background subshells for webhook notifications**
`docker/entrypoint.sh:49-56`

Each segment spawns a `( curl ... ) &` subshell with no concurrency limit. At the 200-second default segment time this is negligible, but if `SEGMENT_TIME` is set very low or the webhook endpoint is consistently slow, subshells could accumulate. A named FIFO with a single worker would be more robust.

**12. Hardcoded private IPs in `tester.py` defaults**
`scripts/webhook-subscriber-example/tester.py:141,146`

`http://192.168.178.96:30081` and `:30080` are hardcoded as CLI defaults. Since this is an example script the addresses are expected to be changed, but a note in the `--help` text pointing to the Makefile's `HLS_STREAM` pattern would reduce first-run confusion.

---

## Summary

| # | Severity | File | Issue |
|---|----------|------|-------|
| 1 | High | `entrypoint.sh:22` | Recovery writes wrong `.next_segment` when multiple partials exist |
| 2 | High | `deployment.tmpl.yaml:83` | ntfy may fail to bind port 80 without `NET_BIND_SERVICE` |
| 3 | High | `nginx-config.yaml:27` | MD5 cache key collides on same-size ring buffer rewrites |
| 4 | Medium | `entrypoint.sh:128` | `trap` re-registered each loop iteration |
| 5 | Medium | `nginx-config.yaml:19` | Blocking MD5 in nginx header phase |
| 6 | Medium | `nginx-config.yaml:24` | Lua file handle leak on error |
| 7 | Medium | `tester.py:125` | Executor hangs 300s on KeyboardInterrupt |
| 8 | Low | `entrypoint.sh:25` | Recovered segments not webhook-notified |
| 9 | Low | `Dockerfile:35` | Missing `--no-install-recommends` |
| 10 | Low | `Dockerfile:5` | `-march=skylake` undocumented CPU constraint |
| 11 | Low | `entrypoint.sh:49` | Unbounded webhook subshells |
| 12 | Low | `tester.py:141` | Hardcoded private IPs in defaults |

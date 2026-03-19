# Improvements

Ordered by value — highest impact first.

---

## 9. ~~`stream_has_content` spawns a full ffmpeg decode on every poll~~

> **Won't fix** — the luma check is intentional. The stream is available most of the time but
> is predominantly a black screen; the recorder should only capture when there is actual content.
> A lightweight HTTP/playlist check would not detect a live-but-black stream, so the ffmpeg
> decode is the correct approach.

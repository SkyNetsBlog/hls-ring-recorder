import argparse
import base64
import hashlib
import mmap
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from tqdm import tqdm

_PRIDE = ["\033[91m", "\033[38;5;208m", "\033[93m", "\033[92m", "\033[94m", "\033[95m"]
_RESET = "\033[0m"


class PrideTqdm(tqdm):
    @staticmethod
    def format_meter(n, total, elapsed, **kwargs):
        s = tqdm.format_meter(n, total, elapsed, **kwargs)
        result = []
        color_idx = 0
        for ch in s:
            if ch == "█":
                result.append(f"{_PRIDE[color_idx % len(_PRIDE)]}█{_RESET}")
                color_idx += 1
            else:
                result.append(ch)
        return "".join(result)


def _make_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1,  # waits 1s, 2s, 4s between attempts
        status_forcelist=[502, 503, 504],
        allowed_methods={"HEAD", "GET"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_ts_urls(url, session):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return [
        urljoin(url, a["href"]) for a in soup.find_all("a", href=True) if a["href"].endswith(".ts")
    ]


def remote_checksum(url, session):
    """Return a checksum string from the server via HEAD request.

    Uses Content-MD5 if present.
    Returns None if absent or if the request fails.
    """
    try:
        r = session.head(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException:
        return None
    if "Content-MD5" in r.headers:
        return base64.b64decode(r.headers["Content-MD5"]).hex()
    return None


def local_checksum(filepath):
    md5 = hashlib.md5()
    if os.path.getsize(filepath) == 0:
        return md5.hexdigest()
    with open(filepath, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            md5.update(mm)
    return md5.hexdigest()


class PositionPool:
    """Thread-safe pool of tqdm bar positions (0..size-1)."""

    def __init__(self, size):
        self._lock = threading.Lock()
        self._free = list(range(size))

    def acquire(self):
        with self._lock:
            return self._free.pop()

    def release(self, pos):
        with self._lock:
            self._free.append(pos)


def sync_file(file_url, output_dir, pos_pool, session):
    filename = os.path.basename(urlparse(file_url).path)
    filepath = os.path.join(output_dir, filename)
    try:
        remote = remote_checksum(file_url, session)
        if remote and os.path.exists(filepath) and local_checksum(filepath) == remote:
            tqdm.write(f"  {filename} (up to date)")
            return

        pos = pos_pool.acquire()
        try:
            with session.get(file_url, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0)) or None
                with PrideTqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    desc=filename,
                    bar_format="{l_bar}{bar}| {n_fmt:>7}/{total_fmt:<7} [{elapsed:>5}<{remaining:<5}, {rate_fmt:>10}]",
                    position=pos,
                    leave=True,
                ) as bar:
                    with open(filepath, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                            bar.update(len(chunk))
        finally:
            pos_pool.release(pos)
    except Exception as e:
        tqdm.write(f"  {filename}: error — {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync .ts segments from the nginx file server to a local directory."
    )
    parser.add_argument("url", help="base URL of the nginx segment listing")
    parser.add_argument(
        "output_dir", metavar="output-dir", help="local directory to sync segments into"
    )
    parser.add_argument(
        "--workers", type=int, default=1, metavar="N", help="parallel download workers (default: 1)"
    )
    args = parser.parse_args()

    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    session = _make_session()

    print(f"Fetching file list from {args.url} ...")
    ts_urls = fetch_ts_urls(args.url, session)
    print(f"Found {len(ts_urls)} .ts file(s)")

    pos_pool = PositionPool(args.workers)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(sync_file, url, output_dir, pos_pool, session): url for url in ts_urls
        }
        for future in as_completed(futures):
            exc = future.exception()
            if exc:
                filename = os.path.basename(urlparse(futures[future]).path)
                tqdm.write(f"  {filename}: unhandled error — {exc}")

    print(f"Done. Files saved to {output_dir}")


if __name__ == "__main__":
    main()

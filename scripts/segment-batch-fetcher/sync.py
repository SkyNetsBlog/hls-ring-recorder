import argparse
import base64
import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from tqdm import tqdm


_CHECKSUM_CHUNK_SIZE = 65536  # 64 KiB


class Manifest:
    def __init__(self, output_dir):
        self._path = os.path.join(output_dir, ".sync-manifest.json")
        self._lock = threading.Lock()
        try:
            with open(self._path) as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def get(self, filename):
        with self._lock:
            return self._data.get(filename)

    def set(self, filename, checksum):
        with self._lock:
            self._data[filename] = checksum
            self._save()

    def _save(self):
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f)
        os.replace(tmp, self._path)


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
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(_CHECKSUM_CHUNK_SIZE), b""):
            md5.update(chunk)
    return md5.hexdigest()


def sync_file(file_url, output_dir, session, manifest):
    filename = os.path.basename(urlparse(file_url).path)
    filepath = os.path.join(output_dir, filename)
    try:
        remote = remote_checksum(file_url, session)
        if remote:
            # One-time migration: populate manifest for files already on disk
            if manifest.get(filename) is None and os.path.exists(filepath):
                manifest.set(filename, local_checksum(filepath))
            if manifest.get(filename) == remote and os.path.exists(filepath):
                tqdm.write(f"  {filename} (up to date)")
                return
        with session.get(file_url, stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            content_length = int(r.headers.get("content-length", 0)) or None
            with open(filepath, "wb") as f:
                with tqdm(
                    total=content_length,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=filename,
                    leave=False,
                ) as file_bar:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        file_bar.update(len(chunk))
        manifest.set(filename, local_checksum(filepath))
        tqdm.write(f"  {filename} (updated)")
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
    manifest = Manifest(output_dir)

    print(f"Fetching file list from {args.url} ...")
    ts_urls = fetch_ts_urls(args.url, session)
    print(f"Found {len(ts_urls)} .ts file(s)")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(sync_file, url, output_dir, session, manifest): url for url in ts_urls}
        with tqdm(total=len(ts_urls), unit="file", position=0, colour="green") as bar:
            for future in as_completed(futures):
                bar.update(1)
                exc = future.exception()
                if exc:
                    filename = os.path.basename(urlparse(futures[future]).path)
                    tqdm.write(f"  {filename}: unhandled error — {exc}")

    print(f"Done. Files saved to {output_dir}")


if __name__ == "__main__":
    main()

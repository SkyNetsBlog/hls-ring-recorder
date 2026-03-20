import base64
import hashlib
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sync import (
    PositionPool,
    _make_session,
    fetch_ts_urls,
    local_checksum,
    remote_checksum,
    sync_file,
)

BASE_URL = "http://example.com/segments/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _md5_header(data: bytes) -> dict:
    return {"Content-MD5": _md5_b64(data)}


# ---------------------------------------------------------------------------
# fetch_ts_urls
# ---------------------------------------------------------------------------

def test_fetch_ts_urls_filters_ts_only(requests_mock):
    html = (
        '<a href="seg001.ts">seg</a>'
        '<a href="index.m3u8">m3u8</a>'
        '<a href="page.html">html</a>'
    )
    requests_mock.get(BASE_URL, text=html)
    result = fetch_ts_urls(BASE_URL, requests.Session())
    assert result == [BASE_URL + "seg001.ts"]


def test_fetch_ts_urls_resolves_relative_hrefs(requests_mock):
    requests_mock.get(BASE_URL, text='<a href="segment_001.ts">s</a>')
    result = fetch_ts_urls(BASE_URL, requests.Session())
    assert result == [BASE_URL + "segment_001.ts"]


def test_fetch_ts_urls_empty_page(requests_mock):
    requests_mock.get(BASE_URL, text="<html></html>")
    result = fetch_ts_urls(BASE_URL, requests.Session())
    assert result == []


def test_fetch_ts_urls_http_error_raises(requests_mock):
    requests_mock.get(BASE_URL, status_code=404)
    with pytest.raises(requests.HTTPError):
        fetch_ts_urls(BASE_URL, requests.Session())


# ---------------------------------------------------------------------------
# remote_checksum
# ---------------------------------------------------------------------------

def test_remote_checksum_header_present(requests_mock):
    data = b"hello world"
    requests_mock.head(BASE_URL + "seg.ts", headers=_md5_header(data))
    result = remote_checksum(BASE_URL + "seg.ts", requests.Session())
    assert result == _md5_hex(data)


def test_remote_checksum_header_absent(requests_mock):
    requests_mock.head(BASE_URL + "seg.ts", status_code=200)
    result = remote_checksum(BASE_URL + "seg.ts", requests.Session())
    assert result is None


def test_remote_checksum_connection_error_returns_none(requests_mock):
    requests_mock.head(BASE_URL + "seg.ts", exc=requests.ConnectionError)
    result = remote_checksum(BASE_URL + "seg.ts", requests.Session())
    assert result is None


def test_remote_checksum_502_returns_none(requests_mock):
    requests_mock.head(BASE_URL + "seg.ts", status_code=502)
    result = remote_checksum(BASE_URL + "seg.ts", requests.Session())
    assert result is None


# ---------------------------------------------------------------------------
# local_checksum
# ---------------------------------------------------------------------------

def test_local_checksum_known_content(tmp_path):
    data = b"test content"
    f = tmp_path / "test.ts"
    f.write_bytes(data)
    assert local_checksum(str(f)) == _md5_hex(data)


def test_local_checksum_empty_file(tmp_path):
    f = tmp_path / "empty.ts"
    f.write_bytes(b"")
    assert local_checksum(str(f)) == "d41d8cd98f00b204e9800998ecf8427e"


# ---------------------------------------------------------------------------
# PositionPool
# ---------------------------------------------------------------------------

def test_position_pool_acquire_all_values():
    pool = PositionPool(3)
    acquired = {pool.acquire() for _ in range(3)}
    assert acquired == {0, 1, 2}


def test_position_pool_release_restores_value():
    pool = PositionPool(3)
    pos = pool.acquire()
    pool.release(pos)
    assert pool.acquire() == pos


def test_position_pool_lifo_order():
    pool = PositionPool(3)
    # drain the pool
    pool.acquire()
    pool.acquire()
    pool.acquire()
    pool.release(1)
    pool.release(0)
    # LIFO: last released (0) is returned first
    assert pool.acquire() == 0


def test_position_pool_thread_safety():
    pool = PositionPool(10)
    in_use = set()
    lock = threading.Lock()
    errors = []

    def worker():
        pos = pool.acquire()
        with lock:
            if pos in in_use:
                errors.append(f"duplicate position: {pos}")
            in_use.add(pos)
        # simulate a small amount of work while holding the position
        with lock:
            in_use.discard(pos)
        pool.release(pos)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


# ---------------------------------------------------------------------------
# sync_file
# ---------------------------------------------------------------------------

def _mock_pride_tqdm(mock):
    """Wire up a PrideTqdm mock to act as a well-behaved context manager."""
    bar = MagicMock()
    mock.return_value.__enter__ = MagicMock(return_value=bar)
    mock.return_value.__exit__ = MagicMock(return_value=False)


@patch("sync.PrideTqdm")
def test_sync_file_absent_downloads(mock_pt, requests_mock, tmp_path):
    _mock_pride_tqdm(mock_pt)
    data = b"segment data"
    url = BASE_URL + "seg001.ts"
    requests_mock.head(url, headers=_md5_header(data))
    requests_mock.get(url, content=data)

    sync_file(url, str(tmp_path), PositionPool(1), requests.Session())

    assert (tmp_path / "seg001.ts").read_bytes() == data


@patch("sync.PrideTqdm")
def test_sync_file_up_to_date_skips_get(mock_pt, requests_mock, tmp_path):
    _mock_pride_tqdm(mock_pt)
    data = b"existing segment"
    url = BASE_URL + "seg002.ts"
    (tmp_path / "seg002.ts").write_bytes(data)
    requests_mock.head(url, headers=_md5_header(data))

    sync_file(url, str(tmp_path), PositionPool(1), requests.Session())

    # Only the HEAD request should have been issued
    assert requests_mock.call_count == 1


@patch("sync.PrideTqdm")
def test_sync_file_checksum_mismatch_redownloads(mock_pt, requests_mock, tmp_path):
    _mock_pride_tqdm(mock_pt)
    old_data = b"old content"
    new_data = b"new content"
    url = BASE_URL + "seg003.ts"
    (tmp_path / "seg003.ts").write_bytes(old_data)
    requests_mock.head(url, headers=_md5_header(new_data))
    requests_mock.get(url, content=new_data)

    sync_file(url, str(tmp_path), PositionPool(1), requests.Session())

    assert (tmp_path / "seg003.ts").read_bytes() == new_data


@patch("sync.PrideTqdm")
def test_sync_file_no_remote_checksum_downloads(mock_pt, requests_mock, tmp_path):
    _mock_pride_tqdm(mock_pt)
    data = b"segment without checksum"
    url = BASE_URL + "seg004.ts"
    requests_mock.head(url, status_code=200)  # no Content-MD5
    requests_mock.get(url, content=data)

    sync_file(url, str(tmp_path), PositionPool(1), requests.Session())

    assert (tmp_path / "seg004.ts").read_bytes() == data


@patch("sync.PrideTqdm")
def test_sync_file_get_error_does_not_raise(mock_pt, requests_mock, tmp_path):
    _mock_pride_tqdm(mock_pt)
    url = BASE_URL + "seg005.ts"
    requests_mock.head(url, status_code=200)  # no checksum → triggers download
    requests_mock.get(url, status_code=500)

    # Must not raise; error is swallowed and logged via tqdm.write
    sync_file(url, str(tmp_path), PositionPool(1), requests.Session())


# ---------------------------------------------------------------------------
# _make_session
# ---------------------------------------------------------------------------

def test_make_session_returns_requests_session():
    assert isinstance(_make_session(), requests.Session)


def test_make_session_retry_total():
    adapter = _make_session().get_adapter("http://")
    assert adapter.max_retries.total == 4


def test_make_session_backoff_factor():
    adapter = _make_session().get_adapter("http://")
    assert adapter.max_retries.backoff_factor == 1


def test_make_session_status_forcelist():
    adapter = _make_session().get_adapter("http://")
    assert set(adapter.max_retries.status_forcelist) == {502, 503, 504}


def test_make_session_both_schemes_mounted():
    session = _make_session()
    assert isinstance(session.get_adapter("http://"), HTTPAdapter)
    assert isinstance(session.get_adapter("https://"), HTTPAdapter)

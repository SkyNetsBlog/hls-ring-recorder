import base64
import hashlib
import os
import sys
from unittest.mock import patch

import pytest
import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sync import (
    Manifest,
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
    html = '<a href="seg001.ts">seg</a><a href="index.m3u8">m3u8</a><a href="page.html">html</a>'
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
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_missing_file_starts_empty(tmp_path):
    m = Manifest(str(tmp_path))
    assert m.get("seg001.ts") is None


def test_manifest_loads_existing(tmp_path):
    import json
    manifest_path = tmp_path / ".sync-manifest.json"
    manifest_path.write_text(json.dumps({"seg001.ts": "abc123"}))
    m = Manifest(str(tmp_path))
    assert m.get("seg001.ts") == "abc123"


def test_manifest_set_persists(tmp_path):
    m = Manifest(str(tmp_path))
    m.set("seg001.ts", "deadbeef")
    m2 = Manifest(str(tmp_path))
    assert m2.get("seg001.ts") == "deadbeef"


def test_manifest_corrupted_json_starts_empty(tmp_path):
    (tmp_path / ".sync-manifest.json").write_text("not valid json{{")
    m = Manifest(str(tmp_path))
    assert m.get("seg001.ts") is None


# ---------------------------------------------------------------------------
# sync_file
# ---------------------------------------------------------------------------


def test_sync_file_absent_downloads(requests_mock, tmp_path):
    data = b"segment data"
    url = BASE_URL + "seg001.ts"
    requests_mock.head(url, headers=_md5_header(data))
    requests_mock.get(url, content=data)
    manifest = Manifest(str(tmp_path))

    sync_file(url, str(tmp_path), requests.Session(), manifest)

    assert (tmp_path / "seg001.ts").read_bytes() == data
    assert manifest.get("seg001.ts") == _md5_hex(data)


def test_sync_file_up_to_date_skips_get(requests_mock, tmp_path):
    data = b"existing segment"
    url = BASE_URL + "seg002.ts"
    (tmp_path / "seg002.ts").write_bytes(data)
    requests_mock.head(url, headers=_md5_header(data))
    manifest = Manifest(str(tmp_path))
    manifest.set("seg002.ts", _md5_hex(data))

    sync_file(url, str(tmp_path), requests.Session(), manifest)

    # Only the HEAD request should have been issued
    assert requests_mock.call_count == 1


def test_sync_file_checksum_mismatch_redownloads(requests_mock, tmp_path):
    old_data = b"old content"
    new_data = b"new content"
    url = BASE_URL + "seg003.ts"
    (tmp_path / "seg003.ts").write_bytes(old_data)
    requests_mock.head(url, headers=_md5_header(new_data))
    requests_mock.get(url, content=new_data)
    manifest = Manifest(str(tmp_path))

    sync_file(url, str(tmp_path), requests.Session(), manifest)

    assert (tmp_path / "seg003.ts").read_bytes() == new_data


def test_sync_file_no_remote_checksum_downloads(requests_mock, tmp_path):
    data = b"segment without checksum"
    url = BASE_URL + "seg004.ts"
    requests_mock.head(url, status_code=200)  # no Content-MD5
    requests_mock.get(url, content=data)
    manifest = Manifest(str(tmp_path))

    sync_file(url, str(tmp_path), requests.Session(), manifest)

    assert (tmp_path / "seg004.ts").read_bytes() == data


def test_sync_file_get_error_does_not_raise(requests_mock, tmp_path):
    url = BASE_URL + "seg005.ts"
    requests_mock.head(url, status_code=200)  # no checksum → triggers download
    requests_mock.get(url, status_code=500)
    manifest = Manifest(str(tmp_path))

    # Must not raise; error is swallowed and logged via tqdm.write
    sync_file(url, str(tmp_path), requests.Session(), manifest)


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

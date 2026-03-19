# segment-batch-fetcher

A script that fetches `.ts` segment files from an HLS directory listing and downloads them to a local folder. Files that are already up to date (matched by checksum) are skipped.

## Requirements

- [uv](https://docs.astral.sh/uv/)

## Usage

```sh
uv run src/sync.py <url> <output-folder>
```

**Example:**

```sh
uv run src/sync.py http://192.168.178.96:30080/ ./tmp/segments
```

`uv` will automatically create an isolated virtual environment and install dependencies on first run.

## How it works

1. Fetches the HTML directory listing at `<url>` and extracts all `.ts` file links
2. For each file, issues a `HEAD` request to check the remote checksum (`Content-MD5` or `ETag`)
3. If a local copy exists and the checksum matches, the file is skipped
4. Otherwise, the file is downloaded with a progress bar

## Development

Install dependencies into a local virtualenv:

```sh
uv sync
```

Run the script:

```sh
uv run src/sync.py <url> <output-folder>
```

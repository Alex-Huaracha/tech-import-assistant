"""Download the documents listed in sources.toml into data/raw/.

The files are saved byte for byte, without decoding: the server sometimes
declares the wrong encoding, so decoding is ingest.py's job, using the
encoding recorded in the catalog.

A manifest (data/raw/manifest.json) records when each file was fetched and its
SHA-256 hash. Re-downloading with --force and comparing hashes tells you
whether SUNAT changed a document since the last run.

Usage:
    uv run python -m tech_import_assistant.indexing.download           # only missing files
    uv run python -m tech_import_assistant.indexing.download --force   # re-download everything
"""

import argparse
import hashlib
import json
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tech_import_assistant.config import settings
from tech_import_assistant.schemas import Source

MANIFEST_FILE = settings.raw_dir / "manifest.json"


def load_sources() -> list[Source]:
    """Read and validate the catalog; a typo in sources.toml fails here, loudly."""
    with settings.sources_file.open("rb") as f:
        return [Source.model_validate(entry) for entry in tomllib.load(f)["source"]]


def raw_path(source: Source) -> Path:
    return settings.raw_dir / f"{source.id}.{source.format}"


def fetch(client: httpx.Client, url: str) -> bytes:
    response = client.get(url)
    response.raise_for_status()
    return response.content  # raw bytes, not response.text


def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--force", action="store_true", help="re-download files that already exist"
    )
    args = parser.parse_args()

    sources = load_sources()  # validate the whole catalog before any network call
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()

    client = httpx.Client(
        headers={"User-Agent": settings.http_user_agent},
        timeout=settings.http_timeout_seconds,
        follow_redirects=True,
    )
    with client:
        for source in sources:
            path = raw_path(source)
            if path.exists() and not args.force:
                print(f"{source.id:12s} skipped (already in {path.name})")
                continue

            content = fetch(client, str(source.url))
            sha256 = hashlib.sha256(content).hexdigest()
            changed = manifest.get(source.id, {}).get("sha256") not in (None, sha256)

            path.write_bytes(content)
            manifest[source.id] = {
                "url": str(source.url),
                "file": path.name,
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "bytes": len(content),
                "sha256": sha256,
            }
            note = "  <- CHANGED since last download" if changed else ""
            print(f"{source.id:12s} {len(content):>9,} bytes -> {path.name}{note}")

            time.sleep(settings.seconds_between_requests)

    MANIFEST_FILE.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

"""Shared helpers for the importers, the builder and the validator.

Standard library only. No recursion: every walk uses a loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SOURCES_FILE = REPO_ROOT / "tools" / "sources.json"
SCHEMA_DIR = REPO_ROOT / "schema"

USER_AGENT = "software-identity-catalog-importer/1"

# Record field order (design section 2). Records are written in this order.
RECORD_FIELDS = ["id", "name", "kind", "tags", "keys", "homepage", "evidence"]

STRONG_KEYS = [
    "steamAppIds",
    "xboxIds",
    "epicIds",
    "gogIds",
    "uplayIds",
    "battlenetIds",
    "microsoftStoreIds",
    "wingetIds",
    "scoopIds",
    "discordIds",
    "productCodes",
    "upgradeCodes",
    "packageFamilyNames",
]
WEAK_KEYS = [
    "executables",
    "executablePaths",
    "launcherExecutables",
    "installedNames",
    "productNames",
    "shortcutNames",
    "uninstallKeys",
    "commands",
    "monikers",
    "aliases",
]
KEY_FIELDS = STRONG_KEYS + WEAK_KEYS
INTEGER_KEYS = {"steamAppIds"}

GUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


class ImportFailure(Exception):
    """A source could not be read. Its previous records stay untouched."""


def normalize_guid(value: str) -> str | None:
    """Return `{XXXXXXXX-...}` uppercase, or None when the value is not a bare GUID.

    Surrounding braces and spaces are removed first, so `{ {guid}}` is accepted.
    Values with anything else around the GUID (for example `{guid}_is1`, an Inno
    Setup key) are not MSI codes and return None.
    """
    core = value.strip().strip("{} \t")
    if not GUID_RE.match(core):
        return None
    return "{" + core.upper() + "}"


def basename(path: str) -> str:
    return re.split(r"[\\/]", path)[-1]


def sort_key(value):
    """Integers before strings, each in natural order (keeps mixed bad input sortable)."""
    return (isinstance(value, str), value)


def normalize_record(record: dict) -> dict:
    """Sort and dedupe every list, drop empty lists, order fields. Content is kept."""
    out: dict = {}
    for field in RECORD_FIELDS:
        if field not in record:
            continue
        value = record[field]
        if field == "tags":
            value = sorted(set(value), key=sort_key)
            if not value:
                continue
        elif field == "keys":
            keys: dict = {}
            for key in KEY_FIELDS:
                items = value.get(key)
                if not items:
                    continue
                keys[key] = sorted(set(items), key=sort_key)
            unknown = sorted(set(value) - set(KEY_FIELDS))
            for key in unknown:  # kept so the validator reports them
                keys[key] = value[key]
            value = keys
        elif value is None or value == "":
            continue
        out[field] = value
    unknown = sorted(set(record) - set(RECORD_FIELDS))
    for field in unknown:  # kept so the validator reports them
        out[field] = record[field]
    return out


def dump_line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))


def entries_path(source: str) -> Path:
    return DATA_DIR / source / "entries.json"


def render_entries(source: str, records: list[dict]) -> str:
    records = sorted((normalize_record(r) for r in records), key=lambda r: r["id"])
    lines = ["{", f'  "source": {json.dumps(source)},', '  "entries": [']
    for index, record in enumerate(records):
        sep = "," if index + 1 < len(records) else ""
        lines.append("    " + dump_line(record) + sep)
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_text_atomic(path: Path, text: str) -> bool:
    """Write `text` to `path` through a temporary file. Returns True when it changed."""
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def read_entries(source: str) -> list[dict]:
    path = entries_path(source)
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc.get("entries", [])


def write_entries(source: str, records: list[dict]) -> bool:
    return write_text_atomic(entries_path(source), render_entries(source, records))


def read_sources() -> dict:
    if not SOURCES_FILE.exists():
        return {}
    return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))


def update_source(source: str, entry: dict) -> None:
    sources = read_sources()
    sources[source] = entry
    text = json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(SOURCES_FILE, text)


def download(url: str, dest: Path, max_bytes: int, timeout: float = 120.0) -> str:
    """Stream `url` to `dest`, refusing more than `max_bytes`. Returns the SHA-256."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, open(dest, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ImportFailure(f"{url}: response exceeds {max_bytes} bytes")
                digest.update(chunk)
                out.write(chunk)
    except ImportFailure:
        raise
    except Exception as exc:  # network, HTTP and file errors all mean the source failed
        raise ImportFailure(f"{url}: {exc}") from exc
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def run_importer(name: str, main) -> None:
    """Run an importer's main(); on failure print one line and exit 1."""
    try:
        main()
    except ImportFailure as exc:
        print(f"{name}: FAILED, previous records kept: {exc}", file=sys.stderr)
        sys.exit(1)

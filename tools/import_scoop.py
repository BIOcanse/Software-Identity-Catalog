"""Import the ScoopInstaller buckets into data/scoop/entries.json.

For each bucket the default branch's commit SHA is resolved first, then that
commit's tarball is read in a temporary directory. Only `bucket/*.json` is read.

Usage: python tools/import_scoop.py [--work-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from catalog_common import (
    USER_AGENT,
    ImportFailure,
    basename,
    download,
    read_entries,
    run_importer,
    update_source,
    write_entries,
)

SOURCE = "scoop"
BUCKETS = [
    ("main", "ScoopInstaller/Main"),
    ("extras", "ScoopInstaller/Extras"),
    ("versions", "ScoopInstaller/Versions"),
    ("games", "ScoopInstaller/Games"),
]
MAX_TARBALL_BYTES = 256 << 20
MAX_MEMBERS = 200_000
MAX_MANIFEST_BYTES = 4 << 20
MAX_LIST_ITEMS = 1000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def resolve_commit(repo: str) -> str:
    """Return the default branch's commit SHA, via git ls-remote or the GitHub API."""
    errors = []
    try:
        result = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        sha = result.stdout.split()[0] if result.stdout.split() else ""
        if result.returncode == 0 and SHA_RE.match(sha):
            return sha
        errors.append(f"git ls-remote exit {result.returncode}: {result.stderr.strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(f"git ls-remote: {exc}")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github.sha"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/HEAD", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            sha = response.read(100).decode("ascii", "replace").strip()
        if SHA_RE.match(sha):
            return sha
        errors.append(f"GitHub API returned {sha[:60]!r}")
    except Exception as exc:
        errors.append(f"GitHub API: {exc}")
    raise ImportFailure(f"cannot resolve the commit of {repo}: {'; '.join(errors)}")


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError("list over the item limit")
        return value
    return [value]


def collect(block: dict, executables: set, commands: set, shortcut_names: set) -> None:
    """Read `bin` and `shortcuts` from one manifest block (top level or an architecture)."""
    for item in as_list(block.get("bin")):
        if isinstance(item, str):
            path, alias = item, None
        elif isinstance(item, list) and item and isinstance(item[0], str):
            path = item[0]
            alias = item[1] if len(item) > 1 and isinstance(item[1], str) else None
        else:
            continue
        name = basename(path.strip())
        if name.lower().endswith(".exe") and len(name) > 4:
            executables.add(name)
        if alias and alias.strip():
            commands.add(alias.strip())
    for item in as_list(block.get("shortcuts")):
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            continue
        name = basename(item[0].strip())
        if name.lower().endswith(".exe") and len(name) > 4:
            executables.add(name)
        if len(item) > 1 and isinstance(item[1], str) and item[1].strip():
            shortcut_names.add(item[1].strip())


def manifest_record(bucket: str, app: str, manifest: dict) -> dict:
    executables: set = set()
    commands: set = set()
    shortcut_names: set = set()
    collect(manifest, executables, commands, shortcut_names)
    architecture = manifest.get("architecture")
    if isinstance(architecture, dict):
        for arch in sorted(architecture):
            if isinstance(architecture[arch], dict):
                collect(architecture[arch], executables, commands, shortcut_names)
    keys = {
        "scoopIds": [f"{bucket}/{app}"],
        "executables": sorted(executables),
        "shortcutNames": sorted(shortcut_names),
        "commands": sorted(commands),
    }
    record = {"id": f"{SOURCE}:{bucket}/{app}", "name": app, "keys": keys}
    homepage = manifest.get("homepage")
    if isinstance(homepage, str) and re.match(r"^https?://", homepage.strip()):
        record["homepage"] = homepage.strip()
    return record


def read_bucket(bucket: str, tarball: Path, stats: dict) -> list[dict]:
    records = []
    members = 0
    try:
        with tarfile.open(tarball, mode="r:gz") as archive:
            for member in archive:
                members += 1
                if members > MAX_MEMBERS:
                    raise ImportFailure(f"{bucket}: tarball has more than {MAX_MEMBERS} members")
                parts = member.name.split("/")
                if len(parts) != 3 or parts[1] != "bucket" or not parts[2].endswith(".json"):
                    continue
                if not member.isfile():
                    continue
                if member.size > MAX_MANIFEST_BYTES:
                    stats["skipped"] += 1
                    continue
                app = parts[2][: -len(".json")]
                handle = archive.extractfile(member)
                try:
                    manifest = json.loads(handle.read().decode("utf-8-sig"))
                    if not isinstance(manifest, dict):
                        raise ValueError("manifest is not an object")
                    records.append(manifest_record(bucket, app, manifest))
                except (ValueError, UnicodeDecodeError):
                    stats["skipped"] += 1
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ImportFailure(f"{bucket}: cannot read the tarball: {exc}") from exc
    if not records:
        raise ImportFailure(f"{bucket}: no manifests found under bucket/")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work-dir", type=Path, help="keep downloads here instead of a temporary directory")
    args = parser.parse_args()

    records: list[dict] = []
    revision: dict = {}
    stats = {"skipped": 0}
    counts = {}
    with tempfile.TemporaryDirectory(prefix="sic-scoop-") as tmp:
        work = args.work_dir or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        for bucket, repo in BUCKETS:
            sha = resolve_commit(repo)
            tarball = work / f"{bucket}-{sha}.tar.gz"
            if not tarball.exists():
                download(f"https://codeload.github.com/{repo}/tar.gz/{sha}", tarball, MAX_TARBALL_BYTES)
            bucket_records = read_bucket(bucket, tarball, stats)
            counts[bucket] = len(bucket_records)
            records.extend(bucket_records)
            revision[bucket] = {"repository": repo, "commit": sha}

    previous = len(read_entries(SOURCE))
    changed = write_entries(SOURCE, records)
    update_source(SOURCE, {"revision": {"buckets": revision}})
    per_bucket = ", ".join(f"{b} {n}" for b, n in counts.items())
    print(
        f"scoop: {len(records)} records (was {previous}): {per_bucket}; "
        f"skipped manifests {stats['skipped']}; entries.json {'changed' if changed else 'unchanged'}"
    )


if __name__ == "__main__":
    run_importer(SOURCE, main)

"""Import the WinGet community source index into data/winget/entries.json.

Reads `Public/index.db` from the prebuilt index the WinGet client downloads
(https://cdn.winget.microsoft.com/cache/source.msix). The index is read in a
temporary directory and never stored in the repository.

Usage: python tools/import_winget.py [--msix FILE] [--work-dir DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from catalog_common import (
    ImportFailure,
    download,
    normalize_guid,
    read_entries,
    run_importer,
    sha256_file,
    update_source,
    write_entries,
)

SOURCE = "winget"
INDEX_URL = "https://cdn.winget.microsoft.com/cache/source.msix"
MAX_MSIX_BYTES = 512 << 20
MAX_INDEX_BYTES = 2 << 30
MAX_TABLE_ROWS = 5_000_000
REQUIRED_TABLES = [
    "metadata", "ids", "names", "monikers", "versions", "manifest",
    "tags", "tags_map", "commands", "commands_map", "pfns", "pfns_map",
    "productcodes", "productcodes_map", "upgradecodes", "upgradecodes_map",
]
# (value table, value column, map table, record field)
MAPS = [
    ("tags", "tag", "tags_map", "tags"),
    ("commands", "command", "commands_map", "commands"),
    ("pfns", "pfn", "pfns_map", "packageFamilyNames"),
    ("productcodes", "productcode", "productcodes_map", "productCodes"),
    ("upgradecodes", "upgradecode", "upgradecodes_map", "upgradeCodes"),
]
VERSION_PART = re.compile(r"^(\d*)(.*)$")


def version_key(version: str) -> tuple:
    """WinGet-style ordering: dot parts, numeric prefix first, a suffix sorts below none."""
    parts = version.strip().split(".")
    while len(parts) > 1 and parts[-1] in ("0", ""):
        parts.pop()
    key = []
    for part in parts:
        match = VERSION_PART.match(part)
        digits, rest = match.group(1), match.group(2)
        key.append((int(digits) if digits else 0, rest == "", rest.lower()))
    return tuple(key)


def extract_index(msix: Path, work: Path) -> Path:
    try:
        with zipfile.ZipFile(msix) as archive:
            info = archive.getinfo("Public/index.db")
            if info.file_size > MAX_INDEX_BYTES:
                raise ImportFailure(f"index.db is {info.file_size} bytes, over the limit")
            target = work / "index.db"
            with archive.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ImportFailure(f"cannot read Public/index.db from the msix: {exc}") from exc
    return target


def read_index(db_path: Path) -> tuple[list[dict], dict, dict]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise ImportFailure(f"index schema changed, missing tables: {', '.join(missing)}")
        for table in REQUIRED_TABLES:
            count = con.execute(f"select count(*) from [{table}]").fetchone()[0]
            if count > MAX_TABLE_ROWS:
                raise ImportFailure(f"table {table} has {count} rows, over the limit")
        metadata = dict(con.execute("select name, value from metadata"))

        packages: dict[int, dict] = {}
        manifest_package: dict[int, int] = {}
        query = (
            "select m.rowid, m.id, i.id, n.name, mo.moniker, v.version from manifest m "
            "join ids i on i.rowid = m.id join names n on n.rowid = m.name "
            "join monikers mo on mo.rowid = m.moniker join versions v on v.rowid = m.version"
        )
        for manifest_id, package_row, package_id, name, moniker, version in con.execute(query):
            manifest_package[manifest_id] = package_row
            package = packages.get(package_row)
            if package is None:
                package = {"id": package_id, "versions": [], "monikers": set(), "uninstallKeys": set()}
                for _, _, _, field in MAPS:
                    package[field] = set()
                packages[package_row] = package
            package["versions"].append((version_key(version), version, name))
            if moniker:
                package["monikers"].add(moniker)

        stats = {"productCodesDropped": 0, "upgradeCodesDropped": 0, "manifests": len(manifest_package)}
        for table, column, map_table, field in MAPS:
            query = (
                f"select mp.manifest, x.[{column}] from [{map_table}] mp "
                f"join [{table}] x on x.rowid = mp.[{column}]"
            )
            for manifest_id, value in con.execute(query):
                package_row = manifest_package.get(manifest_id)
                if package_row is None or not value:
                    continue
                if field in ("productCodes", "upgradeCodes"):
                    guid = normalize_guid(value)
                    if guid is None:
                        # A product code that is not a GUID is the Apps and Features key name of a non-MSI
                        # installer (7-zip, {GUID}_is1). It is kept as weak evidence, never as a product code.
                        if field == "productCodes":
                            packages[package_row]["uninstallKeys"].add(value.strip())
                        stats[field + "Dropped"] += 1
                        continue
                    value = guid
                packages[package_row][field].add(value)
    finally:
        con.close()

    records = []
    for package in packages.values():
        versions = sorted(package["versions"], key=lambda item: (item[0], item[1]))
        latest_name = versions[-1][2]
        keys = {"wingetIds": [package["id"]], "monikers": sorted(package["monikers"])}
        for _, _, _, field in MAPS:
            if field != "tags":
                keys[field] = sorted(package[field])
        keys["uninstallKeys"] = sorted(package["uninstallKeys"])
        record = {"id": f"{SOURCE}:{package['id']}", "name": latest_name, "keys": keys}
        if package["tags"]:
            record["tags"] = sorted(package["tags"])
        records.append(record)
    return records, metadata, stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--msix", type=Path, help="use this source.msix instead of downloading it")
    parser.add_argument("--work-dir", type=Path, help="keep downloads here instead of a temporary directory")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="sic-winget-") as tmp:
        work = args.work_dir or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        if args.msix:
            msix = args.msix
            msix_sha = sha256_file(msix)
        else:
            msix = work / "source.msix"
            msix_sha = download(INDEX_URL, msix, MAX_MSIX_BYTES)
        db_path = extract_index(msix, work)
        try:
            records, metadata, stats = read_index(db_path)
        except sqlite3.Error as exc:
            raise ImportFailure(f"cannot read index.db: {exc}") from exc

    if not records:
        raise ImportFailure("index.db holds no packages")
    previous = len(read_entries(SOURCE))
    changed = write_entries(SOURCE, records)

    revision = {
        "databaseIdentifier": metadata.get("databaseIdentifier"),
        "schemaVersion": f"{metadata.get('majorVersion')}.{metadata.get('minorVersion')}",
        "msixSha256": msix_sha,
    }
    last_write = metadata.get("lastwritetime")
    if last_write and last_write.isdigit():
        stamp = dt.datetime.fromtimestamp(int(last_write), tz=dt.timezone.utc)
        revision["lastWriteTime"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        revision["lastWriteTimeRaw"] = last_write
    update_source(SOURCE, {"url": INDEX_URL, "revision": revision})

    print(
        f"winget: {len(records)} records (was {previous}), {stats['manifests']} manifests, "
        f"non-GUID product codes kept as uninstall keys {stats['productCodesDropped']}, dropped "
        f"upgrade codes {stats['upgradeCodesDropped']}; entries.json "
        f"{'changed' if changed else 'unchanged'}; index {revision.get('lastWriteTime')}"
    )


if __name__ == "__main__":
    run_importer(SOURCE, main)

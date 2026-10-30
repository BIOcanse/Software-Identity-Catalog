"""Import Discord's detectable applications into data/discord/entries.json.

Fetches https://discord.com/api/v9/applications/detectable into a temporary
directory, records the fetch time and the response SHA-256, and keeps only the
fields SOURCES.md lists.

Usage: python tools/import_discord.py [--input FILE] [--work-dir DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import tempfile
from pathlib import Path

from catalog_common import (
    ImportFailure,
    basename,
    download,
    read_entries,
    run_importer,
    sha256_file,
    update_source,
    write_entries,
)

SOURCE = "discord"
URL = "https://discord.com/api/v9/applications/detectable"
MAX_RESPONSE_BYTES = 256 << 20
MAX_APPLICATIONS = 1_000_000
MAX_ITEMS = 1000
# Discord distributor -> record key. google_play is not kept.
STORE_KEYS = {
    "steam": "steamAppIds",
    "xbox": "xboxIds",
    "epic": "epicIds",
    "gog": "gogIds",
    "uplay": "uplayIds",
    "battlenet": "battlenetIds",
    "microsoft": "microsoftStoreIds",
}


def items(value) -> list:
    if not isinstance(value, list):
        return []
    if len(value) > MAX_ITEMS:
        raise ImportFailure(f"a list has {len(value)} items, over the limit")
    return value


def application_record(app: dict, stats: dict) -> dict | None:
    app_id = app.get("id")
    name = app.get("name")
    if not isinstance(app_id, str) or not app_id.isdigit() or not isinstance(name, str) or not name.strip():
        stats["skipped"] += 1
        return None
    keys: dict[str, set] = {"discordIds": {app_id}}
    aliases = {a.strip() for a in items(app.get("aliases")) if isinstance(a, str) and a.strip()}
    if aliases:
        keys["aliases"] = aliases
    for exe in items(app.get("executables")):
        if not isinstance(exe, dict) or exe.get("os") != "win32":
            continue
        given = exe.get("name")
        if not isinstance(given, str):
            continue
        # A leading ">" is Discord's marker for an exact match; it is not part of the name.
        given = given.strip().lstrip(">").strip().replace("\\", "/")
        file_name = basename(given)
        if not file_name:
            continue
        if exe.get("is_launcher") is True:
            keys.setdefault("launcherExecutables", set()).add(file_name)
            continue
        keys.setdefault("executables", set()).add(file_name)
        if "/" in given:
            keys.setdefault("executablePaths", set()).add(given)
    for sku in items(app.get("third_party_skus")):
        if not isinstance(sku, dict):
            continue
        field = STORE_KEYS.get(sku.get("distributor"))
        value = sku.get("id")
        if field is None or not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if field == "steamAppIds":
            if not value.isdigit() or int(value) <= 0:
                stats["badSteamIds"] += 1
                continue
            value = int(value)
        keys.setdefault(field, set()).add(value)
    return {
        "id": f"{SOURCE}:{app_id}",
        "name": name.strip(),
        "kind": "game",
        "keys": {field: sorted(values) for field, values in keys.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, help="read this saved response instead of fetching it")
    parser.add_argument("--work-dir", type=Path, help="keep the response here instead of a temporary directory")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="sic-discord-") as tmp:
        work = args.work_dir or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        if args.input:
            response = args.input
            digest = sha256_file(response)
            fetched = dt.datetime.fromtimestamp(response.stat().st_mtime, tz=dt.timezone.utc)
        else:
            response = work / "detectable.json"
            fetched = dt.datetime.now(dt.timezone.utc)
            digest = download(URL, response, MAX_RESPONSE_BYTES)
        try:
            apps = json.loads(response.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ImportFailure(f"the response is not JSON: {exc}") from exc

    if not isinstance(apps, list) or not apps:
        raise ImportFailure("the response is not a non-empty list")
    if len(apps) > MAX_APPLICATIONS:
        raise ImportFailure(f"the response has {len(apps)} applications, over the limit")
    stats = {"skipped": 0, "badSteamIds": 0}
    records = []
    seen = set()
    for app in apps:
        record = application_record(app, stats) if isinstance(app, dict) else None
        if record is None or record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)

    previous = len(read_entries(SOURCE))
    changed = write_entries(SOURCE, records)
    update_source(SOURCE, {
        "url": URL,
        "revision": {"fetched": fetched.strftime("%Y-%m-%dT%H:%M:%SZ"), "responseSha256": digest},
    })
    print(
        f"discord: {len(records)} records (was {previous}) from {len(apps)} applications; "
        f"skipped {stats['skipped']}, non-numeric Steam IDs {stats['badSteamIds']}; "
        f"entries.json {'changed' if changed else 'unchanged'}"
    )


if __name__ == "__main__":
    run_importer(SOURCE, main)

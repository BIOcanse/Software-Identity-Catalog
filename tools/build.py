"""Build the release files from data/*/entries.json (design sections 3 and 5).

1. Normalizes every source file in place (sorted lists, no duplicates, records
   sorted by id, one record per line). Content is not changed.
2. Links records that share a strong key into groups (union-find, loops only).
3. Writes dist/catalog-open.json, dist/catalog-discord.json and dist/manifest.json.

The catalog files hold no timestamps. `created` and `catalogVersion` exist only
in the manifest and come from arguments or the environment, so the same inputs
give byte-identical catalog files.

Usage:
  python tools/build.py --created 2026-10-30T00:00:00Z [--catalog-version 2026.10.30]
                        [--failed winget,discord] [--out-dir DIR] [--report-keys N]
Environment fallbacks: CATALOG_CREATED, CATALOG_VERSION, CATALOG_FAILED_SOURCES.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import deque
from pathlib import Path

from catalog_common import (
    REPO_ROOT,
    STRONG_KEYS,
    basename,
    dump_line,
    entries_path,
    read_entries,
    read_sources,
    render_entries,
    write_text_atomic,
)

SCHEMA_VERSION = 1
OPEN_SOURCES = ["curated", "winget", "scoop"]
DISCORD_SOURCES = ["discord"]
ALL_SOURCES = OPEN_SOURCES + DISCORD_SOURCES
FILES = [
    # name, sources, license (SPDX expression; NOASSERTION: the source publishes none)
    ("catalog-open.json", OPEN_SOURCES, "CC0-1.0 AND MIT AND Unlicense"),
    ("catalog-discord.json", DISCORD_SOURCES, "NOASSERTION"),
]
EXECUTABLE_FIELDS = ["executables", "launcherExecutables", "executablePaths"]

# Strong-key values that never link records. Each rule is explicit, has a reason,
# and is written into the catalog files as `excludedKeys` so consumers see it.
ZERO_GUID = "{00000000-0000-0000-0000-000000000000}"
EXCLUDED_KEY_RULES = [
    {
        "fields": ["productCodes", "upgradeCodes"],
        "match": lambda value: value == ZERO_GUID,
        "rule": "all-zero GUID: a placeholder, not a real MSI code",
    },
]


def key_value(field: str, value):
    """The comparison form of a strong key: integers as is, strings case-insensitive."""
    return value if isinstance(value, int) else value.casefold()


def excluded_reason(field: str, value) -> str | None:
    for rule in EXCLUDED_KEY_RULES:
        if field in rule["fields"] and rule["match"](value):
            return rule["rule"]
    return None


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.size = [1] * size

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:  # path compression, iterative
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def key_index(records: list[dict]) -> tuple[dict, dict]:
    """Map (field, comparison value) -> record indexes; also collect excluded keys."""
    index: dict[tuple, list[int]] = {}
    excluded: dict[tuple, dict] = {}
    for position, record in enumerate(records):
        keys = record.get("keys", {})
        for field in STRONG_KEYS:
            for value in keys.get(field, []):
                compare = (field, key_value(field, value))
                reason = excluded_reason(field, value)
                if reason is not None:
                    entry = excluded.setdefault(compare, {"key": field, "value": value, "rule": reason, "records": 0})
                    entry["records"] += 1
                    continue
                index.setdefault(compare, []).append(position)
    return index, excluded


def build_groups(records: list[dict]) -> tuple[list[dict], dict[int, str], list[dict]]:
    """Return (groups with 2+ members, record index -> group id, excluded keys)."""
    index, excluded = key_index(records)
    uf = UnionFind(len(records))
    for members in index.values():
        first = members[0]
        for other in members[1:]:
            uf.union(first, other)

    components: dict[int, list[int]] = {}
    for position in range(len(records)):
        components.setdefault(uf.find(position), []).append(position)

    group_of: dict[int, str] = {}
    groups = []
    for members in components.values():
        members.sort(key=lambda p: records[p]["id"])
        root = members[0]
        group_id = records[root]["id"]
        for position in members:
            group_of[position] = group_id
        if len(members) < 2:
            continue
        # Breadth-first walk from the root over shared keys. Each member records the
        # key that first reached it. A key is expanded once, so the walk is linear.
        link: dict[int, dict] = {root: {}}
        expanded: set = set()
        queue = deque([root])
        while queue:
            current = queue.popleft()
            keys = records[current].get("keys", {})
            for field in STRONG_KEYS:
                for value in keys.get(field, []):
                    compare = (field, key_value(field, value))
                    if compare in expanded or compare not in index:
                        continue
                    expanded.add(compare)
                    for other in sorted(index[compare], key=lambda p: records[p]["id"]):
                        if other in link:
                            continue
                        link[other] = {"key": field, "value": value, "via": records[current]["id"]}
                        queue.append(other)
        out_members = []
        for position in members:
            member = {"id": records[position]["id"]}
            member.update(link.get(position, {}))
            out_members.append(member)
        groups.append({"id": group_id, "members": out_members})
    groups.sort(key=lambda g: g["id"])
    excluded_list = sorted(excluded.values(), key=lambda e: (e["key"], str(e["value"])))
    return groups, group_of, excluded_list


def ambiguous_executables(records: list[dict], group_of: dict[int, str]) -> list[str]:
    """Executable basenames, compared case-insensitively, that occur in more than one group."""
    seen: dict[str, set] = {}
    for position, record in enumerate(records):
        keys = record.get("keys", {})
        for field in EXECUTABLE_FIELDS:
            for value in keys.get(field, []):
                name = basename(value).casefold()
                if name:
                    seen.setdefault(name, set()).add(group_of[position])
    return sorted(name for name, owners in seen.items() if len(owners) > 1)


def render_catalog(name: str, sources: list[str], records, groups, ambiguous, excluded) -> str:
    def block(label: str, rows: list, last: bool) -> list[str]:
        lines = [f'  "{label}": [']
        for i, row in enumerate(rows):
            lines.append("    " + dump_line(row) + ("," if i + 1 < len(rows) else ""))
        lines.append("  ]" + ("" if last else ","))
        return lines

    lines = [
        "{",
        f'  "schemaVersion": {SCHEMA_VERSION},',
        f'  "file": {json.dumps(name)},',
        f'  "sources": {dump_line(sources)},',
    ]
    lines += block("records", records, False)
    lines += block("groups", groups, False)
    lines += block("excludedKeys", excluded, False)
    lines += block("ambiguousExecutables", ambiguous, True)
    lines.append("}")
    return "\n".join(lines) + "\n"


def report_top_keys(records: list[dict], limit: int) -> None:
    index, _ = key_index(records)
    top = sorted(index.items(), key=lambda item: -len(item[1]))[:limit]
    print(f"top {limit} strong keys by record count:")
    for (field, value), members in top:
        sample = ", ".join(records[p]["id"] for p in members[:3])
        print(f"  {len(members):6d}  {field}={value}  e.g. {sample}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--created", default=os.environ.get("CATALOG_CREATED"),
                        help="manifest creation time, ISO 8601 UTC (env CATALOG_CREATED)")
    parser.add_argument("--catalog-version", default=os.environ.get("CATALOG_VERSION"),
                        help="catalog version, default YYYY.MM.DD of --created (env CATALOG_VERSION)")
    parser.add_argument("--failed", default=os.environ.get("CATALOG_FAILED_SOURCES", ""),
                        help="comma-separated sources whose import failed this build")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--report-keys", type=int, default=0, help="print the N most shared strong keys")
    args = parser.parse_args()

    if not args.created or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", args.created):
        parser.error("--created (or CATALOG_CREATED) is required as YYYY-MM-DDTHH:MM:SSZ")
    version = args.catalog_version or args.created[:10].replace("-", ".")
    if not re.match(r"^\d{4}\.\d{2}\.\d{2}(\.\d+)?$", version):
        parser.error("--catalog-version must be YYYY.MM.DD or YYYY.MM.DD.N")
    failed = sorted({s.strip() for s in args.failed.split(",") if s.strip()})
    unknown = [s for s in failed if s not in ALL_SOURCES]
    if unknown:
        parser.error(f"unknown sources in --failed: {', '.join(unknown)}")

    # 1. Normalize every source file.
    by_source: dict[str, list[dict]] = {}
    entries_sha: dict[str, str] = {}
    rewritten = []
    for source in ALL_SOURCES:
        text = render_entries(source, read_entries(source))
        if write_text_atomic(entries_path(source), text):
            rewritten.append(source)
        entries_sha[source] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        by_source[source] = json.loads(text)["entries"]

    open_records = sorted((r for s in OPEN_SOURCES for r in by_source[s]), key=lambda r: r["id"])
    all_records = sorted((r for s in ALL_SOURCES for r in by_source[s]), key=lambda r: r["id"])
    ids = [r["id"] for r in all_records]
    if len(ids) != len(set(ids)):
        sys.exit("build: duplicate record ids across sources")
    if args.report_keys:
        report_top_keys(all_records, args.report_keys)

    # 2. Groups. The open file links only open records; the Discord file links all
    #    records and lists the groups that contain a Discord record.
    open_groups, open_group_of, open_excluded = build_groups(open_records)
    all_groups, all_group_of, all_excluded = build_groups(all_records)
    discord_groups = [g for g in all_groups if any(m["id"].startswith("discord:") for m in g["members"])]
    discord_records = by_source["discord"]
    discord_excluded = [e for e in all_excluded]

    outputs = {
        "catalog-open.json": render_catalog(
            "catalog-open", OPEN_SOURCES, open_records, open_groups,
            ambiguous_executables(open_records, open_group_of), open_excluded),
        "catalog-discord.json": render_catalog(
            "catalog-discord", DISCORD_SOURCES, discord_records, discord_groups,
            ambiguous_executables(all_records, all_group_of), discord_excluded),
    }

    # 3. Release files.
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for name, sources, license_id in FILES:
        data = outputs[name].encode("utf-8")
        write_text_atomic(out_dir / name, outputs[name])
        files.append({
            "name": name,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "license": license_id,
            "sources": sources,
        })
    recorded = read_sources()
    sources_out = {}
    for source in ALL_SOURCES:
        entry = dict(recorded.get(source, {}))
        entry["status"] = "failed" if source in failed else "ok"
        entry["records"] = len(by_source[source])
        entry["entriesSha256"] = entries_sha[source]
        sources_out[source] = entry
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "catalogVersion": version,
        "created": args.created,
        "files": files,
        "sources": sources_out,
    }
    write_text_atomic(out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    largest = max(all_groups, key=lambda g: len(g["members"]), default=None)
    print(
        f"build {version}: records " + ", ".join(f"{s} {len(by_source[s])}" for s in ALL_SOURCES)
        + f"; rewritten {', '.join(rewritten) or 'none'}; failed {', '.join(failed) or 'none'}"
    )
    print(
        f"groups: open {len(open_groups)}, discord file {len(discord_groups)}, all {len(all_groups)}; "
        f"excluded keys {len(all_excluded)}"
    )
    if largest:
        linkers: dict[str, int] = {}
        for member in largest["members"]:
            if "key" in member:
                linkers[member["key"]] = linkers.get(member["key"], 0) + 1
        print(f"largest group {largest['id']}: {len(largest['members'])} members, linked by {linkers}")
    for item in files:
        print(f"  {item['name']}: {item['bytes']} bytes")


if __name__ == "__main__":
    main()

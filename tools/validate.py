"""Validate data/*/entries.json and, when present, the release files in dist/.

There is no jsonschema library in the standard library, so this file interprets
the subset of JSON Schema 2020-12 that schema/*.schema.json uses. A keyword it
does not know is an error, so the schema files and these checks stay in step.
Rules JSON Schema cannot state (sorted lists, sorted unique ids, id prefix,
`evidence` only on curated records) are checked in code below.

Usage: python tools/validate.py [--dist DIR] [--no-dist]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from catalog_common import DATA_DIR, KEY_FIELDS, REPO_ROOT, SCHEMA_DIR, render_entries, sort_key

MAX_ERRORS = 30
ANNOTATIONS = {"$schema", "$id", "$defs", "title", "description"}
KNOWN = ANNOTATIONS | {
    "$ref", "type", "enum", "const", "pattern", "minLength", "minimum", "minItems",
    "uniqueItems", "items", "required", "properties", "additionalProperties", "minProperties",
}
TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class Schema:
    """A loaded schema document and its compiled patterns."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.root = json.loads(path.read_text(encoding="utf-8"))
        self.patterns: dict[str, re.Pattern] = {}
        self._check_keywords()

    def _check_keywords(self) -> None:
        stack = [self.root]
        while stack:
            node = stack.pop()
            unknown = set(node) - KNOWN
            if unknown:
                raise SystemExit(f"{self.path.name}: unsupported keywords {sorted(unknown)}")
            for key in ("$defs", "properties"):
                stack.extend(node.get(key, {}).values())
            for key in ("items", "additionalProperties"):
                if isinstance(node.get(key), dict):
                    stack.append(node[key])

    def resolve(self, ref: str) -> dict:
        if not ref.startswith("#/"):
            raise SystemExit(f"{self.path.name}: only local $ref is supported, got {ref}")
        node = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    def pattern(self, text: str) -> re.Pattern:
        if text not in self.patterns:
            self.patterns[text] = re.compile(text)
        return self.patterns[text]

    def validate(self, instance, start: dict | None = None, path: str = "$") -> list[str]:
        errors: list[str] = []
        stack = [(start if start is not None else self.root, instance, path)]
        while stack and len(errors) < MAX_ERRORS:
            schema, value, where = stack.pop()
            if "$ref" in schema:
                stack.append((self.resolve(schema["$ref"]), value, where))
            expected = schema.get("type")
            if expected is not None and not TYPES[expected](value):
                errors.append(f"{where}: expected {expected}")
                continue
            if "enum" in schema and value not in schema["enum"]:
                errors.append(f"{where}: {value!r} not in {schema['enum']}")
            if "const" in schema and value != schema["const"]:
                errors.append(f"{where}: must be {schema['const']!r}")
            if isinstance(value, str):
                if len(value) < schema.get("minLength", 0):
                    errors.append(f"{where}: shorter than {schema['minLength']}")
                if "pattern" in schema and not self.pattern(schema["pattern"]).search(value):
                    errors.append(f"{where}: {value[:80]!r} does not match {schema['pattern']}")
            if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
                errors.append(f"{where}: below {schema['minimum']}")
            if isinstance(value, list):
                if len(value) < schema.get("minItems", 0):
                    errors.append(f"{where}: fewer than {schema['minItems']} items")
                if schema.get("uniqueItems"):
                    seen = {json.dumps(item, sort_keys=True) for item in value}
                    if len(seen) != len(value):
                        errors.append(f"{where}: duplicate items")
                if "items" in schema:
                    for i in range(len(value) - 1, -1, -1):
                        stack.append((schema["items"], value[i], f"{where}[{i}]"))
            if isinstance(value, dict):
                if len(value) < schema.get("minProperties", 0):
                    errors.append(f"{where}: fewer than {schema['minProperties']} properties")
                for name in schema.get("required", []):
                    if name not in value:
                        errors.append(f"{where}: missing {name}")
                properties = schema.get("properties", {})
                extra = schema.get("additionalProperties", True)
                for name in sorted(value, reverse=True):
                    if name in properties:
                        stack.append((properties[name], value[name], f"{where}.{name}"))
                    elif extra is False:
                        errors.append(f"{where}: unexpected field {name}")
                    elif isinstance(extra, dict):
                        stack.append((extra, value[name], f"{where}.{name}"))
        return errors


def check_record_rules(source: str, record: dict, where: str) -> list[str]:
    """The rules the schema cannot state."""
    errors = []
    record_id = record.get("id", "")
    if not isinstance(record_id, str) or not record_id.startswith(source + ":"):
        errors.append(f"{where}: id must start with {source}:")
    if "evidence" in record and source != "curated":
        errors.append(f"{where}: evidence is for curated records only")
    if source == "discord" and record.get("kind") != "game":
        errors.append(f"{where}: Discord records are games")
    lists = [("tags", record.get("tags"))]
    keys = record.get("keys") if isinstance(record.get("keys"), dict) else {}
    lists += [(f"keys.{field}", keys.get(field)) for field in KEY_FIELDS]
    for name, items in lists:
        if isinstance(items, list):
            try:
                ordered = items == sorted(items, key=sort_key)
            except TypeError:
                ordered = False
            if not ordered:
                errors.append(f"{where}.{name}: not sorted")
    return errors


def validate_entries(schema: Schema, path: Path) -> tuple[int, list[str]]:
    source = path.parent.name
    text = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return 0, [f"{path.name}: not JSON: {exc}"]
    errors = []
    if not isinstance(doc, dict) or doc.get("source") != source:
        return 0, [f"source must be {source!r}, the directory name"]
    entries = doc.get("entries") if isinstance(doc.get("entries"), list) else []
    errors += schema.validate(doc, schema.resolve("#/$defs/entriesFile"))
    previous = None
    for i, record in enumerate(entries):
        if len(errors) >= MAX_ERRORS:
            break
        if not isinstance(record, dict):
            continue
        where = f"entries[{i}]"
        errors += check_record_rules(source, record, where)
        record_id = record.get("id")
        if isinstance(record_id, str):
            if previous is not None and record_id <= previous:
                errors.append(f"{where}: id {record_id} is not after {previous} (sorted, unique)")
            previous = record_id
    if not errors and render_entries(source, entries) != text:
        errors.append("not in normalized form (one record per line); run tools/build.py")
    return len(entries), errors


def validate_dist(record_schema: Schema, manifest_schema: Schema, dist: Path) -> list[str]:
    manifest_path = dist / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = [f"manifest.json {e}" for e in manifest_schema.validate(manifest)]
    if errors:
        return errors
    record_start = record_schema.resolve("#/$defs/record")
    for item in manifest["files"]:
        path = dist / item["name"]
        if not path.exists():
            errors.append(f"{item['name']}: listed in the manifest but missing")
            continue
        data = path.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            errors.append(f"{item['name']}: bytes or SHA-256 differ from the manifest")
        catalog = json.loads(data.decode("utf-8"))
        for field in ("schemaVersion", "file", "sources", "records", "groups", "excludedKeys", "ambiguousExecutables"):
            if field not in catalog:
                errors.append(f"{item['name']}: missing {field}")
        records = catalog.get("records", [])
        ids = [r.get("id") for r in records if isinstance(r, dict)]
        if ids != sorted(set(ids)):
            errors.append(f"{item['name']}: record ids are not sorted and unique")
        for i, record in enumerate(records):
            if len(errors) >= MAX_ERRORS:
                return errors
            errors += [f"{item['name']} {e}" for e in record_schema.validate(record, record_start, f"records[{i}]")]
            source = str(record.get("id", "")).split(":", 1)[0]
            if source not in item["sources"]:
                errors.append(f"{item['name']} records[{i}]: source {source} not listed for this file")
        members_seen: set = set()
        for g, group in enumerate(catalog.get("groups", [])):
            member_ids = [m.get("id") for m in group.get("members", [])]
            if len(member_ids) < 2 or group.get("id") != min(member_ids):
                errors.append(f"{item['name']} groups[{g}]: id must be the smallest of 2+ member ids")
            if members_seen.intersection(member_ids):
                errors.append(f"{item['name']} groups[{g}]: a record is in two groups")
            members_seen.update(member_ids)
            linked = [m for m in group.get("members", []) if "key" in m]
            if len(linked) != len(member_ids) - 1:
                errors.append(f"{item['name']} groups[{g}]: every member but the root needs its linking key")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--no-dist", action="store_true", help="check data/ only")
    args = parser.parse_args()

    record_schema = Schema(SCHEMA_DIR / "record.schema.json")
    manifest_schema = Schema(SCHEMA_DIR / "manifest.schema.json")
    failed = False
    for path in sorted(DATA_DIR.glob("*/entries.json")):
        count, errors = validate_entries(record_schema, path)
        label = f"data/{path.parent.name}/entries.json"
        print(f"{label}: {count} records, {'ok' if not errors else f'{len(errors)} errors'}")
        for error in errors[:MAX_ERRORS]:
            print(f"  {error}")
        failed = failed or bool(errors)
    if not args.no_dist and (args.dist / "manifest.json").exists():
        errors = validate_dist(record_schema, manifest_schema, args.dist)
        print(f"{args.dist.name}/: {'ok' if not errors else f'{len(errors)} errors'}")
        for error in errors[:MAX_ERRORS]:
            print(f"  {error}")
        failed = failed or bool(errors)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

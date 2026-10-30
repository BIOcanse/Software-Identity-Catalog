# Design

Status: current design. Rewrite this whole file when the design changes.

## 1. What the catalog does

The catalog collects public software identity data from several sources into one record format, so a consumer can recognize software from evidence it can read locally. The evidence types are:
- platform and store IDs;
- MSI product and upgrade codes;
- MSIX package family names;
- package-manager IDs;
- installed names;
- PE product names;
- executable names.

Each source has an importer. An importer reads the source at a recorded revision and writes that source's normalized records. The builder then:
1. reads every source's records;
2. links records that share a strong key into groups;
3. writes the release files and a manifest with sizes, hashes, licenses and source revisions.

The catalog never decides on its own that software is present. It only says which evidence belongs to which software. The consumer reads its local evidence and decides.

## 2. Record

One record describes one piece of software as one source sees it. Fields:
- `id`: `<source>:<native id>`. Examples:
  - `winget:Google.Chrome`
  - `scoop:extras/vscode`
  - `discord:356875221078245376`
  - `curated:app-google-chrome`

  The ID is unique across the catalog and stable across builds as long as the source keeps its own ID.
- `name`: the display name the source gives.
- `kind`: optional. One of `game`, `application`, `windows-component`, `windows-service`. Set only when the source states it: Discord records are games, and curated records carry their checked kind.
- `tags`: optional list of plain words, for example `high-performance` on curated entries.
- `keys`: the evidence. Every list is sorted and has no duplicates. Absent lists are left out.
  - Strong keys:
    - `steamAppIds`, `xboxIds`, `epicIds`, `gogIds`, `uplayIds`, `battlenetIds`, `microsoftStoreIds`: store IDs;
    - `wingetIds`: WinGet package IDs;
    - `scoopIds`: `<bucket>/<app>`;
    - `discordIds`: Discord application IDs;
    - `productCodes`, `upgradeCodes`: MSI codes, uppercase with braces;
    - `packageFamilyNames`: MSIX package family names.
  - Weaker keys:
    - `executables`: file names of the program's own executables, with their original case;
    - `executablePaths`: relative path suffixes as the source gives them, with forward slashes, for example `_retail_/wow.exe`;
    - `launcherExecutables`: executables the source marks as launchers;
    - `installedNames`: names in Apps and Features;
    - `productNames`: PE product names;
    - `shortcutNames`: Start menu shortcut names;
    - `uninstallKeys`: Apps and Features key names of non-MSI installers, for example `{GUID}_is1` or `7-zip`;
    - `commands`: command aliases;
    - `monikers`, `aliases`: other names.
- `homepage`: optional URL.
- `evidence`: curated records only. How the entry was checked.

Fields a source does not have are left out. They are never filled with guesses.

Strong keys compare case-insensitively: WinGet IDs are case-insensitive, and the WinGet index stores package family names and product codes in lowercase. Steam app IDs are integers. Every other ID is a string.

## 3. Groups

Two records describe the same software when they share a strong key, for example the same Steam app ID or the same MSI product code. A group is a set of records connected this way, directly or through other records.

- The builder computes groups with union-find, using loops only and no recursion.
- It records which key linked each record into its group.
- Weak keys never link records: names, executables, paths, commands and aliases. A file name such as `game.exe` belongs to hundreds of unrelated games.
- A group's ID is the smallest record ID in it, so it stays the same when unrelated records change.
- A key value that never links records is excluded by an explicit rule with a reason. Today the only one is the all-zero GUID, a placeholder some installers use as a product code. Each catalog file lists its excluded keys with their rule and record count.

The builder also writes a list of executable names that appear in more than one group. A consumer must not identify software from one of these names alone.

## 4. Repository data

- `data/<source>/entries.json`: one file per source, records sorted by `id`, one record per line. Keeping this in the repository makes each build's changes reviewable as a diff.
- The source data itself is never stored. This includes the WinGet index, bucket checkouts and the Discord response. Builds read them at build time.

## 5. Release files

Each build that changes any record publishes a GitHub release. The tag is `catalog-YYYY.MM.DD`, with `.N` added for a second build on the same day. The release has:
- `catalog-open.json`: the records from openly licensed sources (curated, WinGet, Scoop), their groups, and the list of ambiguous executables.
- `catalog-discord.json`: the Discord records and the groups they join. This file is separate because the source publishes no license, so a consumer can leave it out.
- `catalog-pcgamingwiki.json`: once that source is imported. It is published under CC BY-NC-SA 3.0.
- `manifest.json.sig`: an ECDSA P-256 signature over the SHA-256 of `manifest.json`, in DER form. The public key is `keys/manifest-signing.pub.pem`. The private key exists only in the repository's Actions secret `MANIFEST_SIGNING_KEY`. A build without the secret publishes nothing.
- `manifest.json`, with these fields:
  - `schemaVersion`, `catalogVersion` and `created`;
  - for each file: name, bytes, SHA-256 and license;
  - for each source: the revision used and the record count.

Each catalog file is one JSON object with these fields:
- `schemaVersion` and `file`;
- `sources`: the sources it contains, with their revisions;
- `records`: sorted by `id`;
- `groups`: each with its ID and members. Each member lists the key and the record that linked it. Only groups with two or more members are listed; any other record is its own group.
- `excludedKeys`;
- `ambiguousExecutables`.

`catalog-open.json` groups only the open records. `catalog-discord.json` computes groups and ambiguous executables over all records and lists the groups that contain a Discord record.

Builds are deterministic: the same source revisions give byte-identical files.

## 6. Builds

- The workflow runs on manual dispatch and once a week.
- A build:
  1. runs every importer;
  2. runs the builder;
  3. validates every record against `schema/`;
  4. commits the changed `data/` files;
  5. publishes a release when anything changed.
- An importer whose source fails keeps that source's previous records, and the manifest records the failure. A failed source never empties its directory.

## 7. Consumer contract (Resource Manager)

- RM ships a built-in copy of the release files with each version.
- RM updates from this repository only when the user asks. It downloads `manifest.json` and `manifest.json.sig`, verifies the signature with the public key it ships, downloads the files the manifest lists, checks each SHA-256, and swaps in the new set in one step. The previous set stays on disk for rollback, and the user can switch back to the built-in copy.
- The user chooses whether `catalog-discord.json` is used. The choice is visible and can be turned off.
- RM does not keep the full JSON in memory. It builds a compact lookup index from the files once per update and looks records up when it identifies software.

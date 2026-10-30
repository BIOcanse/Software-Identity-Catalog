# Sources

This catalog is compiled from the sources below. For each source this file records where the data comes from, how it is read, what is kept, its license and attribution, and how often it is refreshed. The revision of each source used by a build is in that release's `manifest.json`.

## WinGet community repository

- **Data:** [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs). Copyright (c) Microsoft Corporation. Licensed under the MIT License.
- **Access:** the prebuilt community source index that the WinGet client itself downloads (`https://cdn.winget.microsoft.com/cache/source.msix`, which contains `Public/index.db`). Microsoft builds the index from the repository's manifests. The index is read with SQLite and is not redistributed.
- **Kept:**
  - package ID, latest package name, monikers, tags and commands;
  - MSI product codes and upgrade codes from every version;
  - MSIX package family names.

  The index's product-code table also holds the Apps and Features key names of non-MSI installers, for example `7-zip` or `{GUID}_is1`. Bare GUIDs are MSI product codes and are kept as product codes. Every other key name is kept as `uninstallKeys`; these never link records.
- **Not kept:** installer URLs, hashes, descriptions, license texts, release notes.
- **Refresh:** every build.
- **Directory:** `data/winget/`. The MIT license text is in [data/winget/NOTICE.md](data/winget/NOTICE.md).

## Scoop buckets

- **Data:**
  - [ScoopInstaller/Main](https://github.com/ScoopInstaller/Main), Unlicense.
  - [ScoopInstaller/Extras](https://github.com/ScoopInstaller/Extras), Unlicense.
  - [ScoopInstaller/Versions](https://github.com/ScoopInstaller/Versions), Unlicense.
  - [ScoopInstaller/Games](https://github.com/ScoopInstaller/Games), CC0-1.0.
- **Access:** each bucket's default branch at a recorded commit.
- **Kept:** bucket and app name, homepage, executable names from `bin` and `shortcuts` (including the architecture blocks), shortcut names, and command aliases from `bin`.
- **Not kept:** versions, download URLs, hashes, scripts, descriptions.
- **Refresh:** every build.
- **Directory:** `data/scoop/`.

## Discord detectable applications

- **Data:** the public list Discord serves at `https://discord.com/api/v9/applications/detectable`. Discord uses it to show which game a user is playing.
- **License:** none is published. Only factual identification data is kept: names, executable names and paths, and store IDs. The records are in their own files, `data/discord/` and `catalog-discord.json`, so a consumer can leave them out. See [data/discord/NOTICE.md](data/discord/NOTICE.md).
- **Kept:**
  - application ID, name and aliases;
  - Windows executables, separated into game executables and launchers;
  - store IDs from `third_party_skus`: Steam, Xbox, Epic, GOG, Ubisoft Connect, Battle.net and Microsoft.
- **Not kept:** icons and cover hashes, overlay flags, age ratings, themes, Google Play IDs, and macOS and Linux executables.
- **Refresh:** every build. The fetch time and the SHA-256 of the response are recorded.

## PCGamingWiki

- **Data:** [PCGamingWiki](https://www.pcgamingwiki.com), content licensed under CC BY-NC-SA 3.0.
- **Status:** not imported yet. Anonymous Cargo queries on the wiki's API are refused ("You don't have permission to run arbitrary Cargo queries", checked 2026-10-30). The import starts once there is a route the wiki permits, such as granted API access or a published dump.
- **Planned use:** Steam, GOG and Epic IDs for the same game, kept in their own file under CC BY-NC-SA 3.0 with attribution. See [data/pcgamingwiki/NOTICE.md](data/pcgamingwiki/NOTICE.md).

## Steam most-played charts

- **Data:** Steam's official most-played charts, read on 2026-07-12.
- **Use:** they chose which games got a hand-curated entry. Only app IDs and names, which are facts, appear in `data/curated/`.

## Resource Manager curation

- **Data:** entries in `data/curated/`. They were checked on real Windows installs: installed names, executable names, PE product names, Steam app IDs and WinGet IDs. Each entry's `evidence` field says how it was checked.
- **License:** CC0-1.0, see [data/curated/LICENSE.md](data/curated/LICENSE.md).
- **Refresh:** by hand, through pull requests.

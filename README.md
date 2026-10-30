# Software Identity Catalog

An aggregate catalog of public software identity data. It is used to recognize installed and running Windows software from strong evidence: store and platform IDs, installer product codes, package family names, package-manager IDs and executable names.

**This catalog is compiled from the sources listed below.** Each record keeps its source in its ID. Each source's data lives in its own directory under `data/`, under that source's license. Nothing here is original research except the hand-curated entries in `data/curated/`.

Resource Manager is the main consumer. It ships a built-in copy and updates from this repository's releases only when the user asks. The data is plain JSON, and any project may use it under the licenses below.

## Sources

| Source | What is taken | License of the source | Directory |
|---|---|---|---|
| [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs), read through the WinGet community source index | package IDs, names, monikers, tags, commands, MSI product and upgrade codes, uninstall key names of non-MSI installers, MSIX package family names | MIT | `data/winget/` |
| [ScoopInstaller/Main](https://github.com/ScoopInstaller/Main), [Extras](https://github.com/ScoopInstaller/Extras), [Versions](https://github.com/ScoopInstaller/Versions) | app names, homepages, executable names from `bin` and `shortcuts`, shortcut names, command aliases | Unlicense | `data/scoop/` |
| [ScoopInstaller/Games](https://github.com/ScoopInstaller/Games) | the same fields | CC0-1.0 | `data/scoop/` |
| Discord's public list of detectable applications (`/api/v9/applications/detectable`) | game names, aliases, Windows executable names and paths, Steam, Xbox, Epic, GOG, Ubisoft Connect, Battle.net and Microsoft Store IDs | no license is published; see [data/discord/NOTICE.md](data/discord/NOTICE.md) | `data/discord/` |
| [PCGamingWiki](https://www.pcgamingwiki.com) | planned: store IDs across Steam, GOG and Epic | CC BY-NC-SA 3.0 | `data/pcgamingwiki/` (empty, see [NOTICE](data/pcgamingwiki/NOTICE.md)) |
| Steam's official most-played charts | Steam app IDs and names of the most-played games, used to choose curated entries | facts | `data/curated/` |
| Resource Manager curation | entries checked on real installs: executable names, PE product names, installed names | CC0-1.0 | `data/curated/` |

The full list, with access routes, refresh cadence and the exact revision each build used, is in [SOURCES.md](SOURCES.md). Every release's `manifest.json` also records the source revisions.

## Layout

```
data/<source>/      normalized records from one source, sorted, one file per source
schema/             JSON Schema of a record and of the release manifest
tools/              importers and the builder (Python 3, standard library only)
docs/design.md      record format, merge rule, release outputs, consumer contract
.github/workflows/  scheduled and manual builds
```

## Releases

Each build publishes a GitHub release with these files:
- `catalog-open.json`: the curated, WinGet and Scoop records, plus the groups that link records describing the same software.
- `catalog-discord.json`: the Discord records. They are in a separate file because the source publishes no license.
- `manifest.json`: catalog version, schema version, and each file's size, SHA-256, license and source revisions.
- `manifest.json.sig`: the manifest's signature (ECDSA P-256 over SHA-256). The public key is [keys/manifest-signing.pub.pem](keys/manifest-signing.pub.pem).

Consumers verify the signature, then check every file against `manifest.json` before using it.

## Licenses

- Tools (`tools/`, workflows): MIT, see [LICENSE](LICENSE).
- Curated entries (`data/curated/`): CC0-1.0.
- Every other directory under `data/` keeps its source's license. The directory's `NOTICE.md` names the license and the attribution.

Any rights holder who wants their data removed can open an issue. It will be removed in the next release.

## 中文说明

这是一个汇总的软件识别库，用来根据强证据识别 Windows 上已安装和正在运行的软件。强证据包括商店和平台 ID、安装包产品码、包族名、包管理器 ID 和可执行文件名。

**当前库是基于上表列出的来源整理的。** 每条记录的 ID 都标明了来源。每个来源的数据单独放在 `data/` 下自己的目录里，沿用该来源的许可证。除了 `data/curated/` 里人工核对的条目，这里没有自己调查得来的数据。

主要使用者是资源管理器（Resource Manager）。它自带一份内置副本，只在用户手动要求时才从本库的发布版更新。数据是普通 JSON，任何项目都可以按上面的许可证使用。来源方如果要求移除数据，请提 issue，下一个发布版就会移除。

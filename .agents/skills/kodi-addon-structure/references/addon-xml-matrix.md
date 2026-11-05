# addon.xml and repository matrices

Primary source: `xbmc/addons/addoninfo/AddonInfoBuilder.cpp` and `xbmc/addons/Repository.cpp` (branches Matrix, Nexus, Omega, Piers = Kodi 22; master = Kodi 23 was diffed against Piers and the parsing logic read here is identical apart from refactors); schemas from `xbmc/addon-check` (`kodi_addon_checker/xml_schema/`). Where a checker schema is narrower than Kodi, Kodi (source) is the runtime truth; the checker is what the official repository enforces.

## Root and `<requires>`

| Node | Attributes | Source behaviour |
|---|---|---|
| `<addon>` | `id`, `name`, `version`, `provider-name` | id characters `A-Za-z0-9.-_@!$` only, otherwise "identifier … is invalid" |
| `<requires><import>` | `addon`, `version`, `minversion`, `optional` | `versionMin = minversion` else `version`; `MeetsVersion = !(versionMin > provider.version \|\| version < provider.abi)` |
| `<backwards-compatibility abi="x">` | in provider add-ons (e.g. `xbmc.python`) | provider's ABI floor |

`xbmc.*` versions the checker considers current (`versions.py`): `xbmc.python` advised matrix 3.0.0 / nexus 3.0.1 / omega 3.0.1 / piers 3.0.1 (Kodi Piers/22 actually provides 3.1.1, master/23 provides 3.1.2); `xbmc.gui` 5.15.0 / 5.16.0 / 5.17.0 / 5.17.0; `xbmc.json` 12.4.0 / 13.0.0 / 13.5.0 / 13.6.0; `xbmc.addon` 19.1.0 / 20.0.0 / 21.0.0 / 21.90.700. An add-on that does not import `xbmc.addon` simply has no declared minimum for it.

## Metadata elements (`xbmc.addon.metadata`)

| Element | In Kodi source | Checker xsd (`matrix_metadata.xsd`) |
|---|---|---|
| `summary`, `description`, `disclaimer`, `news` | `GetTextList`: per-`lang`, first wins, default `en_GB`, `no`→`nb_NO` | summary ≥1, ≤1500; description ≤2500; news ≤1500 (`nonEmptyStringCapped`) |
| `lang` | any | pattern `[a-z]{2,3}(_[A-Z]{2}(@\S+)?)?` |
| `platform` | whitespace-split tag list (below) | enum only `linux osx osx64 osx32 ios windx windows windowsstore android freebsd all` |
| `license`, `source`, `email`, `website`, `forum` | text | text; URLs HEAD-checked |
| `lifecyclestate` | needs `type` attr | `normal deprecated broken` |
| `broken` | legacy | — |
| `reuselanguageinvoker` | `"true"` only | `xs:boolean` |
| `assets` | `icon`, `screenshot`, `fanart`, `banner`, `clearlogo`, and (Nexus+) `thumb` | `icon fanart screenshot(≤10) clearlogo banner`; no `thumb` |
| `size`, `language` | parsed as extra info | — |
| `news` empty on installed add-on | Kodi falls back to `changelog.txt` | — |

### `<platform>` tags accepted by Kodi 22 Piers (`PlatformSupportsAddon`; same list in master/23)

`all`; `android`, `android-armv7`, `android-aarch64`, `android-i686`, `android-x86_64`; `linux`, `linux-armv7`, `linux-aarch64`, `linux-armv8`, `linux-i686`, `linux-x86_64`; `freebsd`; `windx`, `windows`, `windows-i686`, `windows-x86_64`, `windows-arm64`; `windowsstore`; `darwin_embedded`, `ios`, `ios-aarch64`, `tvos`, `tvos-aarch64`; `osx`, `osx64`, `osx-x86_64`, `osxarm64`, `osx-arm64`. The Matrix list was only partly compared with master: the per-version tag diff is `[unverified]`. Use `all` unless the add-on ships native code.

### Wiki-only facts (not enforced by Kodi core)

`<assets>` v17+; `<banner>`/`<clearlogo>` v18+; `<lifecyclestate>` v19+; version strings follow Debian ordering with `~beta`/`~alpha` forms; needs `LICENSE.txt` (addon-check regex also accepts `LICENSE` and `LICENSE.md`); lowercase file paths; no executable or binary files; XML declaration, no BOM; `xbmc.log` not `print`; no forced `Container.SetViewMode`; no direct DB access; must not install or modify other add-ons.

## Repository `<dir>` schema (`nexus_repository.xsd`, `Repository.cpp`)

| Child | Required | Attributes | Notes |
|---|---|---|---|
| `info` | yes | `compressed` (xsd only) | Kodi ignores the attribute; `.gz` or gzip content-type auto-inflated |
| `checksum` | yes | `verify` (string; Kodi accepts `md5 sha1 sha256 sha512`) | without `verify` change-detection only |
| `datadir` | yes | `zip` (boolean) | zips at `<datadir>/<id>/<id>-<version>.zip` |
| `artdir` | no | — | defaults to `datadir` |
| `hashes` | no | enum `sha256 sha512 sha1 md5` (xsd); Kodi also accepts `true`/`false` | per-zip digest from `<zip>.<type>` or header `content-<type>` |
| `dir` attrs | — | `minversion`, `maxversion` | pick the `<dir>` for the running Kodi; none matching (Kodi 21+) → "does not have any directory matching" |

`xbmc.addon.repository` `<extension>` itself needs `point` and optional `name`; the xsd requires at least one `<dir>`.

## Entry-point rules (addon-check)

`check_entrypoint.py` counts radon logical lines of code of every `library=` `.py` file and warns at ≥15 (default `max_entrypoint_count` 15). `check_files.py` raises PROBLEM for any file with the executable bit and WARNING for extensions outside its whitelist (`py xml gif png jpg jpeg md txt po json markdown yml rst ini flv wav mp4 html css lst pkla g template in cfg xsd directory help list mpeg pls info ttf xsp theme yaml dict crt ico`).

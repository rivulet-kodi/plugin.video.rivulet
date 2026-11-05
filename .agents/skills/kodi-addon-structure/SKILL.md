---
name: kodi-addon-structure
description: "Kodi add-on packaging and API compatibility for Kodi 19-22: addon.xml (requires/imports, extension points, metadata, assets, news, license, platform), repository add-ons (addons.xml, checksum, datadir, hashes), strings.po language files, string-id ranges, and Python API differences between Matrix, Nexus, Omega and Piers; use when editing addon.xml, repository.*/addon.xml, resource.language files, release/version metadata, or any code that must run across Kodi versions."
---

# Kodi add-on structure and cross-version API

Everything here was read from Kodi source (`xbmc/xbmc`, branches `Matrix`=19, `Nexus`=20, `Omega`=21, `Piers`=22 (release candidate), `master`=23 unreleased; every claim about 22 was checked on `Piers`, `master` is cited only where it differs), the Kodi wiki, or `xbmc/addon-check`. **Source code beats the wiki and the checker**: where they disagree it is called out. Anything not read in a primary source is marked `[unverified]`.

Long tables live in `references/`:
- `references/addon-xml-matrix.md` — element/attribute matrix, assets, platform tags, repository `<dir>` schema.
- `references/python-api-versions.md` — Python API changes per Kodi version, with the doxygen `@python_vNN` evidence.
- `references/addon-check-rules.md` — what `kodi-addon-checker` flags, and which rules Kodi itself never enforces.

## When to use

- Editing `addon.xml` of an add-on or `repository.*/addon.xml`; bumping a version; writing `<news>`.
- Touching `resources/language/resource.language.*/strings.po` or allocating string ids.
- Writing code that calls Kodi Python APIs that differ between 19/20/21/22.
- Reviewing the publishing pipeline for a third-party repository (index, checksum, zips).

## The spec (addon.xml)

Skeleton (Matrix and later):

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.x" name="X" version="1.2.3" provider-name="me">
  <requires>
    <import addon="xbmc.python" version="3.0.0"/>
    <import addon="script.module.requests" version="2.22.0"/>
  </requires>
  <extension point="xbmc.python.pluginsource" library="default.py">
    <provides>video</provides>
  </extension>
  <extension point="xbmc.service" library="service.py"/>
  <extension point="xbmc.addon.metadata">   <!-- `kodi.addon.metadata` is also accepted -->
    <summary lang="en_GB">…</summary> <description lang="en_GB">…</description>
    <platform>all</platform> <license>…</license> <source>…</source> <news>…</news>
    <assets><icon>resources/icon.png</icon><fanart>resources/fanart.jpg</fanart></assets>
  </extension>
</addon>
```

Root attributes `id`, `name`, `version`, `provider-name` are required by `addon.xsd`. Valid id characters (source): `A-Za-z0-9.-_@!$`; anything else is rejected at load (`AddonInfoBuilder.cpp`).

### `<import>` semantics (source: `AddonInfoBuilder.cpp`, `AddonInfo.h`, `AddonInfo.cpp::MeetsVersion`)

| Attribute | Meaning |
|---|---|
| `addon` | id of the dependency (required) |
| `version` | the API version you were written against. With no `minversion` it is also the **minimum** provider version |
| `minversion` | explicit minimum provider version; if absent it defaults to `version` |
| `optional="true"` | dependency may be missing |

Compatibility test: `!(minversion > provider.version || version < provider.abi)`, where `provider.abi` is the provider's `<backwards-compatibility abi=…>`. Only `xbmc.*`/`kodi.*` imports are checked at install-compat time (`CAddonMgr::IsCompatible`); others are resolved by the installer.

`xbmc.python` provided versions (source: `addons/xbmc.python/addon.xml`):

| Kodi | provides | ABI |
|---|---|---|
| 19 Matrix | 3.0.0 | 3.0.0 |
| 20 Nexus | 3.0.1 | 3.0.0 |
| 21 Omega | 3.0.1 | 3.0.0 |
| 22 Piers | 3.1.1 | 3.0.0 |
| 23 master (unreleased) | 3.1.2 | 3.0.0 |

So `version="3.0.0"` installs on Kodi 19 through 22 (and master); `version="3.0.1"` excludes Matrix (wiki's table stops at Nexus; source wins). `xbmc.python` 2.x is the Python 2 line and is rejected by addon-check (`check_python_dependency`: "Add-ons should target Python 3 and up").

### Extension points used by Python add-ons

| `point` | Required attrs | Notes |
|---|---|---|
| `xbmc.python.pluginsource` | `library` | child `<provides>` = space list of `video audio image game executable`; every invocation is `plugin://` + `sys.argv` |
| `xbmc.python.script` | `library` | `RunScript()` target |
| `xbmc.service` | `library` | started on profile login, stopped on logout/exit; must poll `xbmc.Monitor.abortRequested()` / `waitForAbort()` |
| `xbmc.addon.repository` | `name` | see "Repository add-ons" |
| `xbmc.addon.metadata` | — | exactly one; `kodi.addon.metadata` accepted as alias |

`start="startup"` on `xbmc.service` is **dead since Leia**: the wiki says the option was removed and `Service.cpp` (Matrix, Piers, master) never reads a `start` attribute. The generic parser stores it as extension info, nothing consumes it. Omit it. (checker schema `leia_service.xsd` also does not list `start`; whether the checker rejects it was not run.)

### Metadata block

| Element | Rule | Source |
|---|---|---|
| `<summary lang="en_GB">` | required, one per language, first wins per `lang`; missing `lang` defaults to `en_GB`; `lang="no"` maps to `nb_NO` | `AddonInfoBuilder.cpp::GetTextList`; addon-check xsd: min 1, max 1500 chars |
| `<description>` | optional per language; checker cap 2500 chars | addon-check `matrix_metadata.xsd` |
| `<disclaimer>` | optional per language | `GetTextList` |
| `<news>` | free text; **checker/wiki cap 1500 chars, Kodi core does not enforce it** | wiki + xsd `nonEmptyStringCapped`; `AddonInfoBuilder.cpp` |
| `<platform>` | whitespace-separated tag list | see `references/addon-xml-matrix.md` |
| `<license>` | free text (SPDX recommended). Wiki separators: `\|` choice, `&` or `,` multiple. SPDX `AND`/`OR` are **not** in the wiki grammar | wiki Addon.xml |
| `<source>`, `<website>`, `<forum>`, `<email>` | optional; checker HEAD-requests source/website/forum | `check_url.py` |
| `<assets>` | `icon`, `fanart`, `screenshot`(≤10), `banner`, `clearlogo`; Kodi ≥20 also parses `thumb` (checker schema does not allow it) | `AddonInfoBuilder.cpp`; xsd |
| `<lifecyclestate type="normal\|deprecated\|broken">` | only honoured if `type` is present; `<broken>` is the legacy form | `AddonInfoBuilder.cpp` |
| `<reuselanguageinvoker>` | `true` exactly; anything else is false | `ScriptRunner.cpp` (Nexus+), `PluginDirectory.cpp` (Matrix) |
| `<size>`, `<language>` | parsed into extra info | `AddonInfoBuilder.cpp` |

Asset constraints (addon-check `check_artwork.py`; wiki "Add-on rules/structure" agree except where noted):

| Asset | Format | Size | Transparency | Max file |
|---|---|---|---|---|
| icon | PNG | 256×256 or 512×512 | none allowed | — |
| fanart | jpg/jpeg/png | 1280×720, 1920×1080 or 3840×2160 | any | 1000 KB (wiki: 1 MB) |
| screenshot | jpg/jpeg/png | 1280×720 or 1920×1080 | none allowed | 750 KB |
| banner | jpg/jpeg/png | 1000×185 | none allowed | — |
| clearlogo | PNG | 400×155 or 800×310 | required | — |

`has_transparency` is true if any pixel in an RGBA image has alpha < 255, not only if the mode is RGBA.

## Repository add-ons (`xbmc.addon.repository`)

Source: `xbmc/addons/Repository.cpp` (identical semantics in all four branches for the elements below).

```xml
<extension point="xbmc.addon.repository" name="…">
  <dir minversion="19.0.0" maxversion="…">           <!-- both optional; several <dir> allowed -->
    <info>https://host/addons.xml</info>             <!-- .gz or gzip content-type is auto-inflated -->
    <checksum verify="sha256">https://host/addons.xml.sha256</checksum>
    <datadir zip="true">https://host/</datadir>
    <artdir>https://host/</artdir>                   <!-- defaults to datadir -->
    <hashes>sha256</hashes>                          <!-- per-zip digests -->
  </dir>
</extension>
```

| Element/attr | Behaviour in source |
|---|---|
| `<info compressed="…">` | **attribute is never read**; compression is detected from the `.gz` extension or MIME type |
| `<checksum>` | URL to a digest of the index; used for change detection |
| `<checksum verify="md5\|sha1\|sha256\|sha512">` | Kodi additionally verifies the downloaded index against that digest type; without `verify` there is no integrity check |
| `<hashes>` | `true` (deprecated alias of md5) \| `false` \| `md5\|sha1\|sha256\|sha512`; each zip's digest is fetched from `<zip>.<type>` or HTTP header `content-<type>`; md5 logs "broken hash function" |
| plain `http://` or `verifypeer=false` datadir | logged as insecure |
| `<info>` directly under the extension | old schema; error "uses old schema definition" |
| no `<dir>` matches the running Kodi | Kodi 21+ logs "does not have any directory matching" |

The zip layout is `<addon-id>/<addon-id>-<version>.zip` under `datadir`; the version in the filename must equal `addon.xml` `version`. Repository add-ons themselves are normal add-ons: they update through the repository they install.

## Per-version differences (19 / 20 / 21 / 22)

| Area | 19 Matrix | 20 Nexus | 21 Omega | 22 Piers |
|---|---|---|---|---|
| `xbmc.python` provides | 3.0.0 | 3.0.1 | 3.0.1 | 3.1.1 (master/23: 3.1.2) |
| bundled CPython (depends builds; Linux distro builds use system Python `[inference]`) | 3.8.5 | 3.11.2 | 3.11.7 | 3.14.10 (same on master) |
| `<assets><thumb>` parsed | no | yes | yes | yes |
| `xbmc.translatePath/validatePath/makeLegalFilename` | present, deprecated | **removed** | removed | removed |
| `xbmcvfs.translatePath` etc. | new | yes | yes | yes |
| `LOGNOTICE`/`LOGSEVERE` | removed | removed | removed | removed |
| `ListItem.setInfo` | full | partially deprecated (warns) | same | same; `playcount`/`overlay` still honoured (`ListItem.cpp` on Piers: overlay :421, playcount :462) |
| `InfoTagVideo` setters | read-only tag | setters added | + a few | + more (see reference) |
| `Addon.getSettings()` / `Settings` class | no | yes | yes | yes |
| `Addon.getSettingBool/Int/Number/String`, `setSetting*` | ok | deprecated | deprecated | deprecated |
| `Dialog.yesno(defaultbutton=)` | no | yes | yes | yes |
| `ListItem.setFileTitle/setCount/setSize/setOverlay` | no | no | no | yes |
| `Player.getSubtitles()` / `getAvailable{Audio,Subtitle,Video}Streams()` language strings | ISO 639-2 (`eng`) | same | same | **BCP 47** (`en`, `en-AU`); `xbmc.getLanguage(fmt)` returns `""` when the language has no code in `fmt` |
| Python bindings | generated | generated | generated | **stock SWIG `-builtin`** (xbmc/xbmc#28958, merged 2026-09-08; its merge commit is an ancestor of `Piers`, and `Piers` carries `xbmc/interfaces/swig/SwigRuntime.cpp` and `kodi_*.i`): subclasses of `xbmc*`/`xbmcgui` types reject class-level `setattr`/`delattr` |

Details and `@python_vNN` quotes: `references/python-api-versions.md`.

## Language files

Source: `xbmc/resources/LocalizeStrings.cpp`, `xbmc/utils/POUtils.cpp`; wiki "Language support".

- Layout: `resources/language/resource.language.<ll_CC lowercase>/strings.po`. Source language dir is `resource.language.en_gb` (hard-coded default `LANGUAGE_DEFAULT`). Missing language or missing id falls back to en_GB. (Kodi 19 and 22 `LocalizeStrings.cpp` also accept the pre-Leia default name `English` (`LANGUAGE_OLD_DEFAULT`) and Piers has `FindLegacyLanguage`; `master`/23 removed both. Do not depend on them.)
- An entry is recognised only with `msgctxt "#<decimal id>"`. **For en_GB the text is the `msgid`** (`msgstr` is ignored); for every other language the text is `msgstr`, and an empty `msgstr` falls back to the English `msgid`.
- Header entry with empty `msgid ""` is required by addon-check; UTF-8 without BOM.
- Id ranges (wiki "Language support"): `30000-30999` plugins and plugin settings, `31000-31999` skins, `32000-32999` scripts, `33000-33999` common strings.
- `xbmcaddon.Addon().getLocalizedString(id)` reads the add-on's own `.po`. `xbmc.getLocalizedString(id)` reads only core strings and, for skin ids, the active skin — never a plugin's. A missing id returns `""`.
- Never renumber published ids; allocate above the maximum.

## Best practices

1. Version in `addon.xml` is the single source; the zip filename, tag and index must agree. Bump on every release; artwork-only changes need a version bump too (the `repository.rivulet` 1.0.2 news records Kodi refetching a repository add-on's icon only when its version changes; not confirmed in Kodi source `[unverified]`).
2. Declare the lowest API you actually need (`xbmc.python 3.0.0` for a Kodi 19 floor). Never raise `version` just to silence a warning; it hides the add-on on older Kodi.
3. Keep entry-point files (`library=`) thin: bootstrap `sys.path`, then import. addon-check warns at ≥15 logical lines per entry point.
4. Gate version differences in one compat module, by feature detection or `System.BuildVersion` major, not scattered `if`s.
5. Paths: `xbmcvfs.translatePath('special://…')`. Logging: `xbmc.log`, never `print`.
6. Never ship executable bits on files in the zip (checker: PROBLEM); `RunScript()` does not need them.
7. Repository: publish a sha256 index digest and `checksum verify="sha256"`; publish `<zip>.sha256` and `<hashes>sha256</hashes>`; serve over https.
8. Keep `<news>` ≤1500 chars if the add-on should pass addon-check; keep history in a repo `CHANGELOG`, not in the XML every client downloads with `addons.xml`.
9. `<reuselanguageinvoker>` only for stateless plugin scripts: see next section.

### `reuselanguageinvoker` decision

Source: Matrix `PluginDirectory.cpp:168-173`; Nexus+ `ScriptRunner.cpp:69-78`; `PythonInvoker.cpp` execute/reset; `ScriptInvocationManager`.

When `true`, Kodi keeps one sub-interpreter thread and re-executes the script in `__main__`'s dict with the **same `sys.modules`**. Consequences: module-level code in imported modules runs once; cached `sys.argv` goes stale (wiki warns); globals persist between plugin calls; only `InvokerStateScriptDone` keeps the thread reusable. Use it only if (a) all per-call state is re-derived from `sys.argv` on each run, (b) no module-global singletons, (c) no windows or threads outliving `main()`.

## This repo's traps and conventions (verified)

- **Single compat layer**: `lib/ui/compat.py` is the only sanctioned path to version-dependent Kodi APIs (`kodi_major_version()`, `set_video_info`, `set_resume_point`, `set_video_cast`, `setting_bool`/`setting_int`). Paths use `xbmcvfs.translatePath`. `L()` is `lib/ui/compat.py:28` and uses `Addon.getLocalizedString`.
- **Direct `setInfo('video', {'playcount','overlay'})`** bypasses compat: `lib/ui/detailwindow.py:530`, `:904`, `lib/ui/home.py:180`. Works on 19-22 (Piers and master still honour both keys) but logs a deprecation warning on Kodi 20+ per call.
- **Settings read as raw strings**: `getSettingBool/Int` have been observed misbehaving live, so code reads `getSetting()` text (`lib/settings.py`, `lib/ui/compat.py:80-96`). Keep it.
- **Kodi 22 SWIG**: never write to a Kodi-derived class after creation (AGENTS.md "Never write to a Kodi-derived class"; fix `e350763`, issue #64). Decorate in the class body (`@gated_input`).
- **Version metadata**: `addon.xml` `version` drives `release/X.Y.Z` PRs and the tag check (AGENTS.md "Release"); `repository.rivulet/addon.xml` has its own version (`vX.Y.Z-repo` tags, `.github/workflows/repo.yml`).
- **Publishing**: `repo.yml` builds the zip with `git archive` (file modes are preserved), writes `addons.xml`, and an md5 digest file (`hashlib.md5`, lines 194-200).
- **Localization**: ids from `#30000`; every user-visible string must exist in all 14 locales; `tests/test_localization.py` enforces unique numeric ids and that every `L()` id exists. Ids are currently `#30000-#30554`.
- **Entry point**: `default.py` mutates `router.BASE_URL` / `router.ADDON_HANDLE` at import and relies on process teardown ordering (`home.release_items()`, AGENTS.md "Home's ListItems"): never enable `reuselanguageinvoker`.
- **Bridge**: `resources/s4me_bridge/bridge.py` is started through `RunScript(path,port)` (`lib/s4me.py:259`) with a single positional argument; it is a separate Kodi script, not a Python subprocess.

## Review checklist

- [ ] `addon.xml` is well-formed, has the XML declaration, no BOM, `id`/`name`/`version`/`provider-name`.
- [ ] `xbmc.python` import ≥3.0.0 and ≤ the lowest Kodi you support provides; `script.module.*` versions exist in every supported Kodi repo (`mirrors.kodi.tv/addons/<branch>/addons.xml.gz`).
- [ ] `en_GB` `summary` present; description/summary within caps; `<news>` length known and intentional.
- [ ] `<license>` spelled in a form the wiki documents, and a `LICENSE`/`LICENSE.txt`/`LICENSE.md` file ships.
- [ ] Assets: icon 256/512 PNG **without alpha < 255**; fanart ≤1000 KB at a legal size; screenshots 1280×720 or 1920×1080, ≤750 KB, no alpha.
- [ ] No `start=` on `xbmc.service`; no `compressed=` expectation on repository `<info>`.
- [ ] Repository add-on: `checksum verify=…`, `<hashes>`, https, index and zip digests produced by CI.
- [ ] No `xbmc.translatePath`/`validatePath`/`makeLegalFilename`; no `LOGNOTICE`/`LOGSEVERE`; version-dependent calls go through the compat layer.
- [ ] New `ListItem.setInfo` calls are replaced with `InfoTagVideo` setters on Kodi ≥20 (feature-detected).
- [ ] No class-level writes to Kodi-derived classes (Kodi 22).
- [ ] strings.po: `msgctxt "#id"`, ids in 30000-30999 and never reused; en_gb present; all locales have the id.
- [ ] No file in the zip has the executable bit; no non-whitelisted extension.
- [ ] `reuselanguageinvoker` absent unless the plugin script is fully stateless.

## Sources

- https://kodi.wiki/view/Addon.xml
- https://kodi.wiki/view/Add-on_rules
- https://kodi.wiki/view/Add-on_structure
- https://kodi.wiki/view/Language_support
- https://kodi.wiki/view/Service_add-ons
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/addoninfo/AddonInfoBuilder.cpp (and `/Matrix/`, `/Nexus/`, `/Omega/`; `/master/` is Kodi 23)
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/addoninfo/AddonInfo.h and `AddonInfo.cpp`
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/AddonManager.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/Repository.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/Service.cpp (and `/Matrix/`; `/master/` identical to Piers)
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/addons/xbmc.python/addon.xml (and `/Matrix/`, `/Nexus/`, `/Omega/`; `/master/` is Kodi 23)
- https://raw.githubusercontent.com/xbmc/xbmc/Matrix/xbmc/filesystem/PluginDirectory.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/generic/ScriptRunner.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/python/PythonInvoker.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/resources/LocalizeStrings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/utils/POUtils.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/legacy/ModuleXbmc.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/legacy/ListItem.cpp, `ListItem.h`, `Addon.h`, `ModuleXbmc.h`, `ModuleXbmcvfs.h`, `InfoTagVideo.h` (and the other branches)
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/tools/depends/target/python3/PYTHON3-VERSION (Matrix: `Makefile`)
- https://github.com/xbmc/xbmc/pull/28958 (SWIG-direct bindings; present in `Piers` = Kodi 22 and in `master` = Kodi 23)
- https://github.com/xbmc/xbmc/tree/Piers/xbmc/interfaces/swig (`SwigRuntime.cpp`, `kodi_*.i` on the Piers branch)
- https://github.com/xbmc/addon-check (`kodi_addon_checker/check_artwork.py`, `check_files.py`, `check_entrypoint.py`, `check_dependencies.py`, `common/__init__.py`, `xml_schema/matrix_metadata.xsd`, `nexus_repository.xsd`, `leia_service.xsd`, `addon.xsd`)
- https://mirrors.kodi.tv/addons/matrix/addons.xml.gz (also `nexus`, `omega`, `piers` = Kodi 22)

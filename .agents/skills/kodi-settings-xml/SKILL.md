---
name: kodi-settings-xml
description: "Kodi add-on resources/settings.xml (format version 1) and the xbmcaddon settings API on Kodi 19-22: element/attribute rules, controls, constraints, dependencies, persistence and this repo's traps; use when adding, editing or reviewing any setting, its strings, or code that reads/writes a setting."
---

# Kodi add-on settings (settings.xml v1 + Python API)

Source of truth is Kodi's C++ (`xbmc/settings/**`, `xbmc/addons/settings/**`,
`xbmc/interfaces/legacy/Addon.cpp`). The wiki pages are partly stale: where a
wiki page and the source disagree, the source wins and this file says so.
Long tables live in `references/`:

- `references/settings-types.md` - type x control x format matrix, control
  children, constraints, level table, legacy conversion table.
- `references/dependencies-and-conditions.md` - `enable`/`visible`/`parent`,
  operators, `<requirement>` conditions per Kodi version.
- `references/api-and-persistence.md` - read/write API semantics, dialog
  OK/Cancel behaviour, saved-file format, actions.

## When to use

- Adding, renaming, removing or re-typing a `<setting>` in `resources/settings.xml`.
- Writing or reviewing code that calls `getSetting*`, `setSetting*`, `openSettings`,
  `Monitor.onSettingsChanged`, or an `action` setting's built-in.
- Reviewing translations/labels/help text for settings.

## The spec in one page

File: `<addon>/resources/settings.xml`. Skeleton (the only valid shape):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<settings version="1">
  <section id="my.addon.id">
    <category id="general" label="30001" help="30002">
      <group id="1" label="30003">
        <setting id="my_flag" type="boolean" label="30010" help="30011">
          <level>0</level>
          <default>true</default>
          <control type="toggle"/>
        </setting>
      </group>
    </category>
  </section>
</settings>
```

Hard rules (each verified in source; details in the references):

1. `<settings version="1">` is mandatory. Missing/0 means the legacy format and
   a different parser. Exactly one `<section>`, whose `id` is the add-on id.
2. `category`/`group`/`setting` ids are required and non-empty; a repeated id
   merges instead of erroring.
3. `label` and `help` are ATTRIBUTES holding integer string ids. A `<help>`
   child element is ignored by every Kodi version (19, 20, 21, 22):
   `ISetting::Deserialize` only reads the `help` attribute, `<visible>` is the
   only one of those that is a child element.
4. Every `<setting>` needs a `<control>` (except level 4). Valid
   type/control/format pairs are in `references/settings-types.md`; the add-on
   dialog does not register a `label` control.
5. `boolean` and `integer` need a non-empty `<default>`; `string` needs
   `<default>` unless `<allowempty>true</allowempty>`. An empty `<default/>`
   on a boolean/integer makes the setting fail to load.
6. Integer/number bounds are enforced only when `minimum != maximum`
   (both default 0). `<minimum>0</minimum>` alone validates nothing; always
   give both.
7. `<level>`: 0 Basic, 1 Standard, 2 Advanced, 3 Expert, 4 Internal (never
   shown). It is compared with the user's GLOBAL GUI level (default
   Standard), so level 2/3 settings are invisible to default users.
8. `<hidden>true</hidden>` on an `edit` control masks input only; the value is
   stored in clear text in `userdata`. The wiki's `<verifynewvalue>` does not
   exist; the element is `<verifynew>`.
9. `<writable>` only matters for `button format="path"` (folder). When true
   Kodi creates, then deletes, a file named `1` inside the chosen folder
   (overwriting an existing file of that name). It is ignored for `file`/`image`.
10. `masking` `executable` is non-empty only on Windows.
11. Stored values for ids not in the definition are kept as hidden Internal
    strings, returned by `getSetting`, and re-saved. Renames leave stale
    values unless `<updates><update type="rename">old</update></updates>` is used.
12. A stored value flagged `default="true"` is reset to the CURRENT `<default>`
    on load; changing a default in a release changes behaviour for every
    user who never changed it.

## Per-version differences (19 / 20 / 21 / 22, plus 23 master)

| topic | 19 Matrix | 20 Nexus | 21 Omega | 22 Piers (RC) | 23 master (unreleased) |
|---|---|---|---|---|---|
| v1 `settings.xml` | supported | same | same | same | same |
| `<help>` as child | ignored | ignored | ignored | ignored | ignored |
| `colorbutton` control | absent | added | present | present | present |
| `Addon.getSettings()` / `xbmcaddon.Settings` | absent | added | present | present | present |
| `getSettingBool/Int/Number/String` | present (since 18) | same | same | same | same |
| `InfoBool` property condition | present | present | present | present | present |
| `CSettingsManager::Version` (definition `version=` must not exceed it) | 2 | 2 | 2 | 4 | 5 |
| `<requirement>` simple conditions | base set | + `has_filesystem_nfs`, `have_libbluray`, `has_cdda_ripper`, `has_dvd_drive` | `has_dvd_drive` -> `has_optical_drive`, + `have_webos`, `has_xbmchelper` | + `have_gbm`, `has_uwp`; `has_xbmchelper` gone | + `have_android`, `have_wasm` |
| slider default value format (`{:d}`, `{} %`, `{:.1f}`) | fmt-style | fmt-style | fmt-style | fmt-style | fmt-style |

Compared across all five branches: `<help>` handling (ISetting/AddonSettings),
control type registry, `InfoBool`, simple conditions, `Version`, slider
default format. Also re-checked on `Piers`: integer/number `CheckValidity`
(`m_min != m_max`), `UpdateSettingInActiveDialog`, dialog confirm/reload,
`CAddonMgr::ReloadSettings` first-match, `<close>` handling, `Writable` use,
`<verifynew>`, `WrongTypeException`. The remaining references were read on
`master` (23); other per-branch drift is `[unverified]`.
The floor of this repo is Kodi 19, so use only what the Matrix column has
unless the use is guarded.

## Best practices

- Put a non-edit control (toggle/action/list) FIRST in each category. Kodi
  focuses the first setting of a category when it opens; an `edit` control
  entered that way traps the arrow keys on some builds (see repo trap below).
- Give every user-facing setting `help="NNNNN"` (attribute). Use
  `<heading>` on every `edit`/`button`/`list`/`slider` control.
- Give both `<minimum>` and `<maximum>` on integers (ports 1-65535 or 0-65535).
- Gate dependent settings with `<dependency type="enable">` on the toggle that
  governs them (not just `parent=`, which only reacts to a disabled parent).
- Mask secrets with `<hidden>true</hidden>`; never log setting values.
- Use `type="string"` + `<options>` + `list`/`spinner` for fixed choices, and
  keep option values identical to what the code compares against.
- Use groups (`<group label=...>`) to replace the legacy `lsep`/`sep`.
- Storage-only settings: `<visible>false</visible>` still loads and saves,
  and is not built into the dialog, so "Reset" does not touch it. A level 4
  (Internal) setting may omit `<control>`; below level 4 a missing
  `<control>` rejects the setting.
- Read settings defensively: raw `getSetting()` + parse + literal default. The
  typed getters raise `WrongTypeException` on a missing/mismatched id.
- Do not write settings from a process while the dialog may be open and expect
  them to persist: see "Dialog trap".
- Keep `<default>` in XML and any literal default in code in sync, in the same change.
- New label/help string => add the id to every `strings.po`, not just en_gb.

### Dialog trap (applies to every version)

While the add-on settings dialog is open, `setSetting` for that add-on is
routed into the dialog's in-memory state (an undefined id is dropped). The
value is written to disk only when the user presses OK; Back/Escape/Cancel
reloads the file and discards it. Consequences:

- An `action` setting that calls `setSetting` (via `RunPlugin`) loses the
  write on cancel, though its other side effects remain.
- OK saves the dialog's whole in-memory state and overwrites writes other
  processes made meanwhile.
- `<close>true</close>` on the action saves and closes first, then runs the
  built-in, so a write inside it goes straight to disk (at the cost of
  closing the dialog on every click).

## This repo's traps and conventions

Verified in the working tree (`/resources/settings.xml`, AGENTS.md, lib/, tests/):

- Single `<section id="plugin.video.rivulet">`, 10 categories in user order
  (`home`, `playback`, `account`, `server`, `server_bittorrent`,
  `server_resources`, `server_proxy`, `server_addons`, `server_diagnostics`,
  `s4me`), split into labelled groups (Lists / Ratings / Maintenance,
  Sources / Resume / Subtitles / Buffering, ...). Category ids are not stored,
  so regrouping is free; setting ids are stored, so never rename one. Every
  setting has `<level>` and a `help` attribute; ids are 30xxx present in all
  14 locales' `strings.po` (new ids go above the current max in
  `resources/language/resource.language.en_gb/strings.po` and into every
  locale).
- `tests/test_settings_xml.py` enforces the spec rules that silently break in
  Kodi: no `<help>` element and a `help` on every shown setting, both integer
  bounds, ports within 0/1-65535, dependency targets that exist, every
  `EXTRA_ENV_SETTINGS` row gated by `server_enable` (except `server_url` and
  `bt_keep_torrents`, which matter for an external server), masked secrets,
  `<close>` on the key-pairing actions.
- First-setting rule: `tests/test_settings_xml.py` fails any category whose
  first setting is an `edit` control with nothing to move to, and requires
  `server_url` not to be first in its category. Keep `server_enable` /
  `s4me_enable` first; the `ratings_*_key` edits sit last in `home`.
- Env-forwarded server settings: `lib/service_runner.py` `EXTRA_ENV_SETTINGS`
  (setting id -> env var -> kind `string|int|mb_to_bytes|bool`) and
  `EXTRA_ENV_TYPED_DEFAULTS` (fallback for untouched settings). A new
  engine-facing setting needs: the `<setting>`, a row in
  `EXTRA_ENV_SETTINGS`, a typed default (non-string kinds) equal to the XML
  `<default>`, and strings in all locales. `int` kinds forward the raw
  `str(value)` to the engine, so XML bounds are the only validation.
- `ServiceMonitor.onSettingsChanged` (`lib/service_runner.py`) re-snapshots
  and restarts the engine only when the snapshot (enabled, binary, url,
  force_library, env rows) changes.
- Reads go through `lib/settings.py` `setting_bool`/`setting_int`
  (wrapped by `lib/ui/compat.py`): raw `getSetting()` parse, never raises,
  default on empty/unparseable. Keep using it; the typed getters raise on a
  missing id (the file's docstring attributes observed wrong values to them;
  the source shows exceptions, not wrong values `[unverified which one the
  observation was]`).
- Python-side literal defaults duplicate XML `<default>`s in many call sites
  (e.g. `lib/ui/player.py` `setting_bool('resume_ask', True)`,
  `lib/ui/streamswindow.py` `_BINGE_COUNTDOWN_DEFAULT_SECONDS`). Change them
  together.
- Hidden storage settings: `cache_generation` and `home_layout_generation`
  (`type="string"`, `<visible>false</visible>`, edit control) are bumped by
  `lib/ui/cacherefresh.py` and `lib/ui/homelists.py` and compared by
  `lib/ui/home.py` `_home_signature`. They are written by `RunPlugin` actions
  fired from the open dialog, so the Dialog trap applies; Home has a second
  signal for each (the saved layout itself, deleted cache files), which is why
  those actions carry no `<close>` (it would run them after Home looked). The
  key-pairing actions (`ratings_pair_*`) do carry `<close>true</close>`: their
  only output is the setting.
- Retired ids (`home_show_continue`, `home_show_mystuff`, `home_show_movies`,
  `home_show_series`) exist only as stale stored values; migration reads them
  with raw `getSetting` (`lib/ui/homewindow.py:13-45`), which works because
  Kodi keeps undefined stored ids. Follow that pattern or use `<updates>`.
- Action settings use `<data>RunPlugin(plugin://plugin.video.rivulet/?action=...)</data>`
  with `<control type="button" format="action"/>`; handlers are in the
  `dispatch` table of `lib/ui/router.py`. `&` must be written `&amp;` in the XML.
- `cache_dir` deliberately uses `<writable>false</writable>` (see rule 9; the
  comment in settings.xml is accurate).
- `s4me_*` are hidden from non-Italian users via `<dependency type="visible">`
  with `InfoBool` conditions; `String.IsEqual(System.Language,Italian)` only
  matches the English language name, so non-English UIs on an Italian locale
  never see the category (documented in the XML comment).

## Review checklist

1. Header: `version="1"`, one `<section>` with the add-on id.
2. Every setting: `id` unique, `type`, integer `label`, `<level>`, valid
   `<control>` for the type, `<default>` valid for the type.
3. No `<help>` element; `help="NNNNN"` attribute instead.
4. Integers/numbers: both `minimum` and `maximum`; ports 0/1..65535.
5. Secrets (keys, passwords, tokens) have `<hidden>true</hidden>`.
6. Settings that only matter when another is on carry an `enable` dependency;
   dependency `setting=` ids exist (a typo evaluates false).
7. `list`/`spinner` options match the values compared in code; `allowempty`
   consistent with `<default>`.
8. `path`: `writable=false` unless a writable folder check is wanted; format
   `file`/`image`/`path` consistent with the intent.
9. Actions: `<data>` valid built-in, `&amp;` escaped, handler exists, and no
   write-then-cancel loss (Dialog trap) for state that must persist.
10. Every label/help/heading/option id exists in all locale `strings.po`.
11. Env-forwarded settings: `EXTRA_ENV_SETTINGS` row + typed default match.
12. New/changed `<default>` considered for users with `default="true"` values.
13. First setting of each category is not an `edit` (`tests/test_settings_xml.py`).
14. Reads use the defensive wrapper; no new `getSettingBool` calls.

## Sources

Kodi source (raw). URLs use `Piers` (Kodi 22, the branch to read first); the same
paths exist on `master` (Kodi 23, unreleased), `Omega` (21), `Nexus` (20) and
`Matrix` (19): swap the branch name to compare:

- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/lib/ISetting.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/lib/Setting.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/lib/SettingSection.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/lib/SettingDependency.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/lib/SettingsManager.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/lib/SettingDefinitions.h
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/lib/SettingLevel.h
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/SettingControl.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/SettingPath.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/SettingConditions.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/windows/GUIControlSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/dialogs/GUIDialogSettingsBase.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/settings/AddonSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/gui/GUIDialogAddonSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/AddonManager.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/legacy/Addon.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/legacy/Settings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/dialogs/GUIDialogFileBrowser.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/utils/XMLUtils.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/view/ViewStateSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/addons/resource.language.en_gb/resources/strings.po (ids 186, 222, 10036-10039)

Wiki (secondary; stale where it disagrees with the code above):

- https://kodi.wiki/view/Add-on_settings (legacy format, marked deprecated for Matrix+)
- https://kodi.wiki/view/Add-on_settings_conversion (v1 structure, level table, type mapping)

Checker:

- https://github.com/xbmc/addon-check (its `kodi_addon_checker/xml_schema` contains no settings.xml schema, so settings.xml is not validated by it)

Not fetched: Kodi's Python API docs (xbmcaddon) and the wiki Language support
page; claims depending on them are marked `[unverified]`.

# Settings types, controls, constraints (format version 1)

Every row below is verified against Kodi source (see the Sources list in
`../SKILL.md`). `Piers` is Kodi 22 and `master` is Kodi 23 (unreleased); branches `Matrix`/`Nexus`/`Omega` were
compared where a row says so.

## Setting `type` x allowed `<control>`

A `<setting>` with no `<control>` is rejected ("missing <control>") unless its
level is Internal (4). Control `type` is matched case-insensitively.

| setting `type` | `<default>` | allowed `<control type= format=>` | notes |
|---|---|---|---|
| `boolean` | required, `true`/`false` | `toggle` (format empty or `boolean`) | empty `<default/>` fails to load (XMLUtils::GetBoolean returns false for empty). |
| `integer` | required, integer | `spinner` (format `string`/`integer`/`number`), `edit` (`integer`), `slider` (`integer`/`percentage`), `list` (`integer`/`string`), `button` | with `<options>` the value is the option's text; label from `label=`. |
| `number` | required, float | `spinner`, `edit` (`number`), `slider` (`number`) | float. |
| `string` | required unless `<allowempty>true` | `edit` (`string`/`ip`/`md5`/`urlencoded`), `list` (`string`), `spinner`, `button` (`path`/`file`/`image`/`addon`/`action`/`infolabel`), `colorbutton` (Nexus+) | `allowempty` defaults false: an empty value is rejected on edit. |
| `urlencodedstring` | as string | `edit format="urlencoded"` | add-on-only type (`AddonSettings::InitializeSettingTypes`). |
| `path` | required unless `allowempty` | `button` with `format` `path` (folder), `file`, `image` | constraints `writable`, `masking`, `sources`, `hideextensions`. |
| `addon` | as string | `button format="addon"` | constraint `<addontype>` mandatory. |
| `date` | `YYYY-MM-DD` | `button format="date"` | |
| `time` | `HH:MM` | `button format="time"` | |
| `action` | none (never stored) | `button format="action"` | `<data>` = built-in to run. |
| `list[string]`, `list[integer]`, `list[addon]` | delimiter-joined | `list` / `button format="addon"` | constraints `delimiter` (default `\|`), `minimumitems`, `maximumitems`. |

## Control element children

| control | child elements (all optional) |
|---|---|
| `toggle` | none |
| `spinner` | `<formatlabel>` (int string id, `format="string"`), `<minimum label="N">` inside constraints gives a name to the minimum value |
| `edit` | `<heading>`, `<hidden>` (mask input with `*`), `<verifynew>` (ask twice; NOT `verifynewvalue`) |
| `button` | `<heading>`, `<hidevalue>`; `format=action`: `<close>`, `<data>`; `format=addon`: `<show more= details=>all\|installed\|installable</show>`; `format=file`: `<usethumbs>`, `<treatasfolder>` |
| `list` | `<heading>`, `<multiselect>`, `<hidevalue>`, `<addbuttonlabel>`, `<formatlabel>` |
| `slider` | `<heading>`, `<popup>` (true: opens a slider dialog from a button), `<formatlabel>` |
| `range` | `<formatlabel>` |
| `title` | in a `<group>`: `<separatorposition>top\|bottom</separatorposition>`, `<hideseparator>` |
| `colorbutton` | Nexus+ only; default is an `AARRGGBB` hex string |

Attribute on `<control>`: `delayed="true"` applies the value after a short
timer (for spinners/sliders). Any other value of `delayed` is an error.

The add-on settings dialog registers only `toggle`, `spinner`, `edit`,
`button`, `list`, `slider`, `range`, `title`, `colorbutton`. `label` is not
registered for add-ons (core `SettingControl.cpp` has it, `AddonSettings.cpp`
does not).

## Constraints

| element | applies to | meaning |
|---|---|---|
| `<minimum>`, `<maximum>`, `<step>` | integer, number | bounds enforced ONLY IF `minimum != maximum`. Defaults are min 0, max 0, step 1, so `<minimum>0</minimum>` alone is a no-op. |
| `<options>` / `<option label="N">value</option>` | integer, string | choices. `label` int = string id (translated), non-int = literal text, absent = value shown. `<options sort="ascending">` sorts. Text in `<options>` itself names a dynamic options filler. |
| `<allowempty>` | string, path, addon | default false. |
| `<allownewoption>` | string with options | user may type a new value. |
| `<delimiter>`, `<minimumitems>`, `<maximumitems>` | list | |
| `<writable>` | path, `format="path"` ONLY | default true. true makes Kodi write+delete a file named `1` in the chosen folder (see `GUIDialogFileBrowser.cpp`, folder branch), overwriting any file with that name. Ignored for `file`/`image`. |
| `<masking>` | path, `file`/`image` | `video`, `audio`, `image`, `executable`, or a mask such as `*.png`, `$AUDIO`. `executable` expands to `.exe|.bat|.cmd|.py` on Windows only and to empty elsewhere. |
| `<sources><source>` | path | restrict the browser to a Kodi media source name. |
| `<addontype>` | addon | e.g. `xbmc.metadata.scraper.movies`. |

## Group / category / setting attributes

| element | attributes | notes |
|---|---|---|
| `<settings>` | `version="1"` | definition rejected if greater than `CSettingsManager::Version` (2 in Matrix/Nexus/Omega, 4 in Piers, 5 in master). Missing or 0 means the legacy format. |
| `<section>` | `id` = add-on id | exactly one in an add-on. |
| `<category>` | `id` (required, non-empty), `label`, `help`, `before`/`after` | `<visible>`, `<requirement>`, `<access>` children. Hidden when it has no visible settings at the current level. |
| `<group>` | `id` (required), `label`, `before`/`after` | replaces legacy `sep`/`lsep`. A label renders a title row. A separator is drawn between groups. |
| `<setting>` | `id` (required), `type`, `label`, `help`, `parent`, `before`/`after` | `help` and `label` are ATTRIBUTES. A `<help>` child element is never read. |

`<setting>` children read by `ISetting`/`CSetting`: `<level>`, `<default>`,
`<visible>`, `<enable>`, `<requirement>`, `<dependencies>`, `<updates>`,
`<constraints>`, `<control>`, and (action) `<data>`.

`label` is an integer string id from `strings.po` (core ids below 30000 may be
reused, e.g. 186 OK). A non-positive or missing label renders no text.

## Level

| `<level>` | name | shown when the user's GUI settings level is |
|---|---|---|
| 0 | Basic | any |
| 1 | Standard | Standard or higher (the default level) |
| 2 | Advanced | Advanced or Expert |
| 3 | Expert | Expert |
| 4 | Internal | never |

The dialog level is the GLOBAL Kodi GUI level (`CViewStateSettings`), cycled in
the dialog by the level button. A setting with `level` above the current one is
not created in the dialog, which also takes it out of "Reset to defaults".
Out-of-range values fall back to Standard.

## Updates

`<updates><update type="rename">old_id</update></updates>` copies the value of
a legacy id when the new id is loaded and had no stored value
(`CSettingsManager::UpdateSetting`). `type="change"` only raises the
`OnSettingUpdate` callback; add-on settings do not implement it
`[unverified for add-ons; grep of AddonSettings.cpp shows no handler]`.

## Legacy -> new type mapping (wiki conversion table, matches source)

| legacy `type` | new `type` + control |
|---|---|
| `text` | `string` + `edit format="string"` (`option="hidden"` -> `<hidden>true`, `option="urlencoded"` -> `urlencodedstring`) |
| `ipaddress` | `string` + `edit format="ip"` |
| `number` | `integer` + `edit format="integer"` |
| `bool` | `boolean` + `toggle` |
| `select` (lvalues) | `integer` + `list`, options label = string ids |
| `enum` | `integer` + `spinner format="string"` |
| `labelenum` | `string` + `spinner format="string"`, stored value is the option text/id, not localized text |
| `slider` | `integer`/`number` + `slider`, `range="min,step,max"` -> constraints |
| `file`/`audio`/`video`/`image`/`executable` | `path` + `button format="file"` or `image` |
| `folder` | `path` + `button format="path"` |
| `action` | `action` + `button format="action"` with `<data>`; `option="close"` -> `<close>true</close>` |
| `fileenum` | removed; use `path` |
| `colorbutton` | Nexus (20)+ |

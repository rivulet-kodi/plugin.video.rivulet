# Python API, persistence, dialog life cycle

All claims from Kodi source: `xbmc/interfaces/legacy/Addon.cpp`,
`Settings.cpp`, `xbmc/addons/settings/AddonSettings.cpp`,
`xbmc/addons/Addon.cpp`, `xbmc/addons/gui/GUIDialogAddonSettings.cpp`,
`xbmc/addons/AddonManager.cpp`, `xbmc/interfaces/python/XBPython.cpp`.

## Read API

| call | behaviour |
|---|---|
| `Addon.getSetting(id)` | string form of the value; `""` for an unknown id or when settings failed to load. Never raises for a missing id. |
| `getSettingBool/Int/Number/String(id)` | raises `WrongTypeException("Invalid setting type")` when the id is not defined, has another type, or the add-on has no `settings.xml`. Do not rely on a default value. Available since Kodi 18. |
| `Addon.getSettings()` -> `xbmcaddon.Settings` | Kodi 20 (Nexus)+. Methods `getBool/Int/Number/String`, `getBoolList/IntList/NumberList/StringList`, `setBool/Int/Number/String`, and the `set*List` forms. `set*` persist immediately via `Save()`. Absent in Matrix (19). |

A defensive reader (`getSetting` + parse + default) works on 19-22 and never
raises; use the typed getters inside `try/except WrongTypeException` if you
use them at all.

## Write API

| call | behaviour |
|---|---|
| `setSetting(id, str)` | if the add-on settings dialog of THIS add-on is open: the value is posted to the dialog (in-memory) and persisted only on OK; if the id is not defined in the dialog's definition the message is dropped silently. Otherwise: stored immediately (an undefined id creates a hidden string setting) and the file is saved. |
| `setSettingBool/Int/Number/String` | same dialog-aware path; outside the dialog they throw `WrongTypeException` for an undefined id/type mismatch. |
| `openSettings()` | opens the add-on dialog modally; Back/Escape = cancel; OK button = confirm. A legacy `xbmcaddon.Addon()` is its own CAddon: see "first-match reload" below. |

Dialog end:

- OK: `OnOkay` sets confirmed, then `addon->SaveSettings()` writes the whole
  settings file (every non-action setting) from the dialog's in-memory state.
  Values written meanwhile by another interpreter are overwritten.
- Back/Escape/Cancel: not confirmed. From Kodi 20 (Nexus) `addon->ReloadSettings()`
  re-reads the file and everything `setSetting` posted into the open dialog is
  discarded. On Kodi 19 (Matrix) the cancelled edits stay in the add-on object's
  memory (the floor of this repo: do not assume Back reverts).
- `CAddonMgr::ReloadSettings(id)` reloads the FIRST live add-on object with
  that id in `m_updateableAddons` (`std::ranges::find_if`), not all of them.
  Other `xbmcaddon.Addon()` objects (a long-lived one in another
  interpreter, e.g. the window UI vs the service) keep their loaded values
  until they re-read, and a later dialog OK from such an object saves its
  whole stale state over the other process's write; create a fresh
  `xbmcaddon.Addon()` to see another process's save
  `[INFERENCE for the consequence; the first-match behaviour is in
  AddonManager.cpp]`.

## Persistence rules

- File: `special://profile/addon_data/<id>/settings.xml`, written as
  `<settings version="N"><setting id="x" [default="true"]>value</setting>...`.
  Actions are never stored.
- `default="true"` marks a value the user never changed (value equals the
  default). `CSettingsManager::LoadSetting` `Reset()`s such a value to the
  CURRENT `<default>`, but `CAddonSettings::Load` then re-applies every stored
  value with `FromString` (Matrix `AddonSettings.cpp:322`, Omega `:407`, Piers
  same), undoing that reset. So for add-ons the stored value wins and a
  changed `<default>` only affects users who never saved settings (no
  `settings.xml` entry). A user who explicitly picks the default value also
  gets the flag.
- Stored ids with no definition load as hidden Internal-level strings
  (`AddSettingWithoutDefinition`), are returned by `getSetting`, and are
  re-saved forever. Renaming an id leaves a stale value; use
  `<updates><update type="rename">` to migrate it. The Python API has no
  delete; blank it with `setSetting(id, "")`.
- A `<visible>false</visible>` setting still loads and saves. It is not
  created in the dialog, so "Reset" never touches it.
- `Monitor.onSettingsChanged()` fires in monitors of that add-on id after each
  `SaveSettings` (dialog OK or direct write). It gives no changed ids; diff a
  snapshot.
- The settings file is parsed on first access per add-on object; a load
  failure is sticky for the lifetime of the object (`m_loadSettingsFailed`).

## Actions

`<setting type="action"><data>BUILTIN</data><control type="button" format="action"><close>true</close>`:

- `<data>` is run through `TMSG_EXECUTE_BUILT_IN`; `$ID` expands to the add-on
  id and `$CWD` to its path. Empty `<data>` does nothing.
- Without `<close>` the dialog stays open and the built-in runs while it is
  open (so any `setSetting` it makes goes through the dialog path above).
- With `<close>true</close>` the dialog is saved and closed first
  (`SaveAndClose`), then the built-in runs.
- Typical built-ins: `RunPlugin(plugin://$ID/?action=x)`, `RunScript($ID,x)`.

## Strings and help

`label`, `help`, `<heading>`, `<option label=>` hold integer ids into
`strings.po` (`msgctxt "#NNNNN"`). Ids below 30000 resolve to Kodi core
strings (the dialog itself uses e.g. 186, 222, 10036-10039). The add-on id
range is 30000 and up `[unverified: the exact upper bound is on the wiki
Language support page, not fetched]`.

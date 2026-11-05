# Python API differences, Kodi 19 → 22

Evidence is the doxygen `@python_vNN` tags and the implementation in `xbmc/interfaces/legacy/*.{h,cpp}` on branches `Matrix` (19), `Nexus` (20), `Omega` (21), `Piers` (22, release candidate). `master` is Kodi 23 (unreleased) and is named only where it differs from Piers; the 22 column was read on `Piers`. Base URL: `https://raw.githubusercontent.com/xbmc/xbmc/<branch>/xbmc/interfaces/legacy/<file>`. "Deprecated" in Kodi means "logs a warning and still works" unless stated.

## Paths and logging

| API | 19 | 20 | 21 | 22 | Evidence |
|---|---|---|---|---|---|
| `xbmc.translatePath`, `xbmc.validatePath`, `xbmc.makeLegalFilename` | present, `@python_v19 Deprecated … Moved to xbmcvfs` | gone | gone | gone | Matrix `ModuleXbmc.h:631`; Nexus/Omega/Piers/master: `ModuleXbmc.h` has no `translatePath`, only `ModuleXbmcvfs.h` does |
| `xbmcvfs.translatePath`, `validatePath`, `makeLegalFilename` | `@python_v19 New function added` | yes | yes | yes | `ModuleXbmcvfs.h:151,185,214` |
| `xbmc.LOGNOTICE`, `xbmc.LOGSEVERE` | removed (use `LOGINFO`, `LOGFATAL`) | removed | removed | removed | `ModuleXbmc.h:74` |
| `xbmc.startServer(..., bWait)` | has `bWait` | `bWait` removed | — | — | `ModuleXbmc.h:863` |

## ListItem and info tags

| API | 19 | 20 | 21 | 22 | Evidence |
|---|---|---|---|---|---|
| `ListItem(label,label2,path,offscreen)` | `iconImage`/`thumbnailImage` removed (v19); `offscreen` since v18 | same | same | same | `ListItem.h:91` |
| `ListItem.setInfo(type, dict)` | full | "Partially deprecated. Use explicit setters in InfoTagVideo/Music/Picture/Game" (`@python_v20`) | + `videoversion` label | still implemented; video keys `count size overlay date` and many others still accepted | `ListItem.h:784-785` |
| warning on `setInfo` | none | logs `"Setting most video properties through ListItem.setInfo() is deprecated and might be removed in future Kodi versions."` | same | same; emitted for every key outside `count/size/overlay/date`, **including `playcount`** | `ListItem.cpp` (Nexus ~542, Piers :573) |
| `setInfo('video', {'playcount': n})` | works | works (deprecated) | works | works: `key == "playcount"` → `setPlaycountRaw` | Piers `ListItem.cpp:615` (also `:462`); same in master |
| `setInfo('video', {'overlay': n})` | works | works | works | works for 0–8 (silently ignores other values) | Piers `ListItem.cpp:421-426`; same in master |
| `InfoTagVideo` (class) | read-only getters via `ListItem.getVideoInfoTag()` | **setters added** (`setPlaycount`, `setResumePoint`, `setCast`, `setGenres`, `setDuration`, `setMediaType`, `setUniqueIDs`, `setRatings`, `setLastPlayed`, `setTitle`, `setPlot`, … ~107 fns) | `setVideoAssetTitle` | + `setAvailableFanart`, `setHDRDetail`, `setOriginalLanguage`, `addSeason(s)` plot arg, `VideoStreamDetail` hdrdetail | `InfoTagVideo.h` (Matrix has 0 of the setters listed) |
| `ListItem.setCast`, `setUniqueIDs`, `setRating`, `addSeason`, `addStreamInfo`, `addAvailableArtwork` | ok | deprecated → `InfoTagVideo.*` | deprecated | deprecated (+`setAvailableFanart` deprecated v22) | `ListItem.h:363-939`, `ListItem.cpp` warnings |
| `ListItem.setProperty('ResumeTime'/'TotalTime')` | ok | deprecated → `InfoTagVideo.setResumePoint` / `getResumeTime*` | same | same | `ListItem.h:1017-1018,1074` |
| `ListItem.setFileTitle`, `setCount`, `setSize`, `setOverlay` | — | — | — | new, replace the matching `setInfo` labels; `setOverlay` throws `ListItemException` outside 0–8 | Piers `ListItem.h:1156-1213` |
| `InfoTagMusic`, `InfoTagPicture`, `InfoTagGame` setters | read-only | added | `+` a few | same | `InfoTag*.h` |

Practice: build one plain info dict; on Kodi ≥20 route each key to an `InfoTagVideo` setter (feature-detect with `getattr`), on 19 call `setInfo`. Keys without a setter (`playcount`, `overlay`) need their own handling: `InfoTagVideo.setPlaycount()` on ≥20; there is no `InfoTagVideo.setOverlay` — `ListItem.setOverlay()` exists only on 22 `[unverified: whether overlay is derived from playcount by the GUI]`.

## Settings

| API | 19 | 20+ |
|---|---|---|
| `Addon.getSetting(id)` / `setSetting(id, str)` | ok | ok (not deprecated) |
| `Addon.getSettingBool/Int/Number/String`, `setSettingBool/Int/Number/String` | ok (`@python_v18`) | `@python_v20 Deprecated. Use Settings.getBool() …` |
| `Addon.getSettings()` → `xbmcaddon.Settings` | absent | new (`Addon.h:103-110`, `Settings.h` v20) |

## GUI, windows and dialogs

| API | Change | Evidence |
|---|---|---|
| `Dialog.yesno/ok/textviewer/…` | `@python_v19`: `line1` renamed `message`, `line2`/`line3` removed | `Dialog.h:85-87,298-300,750-752,779-781` |
| `Dialog.yesno`, `yesnocustom` | `@python_v20` option `defaultbutton` | `Dialog.h:88,137` |
| `Dialog.input` | `@python_v19` `ShowAndVerifyNewPassword`, `bHiddenInput` | `Dialog.h:551-552` |
| `ControlEdit` | `@python_v19` removed `isPassword` | `Control.h:905` |
| `WindowXML`/`WindowXMLDialog` ctor | `@python_v18` `isMedia` param; no change 19→22 in the header | `WindowXML.h:60` |
| bindings generator | 19–21 generated (Groovy); **22 uses stock SWIG `-builtin`**: `SwigPyObjectType` metatype is inherited by subclasses and rejects class-level `setattr`/`delattr`; stubs `.pyi` shipped with `kodi-addon-dev`; signatures are name-only | xbmc/xbmc#28958 (merged to `master` 2026-09-08; the merge commit is an ancestor of `Piers`, which ships `xbmc/interfaces/swig/SwigRuntime.cpp` and `kodi_*.i`, so it applies to Kodi 22 and 23) |

## Player / Monitor

| API | Change | Evidence |
|---|---|---|
| `Player.isPlayingGame`, `getPlayingItem`, `getGameInfoTag` | new in 20 | `Player.h:441,493,667` |
| `Player.getAvailableAudioStreams/SubtitleStreams/VideoStreams` | exist in 20 and 21 (present in Nexus and Omega `Player.h`) | `Player.h` |
| language values from `Player.getSubtitles()`, `getAvailableSubtitleStreams()`, `getAvailableAudioStreams()`, and the video-stream equivalent | **22 behaviour change**: return BCP 47 tags (`en`, `en-AU`) instead of ISO 639-2 three-letter codes (`eng`); a stream with no language is listed by its name. Code comparing against `eng`/`fre` must also accept 2-letter/BCP 47 forms on 22 | Piers `Player.h:580,596,753,792` (`@python_v22`) |
| `Monitor` callbacks (`onSettingsChanged`, `onNotification`, `onScanStarted` …) | no 19→22 additions found in header tags (`@python_v13/v14` only) | `Monitor.h` |
| callbacks fire after `CVideoPlayer::CloseFile()`; `getTime()`/`getTotalTime()` raise during stop/end/error callbacks | Rivulet AGENTS.md "UI phases" (repo-observed, not from a header) | AGENTS.md |

## Other 22-only additions seen (do not rely on them below 22)

`xbmc.getLanguage(xbmc.ISO_NAME)` (and: a language with no code in the requested format now returns an empty string, previously the region alone could be returned), `xbmc.convertLanguage` (accepts BCP 47 input such as `en-AU`, plus `xbmc.ISO_NAME`), `xbmc.getDatabaseName`, `xbmc.getDevicePowerStatus`, `xbmc.getCecAdapterNames` (`ModuleXbmc.h:329,488,658,687,972` on Piers, `@python_v22`). Kodi 23 (`master`) additionally carries `@python_v23` items (e.g. the `EDLPath` ListItem property, new `InfoTagGame` getters); not usable on 22.

## Python runtime

Bundled CPython in Kodi's depends builds: Matrix 3.8.5, Nexus 3.11.2, Omega 3.11.7, Piers 3.14.10 (master also 3.14.10) (`tools/depends/target/python3/Makefile` on Matrix; `PYTHON3-VERSION` on Nexus/Omega/Piers/master). Linux distro packages link the system interpreter `[inference]`. A Kodi 19 floor therefore means Python 3.8 syntax and stdlib; Kodi 22 means code must also survive 3.14 removals `[unverified: no 3.14 removal list read]`.

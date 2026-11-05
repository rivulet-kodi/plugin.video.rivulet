---
name: kodi-advancedsettings
description: "Kodi userdata advancedsettings.xml (tags honoured per Kodi 19-22, the 21+ move of <cache> to GUI filecache.* settings, load order, units, curl network tags) and how an add-on may safely ship, install or replace it; use when touching any shipped advancedsettings template, an install-it button, or Kodi cache/curl timeout tuning."
---

# Kodi advancedsettings.xml

Everything below was read from Kodi source (`xbmc/xbmc`, branches `Matrix`=19, `Nexus`=20, `Omega`=21, `Piers`=22 (release candidate); `master` = 23 alpha, unreleased, only cross-checked). Where the wiki disagrees with the code, the code wins (called out inline). Anything not read in source is marked `[unverified]`.

## When to use

- Editing or reviewing an `advancedsettings.xml` an add-on ships/installs.
- Deciding whether a `<cache>`/`<network>` tag has any effect on a given Kodi version.
- Writing user-facing text about "restart Kodi to apply".
- Considering JSON-RPC `Settings.SetSettingValue` as an alternative.

## Critical facts (read these first)

1. **Kodi 21+ ignores `<cache>` entirely.** `AdvancedSettings.cpp` on Omega and Piers (and 23 master) has no `FirstChildElement("cache")` parse and no `m_cache*` members (only `m_cachePath` for `<cachepath>`). `CFileCache`/`CFile` read the GUI settings `filecache.*` instead. The wiki agrees ("Removed in v21 and added to GUI settings page"; "The cache tags in the advancedsettings.xml file have been deprecated"). No migration, no warning: the tags are silently dead. Kodi 19 and 20 still honour them.
2. **`<network>` curl tags are honoured unchanged on 19, 20, 21 and 22** (`curlclienttimeout`, `curllowspeedtime`, `curlretries`, `curlkeepaliveinterval`, `disableipv6`, `disablehttp2`; identical parse lines and ranges in all four branches).
3. **The file is global.** One file per Kodi userdata (plus an optional per-profile one), parsed by Kodi core, shared by the user, Kodi, and every add-on. An add-on must never overwrite or edit an existing one.
4. **Loaded once.** `CAdvancedSettings::Load()` runs at startup (and on profile switch). Edits need a Kodi restart (wiki note 2: "Kodi will only read the advancedsettings.xml file when it starts up"; the `Load()` comment in source mentions only "In case of profile switch" as a re-read; no file watcher was found in `AdvancedSettings.cpp`).

## Version matrix

| Aspect | 19 Matrix | 20 Nexus | 21 Omega | 22 Piers |
|---|---|---|---|---|
| `<network>` `curlclienttimeout` (default 30 s, range 1-1000) | yes | yes | yes | yes |
| `curllowspeedtime` (default 20 s, range 1-1000) | yes | yes | yes | yes |
| `curlretries` (default 2, range 0-10) | yes | yes | yes | yes |
| `curlkeepaliveinterval` (default 30, 0-300) | yes | yes | yes | yes |
| `disablehttp2` / `disableipv6` (default false) | yes | yes | yes | yes |
| `<cache>` `buffermode` (0-4, default 4) | yes | yes | **ignored** | **ignored** |
| `<cache>` `memorysize` (**bytes**, default 20 MiB = 20971520) | yes | yes | **ignored** | **ignored** |
| `<cache>` `readfactor` (float multiplier, default 4.0) | yes | yes | **ignored** | **ignored** |
| `<cache>` `chunksize` (bytes, 256..1048576, default 131072) | yes | yes | **ignored** | **ignored** |
| GUI `filecache.*` settings exist | no | no | yes | yes |
| Who decides "cache this file" | `CDVDInputStreamFile::Open` | `CFile::Open` | `CFile::Open` | `CFile::Open` |
| GUI defaults | n/a | n/a | mode 4, 20 MB, readfactor 400 (4x), chunk 131072 | mode 4, **64 MB**, readfactor **0 (Adaptive)**, chunk 131072 |

Sources: `xbmc/settings/AdvancedSettings.cpp` (defaults in constructor/`Initialize`, parse in `ParseSettingsFile`), `system/settings/settings.xml` (Omega/Piers `filecache` category; Matrix/Nexus have none), `xbmc/filesystem/FileCache.cpp`, `xbmc/filesystem/File.cpp`. Verified on the `Piers` branch on 2026-11-05 (22 is still an RC, so values could still change before the final release); `master` (23 alpha) currently matches Piers for all rows above.

## `<cache>` tags (Kodi 19, 20 only)

| Tag | Type / unit | Range enforced | Default | Notes |
|---|---|---|---|---|
| `buffermode` | uint | 0-4 | 4 | see table below |
| `memorysize` | uint **bytes** | none | 20 MiB | **total** RAM for the cache; 3/4 forward, 1/4 backward (`back = cacheSize/4`). With `READ_MULTI_STREAM` (mkv/mp4 video) it is halved per buffer and doubled by `CDoubleCache`. `0` = disk cache (`CSimpleFileCache` in `cachepath`, default `special://temp/`), bad on SD/eMMC. A file smaller than `memorysize` (non-A/V) is capped to file size. Floor: 2 chunks |
| `readfactor` | float | none | 4.0 | cache fill rate = average stream byte-rate x readfactor; the reader throttles when `forward < writeRate*readfactor` is false. Large values saturate link/CPU |
| `chunksize` | uint bytes | 256..1048576 | 131072 | used only where the protocol does not force its own (`CFile::DetermineChunkSize`) |

`buffermode` values (`IFileTypes.h`; same numbers in all branches, enum `CACHE_BUFFER_MODES` / `CacheBufferMode`):

| Value | Name | Buffers |
|---|---|---|
| 0 | INTERNET | all internet filesystems: http(s), ftp, webdav, ... |
| 1 | ALL | all network **and local** (`IsHD`) filesystems |
| 2 | TRUE_INTERNET | only http(s)/rtmp/udp-type streams |
| 3 | NONE | nothing (GUI also disables the other three controls) |
| 4 | NETWORK | all network filesystems incl. SMB/NFS and http(s) (**default**) |

Check: an `http://127.0.0.1:...` URL is `IsNetworkFilesystem` (-> `IsStreamedFilesystem` -> `IsHTTP`), so the default mode 4 already caches it. Mode 1 only *adds* local-disk files to the cached set, i.e. it changes playback of the user's whole library, not just your add-on.

## GUI settings (Kodi 21+, Settings > Services > Caching)

**GUI path:** Settings > Services > Caching (`<section id="services" label="14036">` > `<category id="filecache" label="37101">`, identical in Omega and Piers; strings "Services" #14036, "Caching" #37101). Every `filecache.*` setting has `<level>2</level>`, and Kodi's setting levels are 0 Basic (#10036), 1 Standard (#10037), 2 Advanced (#10038), 3 Expert (#10039), so the Caching page only shows when the settings level is **Advanced or Expert** (cog icon bottom left of the Settings screen). `[INFERENCE]` for the level-number-to-name mapping: taken from the order of strings 10036-10039 and the 0/1/2 levels in `settings.xml`, not from the enum definition.

Ids in `system/settings/settings.xml` (category `filecache`, all `<level>2</level>`, i.e. hidden below "Advanced" settings level; `memorysize`/`readfactor`/`chunksize` are disabled in the GUI when `buffermode == 3`):

| Id | Unit in the setting | Same as old tag | Offered options (`ServicesSettings.cpp`) |
|---|---|---|---|
| `filecache.buffermode` | int 0-4 (same enum) | `buffermode` | order shown: NONE, TRUE_INTERNET, INTERNET, NETWORK, ALL |
| `filecache.memorysize` | **MB** (code: `GetInt(..) * 1024 * 1024`) | `memorysize` bytes / 1048576 | 16, 20, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024 (+2048 on 22), 0 = "entire file on disk" |
| `filecache.readfactor` | **x100** integer (code: `GetInt(..) / 100.0f`) | `readfactor` x 100 | 0 = Adaptive, 110, 125, 150, 175, 200, 250, 300, 400, 500, 700, 1000, 1500, 2000, 3000, 5000 |
| `filecache.chunksize` | bytes | `chunksize` | 256 B .. 1 MiB list |

Adaptive readfactor (`< 1.0f`, i.e. setting 0): the factor is recomputed from the cache fill level as `level * -2.5 + 4.0` (range 4.0x..1.5x) (`FileCache.cpp`, Omega/Piers).
Buffer-mode evaluation on 21+ happens in `CFile::Open` and only when the file is DVD/Blu-ray or carries `READ_AUDIO_VIDEO` (set by VideoPlayer's input stream). `[unverified]`: whether Kodi 22 release notes describe the change of defaults (21 -> 22: 20 MB/4x -> 64 MB/adaptive).

## Where the file lives and precedence

`CAdvancedSettings::Load()` parses, in order, each later file overriding only the tags it contains:

1. `special://xbmc/system/advancedsettings.xml` (read-only install dir; distros/OEM)
2. files registered with `AddSettingsFile` (`--settings=` style launch parameter)
3. `CProfileManager::GetUserDataItem("advancedsettings.xml")`: **`special://profile/advancedsettings.xml` if it exists, otherwise `special://masterprofile/advancedsettings.xml`** (Omega `ProfileManager.cpp`). It is *one or the other*, not both.

Consequences:
- `special://masterprofile/` == `<home>/userdata/`; for the default profile `special://profile/` is the same directory.
- A non-master profile with its own `advancedsettings.xml` never sees the masterprofile one.
- The root element must be `<advancedsettings>` (case-insensitive compare); otherwise `Error loading ..., no <advancedsettings> node` is logged and the file is dropped. Malformed XML drops the **whole file** (`Error loading ..., Line N`). The `version="1.0"` attribute is not read by the code. Tags are lowercase.
- Out-of-range values are clamped/ignored per `XMLUtils::GetInt/GetUInt(min,max)` (the previous default is kept for unparsable input).
- Passwords inside `<network>` (`httpproxypassword`) are redacted in the log copy.

## Network tags: what each really controls (`CurlFile.cpp`, identical logic 19-22; Piers verified)

| Tag | Applied as | Real effect |
|---|---|---|
| `curlclienttimeout` | `CURLOPT_CONNECTTIMEOUT` on every handle; **also** `CURLOPT_TIMEOUT` (total time) on the `Exists()`/`Stat()` HEAD-style handles | connect timeout; bounds metadata probes. Per-URL override `|connection-timeout=N` exists. Wiki shows default 10, **source default is 30** (source wins) |
| `curllowspeedtime` | `CURLOPT_LOW_SPEED_TIME`, paired with a hard-coded `CURLOPT_LOW_SPEED_LIMIT = 1` byte/s | how long a read may sit at <1 B/s before curl aborts it. This, not `curlclienttimeout`, bounds a stalled playback read |
| `curlretries` | `CReadState::FillBuffer` retries a failed read up to N times, reconnecting with a Range resume (`SetResume`) | worst case for one stalled read ~ `curllowspeedtime x (curlretries + 1)`. `CFileCache::Open` calls `IOCTRL_SET_RETRY=false` after opening ("we already handle retrying ourselves"); that flips `m_allowRetry` which is copied into each *new* read state in `Seek`/`Open`, so retries are guaranteed on the initially opened state; later cache-initiated reconnects rely on `CFileCache`'s own loop (retry every 2 s while it still holds data, `FileCache.cpp` "Will retry"). `[INFERENCE]` from reading the two files, not from a log |
| `curlkeepaliveinterval` | `TCP_KEEPALIVE`, idle = N/2, interval = N | 0 disables keepalive |
| `disablehttp2` | forces `CURL_HTTP_VERSION_1_1`, else `HTTP_VERSION_2TLS` | wiki: added in 19 |
| `disableipv6` | resolver family | |

Defaults (constructor): connect 30, lowspeed 20, retries 2, keepalive 30. **A template that writes `curlclienttimeout=30` / `curllowspeedtime=20` is a no-op**; only a differing value changes behaviour.

## Per-Kodi-version guidance for add-ons

- 19/20: `<cache>` works; users need a restart. Sizing: `memorysize` is a *global* RAM budget for every cached source in the Kodi process (and `buffermode=1` widens it to local files). Don't copy 200 MiB onto 1 GB boards.
- 21/22: ship **no** `<cache>` block; it cannot work and a test pinning it hides that. The only lever left in the file is `<network>`.
- All: never rely on `advancedsettings.xml` for correctness; treat it as optional tuning.

## Add-on options on 21+: JSON-RPC instead of a file

```python
xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "Settings.SetSettingValue",
    "params": {"setting": "filecache.memorysize", "value": 128}}))   # MB
```

What source says (`SettingsOperations.cpp`, `Setting.cpp`, Omega):
- `SetSettingValue` requires the setting to exist and `IsVisible()` (the `filecache.*` settings carry no visibility condition, so they work even at Standard level; level only hides them from the GUI). Integer values are not range-checked when the setting uses a dynamic options filler (`CheckValidity` skips the check), so any int is accepted; stay on the offered options.
- Effective without restart: `CFileCache::Open` reads memory size and chunk size per file open; `Process()` reads read factor at thread start. `[unverified]` live on a device.
- On 19/20 the call fails (`Invalid params`: setting unknown) and must be gated by version (`xbmc.getInfoLabel('System.BuildVersion')`) or tolerate the error.

Tradeoffs: persists in the user's `guisettings.xml` and is per-profile; it overwrites a value the user chose in the GUI (read with `Settings.GetSettingValue` first and only write when it equals the Kodi default, or ask); no atomic "install" story and no file-conflict, but also no restart notice and no way to cleanly "uninstall" besides restoring the previous value you must have saved yourself. Prefer a user-initiated, opt-in action over doing it silently at startup.

## Best practices

1. Never create/overwrite `advancedsettings.xml` without an explicit user action. If it exists, leave it and tell the user which tags to merge. Never merge programmatically (you cannot know a user's regex/pathsubstitution content).
2. Create atomically and never clobber: write a temp file in the destination directory, fsync, publish with `os.link()` (fails with `FileExistsError` if a file appeared) with a rename-no-replace fallback; mode 0644. Create parent dirs. A half-written file makes Kodi drop the whole file silently, and an existing truncated file will satisfy any later `exists()` check forever.
3. State "restart Kodi to apply" in the success message.
4. Resolve the destination with `xbmcvfs.translatePath('special://masterprofile/advancedsettings.xml')` (or `special://profile/...` if you want the active profile) and mention the real path in logs.
5. Put the reasoning for every tag next to it in the template as XML comments (comments are fine for TinyXML).
6. Do not write tags that equal Kodi's default; they add nothing but a false sense of tuning.
7. Wiki ranges/defaults drift; re-read `AdvancedSettings.cpp` for the branch before changing numbers.

## This repo's traps and conventions (verified in repo)

- Two templates, chosen by Kodi major in `lib/ui/router.py:_install_advancedsettings` (`kodi_major_version() >= 21`): `resources/advancedsettings.xml` (Kodi 19/20: `<network>` + `<cache>` with only `memorysize=67108864` (64 MiB, the default Kodi 21+ ships; up from Kodi 19/20's 20 MiB), no `readfactor` (Kodi's default 4.0 applies; a high factor leaves the reader unthrottled until the cache is nearly full) and no `buffermode` (Kodi's default 4 applies)) and `resources/advancedsettings_kodi21.xml` (21+: the same `<network>` block only). `<network>` is `curlclienttimeout=30`, `curllowspeedtime=20`, `curlretries=1`; XML comments document the measured stall (57 s stop, a 60.000 s retry interval). The two network blocks must stay identical (`test_kodi21_template_has_the_same_network_block_and_no_cache`).
- `lib/advancedsettings.py`: `install(source, dest, shadow_paths=())` returns `STATUS_EXISTS` if `dest` or any shadow path exists, else mkstemp in dest dir, `copyfile`, `chmod 0o644`, `fsync`, `_publish()` = `os.link()` (refuses existing) with a no-hardlink fallback (FAT/exFAT), temp removed on failure, `AdvancedSettingsError` wraps `OSError`. Pure Python (no `xbmc` import: Kodi-independence rule in `AGENTS.md`).
- The router passes `special://profile/advancedsettings.xml` as the shadow path (Kodi loads it instead of the master profile's), installs into `special://masterprofile/advancedsettings.xml`, and notifies 30066 (installed, 19/20), 30627 (installed, 21+: names Settings > Services > Caching), 30067 (exists), 30068 (error, logged at ERROR).
- `resources/settings.xml`: `advancedsettings_install` is an action in Playback > Buffering with `help="30584"`. New strings go above the current max in all 14 locales (never renumber).
- `tests/test_advancedsettings.py` (installer behaviour, shadow paths, 0644, race with a late-created dest, both real templates parsed) and `tests/test_router.py` (paths from `ADDON_ID`, template per Kodi major, outcomes). Changing a template means updating `test_shipped_template_installs_and_is_valid_xml_with_expected_tunables`.
- `lib/ui/streamswindow.py:_PLAYER_CLOSE_TIMEOUT_SECONDS = 75.0` caps the wait for Kodi's stop callback. A stalled read can last ~ `curllowspeedtime x (curlretries+1)`: 40 s with the template, 60 s with Kodi defaults; keep the cap above the default case.
- `lib/ui/player.py` `buffer_enable`/`buffer_mb` (`setting_bool`/`setting_int` at `_prebuffer_torrent`) are the add-on's own engine pre-buffer (HTTP range reads against the streaming server), **not** Kodi's cache; do not conflate them with `<cache>`/`filecache.*`.

## Review checklist

- [ ] Every `<cache>` tag is labelled "Kodi 19/20 only" or removed; nothing claims it works on 21+.
- [ ] No tag equals the Kodi default without a stated reason.
- [ ] `buffermode` is not 1 unless local-file buffering is intended; memory sized for the weakest target and in **bytes** for the file, **MB** for `filecache.memorysize`, readfactor x100 for the GUI id.
- [ ] Installer never replaces an existing file; success text says restart; the "exists" text tells the user what to merge.
- [ ] Profile precedence considered (`special://profile` beats masterprofile).
- [ ] Teardown/timeouts in code derived from `curllowspeedtime x (curlretries+1)` of both the template and Kodi defaults.
- [ ] Template and the test that pins it changed together; strings added to all locales.

## Sources

- https://raw.githubusercontent.com/xbmc/xbmc/Matrix/xbmc/settings/AdvancedSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Nexus/xbmc/settings/AdvancedSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/xbmc/settings/AdvancedSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/settings/AdvancedSettings.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/master/xbmc/settings/AdvancedSettings.cpp (Kodi 23 alpha, cross-check only)
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/system/settings/settings.xml (and `Piers`)
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/xbmc/settings/Settings.h (`SETTING_FILECACHE_*`, "in MBytes", "as integer (x100)")
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/xbmc/settings/ServicesSettings.cpp (option fillers; `Piers` for the 2048 MB option)
- https://raw.githubusercontent.com/xbmc/xbmc/{Matrix,Nexus,Omega,Piers}/xbmc/filesystem/FileCache.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/{Matrix,Omega,Piers}/xbmc/filesystem/File.cpp and `Matrix/xbmc/cores/VideoPlayer/DVDInputStreams/DVDInputStreamFile.cpp`
- https://raw.githubusercontent.com/xbmc/xbmc/{Matrix,Omega,Piers}/xbmc/filesystem/CurlFile.cpp
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/xbmc/filesystem/IFileTypes.h (and `Piers`)
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/xbmc/utils/URIUtils.cpp (`IsInternetStream`, `IsNetworkFilesystem`, `IsHTTP`)
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/xbmc/profiles/ProfileManager.cpp (`GetUserDataItem`)
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/xbmc/interfaces/json-rpc/SettingsOperations.cpp and `xbmc/settings/lib/Setting.cpp`
- https://raw.githubusercontent.com/xbmc/xbmc/Omega/addons/resource.language.en_gb/resources/strings.po (#37101-37116 help texts)
- https://kodi.wiki/view/Advancedsettings.xml (network section; "Removed tags": `<cache>` removed in v21)
- https://kodi.wiki/view/HOW-TO:Modify_the_video_cache ("cache tags ... deprecated", page updated for v21)

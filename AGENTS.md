# Repository Guidelines

## Project Overview

Rivulet (`plugin.video.rivulet`) is a Kodi video addon implementing a **Stremio addon-protocol client**: it browses catalogs published by community Stremio addons, resolves streams, and plays them through an embedded or remote `stremio-server-go` streaming server. It ships its own 1920x1080 skin rather than drawing through the user's Kodi skin.

Two independently versioned artifacts live here: the addon (`addon.xml`, currently 0.17.0) and the Kodi repository addon that delivers it (`repository.rivulet/addon.xml`, currently 1.0.2).

## Architecture & Data Flow

Three layers, and the dependency direction is **enforced by convention, not tooling** — respect it:

```
lib/stremio/   protocol + HTTP. Kodi-independent, pure Python.
lib/store.py   persistence. Kodi-independent.
lib/library.py playback-progress wire format. Kodi-independent, pure.
      |
      v
lib/ui/        every xbmc* import lives here (except service_runner.py)
      |
      v
default.py (plugin)   service.py (xbmc.service, start="startup")
```

**The Kodi-independence rule is real and load-bearing.** `lib/stremio/*`, `lib/store.py`, `lib/library.py`, `lib/serverbin.py`, `lib/ui/urlutil.py` and `lib/ui/playbackmeta.py` contain **zero** `xbmc*` imports, which is what makes them unit-testable without stubs. `lib/stremio/subtitles.py:92-96` documents a constant duplicated rather than imported specifically to preserve this. Never add an `xbmc` import to those modules to save a few lines.

**Plugin flow.** Kodi invokes `default.py` -> `lib/ui/router.py:run()` reads `action` from the query string (`params.get('action', 'home')`) and dispatches. Handlers lazily import `views`/`player` **inside closures** so an unrelated action never pays their import cost — a measured startup optimization, not an accident.

Browsing: `lib/ui/views.py` fans a request across installed addons -> `lib/stremio/addons.py` (`iter_catalogs`, `AddonClient`) -> metadata cached via `lib/ui/metacache.py` -> rendered by a window in `lib/ui/` (`infowindow.py` coverflow, `detailwindow.py` detail, `streamswindow.py` source list) -> `lib/ui/player.py` resolves a playable URL through the streaming server and hands it to `xbmc.Player`.

**Service flow.** `service.py` -> `lib/service_runner.py:main()` supervises the `stremio-server-go` subprocess for the whole Kodi session: probes for a listening server, falls back to an external one, downloads the binary via `lib/serverbin.py` when missing, and syncs playback progress to the Stremio API. It is the only long-lived process and the most complex function in the codebase (radon E-33) — weigh any refactor of it against the shutdown path specifically.

**Fan-out is bounded per call site, deliberately.** `lib/ui/views.py:38` sets `_MAX_ADDON_WORKERS = 8`; `lib/ui/streamswindow.py:103` and `lib/stremio/subtitles.py:92` each carry their *own* local copy. `streamswindow.py:103-107` explains why: every fan-out point bounds its own pool because this runs on low-power ARM boxes. Do not "DRY" these into one shared constant.

## Key Directories

| Path | Purpose |
|---|---|
| `lib/stremio/` | Protocol client: `addons.py` (manifests, catalogs, fan-out), `api.py` (account sync), `server.py` (streaming server), `streaminfo.py` (quality/seeder/cache decoration), `subtitles.py`, `metalinks.py` |
| `lib/ui/` | Windows, dialogs, routing, playback. All Kodi API contact lives here, except `lib/service_runner.py` |
| `resources/skins/Default/1080i/` | 13 window XMLs, authored at 1920x1080 |
| `resources/language/resource.language.*/` | 14 locales, `strings.po` |
| `tests/kodistubs/` | Fake `xbmc*` modules; the reason the suite runs off-Kodi |
| `repository.rivulet/` | The Kodi repository addon, versioned independently |
| `artwork/` | Source SVGs and screenshot masters; excluded from the shipped zip |
| `site/` | Project page deployed to `gh-pages` by `.github/workflows/repo.yml` |

## Development Commands

**Prefer the uv invocations — they need no local setup and are what these gates were last verified with:**

```bash
uvx --with-requirements requirements-dev.txt pytest tests/ -q          # 1778 passed, ~2.5s
uvx --with-requirements requirements-dev.txt ruff check lib tests
uvx --with-requirements requirements-dev.txt mypy
uvx --with-requirements requirements-dev.txt pytest tests/ --cov --cov-report=term-missing
```

The `Makefile` wraps the same gates for a provisioned venv:

```bash
make venv        # python3 -m venv .venv + pip install -r requirements-dev.txt
make test        # pytest tests/
make cov         # pytest tests/ --cov --cov-report=term-missing
make lint        # ruff check lib tests
make typecheck   # mypy
make check       # lint + typecheck + test
make parallel    # pytest -n auto
```

**Trap:** the Makefile switches to `.venv/bin/python` as soon as that file *exists*, whether or not the requirements were installed into it. A half-provisioned `.venv` makes `make lint` fail with `No module named ruff` — which looks like a missing tool, not a missing venv. Fix with `make venv`, or use the uv commands above.

**Never run `make format`.** It runs `ruff format`, which would reformat **73 of 78 files** — the codebase is not ruff-format-clean and CI never checks formatting. Ruff is used as a *linter* only.

## Code Conventions & Common Patterns

**Docstrings carry the reasoning.** This codebase documents *why*, at length, including the measurement or bug that motivated the code. Match it. When you remove a workaround, you are deleting an explanation — read it first. Comments citing a past failure (`lib/store.py:40-52`, `.github/workflows/repo.yml:15-21`) are load-bearing.

**Kodi compatibility funnel.** `lib/ui/compat.py` is the single place that abstracts Kodi 19 vs 20+:
- `L(string_id)` (`compat.py:26`) for every user-visible string — never a literal.
- `log(msg, level=xbmc.LOGDEBUG)` (`compat.py:54`), prefixed automatically.
- `setting_bool(key, default)` / `setting_int(key, default, minimum=None)` (`compat.py:64,79`) parse the **raw `getSetting()` string on purpose** — `getSettingBool()` was observed misbehaving live. Do not "simplify" these to the typed API.

**Error handling.** Typed exceptions per domain (`AddonError`, `DownloadError`, `UnsupportedPlatformError`); `B904` (`raise ... from exc`) is intentionally disabled for `lib/*` in `pyproject.toml:55-58`. URLs are never logged raw — use `safe_url_for_log()` from `lib/stremio/addons.py`.

**String formatting is printf-style.** `UP031` is globally ignored (`pyproject.toml:44-48`); ~198 sites use `"%s" %`. Consistent with the 3.8 floor. Do not modernize them.

**State and persistence** (`lib/store.py`) — JSON files under the addon's `addon_data` dir: `addons`, `auth`, `search_history` (15 entries), `now_playing`, `resume_offset`, `progress` (500 entries, 180-day sweep, daily), `last_version`.
- `update_addons()` (`store.py:977`) is the **only** compare-and-swap path, because Kodi runs `default.py` as concurrent OS processes. It deliberately bypasses the read cache — it must see current on-disk bytes.
- Everything else is plain read-modify-write, with the rationale at `store.py:43-53` (a lost search-history or progress entry is low-stakes).
- No `fcntl`/`msvcrt`: the addon runs on Linux, Windows, Android and macOS.
- `_cached_read()` memoizes on an `os.stat()` fingerprint; its key set is a closed seven paths, so its missing size cap is safe. Making that key set data-controlled would not be.

**Dependency injection.** `lib/ui/dependencies.py` exposes lazy singletons (`get_store()`, `get_client()`) injected into windows; windows do not construct their own.

**Window infrastructure.** `lib/ui/uicommon.py` provides the modal stack (`_MODAL_WINDOW_STACK`), `BaseWindow`, and `BACK_ACTIONS = frozenset({9, 10, 92})` (`uicommon.py:74`). `close_windows_for_playback()` must force-close the stack before playback and reopen after.

**Localization.** 177 ids spanning `#30000`-`#30247` (gaps are normal — never renumber, and allocate above the current maximum) in `resources/language/resource.language.en_gb/strings.po`, the source of truth. Adding a user-visible string means adding it to **all 14 locales**, with `msgctxt "#3XXXX"`. Skin XML reads them as `$LOCALIZE(30247)`.

**Settings.** `resources/settings.xml`, ids are `<category>_<feature>` (`home_show_movies`, `server_enable`, `bt_listen_port`). `server_enable` is first in its category on purpose: Kodi otherwise opens Settings inside the `server_url` text box, trapping the arrow keys.

### Skin authoring traps

- **Kodi parses `<width>` once at skin load**; a control cannot size itself to its text. Prefer one flowing label over fixed-width chips — see `lib/ui/infowindow.py:141-145` for the bug that taught this.
- No `border-radius`, `box-shadow`, or ellipsis truncation exists in Kodi.
- **The `Mono26` glyph trap.** Kodi's default skin maps every font except `Mono26` to NotoSans, and `Mono26` alone to NotoMono. `_SANS_ONLY = ↑ → ▲ ● ★` renders as **tofu** in a `Mono26` label; `_SAFE_ANYWHERE = ° · × – — • …` is fine in either. Any `Mono26` control that Python writes into must be registered in `_DECLARED_MONO_CONTROLS` (`tests/test_glyph_coverage.py:71`) or `test_python_populated_mono_controls_are_declared` fails. Note the failure mode: restyling a control's *font* breaks text that was fine for releases.

## Important Files

| Path | Why |
|---|---|
| `addon.xml` | **Single source of version truth** (Kodi reads it). Cutting a release = bump `version` + prepend a `<news>` entry, then tag `vX.Y.Z` |
| `pyproject.toml` | ruff/mypy/pytest/coverage config. Its `[project] version` is inert dev metadata and is **stale (0.6.1)** — never treat it as the release version |
| `requirements-dev.txt` | Every floor is deliberate; comments explain the 3.8 leg and the `mypy<2.0` cap |
| `lib/ui/router.py` | Action dispatch; add new plugin actions here |
| `lib/ui/compat.py` | The only sanctioned path to Kodi APIs that differ by version |
| `lib/store.py` | All persistence, caps and the CAS path |
| `lib/service_runner.py` | Subprocess supervision; scrutinize the shutdown path |
| `tests/kodistubs/install.py` | Make a new module importable under test by adding it to the reload targets |
| `.coderabbit.yaml` | Review instructions, including the deliberate non-findings |

## Runtime/Tooling Preferences

- **Python 3.8 is a hard floor** (Kodi 19 "Matrix"). No walrus-free worries, but no 3.9+ syntax or stdlib.
- Runtime dependency is declared to Kodi, not pip: `addon.xml` imports `xbmc.python 3.0.0` and `script.module.requests 2.22.0`. The `compat-py38-requests` CI job pins `requests==2.22.0` to hold that floor honest.
- `mypy` is capped `<2.0`: 2.x dropped `--python-version 3.8/3.9` and would silently discard this project's target. `pyproject.toml:76` sets `python_version = "3.9"` — the lowest current mypy accepts — with real 3.8 compatibility guarded by the CI matrix instead.
- Ruff: `select = ["E", "F", "I", "B", "UP"]`, `target-version = "py38"`, `line-length = 100`.
- Dependabot covers `github-actions` only; pip is excluded because these floors are decisions, not lag.

## Testing & QA

pytest, **1778 tests, ~93% coverage against `fail_under = 90`** with branch coverage on. Roughly 2.5s for the full suite — there is no excuse for not running it.

- **Kodi is faked, not installed.** `tests/kodistubs/install.py` is a context manager that snapshots `sys.modules`, injects fresh fake `xbmc`/`xbmcgui`/`xbmcplugin`/`xbmcaddon`/`xbmcvfs` modules, re-imports the targets against them, and restores everything exactly on exit. Fakes are per-test, and `tests/kodistubs/fakes.py:Env` records every Kodi call for assertions.
- **The fake `WindowXML` validates control ids against the real skin XML** (`tests/kodistubs/modules.py`), so a Python/skin mismatch fails in tests.
- **Tests are order-randomized** (`pytest-randomly`). Never rely on cross-test state.
- **Warnings are errors**: `filterwarnings = ["error", ...]` (`pyproject.toml:12-16`). `ResourceWarning` therefore fails the suite — this is why `lib/service_runner.py` closes `HTTPError` responses inside its health-check loop, and why `PERF203` there is deliberate.
- **Network is blocked at the socket level** by an autouse fixture in `tests/conftest.py`; use `FakeSession`/`FakeResponse` from there.
- Windows are exercised by calling `onInit`/`onClick`/`onAction` directly, not through a Kodi event loop.
- `tests/test_glyph_coverage.py` and `tests/test_skin_xml.py` need real Estuary fonts at `/usr/share/kodi/addons/skin.estuary/fonts`; they run locally on a machine with Kodi installed and skip in CI. Expect **1778 passed / 0 skipped** locally versus 2 skips on CI.

CI gates every PR with six required checks: `build`, `lint-type`, `test (3.8)`, `test (3.11)`, `test (3.13)`, `compat-py38-requests`. `main` requires them plus conversation resolution and linear history; squash-merge only.

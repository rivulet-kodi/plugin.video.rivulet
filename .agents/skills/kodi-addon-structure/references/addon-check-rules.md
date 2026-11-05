# kodi-addon-checker rules vs Kodi runtime

`kodi-addon-checker` (https://github.com/xbmc/addon-check, `kodi_addon_checker/`) is what the official Kodi repository runs. A third-party repository need not pass it, but its findings mark real portability or hygiene problems. Severity names are the checker's (`PROBLEM` fails, `WARNING`, `INFORMATION`). **Kodi itself enforces none of these** unless the last column says so.

| Check (file) | Severity | Rule | Enforced by Kodi core? |
|---|---|---|---|
| schema validation (`schema_validation.py`, `xml_schema/*.xsd`) | PROBLEM | metadata structure, news ≤1500, summary ≤1500, description ≤2500, platform enum, assets list | no |
| dependencies (`check_dependencies.py::check_python_dependency`) | PROBLEM | `xbmc.python` import version must be ≥ 3.0.0 | no (install-compat does `MeetsVersion`) |
| artwork (`check_artwork.py`) | PROBLEM | icon/fanart/screenshot/banner/clearlogo declared where required (icon+fanart are mandatory from Krypton), format, size, transparency, max size | no |
| executables (`check_files.py::check_file_permission`) | PROBLEM | no file with the X bit | no |
| file whitelist (`check_files.py::check_file_whitelist`) | WARNING | extension whitelist (see `addon-xml-matrix.md`) | no |
| license file (`check_addon.py`, only when `check_license_file_exists` is enabled) | per `handle_files.addon_file_exists` | regex `^LICENSE\.txt\|LICENSE\.md\|LICENSE$` | no |
| entry point (`check_entrypoint.py`) | WARNING | ≥15 logical lines in a `library=` file | no |
| strings (`check_string.py`) | PROBLEM (CRLF line endings: WARNING) | legacy `strings.xml` forbidden; old language directory layout flagged (`check_files.py::check_for_new_language_directory_structure`); `strings.po` UTF-8, non-empty, no BOM, required header, parses with polib, `resource.language.en_gb` present, dir name matches `RE_LANG_CODE` | partly: `POUtils` needs `msgctxt "#id"`; `en_gb` fallback is hard-coded |
| URLs (`check_url.py`) | WARNING/PROBLEM | HEAD request on `forum`, `source`, `website` | no |
| blacklisted terms (`check_string.py::find_blacklisted_strings`) | PROBLEM/WARNING | a general term list plus Leia deprecations behind `check_kodi_leia_deprecations`; term lists come from the checker's config, not read here | no |
| repository checks (`check_repo.py`) | — | official-repo layout; contents not read `[unverified]` | no |

## Where the checker and Kodi source disagree

- `<news>`: 1500-char cap exists only in the xsd/wiki. `AddonInfoBuilder.cpp` stores any length.
- `<assets><thumb>`: parsed by Kodi from Nexus; the checker schema rejects it.
- `<platform>`: checker enum predates `linux-x86_64`-style tags that Kodi 22 (Piers) accepts.
- `<hashes>`: xsd accepts only `sha256 sha512 sha1 md5`; Kodi also accepts `true`/`false`.
- `<info compressed>`: the xsd allows it, Kodi never reads it.
- `<license>`: both accept free text; the wiki documents `|`, `&`, `,` separators, SPDX `AND`/`OR` are not mentioned.
- `xbmc.service start="…"`: `leia_service.xsd` declares only `point id name library`; the attribute is not read by Kodi. Whether the checker rejects the extra attribute was not executed `[unverified]`.
- Running the checker: `kodi-addon-checker --branch matrix <addon-dir>` (branches `gotham … matrix nexus omega piers`) `[unverified: not run here]`.

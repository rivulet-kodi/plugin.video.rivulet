# Dependencies, visibility, requirements

## Which mechanism for which job

| need | use | evaluated |
|---|---|---|
| always hidden (storage only) | `<visible>false</visible>` on the setting | static |
| always disabled | `<enable>false</enable>` | static |
| disabled/hidden depending on another setting | `<dependencies><dependency type="enable\|visible" ...>` | re-evaluated when the referenced setting changes |
| depends on a Kodi/Python InfoBool | `<condition on="property" name="InfoBool">expr</condition>` | no setting is referenced, so Kodi has no change trigger; treat it as evaluated when the dialog builds `[INFERENCE from ResolveSettingDependencies, which only wires `setting=` references]` |
| depends on the platform | `<requirement>` with a simple condition (`has_gl`, `have_x11`, `isstandalone`, ...) | static |
| indented child look | `parent="other_id"` on the setting | layout only |

`parent` indents the child (one level per ancestor) and disables it only when
the PARENT SETTING IS ITSELF DISABLED (`CSetting::IsEnabled`). A parent
boolean being switched off does not disable children. Pair `parent` with an
explicit `enable` dependency.

`CSetting::SetEnabled` is a no-op when the setting has any dependency.

## Syntax

Single condition (the `<dependency>` element is itself the condition):

```xml
<dependencies>
  <dependency type="enable" setting="buffer_enable">true</dependency>
</dependencies>
```

Negated and compared:

```xml
<dependency type="enable" setting="mode" operator="!is">0</dependency>
<dependency type="visible" setting="level" operator="gt">2</dependency>
```

Combined (`and`/`or` nest; the default combination is AND):

```xml
<dependency type="visible">
  <and>
    <condition setting="a">true</condition>
    <or>
      <condition setting="b" operator="lt">5</condition>
      <condition on="property" name="InfoBool">System.HasAddon(plugin.foo)</condition>
    </or>
  </and>
</dependency>
```

| item | values |
|---|---|
| `<dependency type=>` | `enable`, `visible`, `update` |
| `<condition on=>` | `setting` (default when `setting=` is present), `property` |
| `operator=` | `is`, `lessthan`/`lt`, `greaterthan`/`gt`, `contains`; each takes a `!` prefix; `negated="true"` also valid |
| value compare | numeric when the value parses as an integer, else string; booleans are `true`/`false` |
| property `name=` | `InfoBool` (add-ons), plus the simple conditions listed below for `<requirement>` |

Pitfalls verified in source:

- An empty `<and>`/`<or>` checks false for dependencies.
- A condition on an unknown setting id logs a warning and evaluates false, so
  a typo silently hides/disables the setting.
- `<dependencies>` is read from the `<setting>` node; element order inside the
  setting does not matter.
- `XMLUtils::GetBoolean` accepts `true|yes|on|enabled` and
  `false|no|off|disabled|0`; an empty element is false.
- A category whose settings are all hidden by dependency disappears from the
  category list.

## Simple conditions (`<requirement>`), by version

Common to Matrix..master: `true`, `has_upnp`, `has_airplay`, `have_x11`,
`have_wayland`, `has_gl`, `has_gles`, `has_web_server`, `have_libva`,
`have_osx`, `have_ios`, `have_tvos`, `has_dx`, `isstandalone`,
`has_filesystem_smb`.

| added/removed | conditions |
|---|---|
| Nexus+ | `has_filesystem_nfs`, `have_libbluray`, `has_cdda_ripper` |
| Nexus only | `has_dvd_drive` |
| Omega | `has_optical_drive`, `have_webos`, `has_xbmchelper` |
| Piers (22) | `have_gbm`, `has_uwp`; `has_xbmchelper` removed (also `hasmultiplemonitors`, a non-simple `has*` condition) |
| master (23, unreleased) | + `have_android`, `have_wasm` |

The list is the registry in `xbmc/settings/SettingConditions.cpp` of each
branch. Check the branch before relying on a platform condition.

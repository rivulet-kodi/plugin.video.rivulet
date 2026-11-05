---
name: kodi-skin-windowxml
description: "Authoring and reviewing add-on window XML (resources/skins/Default/1080i/*.xml) loaded by xbmcgui.WindowXML and WindowXMLDialog on Kodi 19-22: how Kodi resolves the file, textures, fonts and includes, which window/control tags the parser actually reads, silent-failure traps (aligny, align case, wrapmultiline in lists, 6-digit colours, focus, animations), and a review checklist; use when creating or editing any add-on skin XML or the Python that drives it."
---

# Kodi add-on skin XML (WindowXML / WindowXMLDialog)

Everything here was read from Kodi source (`xbmc/xbmc`: `Matrix`=19, `Nexus`=20, `Omega`=21, `Piers`=22 RC1; `master` is 23 alpha) and kodi.wiki. Where the wiki and the code disagree, the **code wins** (called out inline). `[unverified]` = not confirmed in source. Long tag matrices live in `references/control-tags.md`; the load/resolve walk-through with source pointers is in `references/resolution-and-loading.md`.

## When to use

- Creating or editing an add-on window XML, or the Python class that opens it (`xbmcgui.WindowXML`, `WindowXMLDialog`, `WindowXMLDialog` subclasses).
- Debugging "control does nothing / wrong colour / wrong alignment / nothing focused / window will not open".
- Reviewing a skin XML diff against what the parser really reads.

Not for: skin add-ons (`xbmc.gui.skin`), `Includes.xml`, `Font.xml`, themes, `settings.xml`, `addon.xml`.

## 1. How an add-on window is resolved (Kodi 19-22 identical)

`xbmcgui.WindowXML(xmlFilename, scriptPath, defaultSkin='Default', defaultRes='720p', isMedia=False)` (`xbmc/interfaces/legacy/WindowXML.cpp`):

1. **The active skin wins by filename.** If the active skin has an XML with the same file name, Kodi loads *that*, not yours. Give add-on windows names no skin uses (prefix them: `DarkConfirmDialog.xml`, never `DialogConfirm.xml`, `Home.xml`, `MyVideoNav.xml`...).
2. Else `<scriptPath>/resources/skins/<activeSkinId>/<res>/<file>` (e.g. `skin.estuary`) lets an add-on ship per-skin overrides.
3. Else `<scriptPath>/resources/skins/<defaultSkin>/<res>/<file>`. Missing everywhere -> `WindowException: XML File for Window is missing`.
4. `<res>` folder: nearest declared resolution to the screen; `defaultRes` is only the fallback (`CSkinInfo::GetSkinPath`). `1080i` is a valid folder name; coordinates are then in a 1920x1080 space and Kodi scales to the real screen.
5. **Textures** resolve as `<parent of the res folder>/media/<name>` (`WindowXML::AllocResources` calls `AddTexturePath`). The *active skin's* media path is searched first and its `Textures.xbt` bundle before loose files, so an add-on file named like a skin texture is shadowed. Prefix your textures (`look_*`, `dlg_*`).
6. **Fonts come only from the active skin's `Font.xml`.** An add-on cannot ship or define fonts. `GUIFontManager::GetFont` matches names case-insensitively and **silently falls back to `font13`** when the name is unknown (all four branches). Stick to the Estuary ladder (see §6).
7. **Includes/variables/constants/expressions are the active skin's.** `CGUIWindow::Prepare` resolves `<include>` only against the active skin's `Includes.xml`. An add-on XML must be self-contained: no `<include>`, `$VAR[]`, `$EXP[]`, `<constant>`. Repeat markup instead.
8. `WindowXMLDialog` sets `RENDER_ORDER_DIALOG` when the XML has no `<zorder>`.
9. `WindowXML::OnClick`/`OnDoubleClick` return false and the container context-menu action is swallowed at the end of `WindowXML::OnMessage`: Python must handle clicks via `onClick(controlId)` / `onAction`.

Open pattern (positional args are deliberate; keyword names differ between Kodi builds):

```python
win = MyWindow('MyWindow.xml', addon.getAddonInfo('path'), 'Default', '1080i')
```

## 2. Window root

Root must be `<window>` (case-insensitive compare, `CGUIWindow::LoadXML`). Tags are **case-sensitive**: wiki "All tag names are lower case. XML tag names are case sensitive."

| Tag | Read by Kodi | Notes |
|---|---|---|
| `<defaultcontrol always="true">ID</defaultcontrol>` | yes | Focus on open. Without it **nothing is focused** (default 0) until Python calls `setFocusId`. `always` = ignore the remembered last-focused control. |
| `<onload>` / `<onunload>` | yes | Builtin run on open/close. |
| `<backgroundcolor>` | yes | Default opaque black clear. `0` skips the clear: cheaper when opaque art covers the screen (wiki, Skinning Manual). |
| `<coordinates><left/><top/></coordinates>` (also `posx/posy/origin`) | yes | Window origin. |
| `<zorder>` | yes | Draw order among dialogs. |
| `<animation type="WindowOpen/WindowClose">` | yes | See §5. |
| `<depth>`, `<camera>` | yes | Stereoscopic 3D / rotations only; `depth` is a no-op in normal 2D. |
| `<menucontrol>`, `<previouswindow>`, `<hitrect>`, `<visible>` | read by `CGUIWindow::Load` | wiki: window-level `<visible>` / `modality` only apply to `type="dialog"` windows. `WindowXMLDialog` wraps `CGUIMediaWindow`, not `CGUIDialog` -> **[unverified]** that root `<visible>` works there. Put visibility on a root `<control type="group">`. |
| `<controls>` | yes | The only container of controls. |
| `<type>`, `<id>` | **no** | Listed in the wiki window header, but `CGUIWindow::Load` has no branch for them (grep of `GUIWindow.cpp` finds no `"type"`/`"id"` tag match). Omit. |

Window defaults (`CGUIWindow::SetDefaults`): `defaultcontrol=0`, `always=false`, clear colour `0xff000000`. `RestoreControlStates` focuses `defaultcontrol` (or the last focused control unless `always`).

## 3. Controls: what is parsed

All in `CGUIControlFactory::Create` (`xbmc/guilib/GUIControlFactory.cpp`). Unknown tags are **silently ignored**, never reported. Tag names are matched exactly (lowercase); `type=` is matched case-insensitively.

### Tags every control accepts

`id`, `left|right|centerleft|centerright|width`, `top|bottom|centertop|centerbottom|height` (legacy `posx/posy` work), `visible` (several `<visible>` siblings are **ANDed**; `allowhiddenfocus="true"`), `enable`, `animation`, `colordiffuse` (element), `depth`, `camera`, `hitrect`, `hitrectcolor`, `description` (ignored, comment only). Focusable controls add `onup ondown onleft onright onnext onprev onback oninfo` (optional `condition=` attribute), `onclick onfocus onunfocus altclick`. `width`/`height` accept `auto` (+ `min`/`max` attributes) on labels in a grouplist and on buttons.

Position values: pixels in the window's coordinate space, `NN%` of parent, `NNr` measured from the parent's right/bottom edge. Children are positioned **relative to the parent group**; a group's `left/top` shifts everything inside.

### Control types (Kodi 19 list; later additions in §4)

`button`, `togglebutton`, `radiobutton`, `label`, `fadelabel`, `textbox`, `image`, `multiimage`, `progress`, `slider`, `sliderex`, `spincontrol`, `spincontrolex`, `edit`, `scrollbar`, `group`, `grouplist`, `list`, `wraplist`, `fixedlist`, `panel`, `epggrid`, `rss`, `videowindow`, `visualisation`, `renderaddon`, `gamewindow`, `mover`, `resize`, `ranges`, `gamecontroller`.

Per-type tag tables: `references/control-tags.md`.

### What Python can wrap with `getControl(id)`

Mirrors `xbmc/interfaces/legacy/Window.cpp::GetControlById`: `button, togglebutton, radiobutton, label, fadelabel, textbox, image, progress, slider, spincontrol, edit, list, wraplist, fixedlist, panel, group, videowindow`. **Not wrappable** -> `RuntimeError: Unknown control type for python`: `grouplist`, `scrollbar`, `multiimage`, `sliderex`, `rss`, etc. Drive such a control through `setVisible()`/`setPosition()` on a wrapped `group` parent, or through window properties + infolabels. Non-existent id -> `RuntimeError: Non-Existent Control`.

### Text, alignment (the silent-failure area)

| Tag | Real behaviour (source) |
|---|---|
| `<align>` | Exact lowercase strings only: `left` (default), `center`, `right` (`bottom` also maps to right), `justify`. Anything else (`Center`, `centre`, `middle`) = left. |
| `<aligny>` on label/button/edit/textbox | **Only `center` has an effect.** `top`, `bottom`, anything else = top. Kodi 19/20/21: `if (strAlign == "center")`; Kodi 22: values `{0, top=0, center=CENTER_Y, bottom=0}`. Wiki says "top or center": correct. Default is **top**, not centred. |
| `<aligny>` on `fixedlist` (Kodi 22 only) | Container tag: `top`/`center`(default)/`bottom` item position. Absent in 19-21 (ignored). |
| `<aspectratio align="" aligny="">` | Different thing: `align` left/center/right, `aligny` top/center/bottom are valid here. |
| `<label>` | Literal text, `$INFO[...]`, `$ADDON[id N]` (add-on string), `$LOCALIZE[N]` (core string), `[B]..[/B]`, `[COLOR x]..[/COLOR]`, `[CR]`. |
| `<wrapmultiline>true` | Wraps a **non-list** `label`. Inside `itemlayout`/`focusedlayout` a label becomes `CGUIListLabel` which is built with overflow TRUNCATE or SCROLL only -> `wrapmultiline` is **ignored**. Use a `textbox` for wrapped text in a list (it honours `align` and `aligny=center`). |
| `<scroll>` | Label scroll mode. In lists default is "scroll while focused"; `true` = always, `false` = never; otherwise clipped, no ellipsis. `scrollspeed` default 60 (wiki), `scrollsuffix`. |
| `<textcolor>`, `<shadowcolor>`, `<focusedcolor>`, `<disabledcolor>`, `<selectedcolor>`, `<invalidcolor>` | Parsed via `CGUIInfoColor`. In a list layout `CGUIListLabel::UpdateColors` does not pass the item, so `$INFO[ListItem...]` in `<textcolor>` is **not** evaluated per item (it takes whatever the container's current item gives); `colordiffuse` is. |
| `<font>` | Name from the active skin's `Font.xml`; unknown -> `font13`. `monofont` is a TextViewer-only textbox option. |
| `<textoffsetx/y>`, `<textwidth>`, `<angle>` | Button/edit/label label-info tags. |

### Colours

- Form: `AARRGGBB` hex, or a colour name from the active skin's colour theme, or `$INFO[...]`/`$VAR[...]` (dynamic).
- `CGUIColorManager::GetColor` uses `sscanf("%x")`: **a 6-digit value `RRGGBB` parses as `0x00RRGGBB` = alpha 0 = invisible**, not opaque. Always write 8 digits.
- Named colours (`white`, `grey`, `green`...) are skin-theme names; an add-on cannot define them. Use hex.

### Image/texture

| Tag / attribute | Notes |
|---|---|
| `<texture>` (image), `<texturefocus>`/`<texturenofocus>`/`<alttexture*>` (button), `<texturebg>`/`<midtexture>`/`<lefttexture>`/`<righttexture>`/`<overlaytexture>` (progress), `<texturesliderbackground|bar|barfocus|nib|nibfocus>` (scrollbar/slider) | Relative names resolve per §1.5. Empty element = no texture (legal). |
| `border="l,t,r,b"` or one number | 9-slice; the border is in *texture pixels*, scaled with the control. |
| `diffuse="file"` | Mask texture multiplied into the image (rounded corners). Scaled to the image unless `<aspectratio scalediffuse="false">`. |
| `colordiffuse="AARRGGBB"` | Tint (attribute on the texture, or element on the control). |
| `flipx` / `flipy` | `true`/`false`. |
| `background="true"` | Force the background (async, large-texture) loader; resized to the control. Local relative names normally use the synchronous loader; remote and non-HD full paths go to the background loader automatically (`GUITexture::AllocResources` / `CGUITextureManager::CanLoad`). Use for any poster/fanart from `$INFO[ListItem...]`. |
| `infill` | Kodi 20+ (Nexus). |
| `<aspectratio>` | Text `stretch` (default) / `scale` (fill + crop) / `keep` (fit + letterbox) / `center` (no scale). Attributes `align`, `aligny`, `scalediffuse`. |
| `<fadetime>`, `<preloaditems>` | Image fade-in ms; container preload 0..2. |

### Containers

`list`, `wraplist`, `fixedlist`, `panel`: `orientation` (horizontal/vertical, default vertical), `pagecontrol` (scrollbar id), `scrolltime` (ms; attributes `tween=`/`easing=`), `focusposition` (wraplist/fixedlist), `preloaditems`, `autoscroll`, `<content>` (static/dynamic), `<itemlayout>`, `<focusedlayout>`.

- `itemlayout`/`focusedlayout` attributes: `width`, `height` (the *scroll axis* one is mandatory: height for vertical, width for horizontal; panels need both), optional `condition=`, `infoupdate=` (ms). Several layouts allowed; the first whose `condition` is true is used, evaluated on list refresh.
- Keep item and focused layouts the **same size** and the container's height (vertical) / width (horizontal) a whole multiple of it (repo test rule 6; Kodi itself only reads each layout's own size, so mismatches show as jumpy scrolling / a clipped last item).
- A `group` inside a layout becomes `CGUIListGroup` (no `defaultcontrol`). Only the focus animation and conditional animations work on layout children (wiki, Container Item Layout).
- Focus-dependent drawing: `<visible>Control.HasFocus(<listId>)</visible>` on a child inside `focusedlayout`. `GUIBaseContainer.cpp` picks the focused layout with `focused = (current == GetOffset() + GetCursor())`, independent of whether the *container* has focus, so an unguarded "focus ring" image shows on the selected row of an unfocused list.
- `fadelabel` in a layout: wiki says not supported in lists; the factory has no list-specific branch for it, so treat as **[unverified]**, avoid.
- Container context-menu/click behaviour: see §1.9.

### Focus and navigation

- `onup/ondown/onleft/onright="ID"` -> that control; pointing at a group/grouplist/container restores its remembered child. Containers and grouplists move focus along their own axis by themselves; for every other direction, and for plain groups, state the target explicitly (an omitted direction having a geometric fallback is **[unverified]**).
- A **hidden (`<visible>` false) or disabled control cannot be focused or navigated to**; use `allowhiddenfocus="true"` plus `Control.HasFocus(id)` only for the "appears on focus" pattern.
- A `list` that should trap left/right: `onleft`/`onright` = its own id.
- Hidden/1x1 controls: legal, used as Python-written property sinks or `pagecontrol` for a list, but each is a real control in the tree. Prefer `Window.Property(name)` + `$INFO[Window.Property(name)]`.
- `defaultcontrol` on a `group`/`grouplist`: focuses that child first; the group's own focus remembers the last child.

## 4. Per-version differences (19 / 20 / 21 / 22)

| Area | 19 Matrix | 20 Nexus | 21 Omega | 22 Piers |
|---|---|---|---|---|
| Window XML resolution (`WindowXML.cpp`) | as in §1 | same | same | same (`g_SkinInfo` -> `GetSkinInfo()`) |
| New control types | base set (§3) | `colorbutton` | `gamecontrollerlist` | (`groupmask` appears only in `master`/23) |
| Button/image tags | - | `colorbox`, `colorwidth/height/posx/posy`, `movingspeed`, `texturecolormask`, `texturecolordisabledmask`, `texture infill` | `texturesliderbardisabled`, `textureslidernibdisabled` | `imagefilter`, `diffusefilter`, `startmovement/endmovement`, `minspertimeblock`, fixedlist `<aligny>` |
| Label `aligny` | center only | center only | center only | explicit table, `bottom` -> top |
| Python 3 | 3.8 floor | 3.8+ | 3.8+ | 3.8+; SWIG `-builtin` (see repo traps) |
| `master` (23 alpha) | | | | adds `groupmask`, texture `mipmap`/`subpixel` |

Authoring for the 19..22 range: use only tags present in Matrix (the base set), or guard with a visible fallback; do not use `infill`, `colorbox`, `colorbutton` or fixedlist `aligny`. Older Kodi ignores unknown tags silently, so failures are cosmetic, not errors.

## 5. Animations

Two syntaxes (`CAnimation::Create`, `VisibleEffect.cpp`):

```xml
<animation type="WindowOpen" reversible="false">          <!-- new: one or more <effect> children -->
  <effect type="fade" start="0" end="100" delay="0" time="200" />
</animation>
<animation effect="zoom" pulse="true" center="auto" start="0,100" end="100,100"
           time="1000" condition="Control.IsVisible(30001)">Conditional</animation>   <!-- old: text = type -->
```

- Types (case-insensitive; `Visible*` prefix matches): `WindowOpen`, `WindowClose`, `Visible`/`VisibleChange`, `Hidden`, `Focus`, `Unfocus`, `Conditional`. `Conditional` **requires** `condition=` or the animation is dropped with a log line.
- `effect`: `fade`, `fadediffuse`, `slide`, `rotate`, `rotatex`, `rotatey`, `zoom`. `start`/`end` mean opacity % (fade), `x,y` px offset (slide), degrees (rotate), `x%,y%` or `x,y,w,h` (zoom). `center="auto"`. `tween` (linear, quadratic, cubic, sine, back, circle, bounce, elastic) + `easing` (in/out/inout). `reversible="false"`, `pulse="true"`, `loop="true"`.
- An animation node needs a child (text or `<effect>`) or it is skipped.
- Animation attributes are case-sensitive.
- **`WindowClose` delays closing.** `CGUIWindowManager::HandleAction` drops every action (warning log "ignoring action N, because topmost modal dialog closing animation is running") while the topmost modal dialog plays one. Back/Esc pressed twice fast is lost; do not use `WindowClose` on modal dialogs that must respond (the repo bans it, rule 9 in `tests/test_skin_xml.py`).
- `pulse`/`loop` Conditional animations keep the GUI redrawing every frame while their condition holds: acceptable for a loading dialog, bad for a permanent shimmer on a low-power box.

## 6. Best practices

1. **One root `<control type="group">`** wrapping everything with `<visible>!Player.HasVideo</visible>` (+ `WindowOpen` fade) when the window must hide during playback; this is the form every window in this repo uses (root `<visible>` is [unverified] for `WindowXMLDialog`, §2).
2. **Fonts: the Estuary ladder only** (`font10 23`, `font12 25`, `Mono26 26`, `font27 27`, `font13 30`, `font32 32`, `font37 37`, `font45 45`, `font60 60`): they exist in Estuary, the default skin; other skins' `Font.xml` may lack them and Kodi falls back to `font13` silently.
3. **Assume nothing about the user's skin.** No named colours, no includes, no skin textures, no `$VAR`; hex colours (8 digits), own textures, literal markup.
4. **Strings**: `$ADDON[<addon.id> <id>]` (id from your `strings.po`), not `$LOCALIZE[]` (core strings only). Keep literals out of `<label>`.
5. **Hex colours: 8 digits, always.** Validate attribute *and* element forms (`colordiffuse`, `textcolor`, `shadowcolor`, `focusedcolor`...).
6. **Images from data** (`$INFO[ListItem.Property(thumb)]`, `$INFO[ListItem.Art(poster)]`): `background="true"`, fixed `width`/`height`, `aspectratio`; reserve GPU memory: huge unscaled JPEGs cost width x height x 4 bytes; use `preloaditems` 2 on posters.
7. **Tinting**: one white texture + `colordiffuse` gives any flat colour/alpha; use `diffuse="mask.png"` for rounded corners instead of baking each shape.
8. **Navigation explicit** for every focusable control that has more than one neighbour; make edge `onleft/onright=self` for trap lists.
9. **Set focus in Python only after the window is shown**: `onInit` -> `setFocusId`. For dialogs, prefer `<defaultcontrol>`.
10. **IDs**: unique per window; keep one id band per window (`30xxx`) so tests/grep can map Python constants to XML.
11. **Hidden helper controls** (1x1, `<visible>false</visible>`) cost layout and message handling; prefer window properties. If Python must own a control's visibility (`setVisible`), do **not** also give it a static `<visible>`: the in-XML condition is re-evaluated and overrides `setVisible` (stated in the `DarkBusyDialog.xml` comment; [unverified] in source).
12. **No text measurement.** Width is parsed once at load; a control cannot grow to its text (except `<width>auto</width>` for labels in a grouplist / buttons). Prefer one flowing label to chips of guessed width.
13. **Test**: parse every window with an XML parser in CI and check: ids unique, nav targets exist and are focusable, textures exist, fonts in the ladder, colours 8-hex, no `<include>`, layout heights equal.

## 7. This repo's conventions and traps

Verified in `AGENTS.md`, `tests/test_skin_xml.py`, `tests/test_glyph_coverage.py`, `tests/kodistubs/modules.py`, `lib/ui/uicommon.py`.

- 13 windows in `resources/skins/Default/1080i/`: `HomeWindow`, `DetailWindow`, `StreamsWindow`, `SearchWindow`, `ShowcaseWindow`, `AddonsWindow`, `AddonCatalogWindow`, `PairDialog`, `Dark{Busy,Confirm,Countdown,OptionList,Progress}Dialog`. All have `<window>` root, `<depth>0.52`, `<coordinates>`, and a root group with a 200 ms `WindowOpen` fade. Textures in `resources/skins/Default/media/` (`white.png`, `look_*`, `dlg_*`, `mask_*`, `pill_fill.png`).
- Opened via `lib/ui/uicommon.py::open_window`: `window_cls(xml_name, addon_skin_path(), 'Default', '1080i', ...)`; `ShowcaseWindow` the same in `infowindow.open_showcase`. Every screen is a `WindowXMLDialog`; `ModalStackWindow` must be first in the bases.
- `tests/kodistubs/modules.py` fake `WindowXML.getControl` validates ids against the real XML and raises like Kodi for undeclared ids and unwrappable types (`WRAPPABLE_CONTROL_TYPES`). Scrollbar/grouplist are not wrappable: `DarkOptionListDialog.xml` wraps the scrollbar in group `30346` and `lib/ui/dialogs.py` moves the group.
- `test_skin_xml.py` rules: 1 well-formed; 2 fonts in the Estuary ladder; 3 static textures exist; 4 `colordiffuse` *attributes* 8-hex; 5 `$ADDON[plugin.video.rivulet N]` ids exist in `strings.po`, `$LOCALIZE[` banned, no untranslated literals except `RIVULET/OK/BACK`; 6 list/fixedlist layout heights equal and divide the control height; 7 no `$`-bound `<textcolor>` in layouts; 8 same-box different-font labels in layouts need `aligny=center`; 9 no `WindowClose` animation. `test_home_row_navigation_follows_the_entire_slot_chain` pins HomeWindow's panel chain.
- Gaps the tests do **not** cover (add if you touch these): 6-digit hex in element forms (`<textcolor>`, `<shadowcolor>`, `<colordiffuse>` element); label `aligny` other than `center`; `wrapmultiline` in layouts; nav targets that exist but are not focusable; unknown/misspelt tags.
- `Mono26` renders through NotoMono in Estuary; `↑ → ▲ ● ★` become tofu. `tests/test_glyph_coverage.py` enforces this.
- Python-populated sinks: 1x1 `visible=false` labels/textboxes/images in Home, Detail, Search, Streams are written by Python (ids `300xx`/`301xx`).
- AGENTS.md says skin XML reads strings as `$LOCALIZE(30360)`; the XML and the tests use `$ADDON[plugin.video.rivulet 30360]` and ban `$LOCALIZE`. Follow the tests.

## 8. Review checklist

- [ ] File name cannot collide with a skin XML (prefix).
- [ ] Root `<window>`; focus defined (`<defaultcontrol>` or Python `setFocusId`); `<defaultcontrol>` id exists and is focusable.
- [ ] No `<include>`, `$VAR`, `$EXP`, named colours, skin-only textures/fonts.
- [ ] Every tag lowercase and in the control's parsed set (`references/control-tags.md`); no stray tags from another control type.
- [ ] `<align>` in {left,center,right,justify}; label `<aligny>` is `center` or absent (never `top`/`bottom`).
- [ ] All colours `AARRGGBB` (8 hex) or `$INFO[...]`; check both attributes and elements.
- [ ] No `wrapmultiline` on a label inside `itemlayout`/`focusedlayout` (use `textbox`).
- [ ] List layouts: `width`/`height` on the scroll axis, same in item and focused layout, container size a multiple; focus-ring images gated by `Control.HasFocus(id)`.
- [ ] Nav: every `on*` target exists and is a focusable control type; a target that is hidden in some states (hidden controls cannot take focus) must be a deliberate no-op; edges trap or route intentionally.
- [ ] Python `getControl` only on wrappable types; ids exist in this window's XML.
- [ ] Posters/fanart use `background="true"` with a fixed box; all static textures exist in `media/`.
- [ ] Animations: `Conditional` has `condition`, no `WindowClose` on modal dialogs, pulse/loop only on temporary UI.
- [ ] Strings via `$ADDON[id N]`; id exists in `strings.po` for all locales.
- [ ] Fonts from the Estuary ladder; glyphs checked per font.
- [ ] Tested on Kodi 19 and the newest target: tags added in 20/21/22 degrade gracefully.

## Sources

- Window resolution: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/legacy/WindowXML.cpp (and `Matrix`, `Nexus`, `Omega`, `master`)
- Window tags: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/GUIWindow.cpp
- Control parsing: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/GUIControlFactory.cpp (and `Matrix`, `Nexus`, `Omega`, `master`)
- Skin path/res: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/addons/Skin.cpp
- Texture resolution/loader: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/TextureManager.cpp , .../GUITexture.cpp , .../AspectRatio.h
- Layouts: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/GUIBaseContainer.cpp , .../GUIListItemLayout.cpp , .../GUIListLabel.cpp , .../GUILabel.cpp , .../GUITextBox.cpp
- Animations: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/VisibleEffect.cpp
- Fonts: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/GUIFontManager.cpp
- Colours: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/GUIColorManager.cpp , .../guiinfo/GUIInfoColor.cpp
- Modal action drop: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/guilib/GUIWindowManager.cpp (`HandleAction`)
- Python wrappers: https://raw.githubusercontent.com/xbmc/xbmc/Piers/xbmc/interfaces/legacy/Window.cpp
- Wiki: https://kodi.wiki/view/Skinning_Manual , https://kodi.wiki/view/Default_Control_Tags , https://kodi.wiki/view/Container_Item_Layout , https://kodi.wiki/view/Aspect_Ratio , https://kodi.wiki/view/Conditional_Visibility , https://kodi.wiki/view/Animating_your_skin

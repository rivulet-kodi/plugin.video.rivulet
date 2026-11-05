# Control tag matrix (Kodi 19-22)

Source of truth: `CGUIControlFactory::Create` in `xbmc/guilib/GUIControlFactory.cpp` (`Matrix`/`Nexus`/`Omega`/`Piers`). The factory reads **one flat set of tags for every control** and each `case` passes only the ones that control's constructor uses; a tag a control does not use is parsed and discarded **without any warning**. The tables list what each type actually consumes. Defaults are from the wiki Skinning Manual unless noted. All tag names are lowercase and case-sensitive; `type=` is matched case-insensitively.

## Common to every control

| Tag | Values |
|---|---|
| `id` | integer; unique per window; required for anything Python, `Control.*` conditions or navigation refers to |
| `left` `top` `right` `bottom` `centerleft` `centerright` `centertop` `centerbottom` `width` `height` | px, `NN%`, `NNr` (from parent's right/bottom). `width`/`height` also `auto` (+ `min`/`max` attributes). Legacy `posx`/`posy` still read. |
| `visible` | `true`/`false`/boolean condition. Several siblings are ANDed (`GetConditionalVisibility`). `allowhiddenfocus="true"` lets a hidden control take focus. |
| `enable` | boolean condition (focusable controls) |
| `animation` | see SKILL.md §5 |
| `colordiffuse` | `AARRGGBB`, theme name, or `$INFO`/`$VAR` |
| `depth`, `camera`, `hitrect`, `hitrectcolor` | 3D / mouse hit area |
| `description` | never read |

Focusable controls also read: `onup ondown onleft onright onnext onprev onback oninfo` (+ optional `condition=`), `onclick`, `onfocus`, `onunfocus`, `altclick`, `ontextchange` (edit).

## label (`CGUILabelControl`; in a layout `CGUIListLabel`)

| Tag | Notes |
|---|---|
| `label`, `info`, `altlabel`, `label2` | text; `$INFO[...]`, `$ADDON[id N]`, `$LOCALIZE[N]`, `[B]`, `[I]`, `[COLOR c]`, `[CR]` |
| `font` | unknown name -> `font13` |
| `textcolor`, `shadowcolor`, `disabledcolor`, `selectedcolor`, `focusedcolor`, `invalidcolor` | 8-hex / name / infolabel colour |
| `align` | `left` (default) `center` `right` `justify`; case-sensitive |
| `aligny` | only `center` is honoured (see SKILL.md §3) |
| `textoffsetx`, `textoffsety`, `textwidth`, `angle` | |
| `scroll` | `true`/`false`; default in lists = scroll while focused |
| `scrollspeed` (60), `scrollsuffix` | |
| `wrapmultiline` | **non-list labels only** (`CGUILabelControl` takes it; `CGUIListLabel` does not) |
| `haspath` | non-list only |
| `width` `auto` | for labels in a grouplist |

## fadelabel
`label` (several allowed), `info`, `scrollout` (default true), `pauseatend`, `randomize`, `scroll`, `scrollspeed`, `font`, colours, `align`, `aligny`. Wiki: not supported inside list layouts.

## textbox (`CGUITextBox`)
`label`/`info`, `font`, `textcolor`, `shadowcolor`, `align` (left/center/right/justify), `aligny` (center honoured: vertical centring when the text fits), `pagecontrol` (scrollbar id), `scrolltime` (ms, default 200 in `Create`), `autoscroll` (element text = bool or condition; attributes `delay`, `time`, `repeat`), `height` `auto`, `monofont` (TextViewer only). Always wraps. Usable inside a layout (`DetailWindow.xml` does it).

## image (`CGUIImage`; `CGUIBorderedImage` when `bordertexture`/`bordersize` set)
`texture` (static path, or `$INFO[...]`/`<info>` for dynamic), attributes `border`, `infill` (20+), `flipx`, `flipy`, `diffuse`, `colordiffuse`, `background`; `aspectratio` (+ `align`, `aligny`, `scalediffuse`); `fadetime`; `bordertexture`, `bordersize`; `imagefilter`/`diffusefilter` (22+, parsed). Texture element empty = nothing drawn.

## multiimage
`imagepath` (folder or `$INFO`), `timeperimage`, `fadetime`, `pauseatend`, `randomize`, `loop`, `aspectratio`. Not wrappable from Python.

## button / togglebutton / radiobutton
`texturefocus`, `texturenofocus`, (`alttexturefocus`, `alttexturenofocus` for toggle), `textureradiofocus/nofocus` family (radio), `label`, `label2`, `font`, `textcolor`, `focusedcolor`, `disabledcolor`, `shadowcolor`, `align`, `aligny`, `textoffsetx/y`, `textwidth`, `angle`, `wrapmultiline` (button label), `onclick`, `onfocus`, `onunfocus`, `altclick`, `pulseonselect`, `width` `auto`. Kodi 20+: `colorbutton` adds `colorbox`, `colorwidth/height/posx/posy`.

## edit
`texturefocus`, `texturenofocus`, `label`, `hinttext`, `password`, `font`, `textcolor`, `focusedcolor`, `textoffsetx/y`, `aligny`, `ontextchange`. Only a focused edit receives raw key presses (the keymap otherwise consumes letters).

## progress
`texturebg`, `lefttexture`, `midtexture`, `righttexture`, `overlaytexture`, `info`/`info2`, `reveal`. Python `ControlProgress.setPercent`.

## scrollbar
`texturesliderbackground`, `texturesliderbar`, `texturesliderbarfocus`, `textureslidernib`, `textureslidernibfocus` (`...bardisabled`/`...nibdisabled` 21+), `orientation`, `showonepage` (default true), `onclick`=`pageup(id)`/`pagedown(id)`. Used as a container `pagecontrol`. **Not wrappable** from Python.

## group
Children `<control>`; `defaultcontrol` (+ `always`), `renderfocusedlast`. Children are positioned relative to the group. In a layout it is `CGUIListGroup` and takes no `defaultcontrol`. Wrappable (`ControlGroup`: `setVisible`, `setPosition`...).

## grouplist
`orientation`, `itemgap` (5), `pagecontrol`, `scrolltime`, `usecontrolcoords`, `align`, `defaultcontrol`, `renderfocusedlast`. Lays children out in sequence on the scroll axis. **Not wrappable** from Python.

## list / wraplist / fixedlist / panel
| Tag | list | wraplist | fixedlist | panel |
|---|---|---|---|---|
| `orientation` (vertical) | yes | yes | yes | yes |
| `pagecontrol` | yes | yes | yes | yes |
| `scrolltime` (+ `tween`/`easing`) | yes | yes | yes | yes |
| `preloaditems` (0..2) | yes | yes | yes | yes |
| `focusposition` | - | yes | yes | - |
| `movement` / `startmovement` / `endmovement` | - | - | `movement`; `start/endmovement` 22+ | - |
| `aligny` (top/center/bottom) | - | - | **22+** only | - |
| `itemlayout` / `focusedlayout` | height (vertical) | same | same | height **and** width |
| `content`, `viewtype`, `autoscroll`, `resetonlabelchange`, `offsetx/y` | yes | yes | yes | yes |

`viewtype` (`list|icon|wide|wrap|biglist|bigicon|bigwide|bigwrap|info|biginfo`, `label=`) has no effect on drawing; it is a hint for skin switching. A container's `<defaultcontrol>` inside `<content>`/provider sets the initial item.

## Layout children
`itemlayout`/`focusedlayout` attributes: `width`, `height`, `condition`, `infoupdate`. Children are normal `<control>`s created with `insideContainer=true`: `label` -> `CGUIListLabel` (single line, truncate/scroll), `group` -> `CGUIListGroup`. Info labels use the `ListItem.*` family. `Container(id).*` reads another container.

## Per-version additions (from the source diffs)

| Version | New |
|---|---|
| 20 Nexus | `colorbutton` control; `colorbox`, `colorwidth`, `colorheight`, `colorposx`, `colorposy`, `movingspeed`, `texturecolormask`, `texturecolordisabledmask`; texture attribute `infill` |
| 21 Omega | `gamecontrollerlist` control; `texturesliderbardisabled`, `textureslidernibdisabled` (falls back to the enabled texture) |
| 22 Piers | `imagefilter`, `diffusefilter`, `startmovement`, `endmovement`, `minspertimeblock`, fixedlist `aligny`, label `aligny` table (`bottom` -> 0) |
| 23 master (alpha) | `groupmask` control; texture attributes `mipmap`, `subpixel` |

Skins/add-ons targeting 19-22 should not rely on any 20+ tag for layout correctness.

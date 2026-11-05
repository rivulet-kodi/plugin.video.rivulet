# Add-on window load path (Kodi 19-22)

Read from `xbmc/xbmc` branches `Matrix`, `Nexus`, `Omega`, `Piers` (22 RC1). Logic is the same across all four unless noted. Raw base: `https://raw.githubusercontent.com/xbmc/xbmc/<branch>/xbmc/`.

## 1. Constructor (`interfaces/legacy/WindowXML.cpp`, `WindowXML::WindowXML`)

1. `activeSkin->GetSkinPath(xmlFilename, &res)`. If the file exists in the **active skin** it is used (name-collision trap).
2. Else `<scriptPath>/resources/skins/<activeSkinID>` (skin-specific override shipped by the add-on).
3. Else `<scriptPath>/resources/skins/<defaultSkin>` with `defaultRes` translated by `CSkinInfo::TranslateResolution` (`1080i`, `720p`, ...).
4. If still missing: `WindowException("XML File for Window is missing")`.

`CSkinInfo::GetSkinPath` (`addons/Skin.cpp`) picks the declared resolution nearest to the display and looks for `<base>/<res>/<file>`; if absent it tries the skin's default resolution. With no `<res>` entries `CSkinInfo::Start()` scans the folder for resolution directories.

## 2. Resources

- `WindowXML::AllocResources` / `Process` call `AddTexturePath(<parent of the res folder>)` -> `<parent>/media/<name>`.
- `CGUITextureManager::GetTexturePath` (`guilib/TextureManager.cpp`) walks texture paths in registration order. The active skin's path is registered first. `m_TexBundle` (`Textures.xbt`) is consulted before loose files. A texture name that exists in the skin shadows an add-on file of the same name. Estuary's bundle uses sub-directory-prefixed names (`colors/white.png`...), so a bare `white.png` does not collide with Estuary; for third-party skins this is **[unverified]**.
- `GUITexture::AllocResources`: `useLarge = info.useLarge || !CanLoad(filename)`. Remote paths and non-"HD" absolute paths go through the background large-texture loader (resized to the control); `background="true"` forces it for local files after a bundled-texture check. Relative names use the synchronous loader. The exact `IsHD` rule is **[unverified]**.
- Fonts: `GUIFontManager::LoadFonts` reads the active skin's `Font.xml` only. `GetFont(name)` is a case-insensitive lookup and **falls back to `font13`** when absent (`"fall back to \"font13\" if we have none"`, present in Matrix, Nexus, Omega, Piers).
- Includes: `CGUIWindow::Prepare` -> `skin->ResolveIncludes(...)` uses the active skin's `Includes.xml` only.

## 3. Window parse (`guilib/GUIWindow.cpp`, `CGUIWindow::Load(TiXmlElement*)`)

Root `<window>` (`EqualsNoCase`). Read tags (exact lowercase): `backgroundcolor`, `onload`, `onunload`, `hitrect`, `previouswindow`, `defaultcontrol` (attr `always`), `menucontrol`, `visible`, `animation`, `zorder`, `coordinates` (`posx|posy|left|top|origin`), `camera`, `depth`, `controls`. Top-level `<control>` compared case-insensitively; **nested** children are found with `FirstChildElement("control")`, which is case-sensitive.

`SetDefaults`: `m_defaultControl = 0`, `m_defaultAlways = false`, `m_clearBackground = 0xff000000`. `RestoreControlStates`: `focusControl = (!m_defaultAlways && m_lastControlID) ? m_lastControlID : m_defaultControl; SET_CONTROL_FOCUS(focusControl)`. `DoProcess` re-focuses `m_defaultControl` when the focused control can no longer focus.

`WindowXMLDialog::LoadXML` sets `RENDER_ORDER_DIALOG` when the XML has no `<zorder>`. `WindowXML::OnClick`/`OnDoubleClick` return false; `ACTION_CONTEXT_MENU` is swallowed at the end of `WindowXML::OnMessage`.

## 4. Closing and input

`CGUIWindow::Close_Internal` delays closing while a `WindowClose` animation runs. `CGUIWindowManager::HandleAction` (`guilib/GUIWindowManager.cpp`) drops every action for a topmost modal dialog while its close animation plays:

> `CGUIWindowManager - HandleAction - ignoring action N, because topmost modal dialog closing animation is running` (Matrix ~l.1064-1075, Piers ~l.1250-1264; `return true`)

## 5. Control construction details

- Hex colours (`GUIColorManager.cpp::GetColor`): `sscanf(..., "%x")`; six digits -> `0x00RRGGBB` (alpha 0).
- `GetAlignmentY` (labels): Matrix/Nexus/Omega `if (strAlign == "center") alignment = XBFONT_CENTER_Y` else 0. Piers: template with `{unknown=0, top=0, center=CENTER_Y, bottom=0}`.
- `align` parse (`GetAlignment`): `right`/`bottom` -> right, `center`, `justify`, else left.
- Label in a container: `new CGUIListLabel(... scrollValue)` (no `wrapMultiLine`). `CGUIListLabel` builds `CGUILabel` with `OVER_FLOW_SCROLL` if `scroll=true` else `OVER_FLOW_TRUNCATE`; wrap overflow is never selected. Non-list: `new CGUILabelControl(..., wrapMultiLine, bHasPath)`.
- `CGUITextBox::Process` honours `XBFONT_CENTER_Y` (vertical centring when the text fits) and `XBFONT_CENTER_X`.
- `scrollValue` default is `CGUIControl::FOCUS`; `<scroll>` sets `ALWAYS`/`NEVER`.
- `CAspectRatio` default: `STRETCH`, align centre/centre, `scaleDiffuse = true` (`guilib/AspectRatio.h`).
- Multiple `<visible>` siblings: `GetConditionalVisibility` ANDs them (`[a] + [b]`).
- `CAnimation::Create` (`guilib/VisibleEffect.cpp`): returns if the node has no child; `condition`, `reversible="false"`; with `<effect>` children the type is the `type` attribute, else the element text; non-matching text (e.g. `Conditional`) stays `ANIM_TYPE_CONDITIONAL`, which logs and returns without a `condition`; `pulse`/`loop` read from the animation node.
- `CZoomEffect`: `start`/`end` with 1 value = x and y scale %, 2 = x,y %, 4 = x,y,w,h; `center="auto"`.
- `GetFont`: see §2.

## 6. Python API boundary (`interfaces/legacy/Window.cpp`)

`GetControlById` builds a Python wrapper by native type. Types without a case -> `Unknown control type for python`. Wrapped: button, togglebutton, label, spin, fadelabel, textbox, image (incl. bordered), progress, slider, videowindow, list/wraplist/fixedlist/panel, group, radiobutton, edit.

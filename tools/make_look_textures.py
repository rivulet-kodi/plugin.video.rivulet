# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""Draw the textures of Home (HomeWindow.xml).

Needs Pillow; run from the repository root and commit the PNGs. Nothing
here runs on the box.

* ``look_fade_left/bottom/top.png`` - black with a smoothstep alpha ramp,
  drawn colour-true: dark under the hero text toward the left and solid
  under the poster rows at the bottom, a light shade behind the clock.
* ``look_reflect_fade.png`` - over a card's mirror image under the row.
* ``look_ring.png`` - the focus ring, 9-slice with border=24.
* ``look_stars_00..10.png`` - the rating as five stars, in half stars.
* ``look_icon_<search|addons|settings|close>[_focus].png`` - the top-bar icons.
* ``card_shade.png`` - the dark foot of a Continue card under its text.
* ``look_panel.png`` / ``look_check.png`` - the episode row and watched tick.
* ``pill_fill.png`` - the pill plate behind the type chips and buttons,
  9-slice with border=28.

``mask_poster.png`` (400x600) and ``mask_land.png`` (784x440), the
rounded diffuse masks the poster and Continue cards are clipped to, are
not drawn here: they are shipped as they are and only need to be a
white rounded rectangle at twice the card's size.

``python tools/make_look_textures.py --check`` redraws everything in
memory and compares it with the shipped files, pixel for pixel.
"""
import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw

MEDIA = os.path.join('resources', 'skins', 'Default', 'media')
SS = 4
WHITE = (255, 255, 255, 255)


def _rrect(width, height, radius, stroke=0):
    image = Image.new('RGBA', (width * SS, height * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    box = (0, 0, width * SS - 1, height * SS - 1)
    if stroke:
        draw.rounded_rectangle(box, radius * SS, outline=WHITE, width=stroke * SS)
    else:
        draw.rounded_rectangle(box, radius * SS, fill=WHITE)
    return image.resize((width, height), Image.LANCZOS)


def _card_shade(width=16, height=220, clear_until=0.58, peak=210):
    """Clear over the picture, dark along the bottom of an episode card.

    Stretched to the whole card and clipped by the landscape mask, so
    the gradient follows the rounded corners instead of a rectangle.
    """
    image = Image.new('RGBA', (width, height))
    pixels = image.load()
    start = int(round(clear_until * (height - 1)))
    span = (height - 1) - start
    for y in range(height):
        if y <= start:
            alpha = 0
        else:
            t = (y - start) / float(span) if span else 1.0
            t = t * t * (3 - 2 * t)
            alpha = int(round(peak * t))
        color = (255, 255, 255, alpha)
        for x in range(width):
            pixels[x, y] = color
    return image


def _search(draw, scale):
    draw.ellipse((14 * scale, 14 * scale, 40 * scale, 40 * scale), outline=WHITE, width=4 * scale)
    draw.line((36 * scale, 36 * scale, 50 * scale, 50 * scale), fill=WHITE, width=4 * scale)


def _check(draw, scale):
    draw.line((16 * scale, 34 * scale, 28 * scale, 46 * scale), fill=WHITE, width=4 * scale)
    draw.line((28 * scale, 46 * scale, 50 * scale, 18 * scale), fill=WHITE, width=4 * scale)


def _close(draw, scale):
    for (x0, y0), (x1, y1) in (((21, 21), (43, 43)), ((43, 21), (21, 43))):
        draw.line((x0 * scale, y0 * scale, x1 * scale, y1 * scale), fill=WHITE, width=4 * scale)


def _addons(draw, scale):
    for left, top in ((14, 14), (34, 14), (14, 34), (34, 34)):
        draw.rounded_rectangle(
            (left * scale, top * scale, (left + 16) * scale, (top + 16) * scale),
            3 * scale,
            outline=WHITE,
            width=3 * scale,
        )


def _settings(draw, scale):
    """A gear: a ring with eight teeth joined to its rim.

    The earlier drawing was a circle with eight detached radial ticks,
    which is the standard brightness/sun glyph - users read it as a
    display control, not as settings. Teeth have to touch the rim and be
    wide enough to read as teeth: these are trapezoids spanning 20 deg at
    the rim, so the silhouette is a gear at a glance and at 56px on a TV.
    """
    # The toothed outline, one closed polygon alternating between the
    # tooth tip radius and the rim. Stroked, not filled, at the 4px the
    # search lens and the addons squares use, so the three icons sit at
    # the same visual weight.
    r_rim, r_tip, half = 15.0, 23.0, math.radians(11)
    points = []
    for index in range(8):
        mid = math.radians(index * 45)
        gap = math.radians(45) / 2 - half
        for angle, radius in (
            (mid - half - gap, r_rim),
            (mid - half, r_tip),
            (mid + half, r_tip),
            (mid + half + gap, r_rim),
        ):
            points.append(
                ((32 + math.cos(angle) * radius) * scale,
                 (32 + math.sin(angle) * radius) * scale)
            )
    draw.polygon(points, outline=WHITE, width=4 * scale)
    # The bore, concentric and wide enough to stay open at 56px: a small
    # one closes up into a dot and the glyph reads as a flower.
    draw.ellipse((23 * scale, 23 * scale, 41 * scale, 41 * scale), outline=WHITE, width=4 * scale)


def _ramp(points, span):
    """Alpha at each of `span` steps along piecewise smoothstep `points`.

    `points` are ``(position 0..1, alpha 0..255)`` in order.
    """
    values = []
    for index in range(span):
        x = index / (span - 1)
        alpha = points[-1][1]
        for (x0, a0), (x1, a1) in zip(points, points[1:]):
            if x0 <= x <= x1:
                t = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
                t = t * t * (3 - 2 * t)
                alpha = a0 + (a1 - a0) * t
                break
        values.append(int(round(alpha)))
    return values


def _fade(points, span, horizontal, thickness=8):
    """Black with an alpha ramp: the fades are drawn colour-true."""
    size = (span, thickness) if horizontal else (thickness, span)
    image = Image.new('RGBA', size)
    for index, alpha in enumerate(_ramp(points, span)):
        box = (index, 0, index + 1, thickness) if horizontal else (0, index, thickness, index + 1)
        image.paste((0, 0, 0, alpha), box)
    return image


#: Left fade: dark under the title and plot, clear past two thirds of
#: the width, so the picture shows to the right of the text.
LOOK_FADE_LEFT = ((0.0, 235), (0.34, 168), (0.66, 0), (1.0, 0))
#: Bottom fade, top to bottom: clear through the picture, solid from the
#: row band (y=630 of 1080, 0.583) down, so posters never sit on art.
LOOK_FADE_BOTTOM = ((0.0, 0), (0.36, 0), (0.52, 150), (0.583, 255), (1.0, 255))
#: A light shade behind the date and clock at the top edge.
LOOK_FADE_TOP = ((0.0, 140), (1.0, 0))


def _star_points(cx, cy, outer, inner):
    import math
    points = []
    for index in range(10):
        radius = outer if index % 2 == 0 else inner
        angle = math.radians(-90 + index * 36)
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    return points


def _stars(half_stars, size=28, gap=5):
    """Five stars, `half_stars` of ten lit. Unlit is white at 30%."""
    width = size * 5 + gap * 4
    lit = Image.new('RGBA', (width * SS, size * SS), (0, 0, 0, 0))
    dim = Image.new('RGBA', (width * SS, size * SS), (0, 0, 0, 0))
    for index in range(5):
        cx = (index * (size + gap) + size / 2) * SS
        cy = (size / 2 + 1) * SS
        points = _star_points(cx, cy, size / 2 * SS, size / 2 * 0.45 * SS)
        ImageDraw.Draw(lit).polygon(points, fill=WHITE)
        ImageDraw.Draw(dim).polygon(points, fill=(255, 255, 255, 77))
    # Lit up to the half-star boundary, dim after it.
    full, half = divmod(half_stars, 2)
    cut = int(min(width, full * (size + gap) + (size / 2 if half else 0)) * SS)
    out = dim.copy()
    if cut:
        out.paste(lit.crop((0, 0, cut, size * SS)), (0, 0))
    return out.resize((width, size), Image.LANCZOS)


def _look_icon(draw_fn, focused):
    """64px icon button face. Idle: a white glyph (the skin dims it).
    Focused: a white disc with the glyph cut out in near-black."""
    glyph = Image.new('RGBA', (64 * SS, 64 * SS), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(glyph), SS)
    if not focused:
        return glyph.resize((64, 64), Image.LANCZOS)
    face = Image.new('RGBA', (64 * SS, 64 * SS), (0, 0, 0, 0))
    ImageDraw.Draw(face).ellipse((0, 0, 64 * SS - 1, 64 * SS - 1), fill=WHITE)
    ink = Image.new('RGBA', face.size, (7, 9, 11, 255))
    face.paste(ink, (0, 0), glyph.split()[3])
    return face.resize((64, 64), Image.LANCZOS)


#: Over a card's mirror image under the row: part-dark at the card's
#: edge, solid black by the middle, so the reflection fades into the
#: black under the rows. A plain (unflipped) texture over the flipped
#: art, so it does not depend on how Kodi orients a diffuse.
LOOK_REFLECT_FADE = ((0.0, 100), (0.45, 235), (0.65, 255), (1.0, 255))

def _pill(size=72, radius=28):
    """A plain (not supersampled) rounded square, stretched 9-slice."""
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=WHITE)
    return image


def textures():
    """``{file name: PIL image}`` for every texture this script owns."""
    out = {
        'look_fade_left.png': _fade(LOOK_FADE_LEFT, 960, True),
        'look_fade_bottom.png': _fade(LOOK_FADE_BOTTOM, 540, False),
        'look_fade_top.png': _fade(LOOK_FADE_TOP, 64, False),
        # Drawn 9-slice with border=24: radius 20 outside a 12px card
        # corner, 5px of white, so the ring is concentric with the art.
        'look_ring.png': _rrect(64, 64, 20, stroke=5),
        'look_reflect_fade.png': _fade(LOOK_REFLECT_FADE, 256, False),
        'card_shade.png': _card_shade(),
        'pill_fill.png': _pill(),
        'look_panel.png': _rrect(96, 96, 32),
        'look_check.png': _look_icon(_check, False),
    }
    for half in range(11):
        out['look_stars_%02d.png' % half] = _stars(half)
    icons = (('search', _search), ('addons', _addons), ('settings', _settings), ('close', _close))
    for name, fn in icons:
        out['look_icon_%s.png' % name] = _look_icon(fn, False)
        out['look_icon_%s_focus.png' % name] = _look_icon(fn, True)
    return out


def _same(path, image):
    try:
        shipped = Image.open(path).convert('RGBA')
    except OSError:
        return False
    if shipped.size != image.size:
        return False
    return ImageChops.difference(shipped, image.convert('RGBA')).getbbox() is None


def main(argv=None):
    check = '--check' in (argv if argv is not None else sys.argv[1:])
    os.makedirs(MEDIA, exist_ok=True)
    stale = []
    for name, image in sorted(textures().items()):
        path = os.path.join(MEDIA, name)
        if check:
            if not _same(path, image):
                stale.append(name)
            continue
        image.save(path, optimize=True)
        print('wrote', path)
    if stale:
        print('differs from the drawing: %s' % ', '.join(stale))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Port of the ``stremio-watched-bitfield`` wire format.

``LibraryItem.state.watched`` is not a boolean. Official Stremio clients
store a packed per-episode bitmap:

    ``{anchorVideoId}:{anchorLength}:{base64(zlib(bitfield))}``

The anchor id is split on the last two colons because video ids
themselves contain colons (``tt1:1:3:4:eJzjAgAACwAL``). This module
mirrors ``stremio-watched-bitfield`` ``lib/bitfield8.js`` and
``lib/watchedBitfield.js`` (pako deflate/inflate is zlib wrapping;
``btoa``/``atob`` are standard base64). No ``xbmc*`` imports.

Two failure modes must not invent a bitmap:

* A string that does not parse, or whose zlib payload does not inflate,
  is unreadable.
* An anchor video id that is not in the current video list makes the
  JavaScript constructor return a *blank* field. Treating that blank as
  the new value would wipe every watched bit the next time we saved.
  Callers that write (``set_all_watched``) therefore get ``None`` and
  leave the original string untouched. Callers that only paint
  checkmarks (``watched_video_ids``) get ``None`` and show nothing.
"""
import base64
import binascii
import zlib

#: ``i64::MIN``. Stremio's watched-bitfield sort uses this for a video
#: that has no series info, so those rows come before season 0, and
#: season 0 comes before season 1. This is NOT ``binge._sort_key``
#: (that one sorts Specials last, which is correct for "what plays
#: next" and wrong for the bitmap).
_MISSING_SERIES = -9223372036854775808


class BitFieldError(ValueError):
    """The packed bitmap could not be decoded. Callers keep the old string."""


class BitField8:
    """``bitfield8.js``: one bit per video, packed into bytes, zlib on the wire.

    ``set`` masks with ``0xFF``. The JavaScript side stores a
    ``Uint8Array``, so ``&= ~mask`` truncates to 8 bits; a plain Python
    int would grow instead and no longer match ``pako.deflate``.
    """

    def __init__(self, n_size):
        n_size = max(0, int(n_size))
        self.length = n_size
        self.values = bytearray((n_size + 7) // 8)

    @classmethod
    def from_packed(cls, compressed, length):
        try:
            raw = zlib.decompress(compressed)
        except zlib.error:
            raise BitFieldError('inflate failed')
        field = cls(0)
        field.values = bytearray(raw)
        if isinstance(length, int) and not isinstance(length, bool):
            field.length = length
        else:
            field.length = len(field.values) * 8
        n_bytes = (max(0, field.length) + 7) // 8
        if n_bytes > len(field.values):
            field.values.extend(b'\x00' * (n_bytes - len(field.values)))
        return field

    def get(self, index):
        byte_index = index // 8
        if byte_index < 0 or byte_index >= len(self.values) or not self.values[byte_index]:
            return False
        return (self.values[byte_index] & (1 << (index % 8))) != 0

    def set(self, index, value):
        byte_index = index // 8
        if byte_index < 0 or byte_index >= len(self.values):
            return
        mask = 1 << (index % 8)
        current = self.values[byte_index]
        if value:
            current = (current | mask) & 0xFF
        else:
            current = current & (~mask & 0xFF)
        self.values[byte_index] = current

    def last_index_of(self, value):
        for index in range(self.length - 1, -1, -1):
            if self.get(index) is bool(value):
                return index
        return -1

    def to_packed(self):
        return zlib.compress(bytes(self.values))


class WatchedBitfield:
    """``watchedBitfield.js``: a ``BitField8`` aligned to a video-id list."""

    def __init__(self, bitfield, video_ids):
        self.bitfield = bitfield
        self.video_ids = list(video_ids)
        self.anchor_not_found = False

    @classmethod
    def construct_from_array(cls, flags, video_ids):
        bitfield = BitField8(len(video_ids))
        for index, flag in enumerate(flags):
            bitfield.set(index, bool(flag))
        return cls(bitfield, video_ids)

    @classmethod
    def construct_and_resize(cls, serialized, video_ids):
        """Decode ``serialized`` onto ``video_ids``, shifting when the
        anchor is no longer the last watched slot.

        Raises ``BitFieldError`` when the string is not
        ``id:length:payload`` or the payload does not inflate. An anchor
        that is simply absent returns a blank field with
        ``anchor_not_found`` set -- the JavaScript behaviour -- so a
        writer can refuse to persist it.
        """
        if not isinstance(serialized, str):
            raise BitFieldError('invalid components length')
        components = serialized.split(':')
        if len(components) < 3:
            raise BitFieldError('invalid components length')
        serialized_buf = components.pop()
        anchor_length_text = components.pop()
        if not anchor_length_text.isdigit():
            raise BitFieldError('invalid anchor length')
        anchor_length = int(anchor_length_text)
        anchor_video_id = ':'.join(components)
        try:
            anchor_video_idx = list(video_ids).index(anchor_video_id)
        except ValueError:
            anchor_video_idx = -1
        offset = (anchor_length - 1) - anchor_video_idx
        anchor_not_found = anchor_video_idx == -1
        if anchor_not_found or offset != 0:
            resized = cls(BitField8(len(video_ids)), video_ids)
            if anchor_not_found:
                resized.anchor_not_found = True
                return resized
            try:
                payload = base64.b64decode(serialized_buf, validate=False)
            except (binascii.Error, ValueError):
                raise BitFieldError('invalid payload')
            previous = BitField8.from_packed(payload, anchor_length)
            for index in range(len(video_ids)):
                previous_index = index + offset
                if 0 <= previous_index < previous.length:
                    resized.bitfield.set(index, previous.get(previous_index))
            return resized
        try:
            payload = base64.b64decode(serialized_buf, validate=False)
        except (binascii.Error, ValueError):
            raise BitFieldError('invalid payload')
        return cls(BitField8.from_packed(payload, len(video_ids)), video_ids)

    def get_video(self, video_id):
        try:
            index = self.video_ids.index(video_id)
        except ValueError:
            return False
        return self.bitfield.get(index)

    def set_video(self, video_id, value):
        """No-op when ``video_id`` is not in this field's list. Returns
        whether a bit was actually addressed."""
        try:
            index = self.video_ids.index(video_id)
        except ValueError:
            return False
        self.bitfield.set(index, bool(value))
        return True

    def serialize(self):
        """Anchor on the last watched video, not the last id.

        Upcoming episodes can be appended without shifting the bits that
        already mean something. An empty id list cannot be serialized.
        """
        if not self.video_ids:
            raise BitFieldError('empty video ids')
        last_index = max(0, self.bitfield.last_index_of(True))
        packed = base64.b64encode(self.bitfield.to_packed()).decode('ascii')
        return '%s:%d:%s' % (self.video_ids[last_index], last_index + 1, packed)


def video_sort_key(video):
    """``(season, episode, released)`` with missing series info at ``i64::MIN``.

    Season 0 is a real season: it sorts after a video that has no season
    at all, and before season 1. Release date is the same string
    tiebreak Stremio uses (ISO 8601 sorts as text).
    """
    if not isinstance(video, dict):
        return (_MISSING_SERIES, _MISSING_SERIES, '')
    season = video.get('season')
    episode = video.get('episode')
    if season is None and episode is None:
        season_key = _MISSING_SERIES
        episode_key = _MISSING_SERIES
    else:
        season_key = season if isinstance(season, int) and not isinstance(season, bool) else _MISSING_SERIES
        episode_key = episode if isinstance(episode, int) and not isinstance(episode, bool) else _MISSING_SERIES
    return (season_key, episode_key, video.get('released') or '')


def sorted_video_ids(videos):
    """Video ids in watched-bitfield order. Entries with no id are dropped."""
    ordered = sorted(
        (video for video in videos or [] if isinstance(video, dict) and video.get('id')),
        key=video_sort_key,
    )
    return [video['id'] for video in ordered]


def _load(serialized, video_ids):
    return WatchedBitfield.construct_and_resize(serialized, list(video_ids or []))


def watched_video_ids(serialized, video_ids):
    """Set of video ids marked watched, or ``None`` when the string must
    not be trusted (unparseable, or the anchor is not in ``video_ids``).

    ``None`` and an empty set are different: an empty set is a real
    bitmap with nothing watched. Painters treat both as "no checkmark".
    """
    if not serialized or not isinstance(serialized, str):
        return None
    try:
        field = _load(serialized, video_ids)
    except BitFieldError:
        return None
    if field.anchor_not_found:
        return None
    return {video_id for video_id in field.video_ids if field.get_video(video_id)}


def set_all_watched(serialized, video_ids, watched=True):
    """Return the new ``state.watched`` string with every id in
    ``video_ids`` set to ``watched`` -- the title-level "Mark as
    (un)watched" action -- decoding and re-encoding the bitmap a single
    time, or ``None`` to mean "do not replace the caller's string".

    ``None`` covers an unparseable payload, an unknown anchor and an
    empty ``video_ids``. A missing bitmap starts a fresh field.
    """
    video_ids = list(video_ids or [])
    if not video_ids:
        return None
    if not serialized:
        field = WatchedBitfield.construct_from_array([False] * len(video_ids), video_ids)
    else:
        try:
            field = _load(serialized, video_ids)
        except BitFieldError:
            return None
        if field.anchor_not_found:
            return None
    for video_id in video_ids:
        field.set_video(video_id, watched)
    return field.serialize()

"""Tests for lib.stremio.subtitles (pure Python subtitle discovery/sorting).

collect_subtitles() is driven entirely through the injected `client` seam
(duck-types AddonClient.subtitles(base, rtype, sid, extra=None)), so this
suite needs no HTTP fakery from conftest - FakeSubtitleClient below plays
that role and records every call for assertions.
"""
import threading

from lib.stremio.addons import AddonError
from lib.stremio.subtitles import collect_subtitles, filter_subtitles


class FakeSubtitleClient:
    """Duck-types AddonClient.subtitles; canned per-base response or exception."""

    def __init__(self, responses=None):
        self._responses = dict(responses or {})
        self.calls = []

    def subtitles(self, base, rtype, sid, extra=None):
        self.calls.append({"base": base, "rtype": rtype, "sid": sid, "extra": extra})
        result = self._responses.get(base, [])
        if isinstance(result, Exception):
            raise result
        return result


def _manifest(resources, types=None, id_prefixes=None):
    return {
        "id": "org.test.addon",
        "types": types if types is not None else ["movie", "series"],
        "idPrefixes": id_prefixes if id_prefixes is not None else ["tt"],
        "resources": resources,
    }


def _descriptor(transport_url, manifest, flags=None):
    return {"transportUrl": transport_url, "manifest": manifest, "flags": flags or {}}


MANIFEST_URL_A = "https://a.example/manifest.json"
MANIFEST_URL_B = "https://b.example/manifest.json"


# --- collect_subtitles: addon selection ---------------------------------


def test_collect_subtitles_queries_only_subtitle_capable_addons():
    supporting = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    non_supporting = _descriptor(MANIFEST_URL_B, _manifest(["catalog"]))
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [{"id": "1", "lang": "en", "url": "https://a.example/1.srt"}],
    })

    result = collect_subtitles(client, [supporting, non_supporting], "movie", "tt1234567")

    assert len(client.calls) == 1
    assert client.calls[0]["base"] == MANIFEST_URL_A
    assert result == [{"id": "1", "lang": "en", "url": "https://a.example/1.srt"}]


def test_collect_subtitles_passes_rtype_rid_and_extra_through():
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    client = FakeSubtitleClient({MANIFEST_URL_A: []})

    collect_subtitles(client, [addon], "series", "tt7:1:2", extra=[("videoSize", "12345")])

    assert client.calls == [{
        "base": MANIFEST_URL_A,
        "rtype": "series",
        "sid": "tt7:1:2",
        "extra": [("videoSize", "12345")],
    }]


def test_collect_subtitles_skips_addon_missing_transport_url():
    addon = {"manifest": _manifest(["subtitles"])}  # no transportUrl key at all
    client = FakeSubtitleClient()

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == []
    assert client.calls == []


def test_collect_subtitles_no_addons_returns_empty_list():
    assert collect_subtitles(FakeSubtitleClient(), [], "movie", "tt1234567") == []


# --- collect_subtitles: concurrency ---------------------------------------


def test_collect_subtitles_queries_multiple_addons_concurrently():
    """Two addons each block on a shared `threading.Barrier(2)` inside
    their own `subtitles()` call - a barrier only releases once BOTH
    parties have reached it. If `collect_subtitles()` still queried
    addons one at a time (the old serial loop), the first addon would
    wait forever for a second party that can't arrive until the first
    one has already returned: a deadlock. `barrier.wait()`'s own timeout
    turns a regression into a fast, deterministic test failure instead
    of hanging the whole suite.
    """
    barrier = threading.Barrier(2, timeout=5.0)

    class BarrierClient:
        def subtitles(self, base, rtype, sid, extra=None):
            barrier.wait()
            return [{"id": base, "lang": "en", "url": "https://%s/1.srt" % base}]

    addon_a = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    addon_b = _descriptor(MANIFEST_URL_B, _manifest(["subtitles"]))

    result = collect_subtitles(BarrierClient(), [addon_a, addon_b], "movie", "tt1234567")

    assert [s["id"] for s in result] == [MANIFEST_URL_A, MANIFEST_URL_B]


def test_collect_subtitles_preserves_addon_order_regardless_of_answer_order():
    """addon A (listed FIRST) is gated to only return once addon B
    (listed second) has already answered - proven via a shared Event
    only addon B sets - yet the merged result must still list addon A's
    subtitle FIRST: collect_subtitles() merges in `addons` order, not
    completion order. This only works if both addons are genuinely
    in-flight at once; a serial loop would deadlock here since addon B
    would never even start until addon A had already returned.
    """
    b_answered = threading.Event()

    class OrderedClient:
        def subtitles(self, base, rtype, sid, extra=None):
            if base == MANIFEST_URL_A:
                assert b_answered.wait(timeout=5.0), "addon A answered before addon B - not gated"
                return [{"id": "from-a", "lang": "en", "url": "https://a.example/1.srt"}]
            b_answered.set()
            return [{"id": "from-b", "lang": "en", "url": "https://b.example/1.srt"}]

    addon_a = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    addon_b = _descriptor(MANIFEST_URL_B, _manifest(["subtitles"]))

    result = collect_subtitles(OrderedClient(), [addon_a, addon_b], "movie", "tt1234567")

    assert [s["id"] for s in result] == ["from-a", "from-b"]


def test_collect_subtitles_failing_addon_does_not_block_or_lose_a_concurrent_sibling():
    """addon A (listed FIRST) raises only after addon B (listed second)
    has already answered - proven via a shared Event only addon B sets -
    showing addon A's failure neither blocks addon B's still-in-flight
    call nor loses its result from the merge. Like the order test above,
    this deadlocks under a serial loop: addon B never starts until addon
    A returns, but addon A is waiting on addon B.
    """
    b_answered = threading.Event()

    class GatedClient:
        def subtitles(self, base, rtype, sid, extra=None):
            if base == MANIFEST_URL_A:
                assert b_answered.wait(timeout=5.0), "addon A raised before addon B answered - not gated"
                raise AddonError("boom")
            b_answered.set()
            return [{"id": "from-b", "lang": "en", "url": "https://b.example/1.srt"}]

    addon_a = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    addon_b = _descriptor(MANIFEST_URL_B, _manifest(["subtitles"]))

    result = collect_subtitles(GatedClient(), [addon_a, addon_b], "movie", "tt1234567")

    assert result == [{"id": "from-b", "lang": "en", "url": "https://b.example/1.srt"}]


# --- collect_subtitles: fault tolerance ----------------------------------


def test_collect_subtitles_swallows_addon_error_and_returns_others():
    failing = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    ok = _descriptor(MANIFEST_URL_B, _manifest(["subtitles"]))
    client = FakeSubtitleClient({
        MANIFEST_URL_A: AddonError("boom"),
        MANIFEST_URL_B: [{"id": "1", "lang": "en", "url": "https://b.example/1.srt"}],
    })

    result = collect_subtitles(client, [failing, ok], "movie", "tt1234567")

    assert result == [{"id": "1", "lang": "en", "url": "https://b.example/1.srt"}]


def test_collect_subtitles_swallows_non_addon_exceptions_too():
    # Contract: a broad `except Exception` per addon, not narrowly AddonError.
    failing = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    ok = _descriptor(MANIFEST_URL_B, _manifest(["subtitles"]))
    client = FakeSubtitleClient({
        MANIFEST_URL_A: ValueError("malformed json"),
        MANIFEST_URL_B: [{"id": "1", "lang": "en", "url": "https://b.example/1.srt"}],
    })

    result = collect_subtitles(client, [failing, ok], "movie", "tt1234567")

    assert result == [{"id": "1", "lang": "en", "url": "https://b.example/1.srt"}]


# --- collect_subtitles: normalization + dedup ----------------------------


def test_collect_subtitles_dedupes_by_url_first_occurrence_wins():
    dup_url = "https://cdn.example/same.srt"
    addon_a = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    addon_b = _descriptor(MANIFEST_URL_B, _manifest(["subtitles"]))
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [{"id": "from-a", "lang": "en", "url": dup_url}],
        MANIFEST_URL_B: [{"id": "from-b", "lang": "fr", "url": dup_url}],
    })

    result = collect_subtitles(client, [addon_a, addon_b], "movie", "tt1234567")

    assert result == [{"id": "from-a", "lang": "en", "url": dup_url}]


def test_collect_subtitles_dedupes_within_a_single_addons_results():
    url = "https://a.example/1.srt"
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [
            {"id": "first", "lang": "en", "url": url},
            {"id": "second", "lang": "en", "url": url},
        ],
    })

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == [{"id": "first", "lang": "en", "url": url}]


def test_collect_subtitles_drops_entries_without_a_usable_url():
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [
            {"id": "no-url-key", "lang": "en"},
            {"id": "empty-url", "lang": "en", "url": ""},
            {"id": "none-url", "lang": "en", "url": None},
            "not-a-dict",
            {"id": "good", "lang": "en", "url": "https://a.example/good.srt"},
        ],
    })

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == [{"id": "good", "lang": "en", "url": "https://a.example/good.srt"}]


def test_collect_subtitles_defaults_missing_or_falsy_id_to_url():
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    url_missing = "https://a.example/no-id-key.srt"
    url_falsy = "https://a.example/empty-id.srt"
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [
            {"lang": "en", "url": url_missing},
            {"id": "", "lang": "en", "url": url_falsy},
        ],
    })

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == [
        {"id": url_missing, "lang": "en", "url": url_missing},
        {"id": url_falsy, "lang": "en", "url": url_falsy},
    ]


def test_collect_subtitles_defaults_missing_or_falsy_lang_to_empty_string():
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    url_missing = "https://a.example/no-lang-key.srt"
    url_none = "https://a.example/none-lang.srt"
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [
            {"id": "1", "url": url_missing},
            {"id": "2", "lang": None, "url": url_none},
        ],
    })

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == [
        {"id": "1", "lang": "", "url": url_missing},
        {"id": "2", "lang": "", "url": url_none},
    ]


def test_collect_subtitles_coerces_non_string_lang_to_str():
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    url = "https://a.example/int-lang.srt"
    client = FakeSubtitleClient({MANIFEST_URL_A: [{"id": "1", "lang": 42, "url": url}]})

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == [{"id": "1", "lang": "42", "url": url}]


# --- collect_subtitles: label (stremio-core commit b1bd9b3f) -------------


def test_collect_subtitles_carries_and_sanitizes_label():
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    url = "https://a.example/1.srt"
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [
            {"id": "1", "lang": "en", "url": url, "label": "  English\r\n (SDH)\n"},
        ],
    })

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == [
        {"id": "1", "lang": "en", "url": url, "label": "English (SDH)"},
    ]


def test_collect_subtitles_omits_label_when_missing_empty_or_blank():
    addon = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    url_missing = "https://a.example/no-label-key.srt"
    url_empty = "https://a.example/empty-label.srt"
    url_blank = "https://a.example/blank-label.srt"
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [
            {"id": "1", "lang": "en", "url": url_missing},
            {"id": "2", "lang": "en", "url": url_empty, "label": ""},
            {"id": "3", "lang": "en", "url": url_blank, "label": "   \r\n  "},
        ],
    })

    result = collect_subtitles(client, [addon], "movie", "tt1234567")

    assert result == [
        {"id": "1", "lang": "en", "url": url_missing},
        {"id": "2", "lang": "en", "url": url_empty},
        {"id": "3", "lang": "en", "url": url_blank},
    ]
    assert all("label" not in sub for sub in result)


def test_collect_subtitles_dedupes_by_url_keeps_first_label_too():
    dup_url = "https://cdn.example/same.srt"
    addon_a = _descriptor(MANIFEST_URL_A, _manifest(["subtitles"]))
    addon_b = _descriptor(MANIFEST_URL_B, _manifest(["subtitles"]))
    client = FakeSubtitleClient({
        MANIFEST_URL_A: [{"id": "from-a", "lang": "en", "url": dup_url, "label": "Forced"}],
        MANIFEST_URL_B: [{"id": "from-b", "lang": "en", "url": dup_url, "label": "Full"}],
    })

    result = collect_subtitles(client, [addon_a, addon_b], "movie", "tt1234567")

    assert result == [{"id": "from-a", "lang": "en", "url": dup_url, "label": "Forced"}]


# --- filter_subtitles -----------------------------------------------------


def _sub(sub_id, lang):
    return {"id": sub_id, "lang": lang, "url": "https://x.example/%s.srt" % sub_id}


def test_filter_subtitles_keeps_only_preferred_lang_in_original_order():
    subs = [_sub("1", "fr"), _sub("2", "en"), _sub("3", "es"), _sub("4", "en")]

    result = filter_subtitles(subs, "en")

    assert [s["id"] for s in result] == ["2", "4"]


def test_filter_subtitles_label_rides_along_untouched():
    # Same fixture/order as test_filter_subtitles_keeps_only_preferred_lang_in_original_order,
    # but every entry now carries a label -- the kept entries and their
    # labels must simply ride along untouched.
    subs = [
        {"id": "1", "lang": "fr", "url": "https://x.example/1.srt", "label": "Francais"},
        {"id": "2", "lang": "en", "url": "https://x.example/2.srt", "label": "English"},
        {"id": "3", "lang": "es", "url": "https://x.example/3.srt", "label": "Espanol"},
        {"id": "4", "lang": "en", "url": "https://x.example/4.srt", "label": "English (SDH)"},
    ]

    result = filter_subtitles(subs, "en")

    assert [s["label"] for s in result] == ["English", "English (SDH)"]


def test_filter_subtitles_en_matches_two_and_three_letter_codes_case_insensitively():
    subs = [_sub("1", "fr"), _sub("2", "ENG"), _sub("3", "En"), _sub("4", "eng")]

    result = filter_subtitles(subs, "en")

    assert [s["id"] for s in result] == ["2", "3", "4"]


def test_filter_subtitles_three_letter_preferred_matches_two_letter_entries():
    subs = [_sub("1", "fr"), _sub("2", "en")]

    result = filter_subtitles(subs, "eng")

    assert [s["id"] for s in result] == ["2"]


def test_filter_subtitles_preferred_lang_itself_case_insensitive():
    subs = [_sub("1", "fr"), _sub("2", "en")]

    result = filter_subtitles(subs, "EN")

    assert [s["id"] for s in result] == ["2"]


def test_filter_subtitles_no_match_returns_empty_list():
    subs = [_sub("1", "fr"), _sub("2", "es"), _sub("3", "de")]

    assert filter_subtitles(subs, "en") == []


def test_filter_subtitles_unknown_code_matches_itself_literally():
    subs = [_sub("1", "xx"), _sub("2", "en")]

    result = filter_subtitles(subs, "xx")

    assert [s["id"] for s in result] == ["1"]


def test_filter_subtitles_none_preferred_lang_returns_every_entry_in_order():
    subs = [_sub("1", "fr"), _sub("2", "en")]

    assert [s["id"] for s in filter_subtitles(subs, None)] == ["1", "2"]


def test_filter_subtitles_empty_string_preferred_lang_returns_every_entry_in_order():
    subs = [_sub("1", "fr"), _sub("2", "en")]

    assert [s["id"] for s in filter_subtitles(subs, "")] == ["1", "2"]


def test_filter_subtitles_empty_list_returns_empty_list():
    assert filter_subtitles([], "en") == []


def test_filter_subtitles_none_list_returns_empty_list():
    assert filter_subtitles(None, "en") == []

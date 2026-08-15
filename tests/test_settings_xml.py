"""Stdlib-only structural validation for `resources/settings.xml`.

Kodi renders this file itself - no Rivulet code runs when the add-on's
settings dialog is open - so nothing else in this suite can catch a
settings layout that is valid XML but unusable with a remote. These
tests pin the layout rules that a television/remote UI depends on.

The rule that matters most here (see
`test_no_category_opens_on_an_edit_control_with_nothing_to_move_to`) is
about Kodi's `edit` control: Kodi auto-focuses a category's FIRST
setting when the dialog opens, and an `edit` control that receives focus
enters text-input mode, where the arrow keys type into the field instead
of moving between settings. That is survivable when there are sibling
settings to arrow to, but a category whose only setting is an `edit`
control is a dead end - the user lands in the text field with nowhere to
go and no way out except Back.

Confirmed against Kodi 21.3 on a real device: `server_url` used to be
the single setting of its own "General" category, and opening Rivulet's
settings dropped the user straight into an inescapable Server URL field.
"""
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_XML = REPO_ROOT / "resources" / "settings.xml"


def _categories():
    """[(category_id, [setting_element, ...]), ...] in document order."""
    root = ET.parse(SETTINGS_XML).getroot()
    return [(cat.get("id"), list(cat.iter("setting"))) for cat in root.iter("category")]


def _control_type(setting):
    control = setting.find("control")
    return control.get("type") if control is not None else None


def test_settings_xml_is_well_formed_and_has_categories():
    categories = _categories()

    assert categories, "settings.xml defines no categories"
    assert all(cat_id for cat_id, _ in categories), "every category needs an id"


@pytest.mark.parametrize("category_id,settings", _categories(), ids=[c for c, _ in _categories()])
def test_no_category_opens_on_an_edit_control_with_nothing_to_move_to(category_id, settings):
    """A category whose FIRST setting is an `edit` control must give the
    user somewhere to arrow to, or focus is trapped in the text field
    (see the module docstring). Prefer putting a toggle/list/button
    first; a single-setting `edit` category is always a trap."""
    assert settings, "category %r has no settings" % category_id

    if _control_type(settings[0]) != "edit":
        return

    assert len(settings) > 1, (
        "category %r auto-focuses an `edit` control (%s) and has no other setting to "
        "arrow to: the user lands in the text field with no way out but Back. Put a "
        "non-edit setting first, or merge this category into another."
        % (category_id, settings[0].get("id"))
    )


def test_server_url_is_reachable_without_entering_a_text_field_first():
    """Regression for the reported bug: opening Rivulet's settings must
    not drop the user directly into the Server URL field."""
    by_category = {cat_id: s for cat_id, s in _categories()}
    owning = [cat for cat, settings in by_category.items()
              if any(s.get("id") == "server_url" for s in settings)]

    assert len(owning) == 1, "server_url should live in exactly one category, found %r" % owning
    settings = by_category[owning[0]]
    first = settings[0]

    assert first.get("id") != "server_url", (
        "server_url is the first setting of category %r, so Kodi auto-focuses it and "
        "enters text-input mode as soon as the settings dialog opens" % owning[0]
    )
    assert _control_type(first) != "edit"

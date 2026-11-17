"""Переименования улиц из текста описания: «Seaside Blvd» = «Father Capodanno Blvd»."""

from __future__ import annotations

from types import SimpleNamespace

import acad_builder
import street_names


def test_formerly_pattern_gives_current_and_old_name():
    text = "181 Father Capodanno Blvd (formerly known as Seaside Blvd.)"
    assert ("Father Capodanno Blvd", "Seaside Blvd.") in street_names.street_aliases(text)


def test_now_pattern_gives_current_and_old_name():
    text = "Seaside Boulevard (now Father Capodanno Blvd.), distant 75 feet"
    assert ("Father Capodanno Blvd.", "Seaside Boulevard") in street_names.street_aliases(text)


def test_plain_text_without_renaming_gives_no_aliases():
    text = "BEGINNING at a point on the northerly side of Doty Avenue, distant 75 feet"
    assert street_names.street_aliases(text) == []


def _plan(main, cross, dist=0.0, aliases=(), former=None):
    desc = SimpleNamespace(
        main_street=main,
        cross_street=cross,
        corner_to_pob_bearing=None,
        distance_to_corner_ft=dist,
        street_aliases=list(aliases),
        main_street_former=former,
    )
    return SimpleNamespace(description=desc)


def test_renamed_street_is_same_corner_when_alias_is_known():
    ours = _plan("Doty Avenue", "Seaside Boulevard", 74.96)
    neighbour = _plan(
        "Father Capodanno Blvd", "Doty Avenue", 25.0,
        aliases=["Father Capodanno Blvd = Seaside Boulevard"],
    )
    note, warn = acad_builder._check_street_reference_and_frontage(ours, neighbour)
    assert not warn
    assert "угловое соседство" in note


def test_different_corner_still_warns():
    ours = _plan("Doty Avenue", "Seaside Boulevard", 74.96)
    other = _plan("Burgher Avenue", "Doty Avenue", 25.0)
    _note, warn = acad_builder._check_street_reference_and_frontage(ours, other)
    assert warn

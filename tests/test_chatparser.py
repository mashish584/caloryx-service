"""Meal Assistant text pipeline - PRD §7, §7.5, §12.6. Pure, no DB (mirrors
tests/test_nutrition.py)."""
from __future__ import annotations

import pytest

from chatparser import (
    band_for_score,
    hash_normalized,
    is_non_food_greeting,
    normalize_text,
    parse_edit_command,
    parse_new_item_phrases,
    score_food_match,
)

# -- normalize ----------------------------------------------------------


def test_normalize_text_lowercases_and_collapses_whitespace():
    assert normalize_text("  I ate  200G   Rice ") == "i ate 200g rice"


def test_normalize_text_strips_trailing_punctuation():
    assert normalize_text("200g rice!!") == "200g rice"
    assert normalize_text("what did I eat?") == "what did i eat"


def test_hash_normalized_is_stable_and_content_sensitive():
    a = hash_normalized(normalize_text("200g rice"))
    b = hash_normalized(normalize_text(" 200G Rice "))
    c = hash_normalized(normalize_text("200g chicken"))
    assert a == b
    assert a != c


# -- T-1 pre-classifier ---------------------------------------------------


@pytest.mark.parametrize(
    "text", ["hi", "hello", "hey", "thanks", "thank you", "good morning", "ok", "how are you"]
)
def test_greetings_are_caught(text):
    assert is_non_food_greeting(normalize_text(text)) is True


@pytest.mark.parametrize("text", ["200g rice", "chicken was actually 150g", "remove the dressing"])
def test_food_shaped_text_is_not_caught(text):
    assert is_non_food_greeting(normalize_text(text)) is False


# -- T1 new-item grammar ---------------------------------------------------


def test_parses_a_single_item_with_digit_quantity_no_space():
    phrases, unconsumed = parse_new_item_phrases(normalize_text("200g cooked rice"))
    assert unconsumed == []
    assert len(phrases) == 1
    phrase = phrases[0]
    assert (phrase.quantity, phrase.unit, phrase.state, phrase.prep, phrase.food_text) == (
        200.0,
        "g",
        "COOKED",
        None,
        "rice",
    )


def test_parses_a_word_number_quantity():
    phrases, _ = parse_new_item_phrases(normalize_text("a piece boiled egg"))
    assert len(phrases) == 1
    phrase = phrases[0]
    assert (phrase.quantity, phrase.unit, phrase.state, phrase.prep, phrase.food_text) == (
        1.0,
        "piece",
        "COOKED",
        "boiled",
        "egg",
    )


def test_prep_word_sets_both_state_and_prep():
    phrases, _ = parse_new_item_phrases(normalize_text("120g grilled chicken breast"))
    phrase = phrases[0]
    assert phrase.state == "COOKED"
    assert phrase.prep == "grilled"
    assert phrase.food_text == "chicken breast"


def test_raw_is_a_state_word_with_no_prep():
    phrases, _ = parse_new_item_phrases(normalize_text("100g raw chicken"))
    phrase = phrases[0]
    assert phrase.state == "RAW"
    assert phrase.prep is None


def test_of_filler_is_stripped():
    phrases, _ = parse_new_item_phrases(normalize_text("1 katori of cooked dal"))
    phrase = phrases[0]
    assert phrase.unit == "katori"
    assert phrase.state == "COOKED"
    assert phrase.food_text == "dal"


@pytest.mark.parametrize("connector", [",", "and", "with", "plus", "&"])
def test_multi_item_messages_split_on_every_connector(connector):
    text = normalize_text("200g rice {} 2 pieces roti".format(connector))
    phrases, unconsumed = parse_new_item_phrases(text)
    assert unconsumed == []
    assert [p.food_text for p in phrases] == ["rice", "roti"]


def test_an_unparseable_segment_is_reported_not_dropped():
    phrases, unconsumed = parse_new_item_phrases(normalize_text("200g rice and something weird"))
    assert [p.food_text for p in phrases] == ["rice"]
    assert unconsumed == ["something weird"]


def test_a_bare_countable_mention_with_no_separate_unit_is_unconsumed():
    """"2 rotis" has no explicit unit distinct from the food name - Chunk 2b
    requires an explicit unit (no default-serving inference, that's Chunk 4),
    so this must NOT silently resolve to some guessed unit."""
    phrases, unconsumed = parse_new_item_phrases(normalize_text("2 rotis"))
    assert phrases == []
    assert unconsumed == ["2 rotis"]


def test_an_unrecognised_unit_word_leaves_the_whole_segment_unconsumed():
    phrases, unconsumed = parse_new_item_phrases(normalize_text("200 smidges rice"))
    assert phrases == []
    assert unconsumed == ["200 smidges rice"]


def test_quantity_and_unit_with_no_food_left_does_not_match():
    phrases, unconsumed = parse_new_item_phrases(normalize_text("200g cooked"))
    assert phrases == []
    assert unconsumed == ["200g cooked"]


# -- T1 edit grammar (§7.5) ------------------------------------------------


def test_was_actually_pattern_produces_edit_item():
    edit = parse_edit_command(normalize_text("chicken was actually 150g"))
    assert edit.intent == "EDIT_ITEM"
    assert edit.target_text == "chicken"
    assert (edit.quantity, edit.unit) == (150.0, "g")


def test_make_the_pattern_produces_edit_item():
    edit = parse_edit_command(normalize_text("make the rice 100g"))
    assert edit.intent == "EDIT_ITEM"
    assert edit.target_text == "rice"
    assert (edit.quantity, edit.unit) == (100.0, "g")


def test_remove_and_delete_both_produce_remove_item():
    assert parse_edit_command(normalize_text("remove the dressing")).intent == "REMOVE_ITEM"
    assert parse_edit_command(normalize_text("delete the dressing")).target_text == "dressing"


def test_add_pattern_produces_add_item_with_a_parsed_phrase():
    edit = parse_edit_command(normalize_text("add 1 piece boiled egg"))
    assert edit.intent == "ADD_ITEM"
    assert edit.item.food_text == "egg"
    assert edit.item.quantity == 1.0


def test_add_pattern_with_no_recognisable_unit_does_not_match():
    """§4's decision: no unit means no ladder-guessed default in Chunk 2b."""
    assert parse_edit_command(normalize_text("add a boiled egg")) is None


def test_set_slot_pattern():
    edit = parse_edit_command(normalize_text("this was breakfast"))
    assert edit.intent == "SET_SLOT"
    assert edit.slot == "BREAKFAST"


def test_non_matching_text_returns_none():
    assert parse_edit_command(normalize_text("what a lovely day")) is None


# -- confidence banding (§12.6) --------------------------------------------


def test_identical_strings_score_high():
    score = score_food_match("Grilled Chicken Breast", "Grilled Chicken Breast")
    assert band_for_score(score) == "HIGH"


def test_a_close_variant_scores_medium_or_high():
    score = score_food_match("chicken breast", "Grilled Chicken Breast")
    assert band_for_score(score) in {"MEDIUM", "HIGH"}


def test_unrelated_strings_score_low():
    score = score_food_match("banana", "Grilled Chicken Breast")
    assert band_for_score(score) == "LOW"

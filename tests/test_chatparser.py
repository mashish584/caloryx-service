"""Meal Assistant text pipeline - PRD §7, §7.5, §12.6. Pure, no DB (mirrors
tests/test_nutrition.py)."""
from __future__ import annotations

import pytest

from chatparser import (
    band_for_score,
    classify_t1_intent,
    extract_nutrition_qa_food,
    has_wellbeing_signal,
    hash_normalized,
    is_diary_query_a_trend_question,
    is_non_food_greeting,
    match_tie_breaks,
    normalize_text,
    parse_edit_command,
    parse_food_mentions,
    parse_new_item_phrases,
    redact_pii,
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


# -- redact_pii (§12.14, Chunk 8c) ---------------------------------------


def test_redact_pii_replaces_an_email_address():
    assert redact_pii("reach me at john.doe@example.com please") == (
        "reach me at [redacted-email] please"
    )


def test_redact_pii_replaces_a_dashed_phone_number():
    assert redact_pii("call me at 987-654-3210 tomorrow") == "call me at [redacted-phone] tomorrow"


def test_redact_pii_replaces_a_plus_prefixed_spaced_phone_number():
    assert redact_pii("my number is +91 98765 43210") == "my number is [redacted-phone]"


def test_redact_pii_leaves_ordinary_food_quantities_untouched():
    text = "200g rice, 1.5 cups milk, 2 eggs, 100g grilled chicken breast"
    assert redact_pii(text) == text


def test_redact_pii_only_touches_the_pii_portion_of_a_mixed_message():
    text = "log 200g rice, and email me the plan at a@b.com"
    assert redact_pii(text) == "log 200g rice, and email me the plan at [redacted-email]"


# -- T-1 pre-classifier ---------------------------------------------------


@pytest.mark.parametrize(
    "text", ["hi", "hello", "hey", "thanks", "thank you", "good morning", "ok", "how are you"]
)
def test_greetings_are_caught(text):
    assert is_non_food_greeting(normalize_text(text)) is True


@pytest.mark.parametrize("text", ["200g rice", "chicken was actually 150g", "remove the dressing"])
def test_food_shaped_text_is_not_caught(text):
    assert is_non_food_greeting(normalize_text(text)) is False


# -- classify_t1_intent (Chunk 6a, §5.5) --------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hi", "SOCIAL"),
        ("thanks", "SOCIAL"),
        ("how do i change my calorie goal", "APP_HELP"),
        ("how does the streak work", "APP_HELP"),
        ("how many calories do i have left", "DIARY_QUERY"),
        ("what did i eat today", "DIARY_QUERY"),
        ("how much protein is in an egg", "NUTRITION_QA"),
        ("how many calories in chicken breast", "NUTRITION_QA"),
        ("should i try keto", "ADVICE_SEEKING"),
        ("is 1200 kcal enough for me", "ADVICE_SEEKING"),
        ("do i have diabetes risk from this", "ADVICE_SEEKING"),
        ("yes", "UNCLEAR"),
        ("the usual", "UNCLEAR"),
    ],
)
def test_classify_t1_intent_matches_the_expected_intent(text, expected):
    assert classify_t1_intent(normalize_text(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "200g rice",
        "chicken was actually 150g",
        "grilled chicken salad with a tahini dressing",
        "asdfgh",  # gibberish is deliberately not detected - falls through
    ],
)
def test_classify_t1_intent_returns_none_for_unrecognized_text(text):
    assert classify_t1_intent(normalize_text(text)) is None


def test_classify_t1_intent_never_misclassifies_a_real_food_mention():
    # Real food words must never accidentally trip APP_HELP/UNCLEAR - the
    # word-boundary matching (not substring) is what prevents "app" matching
    # inside "happy" or "carb" inside "carbonated".
    assert classify_t1_intent(normalize_text("is soda carbonated")) is None
    assert classify_t1_intent(normalize_text("how do i make a happy meal")) is None


def test_nutrition_qa_judgment_framing_is_not_nutrition_qa():
    assert classify_t1_intent(normalize_text("is quinoa healthy")) is None
    assert classify_t1_intent(normalize_text("what should i eat for dinner")) == "ADVICE_SEEKING"


def test_nutrition_qa_trigger_without_extractable_food_is_still_nutrition_qa():
    # "how many calories in" with nothing sensible after it still trips the
    # loose trigger - extraction (answered separately) is allowed to fail.
    intent = classify_t1_intent(normalize_text("how many calories are in this"))
    assert intent == "NUTRITION_QA"


def test_extract_nutrition_qa_food_pulls_out_the_food_name():
    assert extract_nutrition_qa_food(normalize_text("how much protein is in an egg")) == "egg"
    assert (
        extract_nutrition_qa_food(normalize_text("how much protein is in chicken breast"))
        == "chicken breast"
    )


def test_extract_nutrition_qa_food_returns_none_when_it_cant_parse_a_food():
    assert extract_nutrition_qa_food(normalize_text("how many calories should i eat")) is None


def test_diary_query_distinguishes_trend_questions_from_todays_data():
    today = normalize_text("how many calories do i have left")
    trend = normalize_text("how many calories did i eat this week")
    assert classify_t1_intent(today) == "DIARY_QUERY"
    assert is_diary_query_a_trend_question(today) is False
    assert classify_t1_intent(trend) == "DIARY_QUERY"
    assert is_diary_query_a_trend_question(trend) is True


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
    """"2 rotis" has no explicit unit distinct from the food name, so the
    quantified grammar must NOT silently resolve it to some guessed unit -
    it belongs to `parse_food_mentions`, which is explicit about assuming
    the mass (see the second-pass tests below)."""
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


# -- T1 new-item grammar: food-first phrasing ------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # The exact shape that used to fall through to an AI-fallback reply.
        ("noodles 1 bowl", (1.0, "bowl", None, None, "noodles")),
        ("rice 200g", (200.0, "g", None, None, "rice")),
        ("rice 200 g", (200.0, "g", None, None, "rice")),
        ("chicken biryani 600g", (600.0, "g", None, None, "chicken biryani")),
        ("dal 1 katori", (1.0, "katori", None, None, "dal")),
        ("green tea a cup", (1.0, "cup", None, None, "green tea")),
    ],
)
def test_parses_a_quantity_stated_after_the_food(text, expected):
    phrases, unconsumed = parse_new_item_phrases(normalize_text(text))
    assert unconsumed == []
    assert len(phrases) == 1
    phrase = phrases[0]
    assert (phrase.quantity, phrase.unit, phrase.state, phrase.prep, phrase.food_text) == expected


def test_postfix_phrasing_still_reads_state_and_prep():
    phrases, _ = parse_new_item_phrases(normalize_text("boiled egg 2 pieces"))
    phrase = phrases[0]
    assert (phrase.quantity, phrase.unit) == (2.0, "piece")
    assert (phrase.state, phrase.prep, phrase.food_text) == ("COOKED", "boiled", "egg")


def test_postfix_phrasing_does_not_mangle_a_multi_word_food_name():
    """`unit` is anchored to the end and holds no space, so the only split the
    engine can find is the last two tokens - "masala" can never be read as a
    quantity."""
    phrases, _ = parse_new_item_phrases(normalize_text("chicken tikka masala one bowl"))
    assert [p.food_text for p in phrases] == ["chicken tikka masala"]
    assert phrases[0].quantity == 1.0


def test_both_quantity_orders_can_appear_in_one_message():
    phrases, unconsumed = parse_new_item_phrases(normalize_text("200g rice and noodles 1 bowl"))
    assert unconsumed == []
    assert [(p.food_text, p.quantity, p.unit) for p in phrases] == [
        ("rice", 200.0, "g"),
        ("noodles", 1.0, "bowl"),
    ]


def test_postfix_phrasing_still_requires_a_known_unit_word():
    phrases, unconsumed = parse_new_item_phrases(normalize_text("rice 1 smidge"))
    assert phrases == []
    assert unconsumed == ["rice 1 smidge"]


def test_a_trailing_number_with_no_unit_is_not_a_postfix_match():
    phrases, unconsumed = parse_new_item_phrases(normalize_text("rice bowl 1"))
    assert phrases == []
    assert unconsumed == ["rice bowl 1"]


# -- T1 second pass: parse_food_mentions -----------------------------------


def _mentions(text, **kw):
    """The second pass as the pipeline runs it: over whatever the quantified
    grammar left behind."""
    _phrases, unconsumed = parse_new_item_phrases(normalize_text(text))
    return parse_food_mentions(unconsumed, **kw)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2 rotis", (2.0, None, None, "rotis")),
        ("3 eggs", (3.0, None, None, "eggs")),
        ("two boiled eggs", (2.0, "COOKED", "boiled", "eggs")),
        ("an apple", (1.0, None, None, "apple")),
        ("24 almonds", (24.0, None, None, "almonds")),
    ],
)
def test_count_only_mentions_are_read_by_default(text, expected):
    mentions, unconsumed = _mentions(text)
    assert unconsumed == []
    assert len(mentions) == 1
    mention = mentions[0]
    assert (mention.count, mention.state, mention.prep, mention.food_text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "500 ml milk",  # an unrecognised *unit*, not a count of 500
        "200 smidges rice",
        "a really long bit of prose here",  # prose, not a food name
    ],
)
def test_an_implausible_count_or_an_over_long_food_name_stays_unconsumed(text):
    mentions, unconsumed = _mentions(text)
    assert mentions == []
    assert unconsumed == [normalize_text(text)]


def test_a_bare_food_name_is_not_read_unless_the_caller_opts_in():
    assert _mentions("noodles") == ([], ["noodles"])

    mentions, unconsumed = _mentions("noodles", allow_bare_food=True)
    assert unconsumed == []
    assert (mentions[0].count, mentions[0].food_text) == (None, "noodles")


def test_a_bare_food_name_still_reads_state_and_prep():
    mentions, _ = _mentions("boiled egg", allow_bare_food=True)
    assert (mentions[0].count, mentions[0].state, mentions[0].prep, mentions[0].food_text) == (
        None,
        "COOKED",
        "boiled",
        "egg",
    )


def test_with_both_grammars_off_every_segment_comes_straight_back():
    """The pre-second-pass contract, still reachable by configuration."""
    for text in ("2 rotis", "noodles", "something weird"):
        mentions, unconsumed = _mentions(text, allow_count_only=False, allow_bare_food=False)
        assert mentions == []
        assert unconsumed == [text]


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


def test_reordered_whole_words_score_high():
    # Partial ratio alone gives 0.5 (LOW) - USDA-style names invert word order.
    assert band_for_score(score_food_match("greek yogurt", "Yogurt, Greek, plain, nonfat")) == "HIGH"
    assert band_for_score(score_food_match("bananas", "Banana, raw")) == "HIGH"
    # ...but an exact substring still outscores a reordered match.
    assert score_food_match("white rice", "White rice flour") > score_food_match("white rice", "Rice, white")


def test_word_coverage_needs_every_query_word():
    assert band_for_score(score_food_match("greek lamb", "Yogurt, Greek, plain")) != "HIGH"


@pytest.mark.parametrize(
    "query,name,expected",
    [
        ("egg", "Egg, whole, raw, fresh", (2, True, 0, False)),
        ("egg", "Bread, egg", (0, True, 1, False)),
        ("banana", "Bananas, raw", (2, True, 0, False)),
        ("banana", "Pepper, banana, raw", (0, True, 1, False)),
        ("rice", "Cooked White Rice", (2, True, 0, False)),
        ("rice", "Rice crackers", (0, True, 1, False)),
        ("rice", "Licorice", (0, False, 1, False)),
        ("rice", "Rice, fried, NFS", (2, True, 1, True)),  # fried rice is its own food
        ("rice", "Rice, cooked, NFS", (2, True, 0, True)),
        ("brown rice", "Rice, brown, cooked", (1, True, 0, False)),
        ("brown rice", "Snacks, brown rice chips", (0, True, 2, False)),
        ("white rice", "Rice, brown, cooked", (2, False, 1, False)),
        ("egg", "Egg, yolk, dried", (2, True, 2, False)),
        # FNDDS qualifiers don't count as extra words, but never strip a head.
        ("chicken", "Chicken, NS as to part and cooking method, skin not eaten", (2, True, 0, True)),
        ("chicken", "Chicken skin", (0, True, 0, False)),
    ],
)
def test_match_tie_breaks(query, name, expected):
    assert match_tie_breaks(query, name) == expected


# -- has_wellbeing_signal (§5.6, Chunk 6b) ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "i don't deserve to eat today",
        "i hate my body so much",
        "i've been skipping meals all week",
        "i haven't eaten in three days",
        "i keep punishing myself for eating",
        "i feel so guilty about eating that",
    ],
)
def test_wellbeing_signal_triggers_are_caught(text):
    assert has_wellbeing_signal(normalize_text(text)) is True


@pytest.mark.parametrize(
    "text",
    [
        "200g rice",
        "grilled chicken salad with a tahini dressing",
        "how do i change my calorie goal",
        "how many calories do i have left",
        "thanks",
    ],
)
def test_ordinary_messages_do_not_trigger_the_wellbeing_signal(text):
    assert has_wellbeing_signal(normalize_text(text)) is False

"""Hybrid food & composite resolution (PRD §7.2, §7.6, §12.6, Chunk 9b).

The semantic arm is gated by `SEMANTIC_RESOLUTION_ENABLED`, so the first
thing these tests pin down is that the **off** state reaches no new code at
all - that is what makes this chunk deployable before the catalog has ever
been embedded.

Embeddings are mocked throughout. Nothing here can (or claims to) verify that
a real embedding model puts any particular pair of foods above or below a
floor - see documents/known-issues.md. What is verified is the *policy* built
on top of whatever similarity comes back: which band it may produce, when it
is allowed to displace a lexical match, and what happens when the provider is
down.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import override_settings

from assistant import repository, services
from llm import LLMCallError, LLMConfigurationError
from meals import repository as meals_repository

from .test_assistant_services import make_composite, make_food


SEMANTIC_ON = dict(SEMANTIC_RESOLUTION_ENABLED=True, SEMANTIC_MATCH_FLOOR=0.75)


@pytest.fixture(autouse=True)
def _closed_breaker(monkeypatch):
    """The breaker is a DB read; default it closed and record calls so tests
    that care about the failure path can assert on it."""
    monkeypatch.setattr(repository, "circuit_breaker_is_open", lambda cooldown: False)
    monkeypatch.setattr(repository, "record_llm_call_success", lambda: None)
    monkeypatch.setattr(repository, "record_llm_call_failure", lambda threshold: None)


def _stub_lexical(monkeypatch, foods):
    monkeypatch.setattr(meals_repository, "search_foods", lambda query, **kw: foods)


def _stub_semantic(monkeypatch, pairs, calls=None):
    recorded = calls if calls is not None else []

    def search(vector, **kw):
        recorded.append((vector, kw))
        return pairs

    monkeypatch.setattr(meals_repository, "search_foods_by_embedding", search)
    return recorded


def _stub_embed(monkeypatch, vector=None, exc=None, calls=None):
    recorded = calls if calls is not None else []

    def embed_text(text):
        recorded.append(text)
        if exc is not None:
            raise exc
        return vector if vector is not None else [0.1, 0.2]

    monkeypatch.setattr(services, "embed_text", embed_text)
    return recorded


# -- the off state -----------------------------------------------------------


@override_settings(SEMANTIC_RESOLUTION_ENABLED=False)
def test_off_state_never_embeds_and_never_queries_vectors(monkeypatch):
    _stub_lexical(monkeypatch, [make_food(id="f1", name="Something Else")])
    embed_calls = _stub_embed(monkeypatch)
    semantic_calls = _stub_semantic(monkeypatch, [])

    food, score, band = services._resolve_food_by_name("kadhi pakora")

    assert embed_calls == []
    assert semantic_calls == []
    # And the lexical answer is returned exactly as it was before Chunk 9b.
    assert (food.id, band) == ("f1", "LOW")


@override_settings(SEMANTIC_RESOLUTION_ENABLED=False)
def test_off_state_with_no_candidates_is_unchanged(monkeypatch):
    _stub_lexical(monkeypatch, [])
    embed_calls = _stub_embed(monkeypatch)

    assert services._resolve_food_by_name("kadhi pakora") == (None, 0.0, "LOW")
    assert embed_calls == []


# -- the semantic arm only ever rescues a LOW --------------------------------


@override_settings(**SEMANTIC_ON)
def test_a_high_lexical_match_costs_no_embedding_call(monkeypatch):
    # The common case. A HIGH band is already §12.6's "auto-resolve silently",
    # so spending a provider round trip on it would be pure latency and cost.
    _stub_lexical(monkeypatch, [make_food(id="f1", name="Hummus, plain")])
    embed_calls = _stub_embed(monkeypatch)

    food, score, band = services._resolve_food_by_name("hummus")

    assert band == "HIGH"
    assert food.id == "f1"
    assert embed_calls == []


@override_settings(**SEMANTIC_ON)
def test_a_lexical_medium_is_never_displaced_by_a_semantic_one(monkeypatch):
    # Both arms land on MEDIUM; the lexical one wins. The semantic arm can
    # rescue a LOW, never second-guess a match trigram already made.
    _stub_lexical(monkeypatch, [make_food(id="lex", name="Chicken, breast")])
    _stub_embed(monkeypatch)
    _stub_semantic(monkeypatch, [(make_food(id="sem", name="Something Semantic"), 0.99)])

    food, _score, band = services._resolve_food_by_name("chiken")

    assert band == "MEDIUM"
    assert food.id == "lex"


@override_settings(**SEMANTIC_ON)
def test_a_semantic_hit_rescues_a_lexical_low(monkeypatch):
    _stub_lexical(monkeypatch, [make_food(id="lex", name="Totally Unrelated")])
    _stub_embed(monkeypatch)
    _stub_semantic(monkeypatch, [(make_food(id="sem", name="Kadhi Pakora"), 0.88)])

    food, score, band = services._resolve_food_by_name("kadhi pakoda")

    assert food.id == "sem"
    assert band == "MEDIUM"
    # The returned score is the one that produced the band.
    assert score == pytest.approx(0.88)


@override_settings(**SEMANTIC_ON)
def test_a_semantic_hit_resolves_when_trigram_found_nothing_at_all(monkeypatch):
    _stub_lexical(monkeypatch, [])
    _stub_embed(monkeypatch)
    _stub_semantic(monkeypatch, [(make_food(id="sem", name="Kadhi Pakora"), 0.9)])

    food, _score, band = services._resolve_food_by_name("kadhi pakoda")

    assert (food.id, band) == ("sem", "MEDIUM")


@override_settings(**SEMANTIC_ON)
def test_a_similarity_below_the_floor_leaves_the_item_unresolved(monkeypatch):
    # §7.4: "a wrong semantic hit is worse than a cache miss" - below the
    # floor this must stay LOW, which is an unresolved row plus a miss-queue
    # entry, not a silent bad match.
    _stub_lexical(monkeypatch, [make_food(id="lex", name="Totally Unrelated")])
    _stub_embed(monkeypatch)
    _stub_semantic(monkeypatch, [(make_food(id="sem", name="Kadhi Pakora"), 0.74)])

    food, _score, band = services._resolve_food_by_name("kadhi pakoda")

    assert (food.id, band) == ("lex", "LOW")


@override_settings(**SEMANTIC_ON)
@pytest.mark.parametrize("similarity", [0.76, 0.9, 0.999, 1.0])
def test_a_semantic_match_never_reaches_high_however_similar(monkeypatch, similarity):
    # The ceiling is the design, not a tuning artifact: a match found by
    # meaning rather than by name is exactly the one §12.6 wants shown as
    # confirmable rather than auto-resolved silently.
    _stub_lexical(monkeypatch, [])
    _stub_embed(monkeypatch)
    _stub_semantic(monkeypatch, [(make_food(id="sem", name="Kadhi Pakora"), similarity)])

    _food, _score, band = services._resolve_food_by_name("kadhi pakoda")

    assert band == "MEDIUM"


@override_settings(**SEMANTIC_ON)
def test_the_embedded_query_goes_through_the_shared_text_builder(monkeypatch):
    # Both sides of the comparison have to be built the same way or the floor
    # means nothing - the catalog side uses `food_embedding_text` too.
    _stub_lexical(monkeypatch, [])
    embed_calls = _stub_embed(monkeypatch)
    _stub_semantic(monkeypatch, [])

    services._resolve_food_by_name("  kadhi   pakoda ")

    assert embed_calls == ["kadhi pakoda"]


@override_settings(**SEMANTIC_ON, SEMANTIC_CANDIDATE_LIMIT=7)
def test_the_ann_window_size_is_configurable(monkeypatch):
    _stub_lexical(monkeypatch, [])
    _stub_embed(monkeypatch, vector=[0.5, 0.5])
    calls = _stub_semantic(monkeypatch, [])

    services._resolve_food_by_name("kadhi pakoda")

    assert calls == [([0.5, 0.5], {"limit": 7})]


# -- degrading when the provider is unavailable ------------------------------


@override_settings(**SEMANTIC_ON)
def test_a_provider_failure_falls_back_to_the_lexical_answer(monkeypatch):
    _stub_lexical(monkeypatch, [make_food(id="lex", name="Totally Unrelated")])
    _stub_embed(monkeypatch, exc=LLMCallError("provider down"))
    failures = []
    monkeypatch.setattr(repository, "record_llm_call_failure", lambda threshold: failures.append(threshold))

    food, _score, band = services._resolve_food_by_name("kadhi pakoda")

    assert (food.id, band) == ("lex", "LOW")
    # And it counts toward the circuit breaker, like any other failed call.
    assert failures


@override_settings(**SEMANTIC_ON)
def test_a_missing_provider_config_falls_back_without_tripping_the_breaker(monkeypatch):
    # A deploy-time misconfiguration is not evidence the provider is down, so
    # it must not push the breaker toward opening - same split `_call_llm`
    # already makes between LLMConfigurationError and LLMCallError.
    _stub_lexical(monkeypatch, [make_food(id="lex", name="Totally Unrelated")])
    _stub_embed(monkeypatch, exc=LLMConfigurationError("no key"))
    failures = []
    monkeypatch.setattr(repository, "record_llm_call_failure", lambda threshold: failures.append(threshold))

    food, _score, band = services._resolve_food_by_name("kadhi pakoda")

    assert (food.id, band) == ("lex", "LOW")
    assert failures == []


@override_settings(**SEMANTIC_ON)
def test_an_open_circuit_breaker_skips_the_embedding_entirely(monkeypatch):
    monkeypatch.setattr(repository, "circuit_breaker_is_open", lambda cooldown: True)
    _stub_lexical(monkeypatch, [make_food(id="lex", name="Totally Unrelated")])
    embed_calls = _stub_embed(monkeypatch)

    food, _score, band = services._resolve_food_by_name("kadhi pakoda")

    assert (food.id, band) == ("lex", "LOW")
    assert embed_calls == []


@override_settings(**SEMANTIC_ON)
def test_an_empty_ann_result_falls_back_cleanly(monkeypatch):
    # The catalog may simply not be embedded yet - Chunk 9a ships the columns
    # empty, so this is the state every deployment starts in.
    _stub_lexical(monkeypatch, [make_food(id="lex", name="Totally Unrelated")])
    _stub_embed(monkeypatch)
    _stub_semantic(monkeypatch, [])

    food, _score, band = services._resolve_food_by_name("kadhi pakoda")

    assert (food.id, band) == ("lex", "LOW")


# -- composites (§7.6) -------------------------------------------------------


def _stub_composites(monkeypatch, exact=(), semantic=(), calls=None):
    recorded = calls if calls is not None else []
    monkeypatch.setattr(meals_repository, "get_composite_foods", lambda: list(exact))

    def search(vector, **kw):
        recorded.append((vector, kw))
        return list(semantic)

    monkeypatch.setattr(meals_repository, "search_composites_by_embedding", search)
    return recorded


@override_settings(SEMANTIC_RESOLUTION_ENABLED=False)
def test_composite_off_state_is_exact_match_only(monkeypatch):
    dish = make_composite(name="Egg Fried Rice", aliases=["egg rice"])
    calls = _stub_composites(monkeypatch, exact=[dish], semantic=[(dish, 0.99)])
    embed_calls = _stub_embed(monkeypatch)

    assert services._resolve_composite_by_name("something else") is None
    assert calls == []
    assert embed_calls == []


@override_settings(**SEMANTIC_ON)
def test_an_exact_alias_match_still_short_circuits_before_any_embedding(monkeypatch):
    dish = make_composite(name="Egg Fried Rice", aliases=["egg rice"])
    _stub_composites(monkeypatch, exact=[dish])
    embed_calls = _stub_embed(monkeypatch)

    assert services._resolve_composite_by_name("egg rice") is dish
    assert embed_calls == []


@override_settings(**SEMANTIC_ON, SEMANTIC_COMPOSITE_FLOOR=0.85)
def test_a_composite_above_its_floor_matches_semantically(monkeypatch):
    dish = make_composite(name="Chicken Biryani", aliases=[])
    _stub_composites(monkeypatch, exact=[], semantic=[(dish, 0.9)])
    _stub_embed(monkeypatch)

    assert services._resolve_composite_by_name("murgh biryani") is dish


@override_settings(**SEMANTIC_ON, SEMANTIC_COMPOSITE_FLOOR=0.85)
def test_a_composite_below_its_floor_does_not_match(monkeypatch):
    dish = make_composite(name="Chicken Biryani", aliases=[])
    _stub_composites(monkeypatch, exact=[], semantic=[(dish, 0.84)])
    _stub_embed(monkeypatch)

    assert services._resolve_composite_by_name("murgh biryani") is None


@override_settings(**SEMANTIC_ON, SEMANTIC_COMPOSITE_FLOOR=0.85)
def test_a_bare_component_word_never_expands_into_the_dish(monkeypatch):
    # The property `_resolve_composite_by_name`'s docstring has always
    # promised: "dal" is a standalone food, not a silent expansion of
    # "Dal Chawal" into rice as well. Asserted at a similarity high enough
    # that only the fragment guard can be what rejects it.
    dish = make_composite(name="Dal Chawal", aliases=[])
    _stub_composites(monkeypatch, exact=[dish], semantic=[(dish, 0.99)])
    _stub_embed(monkeypatch)

    assert services._resolve_composite_by_name("dal") is None


@override_settings(**SEMANTIC_ON, SEMANTIC_COMPOSITE_FLOOR=0.85)
def test_a_curator_alias_outranks_the_fragment_guard(monkeypatch):
    # A curator writing "dal" as an alias of the dish is a deliberate
    # statement about this catalog; the guard only governs the semantic path.
    dish = make_composite(name="Dal Chawal", aliases=["dal"])
    _stub_composites(monkeypatch, exact=[dish])
    _stub_embed(monkeypatch)

    assert services._resolve_composite_by_name("dal") is dish


@override_settings(**SEMANTIC_ON, SEMANTIC_COMPOSITE_FLOOR=0.85)
def test_a_rejected_fragment_does_not_block_a_later_valid_neighbour(monkeypatch):
    # The nearest neighbour is a dish this query is only a fragment of; the
    # next one down names the same dish exactly. The guard must skip the
    # first without abandoning the search.
    fragment = make_composite(id="c1", name="Paneer Butter Masala Thali", aliases=[])
    real = make_composite(id="c2", name="Paneer Butter Masala", aliases=[])
    _stub_composites(monkeypatch, exact=[], semantic=[(fragment, 0.95), (real, 0.9)])
    _stub_embed(monkeypatch)

    assert services._resolve_composite_by_name("paneer butter masala").id == "c2"


@override_settings(**SEMANTIC_ON)
def test_a_composite_embedding_failure_degrades_to_no_match(monkeypatch):
    _stub_composites(monkeypatch, exact=[])
    _stub_embed(monkeypatch, exc=LLMCallError("provider down"))

    assert services._resolve_composite_by_name("murgh biryani") is None


# -- the fragment guard itself -----------------------------------------------


@pytest.mark.parametrize(
    "query,dish_name,expected",
    [
        ("dal", "Dal Chawal", True),
        ("chawal", "Dal Chawal", True),
        ("dal chawal", "Dal Chawal", False),  # equal, not a strict subset
        ("murgh biryani", "Chicken Biryani", False),  # "murgh" is not in the name
        ("biryani rice", "Chicken Biryani", False),
        ("", "Dal Chawal", False),
        ("DAL", "Dal Chawal", True),  # case-insensitive
    ],
)
def test_fragment_guard(query, dish_name, expected):
    assert services._is_fragment_of(query, make_composite(name=dish_name)) is expected

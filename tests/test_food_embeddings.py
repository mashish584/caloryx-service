"""Embedding infrastructure (PRD §7.2, §7.6, Chunk 9a).

Three seams, none of which need a database or an API key:
  * `meals.embeddings` - pure text building, shared by the catalog side here
    and the query side in Chunk 9b.
  * `llm.embeddings` - the provider boundary, patched at `openai.OpenAI` the
    same way `test_llm_client.py` does it.
  * `backfill_food_embeddings` - the command's control flow, with
    `meals.repository` and `embed_batch` both patched.

Nothing here asserts anything about *resolution*: no call site reads
`Food.embedding` until Chunk 9b, which is the point of shipping 9a on its own.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from io import StringIO

from llm import LLMCallError, LLMConfigurationError
from llm.embeddings import embed_batch, embed_text, reset_cache
from meals.embeddings import composite_embedding_text, food_embedding_text
from meals.repository import _to_vector_literal


@pytest.fixture(autouse=True)
def _reset_client_cache():
    reset_cache()
    yield
    reset_cache()


# -- text building -----------------------------------------------------------


def test_food_text_is_the_name_when_there_is_no_brand():
    assert food_embedding_text("Hummus, plain") == "Hummus, plain"
    assert food_embedding_text("Hummus, plain", None) == "Hummus, plain"
    assert food_embedding_text("Hummus, plain", "") == "Hummus, plain"


def test_food_text_leads_with_the_brand_when_there_is_one():
    assert food_embedding_text("Butter, salted", "Amul") == "Amul Butter, salted"


def test_food_text_collapses_whitespace_but_keeps_comma_structure():
    assert food_embedding_text("  Milk,   whole,\t3.25% milkfat ") == "Milk, whole, 3.25% milkfat"


def test_composite_text_includes_every_alias_after_the_name():
    assert (
        composite_embedding_text("Chicken Biryani", ["biryani", "murgh biryani"])
        == "Chicken Biryani, biryani, murgh biryani"
    )


def test_composite_text_drops_duplicate_and_case_variant_aliases():
    assert composite_embedding_text("Dal Chawal", ["dal chawal", "DAL CHAWAL", "dal rice"]) == (
        "Dal Chawal, dal rice"
    )


def test_composite_text_handles_no_aliases():
    assert composite_embedding_text("Misal Pav") == "Misal Pav"
    assert composite_embedding_text("Misal Pav", []) == "Misal Pav"


# -- pgvector literal --------------------------------------------------------


def test_vector_literal_is_pgvectors_bracketed_text_form():
    assert _to_vector_literal([0.5, -0.25]) == "[0.50000000,-0.25000000]"


def test_vector_literal_never_uses_scientific_notation():
    # A tiny component is real - it must not serialize as "1e-09", which is
    # exactly the shape that makes a stored vector unreadable by eye.
    assert "e" not in _to_vector_literal([1e-9, 2e-9])


# -- provider boundary -------------------------------------------------------


def _fake_embedding_response(vectors, prompt_tokens=10):
    return SimpleNamespace(
        data=[SimpleNamespace(index=i, embedding=v) for i, v in enumerate(vectors)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens),
    )


class _FakeEmbeddings:
    def __init__(self, response=None, exc=None, calls=None):
        self._response = response
        self._exc = exc
        self.calls = calls if calls is not None else []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


def _patch_openai(monkeypatch, embeddings):
    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: SimpleNamespace(embeddings=embeddings))
    return embeddings


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_DIMENSIONS=2)
def test_embed_batch_returns_vectors_and_usage(monkeypatch):
    fake = _patch_openai(monkeypatch, _FakeEmbeddings(_fake_embedding_response([[0.1, 0.2], [0.3, 0.4]])))

    response = embed_batch(["hummus", "rice"])

    assert response.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert response.prompt_tokens == 10
    assert response.model == "text-embedding-3-small"
    assert fake.calls[0]["input"] == ["hummus", "rice"]
    assert fake.calls[0]["dimensions"] == 2


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_DIMENSIONS=2)
def test_embed_batch_reorders_by_index_rather_than_trusting_response_order(monkeypatch):
    # A silent misalignment here attaches every food to its neighbour's
    # vector, which is near-impossible to spot downstream - so the ordering is
    # asserted against a deliberately shuffled response.
    shuffled = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.3, 0.4]),
            SimpleNamespace(index=0, embedding=[0.1, 0.2]),
        ],
        usage=SimpleNamespace(prompt_tokens=4),
    )
    _patch_openai(monkeypatch, _FakeEmbeddings(shuffled))

    assert embed_batch(["hummus", "rice"]).vectors == [[0.1, 0.2], [0.3, 0.4]]


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_DIMENSIONS=2)
def test_embed_batch_rejects_a_width_that_does_not_match_the_column(monkeypatch):
    _patch_openai(monkeypatch, _FakeEmbeddings(_fake_embedding_response([[0.1, 0.2, 0.3]])))

    with pytest.raises(LLMCallError, match="width 3"):
        embed_batch(["hummus"])


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_DIMENSIONS=2)
def test_embed_batch_rejects_a_short_response(monkeypatch):
    _patch_openai(monkeypatch, _FakeEmbeddings(_fake_embedding_response([[0.1, 0.2]])))

    with pytest.raises(LLMCallError, match="1 vectors for 2 inputs"):
        embed_batch(["hummus", "rice"])


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_DIMENSIONS=2)
def test_embed_batch_turns_a_provider_error_into_llm_call_error(monkeypatch):
    import openai

    _patch_openai(monkeypatch, _FakeEmbeddings(exc=openai.OpenAIError("boom")))

    with pytest.raises(LLMCallError):
        embed_batch(["hummus"])


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_MAX_BATCH=2)
def test_embed_batch_refuses_to_silently_split_an_oversized_batch(monkeypatch):
    _patch_openai(monkeypatch, _FakeEmbeddings(_fake_embedding_response([])))

    with pytest.raises(ValueError, match="EMBEDDING_MAX_BATCH"):
        embed_batch(["a", "b", "c"])


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small")
def test_embed_batch_short_circuits_on_empty_input(monkeypatch):
    fake = _patch_openai(monkeypatch, _FakeEmbeddings(_fake_embedding_response([])))

    response = embed_batch([])

    assert response.vectors == []
    assert fake.calls == []


@override_settings(OPENAI_API_KEY="", EMBEDDING_MODEL="text-embedding-3-small")
def test_embed_batch_without_a_key_is_a_configuration_error():
    with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
        embed_batch(["hummus"])


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="")
def test_embed_batch_without_a_model_is_a_configuration_error():
    with pytest.raises(LLMConfigurationError, match="EMBEDDING_MODEL"):
        embed_batch(["hummus"])


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_DIMENSIONS=2)
def test_embed_text_unwraps_the_single_vector(monkeypatch):
    _patch_openai(monkeypatch, _FakeEmbeddings(_fake_embedding_response([[0.1, 0.2]])))

    assert embed_text("hummus") == [0.1, 0.2]


# -- the backfill command ----------------------------------------------------


class _FakeRepo:
    """Stands in for `meals.repository`'s embedding helpers. Rows move out of
    the pending list as they're written, which is what makes the command's
    cursor-style loop terminate - the real predicate behaves the same way."""

    def __init__(self, foods=None, composites=None, width=2, extension=True):
        self.foods = list(foods or [])
        self.composites = list(composites or [])
        self.width = width
        self.extension = extension
        self.written = []

    def vector_extension_installed(self):
        return self.extension

    def embedding_column_width(self, table):
        return self.width

    def _pending_foods(self, sources):
        return [f for f in self.foods if not sources or f["source"] in sources]

    def count_foods_needing_embedding(self, model, sources=None):
        return len(self._pending_foods(sources or []))

    def fetch_foods_needing_embedding(self, model, sources=None, *, limit=256):
        return self._pending_foods(sources or [])[:limit]

    def count_composites_needing_embedding(self, model):
        return len(self.composites)

    def fetch_composites_needing_embedding(self, model, *, limit=256):
        return self.composites[:limit]

    def set_food_embeddings(self, updates, model):
        done = {row_id for row_id, _ in updates}
        self.foods = [f for f in self.foods if f["id"] not in done]
        self.written.extend(("Food", row_id, model) for row_id, _ in updates)
        return len(updates)

    def set_composite_embeddings(self, updates, model):
        done = {row_id for row_id, _ in updates}
        self.composites = [c for c in self.composites if c["id"] not in done]
        self.written.extend(("CompositeFood", row_id, model) for row_id, _ in updates)
        return len(updates)


def _food(food_id, name, source="USDA", brand=None):
    return {"id": food_id, "name": name, "source": source, "brand": brand}


def _run(monkeypatch, repo, embed=None, **options):
    import meals.management.commands.backfill_food_embeddings as command_module

    monkeypatch.setattr(command_module, "repository", repo)
    if embed is not None:
        monkeypatch.setattr(command_module, "embed_batch", embed)
    out = StringIO()
    call_command("backfill_food_embeddings", stdout=out, stderr=out, **options)
    return out.getvalue()


def _embedder(calls=None):
    recorded = calls if calls is not None else []

    def embed(texts):
        recorded.append(list(texts))
        return SimpleNamespace(
            vectors=[[0.1, 0.2] for _ in texts], prompt_tokens=len(texts), latency_ms=1, model="m"
        )

    embed.calls = recorded
    return embed


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_writes_every_pending_row(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus"), _food("f2", "Rice")])
    embed = _embedder()

    _run(monkeypatch, repo, embed)

    assert [row_id for _, row_id, _ in repo.written] == ["f1", "f2"]
    assert embed.calls == [["Hummus", "Rice"]]


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_excludes_open_food_facts_unless_asked(monkeypatch):
    repo = _FakeRepo(
        foods=[_food("f1", "Hummus", "USDA"), _food("f2", "Sabra Hummus", "OPEN_FOOD_FACTS")]
    )

    _run(monkeypatch, repo, _embedder())

    assert [row_id for _, row_id, _ in repo.written] == ["f1"]


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_includes_open_food_facts_when_explicitly_selected(monkeypatch):
    repo = _FakeRepo(foods=[_food("f2", "Hummus", "OPEN_FOOD_FACTS", brand="Sabra")])
    embed = _embedder()

    _run(monkeypatch, repo, embed, source=["open_food_facts"])

    assert [row_id for _, row_id, _ in repo.written] == ["f2"]
    # And the brand leads, per `food_embedding_text`.
    assert embed.calls == [["Sabra Hummus"]]


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2, EMBEDDING_MAX_BATCH=2)
def test_backfill_walks_forward_in_batches(monkeypatch):
    repo = _FakeRepo(foods=[_food("f{}".format(i), "Food {}".format(i)) for i in range(5)])
    embed = _embedder()

    _run(monkeypatch, repo, embed, batch_size=2)

    assert [len(call) for call in embed.calls] == [2, 2, 1]
    assert len(repo.written) == 5


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_honours_limit(monkeypatch):
    repo = _FakeRepo(foods=[_food("f{}".format(i), "Food {}".format(i)) for i in range(5)])

    _run(monkeypatch, repo, _embedder(), limit=2)

    assert len(repo.written) == 2


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_does_composites_too(monkeypatch):
    repo = _FakeRepo(composites=[{"id": "c1", "name": "Chicken Biryani", "aliases": ["biryani"]}])
    embed = _embedder()

    _run(monkeypatch, repo, embed)

    assert [table for table, _, _ in repo.written] == ["CompositeFood"]
    assert embed.calls == [["Chicken Biryani, biryani"]]


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_target_flag_scopes_the_run(monkeypatch):
    repo = _FakeRepo(
        foods=[_food("f1", "Hummus")],
        composites=[{"id": "c1", "name": "Biryani", "aliases": []}],
    )

    _run(monkeypatch, repo, _embedder(), target="foods")

    assert [table for table, _, _ in repo.written] == ["Food"]


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_dry_run_calls_nothing_and_writes_nothing(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus")])
    embed = _embedder()

    output = _run(monkeypatch, repo, embed, dry_run=True)

    assert repo.written == []
    assert embed.calls == []
    assert "Dry run" in output


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_refuses_to_run_without_the_pgvector_extension(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus")], extension=False)

    with pytest.raises(CommandError, match="pgvector extension is not installed"):
        _run(monkeypatch, repo, _embedder())


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=1536)
def test_backfill_refuses_a_dimension_mismatch_before_spending_anything(monkeypatch):
    # The schema hardcodes the column width and EMBEDDING_DIMENSIONS is set
    # independently - a mismatch must surface here, not thousands of paid
    # embeddings later as an insert error.
    repo = _FakeRepo(foods=[_food("f1", "Hummus")], width=2)
    embed = _embedder()

    with pytest.raises(CommandError, match="EMBEDDING_DIMENSIONS=1536"):
        _run(monkeypatch, repo, embed)
    assert embed.calls == []


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_refuses_when_the_column_is_missing(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus")], width=None)

    with pytest.raises(CommandError, match="db push"):
        _run(monkeypatch, repo, _embedder())


@override_settings(OPENAI_API_KEY="", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_refuses_without_a_key(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus")])

    with pytest.raises(CommandError, match="OPENAI_API_KEY"):
        _run(monkeypatch, repo, _embedder())


@override_settings(OPENAI_API_KEY="", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_dry_run_needs_no_key(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus")])

    output = _run(monkeypatch, repo, _embedder(), dry_run=True)

    assert "1 row(s) to embed" in output


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2, EMBEDDING_MAX_BATCH=2)
def test_backfill_stops_on_a_provider_failure_and_keeps_what_it_wrote(monkeypatch):
    repo = _FakeRepo(foods=[_food("f{}".format(i), "Food {}".format(i)) for i in range(4)])
    calls = []

    def embed(texts):
        calls.append(list(texts))
        if len(calls) == 2:
            raise LLMCallError("provider down")
        return SimpleNamespace(
            vectors=[[0.1, 0.2] for _ in texts], prompt_tokens=len(texts), latency_ms=1, model="m"
        )

    with pytest.raises(CommandError, match="Re-run to resume"):
        _run(monkeypatch, repo, embed, batch_size=2)

    # The first batch stays written - the predicate excludes it next time, so
    # resuming is just re-running. Nothing to roll back.
    assert len(repo.written) == 2


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2, EMBEDDING_MAX_BATCH=2)
def test_backfill_rejects_a_batch_size_over_the_cap(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus")])

    with pytest.raises(CommandError, match="EMBEDDING_MAX_BATCH"):
        _run(monkeypatch, repo, _embedder(), batch_size=999)


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="", EMBEDDING_DIMENSIONS=2)
def test_backfill_refuses_without_a_model(monkeypatch):
    repo = _FakeRepo(foods=[_food("f1", "Hummus")])

    with pytest.raises(CommandError, match="EMBEDDING_MODEL is unset"):
        _run(monkeypatch, repo, _embedder())


@override_settings(OPENAI_API_KEY="sk-test", EMBEDDING_MODEL="m", EMBEDDING_DIMENSIONS=2)
def test_backfill_on_a_fully_embedded_catalog_is_a_no_op(monkeypatch):
    repo = _FakeRepo()
    embed = _embedder()

    output = _run(monkeypatch, repo, embed)

    assert repo.written == []
    assert embed.calls == []
    assert "nothing to do" in output

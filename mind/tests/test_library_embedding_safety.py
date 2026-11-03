"""Embedding boundaries use synthetic vectors and temporary library databases only."""

import asyncio
import json
import sqlite3

import pytest

from conftest import run
from leeroy_mind.library.chunks import ChunkDraft, SourceResult
from leeroy_mind.library.core import DEFAULT_EMBED_MODEL, Library
from leeroy_mind.llm.client import LLMError
from leeroy_mind.storage.db import EmbeddingMismatch
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import FakeLLM


@pytest.fixture
def lib(tmp_path, clock):
    library = Library(tmp_path / "library.db", clock=clock)
    library._apply(SourceResult("wiki", [
        ChunkDraft(f"page:{i}", "lore", f"Reference {i}", f"Original reference text {i}.",
                   section=f"Section {i}", ref_ids=(f"item:{i}",)) for i in range(1, 4)]))
    yield library
    library.close()


def vectors(n, value=0.25):
    return [[value] + [0.0] * 511 for _ in range(n)]


class ScriptedEmbedder:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def embed(self, model, inputs, *, dimensions):
        self.calls.append({"model": model, "input": list(inputs), "dimensions": dimensions})
        await asyncio.sleep(0)
        return self.reply(len(self.calls), inputs)


def embedding_rows(lib):
    return [tuple(row) for row in lib.conn.execute("SELECT id,embedding FROM chunks ORDER BY id")]


@pytest.mark.parametrize("batch", [0, -1, True, False, 2.0, 1.5, "2", None])
def test_bad_batch_is_rejected_before_model_or_metadata_changes(lib, batch):
    llm = FakeLLM()
    with pytest.raises(ValueError, match="positive integer"):
        run(lib.embed_pending(llm, batch=batch))
    assert not llm.calls
    assert lib.get_meta("embed_model") is None and lib.get_meta("embed_dim") is None
    assert lib.pending_embeddings() == 3


@pytest.mark.parametrize("limit", [-1, True, False, 2.0, 1.5, "2", float("nan"), float("inf")])
def test_bad_limit_is_rejected_before_model_or_metadata_changes(lib, limit):
    llm = FakeLLM()
    with pytest.raises(ValueError, match="nonnegative integer"):
        run(lib.embed_pending(llm, limit=limit))
    assert not llm.calls
    assert lib.get_meta("embed_model") is None and lib.get_meta("embed_dim") is None


def test_zero_limit_needs_no_client_and_does_not_initialize_or_check_metadata(lib):
    assert run(lib.embed_pending(limit=0)) == 0
    assert lib.get_meta("embed_model") is None and lib.get_meta("embed_dim") is None
    lib.set_meta("embed_model", "another-space")
    before = [tuple(row) for row in lib.conn.execute("SELECT key,value FROM meta ORDER BY key")]
    with lib._tx():
        assert run(lib.embed_pending(limit=0)) == 0
    assert [tuple(row) for row in lib.conn.execute("SELECT key,value FROM meta ORDER BY key")] == before
    assert lib.pending_embeddings() == 3


def test_embedding_refuses_to_await_inside_callers_transaction_without_committing_it(lib):
    llm = FakeLLM()
    with pytest.raises(ValueError, match="inside a library transaction"):
        with lib._tx():
            lib.set_meta("caller-work", "must roll back")
            run(lib.embed_pending(llm))
    assert lib.get_meta("caller-work") is None
    assert lib.get_meta("embed_model") is None
    assert not llm.calls


def test_metadata_initialization_rolls_back_both_keys_when_second_write_fails(lib):
    lib.conn.execute("CREATE TRIGGER reject_embed_dim BEFORE INSERT ON meta "
                     "WHEN new.key='embed_dim' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    lib.conn.commit()
    llm = FakeLLM()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        run(lib.embed_pending(llm))
    assert not llm.calls
    assert lib.get_meta("embed_model") is None and lib.get_meta("embed_dim") is None
    lib.conn.execute("DROP TRIGGER reject_embed_dim")
    lib.conn.commit()
    assert run(lib.embed_pending(llm)) == 3
    assert lib.get_meta("embed_model") == DEFAULT_EMBED_MODEL and lib.get_meta("embed_dim") == "512"


@pytest.mark.parametrize("bad", ["none", "count", "short", "long", "not_vector", "nonnumeric",
                                "nan", "infinite", "negative_infinite", "overflow", "complex"])
def test_entire_response_is_validated_and_packed_before_any_embedding_write(lib, bad):
    response = vectors(3)
    if bad == "none":
        response = None
    elif bad == "count":
        response.pop()
    elif bad == "short":
        response[-1].pop()
    elif bad == "long":
        response[-1].append(0.0)
    elif bad == "not_vector":
        response[-1] = b"x" * 512
    else:
        response[-1][0] = {"nonnumeric": "not a number", "nan": float("nan"),
                          "infinite": float("inf"), "negative_infinite": -float("inf"),
                          "overflow": 1e100, "complex": 1j}[bad]
    statements = []
    lib.conn.set_trace_callback(statements.append)
    llm = ScriptedEmbedder(lambda call, inputs: response)
    with pytest.raises(LLMError, match="finite 512d float32 vectors"):
        run(lib.embed_pending(llm))
    assert len(llm.calls) == 1
    assert lib.pending_embeddings() == 3
    assert lib.conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 0
    assert not any(sql.lstrip().upper().startswith(("UPDATE CHUNKS SET EMBEDDING", "INSERT INTO CHUNKS_VEC",
                                                   "DELETE FROM CHUNKS_VEC")) for sql in statements)


def test_valid_vector_bytes_are_preserved_without_renormalizing(lib):
    response = [[0.125, -0.75, 42.0] + [0.0] * 509 for _ in range(3)]
    expected = pack_vec(response[0])
    llm = ScriptedEmbedder(lambda call, inputs: response[:len(inputs)])
    assert run(lib.embed_pending(llm, batch=2)) == 3
    assert [len(call["input"]) for call in llm.calls] == [2, 1]
    assert all(call["dimensions"] == 512 for call in llm.calls)
    assert {row[1] for row in embedding_rows(lib)} == {expected}
    assert {row[0] for row in lib.conn.execute("SELECT embedding FROM chunks_vec")} == {expected}


@pytest.mark.parametrize("changed", ["source", "key", "kind", "title", "section", "text", "delete", "reuse",
                                    "already_embedded", "model", "dimension"])
def test_changed_rows_or_embedding_space_fail_the_batch_without_overwriting_other_writer(lib, tmp_path,
                                                                                       clock, changed):
    other = Library(tmp_path / "library.db", clock=clock)
    target = lib.conn.execute("SELECT id FROM chunks WHERE key='page:2'").fetchone()[0]
    lib.conn.commit()
    sentinel = pack_vec(vectors(1, value=2.0)[0])

    def reply(call, inputs):
        # Opening/migrating the second connection reads PRAGMA user_version. End
        # that old snapshot before modeling a new writer after the embed began.
        other.conn.commit()
        with other._tx():
            if changed in {"source", "key", "kind", "title", "section", "text"}:
                value = {"source": "curated", "key": "replacement-key", "kind": "strategy",
                         "title": "Updated title", "section": "Updated section", "text": "Updated body"}[changed]
                other.conn.execute(f"UPDATE chunks SET {changed}=? WHERE id=?", (value, target))
            elif changed in {"delete", "reuse"}:
                other.conn.execute("DELETE FROM chunks WHERE id=?", (target,))
                if changed == "reuse":
                    other.conn.execute("INSERT INTO chunks(id,source,key,kind,title,section,text,t_created) "
                                       "VALUES(?, 'wiki','different-page','lore','Reference 2','Section 2',"
                                       "'Original reference text 2.',123)", (target,))
            elif changed == "already_embedded":
                other.conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (sentinel, target))
                other.conn.execute("INSERT INTO chunks_vec(chunk_id,kind,embedding) VALUES(?,'lore',?)",
                                   (target, sentinel))
            else:
                other.set_meta("embed_model" if changed == "model" else "embed_dim",
                               "other-model" if changed == "model" else "1024")
        return vectors(len(inputs))

    llm = ScriptedEmbedder(reply)
    try:
        error = EmbeddingMismatch if changed in {"model", "dimension"} else LLMError
        with pytest.raises(error):
            run(lib.embed_pending(llm))
        assert len(llm.calls) == 1  # no automatic retry loop on a changing source
        expected_vectors = [(target, sentinel)] if changed == "already_embedded" else []
        assert [tuple(row) for row in lib.conn.execute(
            "SELECT chunk_id,embedding FROM chunks_vec ORDER BY chunk_id")] == expected_vectors
        assert [(cid, blob) for cid, blob in embedding_rows(lib) if blob is not None] == expected_vectors
        assert lib.conn.execute("SELECT count(*) FROM chunks_vec v LEFT JOIN chunks c ON c.id=v.chunk_id "
                                "WHERE c.id IS NULL").fetchone()[0] == 0
    finally:
        lib.conn.commit()
        other.close()


def test_attribution_only_change_keeps_valid_vector_and_new_metadata(lib, tmp_path, clock):
    other = Library(tmp_path / "library.db", clock=clock)

    def reply(call, inputs):
        other.conn.commit()
        with other._tx():
            other.conn.execute("UPDATE chunks SET url=?,metadata=? WHERE key='page:2'",
                               ("https://example.invalid/new-attribution", json.dumps({"version": "new"})))
        return vectors(len(inputs))

    try:
        assert run(lib.embed_pending(ScriptedEmbedder(reply))) == 3
        row = lib.conn.execute("SELECT url,metadata,embedding FROM chunks WHERE key='page:2'").fetchone()
        assert row["url"].endswith("/new-attribution")
        assert json.loads(row["metadata"]) == {"version": "new"}
        assert row["embedding"] == pack_vec(vectors(1)[0])
    finally:
        lib.conn.commit()
        other.close()


@pytest.mark.parametrize("failure", ["model", "vector", "stale"])
def test_prior_batches_survive_failure_and_resume_only_pending_rows(lib, tmp_path, clock, failure):
    other = Library(tmp_path / "library.db", clock=clock)

    def reply(call, inputs):
        if call == 2:
            if failure == "model":
                raise LLMError("synthetic model failure")
            if failure == "vector":
                return [[float("nan")] * 512]
            other.conn.commit()
            with other._tx():
                other.conn.execute("UPDATE chunks SET text='Revised third page.' WHERE key='page:3'")
        return vectors(len(inputs))

    try:
        with pytest.raises(LLMError):
            run(lib.embed_pending(ScriptedEmbedder(reply), batch=2))
        saved = embedding_rows(lib)[:2]
        assert all(blob is not None for _, blob in saved)
        assert lib.pending_embeddings() == 1
        resumed = FakeLLM()
        assert run(lib.embed_pending(resumed)) == 1
        assert embedding_rows(lib)[:2] == saved
        assert len(resumed.calls) == 1 and len(resumed.calls[0]["input"]) == 1
        assert "Reference 3" in resumed.calls[0]["input"][0]
        if failure == "stale":
            assert "Revised third page." in resumed.calls[0]["input"][0]
        assert lib.pending_embeddings() == 0
    finally:
        lib.conn.commit()
        other.close()


def test_late_database_error_rolls_back_both_vector_tables_for_whole_batch(lib):
    lib.conn.execute("CREATE TRIGGER reject_second_embedding BEFORE UPDATE OF embedding ON chunks "
                     "WHEN new.key='page:2' BEGIN SELECT RAISE(ABORT,'synthetic vector write failure'); END")
    lib.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic vector write failure"):
        run(lib.embed_pending(FakeLLM()))
    assert lib.pending_embeddings() == 3
    assert lib.conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 0


def test_cancellation_keeps_completed_batch_and_resumes_after_inflight_request(lib):
    async def scenario():
        waiting, release = asyncio.Event(), asyncio.Event()

        class PausedEmbedder:
            calls = 0

            async def embed(self, model, inputs, *, dimensions):
                self.calls += 1
                if self.calls == 2:
                    waiting.set()
                    await release.wait()
                return vectors(len(inputs))

        llm = PausedEmbedder()
        task = asyncio.create_task(lib.embed_pending(llm, batch=2))
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        saved = embedding_rows(lib)[:2]
        assert all(blob is not None for _, blob in saved)
        assert lib.pending_embeddings() == 1
        assert await lib.embed_pending(FakeLLM()) == 1
        assert embedding_rows(lib)[:2] == saved

    run(scenario())

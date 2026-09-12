"""The agent looking things up mid-call, and the store behind it."""

import json

import pytest

from talk2myagent.memory import Memory, summarize, tokenize


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path)


def test_records_survive_a_restart_and_are_never_duplicated(tmp_path):
    first = Memory(tmp_path)
    first.add("The Amazon account email is alex@example.com.")
    first.add("The Amazon account email is alex@example.com.")  # same fact twice
    assert len(first.records) == 1
    reopened = Memory(tmp_path)
    assert len(reopened.records) == 1
    assert reopened.search("account email")[0]["text"].endswith("alex@example.com.")
    assert reopened.path.read_text().count("\n") == 1


def test_retrieval_finds_the_fact_and_refuses_to_guess(memory):
    memory.add("The coffee grinder was delivered on 3 September 2026.")
    memory.add("The Visa used for the grinder ends in 4031.")
    memory.add("Morgan prefers email over phone for follow-ups.")

    delivered = memory.search("delivery date for the coffee grinder")
    assert delivered and "3 September 2026" in delivered[0]["text"]

    card = memory.search("which card was it paid on")
    assert card and "4031" in card[0]["text"]

    # Nothing stored is about this, so it must return nothing rather than the nearest row.
    assert memory.search("what is the customer's date of birth") == []
    assert summarize([]) == "Memory has nothing on that."


def test_plan_facts_become_searchable_sentences(memory):
    memory.add_facts(
        {"order_id": "113-4429911", "account_email": "a@b.com", "unknown": "the amount"}
    )
    assert len(memory.records) == 2  # "unknown" is not a fact
    assert "The order id is 113-4429911." in [r.text for r in memory.records]
    assert memory.search("order id")[0]["text"].endswith("113-4429911.")


def test_a_finished_call_becomes_memory_for_the_next_one(memory):
    memory.add_call(
        {
            "call_id": "20260911-231023-3133f866",
            "created_at": "2026-09-11T23:10:23+00:00",
            "outcome": "completed",
            "summary": "Return approved, refund to the original card.",
            "plan": {"company": "Amazon US"},
            "evaluation": [
                {"verdict": "met", "explanation": "a refund of $165.00 within 5 business days"},
                {"verdict": "unknown", "explanation": "no confirmation number was given"},
            ],
        }
    )
    texts = [r.text for r in memory.records]
    assert any("Amazon US" in t and "completed" in t for t in texts)
    # Only what they actually confirmed is kept as a promise.
    promises = [r for r in memory.records if r.kind == "promise"]
    assert len(promises) == 1 and "$165.00" in promises[0].text
    assert memory.search("what did Amazon promise")[0]["kind"] in {"promise", "call"}


def test_forget_removes_a_record_from_disk_and_the_index(memory):
    keep = memory.add("The account email is a@b.com.")
    drop = memory.add("The temporary access code is 55123.")
    assert memory.search("access code")
    assert memory.forget(drop.id) is True
    assert memory.forget(drop.id) is False
    assert memory.search("access code") == []
    assert [r.id for r in Memory(memory.path.parent.parent).records] == [keep.id]


def test_a_corrupt_line_does_not_take_out_the_store(tmp_path):
    store = Memory(tmp_path)
    store.add("The order id is 113-4429911.")
    with store.path.open("a") as handle:
        handle.write("{not json at all\n")
    reopened = Memory(tmp_path)
    assert len(reopened.records) == 1


def test_rejects_empty_and_oversized_text(memory):
    for bad in ["", "   ", "x" * 2001]:
        with pytest.raises(ValueError, match="1-2000"):
            memory.add(bad)


def test_tokenizer_keeps_identifiers_whole():
    assert "113-4429911" in tokenize("The order id is 113-4429911.")
    assert "alex@example.com" not in tokenize("email alex@example.com")  # split on @
    assert {"alex", "example", "com"} <= set(tokenize("email alex@example.com"))


def test_lookup_is_fast_enough_for_a_live_turn(memory):
    import time

    for i in range(2000):
        memory.add(f"Fact number {i} concerns account {i % 97} and product {i % 13}.")
    started = time.monotonic()
    results = memory.search("account 42 product 5")
    elapsed = time.monotonic() - started
    assert results
    # The whole turn budget to first audio is ~0.34 s; a lookup must be a rounding error.
    assert elapsed < 0.05, f"lookup took {elapsed:.3f}s"


def test_scores_and_json_roundtrip_cleanly(tmp_path):
    store = Memory(tmp_path)
    store.add("The refund goes to the Visa ending in 4031.")
    hit = store.search("visa")[0]
    assert hit["score"] > 0
    # A result dict carries a score; re-reading the file must not choke on it.
    json.dumps(hit)
    assert len(Memory(tmp_path).records) == 1

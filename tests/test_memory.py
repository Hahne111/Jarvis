# tests/test_memory.py
import pytest

@pytest.fixture
def tmp_memory(tmp_path):
    """Memory instance using temp directories (no real CWD dependency)."""
    from jarvis.memory import Memory
    return Memory(
        db_path=str(tmp_path / "test.db"),
        chroma_path=str(tmp_path / "chroma"),
    )

def test_store_and_retrieve_fact(tmp_memory):
    """Stored facts should be retrievable by semantic search."""
    tmp_memory.store_fact("User's name is Piotr.")
    results = tmp_memory.search_facts("What is the user's name?")
    assert any("Piotr" in r for r in results)

def test_search_returns_empty_when_no_facts(tmp_memory):
    """Search on empty DB returns empty list."""
    results = tmp_memory.search_facts("quantum physics")
    assert results == []

def test_save_and_retrieve_summary(tmp_memory):
    """Conversation summaries should persist."""
    tmp_memory.save_conversation_summary("User asked about Python async.")
    summaries = tmp_memory.get_recent_summaries(n=5)
    assert len(summaries) >= 1
    assert "Python async" in summaries[0]

def test_get_recent_summaries_order(tmp_memory):
    """Most recent summaries come first."""
    tmp_memory.save_conversation_summary("First summary.")
    tmp_memory.save_conversation_summary("Second summary.")
    summaries = tmp_memory.get_recent_summaries(n=2)
    assert summaries[0] == "Second summary."

def test_extract_and_store_facts_on_trigger(tmp_memory):
    """extract_and_store_facts should store user message when trigger word present."""
    tmp_memory.extract_and_store_facts("Noted.", "my name is Piotr")
    results = tmp_memory.search_facts("name")
    assert any("Piotr" in r for r in results)

def test_extract_and_store_facts_no_trigger(tmp_memory):
    """extract_and_store_facts should not store when no trigger word present."""
    tmp_memory.extract_and_store_facts("Okay.", "what time is it")
    results = tmp_memory.search_facts("time")
    assert results == []

def test_upsert_deduplicates_facts(tmp_memory):
    """Storing the same fact twice should not create a duplicate."""
    tmp_memory.store_fact("User likes Python.")
    tmp_memory.store_fact("User likes Python.")
    results = tmp_memory.search_facts("Python")
    assert len(results) == 1

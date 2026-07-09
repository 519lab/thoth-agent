"""Focused tests for _AsyncSessionDB message I/O methods not covered by ported files.

Covers: append_message (JSONB columns, counters), replace_messages (atomic
replace), get_messages (ordering/hydration), and get_messages_as_conversation
(OpenAI shape, reasoning fields, ancestor traversal, dedup).
"""
import pytest
import pytest_asyncio

from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(thoth_db_initialized):
    return _AsyncSessionDB()


# ---------------------------------------------------------------------------
# append_message
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_message_returns_int_id(db):
    """append_message returns an integer row ID."""
    await db.create_session("S1", source="cli")
    mid = await db.append_message("S1", role="user", content="hello")
    assert isinstance(mid, int)
    assert mid > 0


@pytest.mark.asyncio
async def test_append_message_increments_message_count(db):
    """Each append_message call increments sessions.message_count by 1."""
    import thoth_db
    await db.create_session("S2", source="cli")
    await db.append_message("S2", role="user", content="one")
    await db.append_message("S2", role="assistant", content="two")
    async with thoth_db.connection() as conn:
        row = await conn.fetchrow("SELECT message_count FROM sessions WHERE id = $1", "S2")
    assert row["message_count"] == 2


@pytest.mark.asyncio
async def test_append_message_tool_calls_jsonb_stored_as_list(db):
    """tool_calls stored as JSONB list — round-trips as Python list without manual decode."""
    await db.create_session("S3", source="cli")
    tc = [{"id": "tc1", "type": "function", "function": {"name": "my_tool", "arguments": "{}"}}]
    mid = await db.append_message("S3", role="assistant", content=None, tool_calls=tc)
    msgs = await db.get_messages("S3")
    assert len(msgs) == 1
    assert msgs[0]["tool_calls"] == tc  # exact round-trip, pool codec decoded it


@pytest.mark.asyncio
async def test_append_message_tool_calls_increments_tool_call_count(db):
    """tool_calls increments sessions.tool_call_count by the number of calls."""
    import thoth_db
    await db.create_session("S4", source="cli")
    tc = [{"id": "t1"}, {"id": "t2"}]
    await db.append_message("S4", role="assistant", content="", tool_calls=tc)
    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT message_count, tool_call_count FROM sessions WHERE id = $1", "S4"
        )
    assert row["message_count"] == 1
    assert row["tool_call_count"] == 2


@pytest.mark.asyncio
async def test_append_message_reasoning_details_jsonb(db):
    """reasoning_details stored as JSONB dict — pool codec handles encode/decode."""
    await db.create_session("S5", source="cli")
    rd = {"type": "chain_of_thought", "tokens": 42}
    await db.append_message("S5", role="assistant", content="answer", reasoning_details=rd)
    msgs = await db.get_messages("S5")
    assert msgs[0]["reasoning_details"] == rd


@pytest.mark.asyncio
async def test_append_message_codex_items_jsonb(db):
    """codex_reasoning_items and codex_message_items stored as JSONB."""
    await db.create_session("S6", source="cli")
    cri = [{"type": "reasoning", "text": "thinking..."}]
    cmi = [{"type": "output_text", "text": "answer"}]
    await db.append_message(
        "S6", role="assistant", content="x",
        codex_reasoning_items=cri, codex_message_items=cmi,
    )
    msgs = await db.get_messages("S6")
    assert msgs[0]["codex_reasoning_items"] == cri
    assert msgs[0]["codex_message_items"] == cmi


@pytest.mark.asyncio
async def test_append_message_platform_message_id(db):
    """platform_message_id persisted and retrievable."""
    await db.create_session("S7", source="telegram")
    await db.append_message("S7", role="user", content="hi", platform_message_id="tg-9999")
    msgs = await db.get_messages("S7")
    assert msgs[0]["platform_message_id"] == "tg-9999"


# ---------------------------------------------------------------------------
# replace_messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replace_messages_clears_and_reinserts(db):
    """replace_messages atomically replaces the session's entire history."""
    await db.create_session("R1", source="cli")
    await db.append_message("R1", role="user", content="old1")
    await db.append_message("R1", role="assistant", content="old2")

    new_msgs = [
        {"role": "user", "content": "new user"},
        {"role": "assistant", "content": "new assistant"},
        {"role": "user", "content": "followup"},
    ]
    await db.replace_messages("R1", new_msgs)

    msgs = await db.get_messages("R1")
    assert len(msgs) == 3
    assert [m["content"] for m in msgs] == ["new user", "new assistant", "followup"]


@pytest.mark.asyncio
async def test_replace_messages_resets_counters(db):
    """replace_messages resets message_count and tool_call_count to new values."""
    import thoth_db
    await db.create_session("R2", source="cli")
    # Seed old messages (includes a tool call)
    await db.append_message("R2", role="assistant", content="", tool_calls=[{"id": "t1"}])
    await db.append_message("R2", role="tool", content="result", tool_name="x")

    # Replace with simple messages, no tool calls
    await db.replace_messages("R2", [
        {"role": "user", "content": "fresh start"},
    ])

    async with thoth_db.connection() as conn:
        row = await conn.fetchrow(
            "SELECT message_count, tool_call_count FROM sessions WHERE id = $1", "R2"
        )
    assert row["message_count"] == 1
    assert row["tool_call_count"] == 0


@pytest.mark.asyncio
async def test_replace_messages_with_tool_calls_jsonb(db):
    """replace_messages persists tool_calls JSONB correctly."""
    await db.create_session("R3", source="cli")
    tc = [{"id": "tc99", "function": {"name": "do_thing", "arguments": "{}"}}]
    await db.replace_messages("R3", [
        {"role": "assistant", "content": "", "tool_calls": tc},
    ])
    msgs = await db.get_messages("R3")
    assert msgs[0]["tool_calls"] == tc


@pytest.mark.asyncio
async def test_replace_messages_accepts_platform_message_id_alias(db):
    """replace_messages accepts both 'platform_message_id' and 'message_id' keys."""
    await db.create_session("R4", source="telegram")
    await db.replace_messages("R4", [
        {"role": "user", "content": "hello", "message_id": "telegram-42"},
    ])
    msgs = await db.get_messages("R4")
    assert msgs[0]["platform_message_id"] == "telegram-42"


# ---------------------------------------------------------------------------
# get_messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_messages_ordering(db):
    """get_messages returns messages in insertion (id ascending) order."""
    await db.create_session("G1", source="cli")
    for i in range(5):
        await db.append_message("G1", role="user" if i % 2 == 0 else "assistant", content=f"m{i}")
    msgs = await db.get_messages("G1")
    contents = [m["content"] for m in msgs]
    assert contents == ["m0", "m1", "m2", "m3", "m4"]


@pytest.mark.asyncio
async def test_get_messages_empty_session(db):
    """get_messages returns [] for a session with no messages."""
    await db.create_session("G2", source="cli")
    msgs = await db.get_messages("G2")
    assert msgs == []


@pytest.mark.asyncio
async def test_get_messages_session_isolation(db):
    """get_messages only returns messages for the given session."""
    await db.create_session("GA", source="cli")
    await db.create_session("GB", source="cli")
    await db.append_message("GA", role="user", content="from A")
    await db.append_message("GB", role="user", content="from B")

    msgs_a = await db.get_messages("GA")
    assert len(msgs_a) == 1
    assert msgs_a[0]["content"] == "from A"


# ---------------------------------------------------------------------------
# get_messages_as_conversation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_messages_as_conversation_basic_shape(db):
    """get_messages_as_conversation returns role+content dicts."""
    await db.create_session("C1", source="cli")
    await db.append_message("C1", role="user", content="hello")
    await db.append_message("C1", role="assistant", content="hi there")

    conv = await db.get_messages_as_conversation("C1")
    assert len(conv) == 2
    assert conv[0] == {"role": "user", "content": "hello"}
    assert conv[1]["role"] == "assistant"
    assert conv[1]["content"] == "hi there"


@pytest.mark.asyncio
async def test_get_messages_as_conversation_reasoning_fields(db):
    """Reasoning fields are surfaced on assistant messages."""
    await db.create_session("C2", source="cli")
    rd = {"type": "cot", "detail": "step by step"}
    await db.append_message(
        "C2", role="assistant", content="answer",
        finish_reason="stop",
        reasoning="my reasoning",
        reasoning_content="rc content",
        reasoning_details=rd,
    )

    conv = await db.get_messages_as_conversation("C2")
    assert len(conv) == 1
    msg = conv[0]
    assert msg["finish_reason"] == "stop"
    assert msg["reasoning"] == "my reasoning"
    assert msg["reasoning_content"] == "rc content"
    assert msg["reasoning_details"] == rd


@pytest.mark.asyncio
async def test_get_messages_as_conversation_tool_calls_hydrated(db):
    """tool_calls appear as list in conversation, not string."""
    await db.create_session("C3", source="cli")
    tc = [{"id": "t1", "function": {"name": "search", "arguments": "{}"}}]
    await db.append_message("C3", role="assistant", content="", tool_calls=tc)

    conv = await db.get_messages_as_conversation("C3")
    assert conv[0]["tool_calls"] == tc


@pytest.mark.asyncio
async def test_get_messages_as_conversation_platform_message_id_as_message_id(db):
    """platform_message_id surfaces as 'message_id' for backward compat."""
    await db.create_session("C4", source="telegram")
    await db.append_message("C4", role="user", content="hi", platform_message_id="tg-101")
    conv = await db.get_messages_as_conversation("C4")
    assert conv[0]["message_id"] == "tg-101"


@pytest.mark.asyncio
async def test_get_messages_as_conversation_include_ancestors(db):
    """include_ancestors=True merges parent session messages before child's."""
    await db.create_session("PARENT", source="cli")
    await db.create_session("CHILD", source="cli", parent_session_id="PARENT")
    await db.append_message("PARENT", role="user", content="parent msg")
    await db.append_message("CHILD", role="user", content="child msg")

    conv = await db.get_messages_as_conversation("CHILD", include_ancestors=True)
    contents = [m["content"] for m in conv]
    # Both should appear, parent first
    assert "parent msg" in contents
    assert "child msg" in contents
    assert contents.index("parent msg") < contents.index("child msg")

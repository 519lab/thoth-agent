import pytest
import pytest_asyncio
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(hermes_db_initialized):
    return _AsyncSessionDB()


@pytest.mark.asyncio
async def test_session_count_empty(db):
    assert await db.session_count() == 0


@pytest.mark.asyncio
async def test_session_count_with_sessions(db):
    await db.create_session(session_id="c1", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session(session_id="c2", source="telegram", model="m", model_config={}, system_prompt="")
    assert await db.session_count() == 2


@pytest.mark.asyncio
async def test_session_count_filtered_by_source(db):
    await db.create_session(session_id="c1", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session(session_id="c2", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session(session_id="c3", source="telegram", model="m", model_config={}, system_prompt="")
    assert await db.session_count(source="cli") == 2
    assert await db.session_count(source="telegram") == 1
    assert await db.session_count() == 3


@pytest.mark.asyncio
async def test_message_count_empty(db):
    await db.create_session(session_id="m1", source="cli", model="m", model_config={}, system_prompt="")
    assert await db.message_count("m1") == 0


@pytest.mark.asyncio
async def test_message_count_single_session(db):
    await db.create_session(session_id="m1", source="cli", model="m", model_config={}, system_prompt="")
    await db.append_message("m1", "user", "hello")
    assert await db.message_count("m1") == 1
    await db.append_message("m1", "assistant", "hi")
    assert await db.message_count("m1") == 2


@pytest.mark.asyncio
async def test_message_count_all_sessions(db):
    await db.create_session(session_id="m1", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session(session_id="m2", source="cli", model="m", model_config={}, system_prompt="")
    await db.append_message("m1", "user", "hello")
    await db.append_message("m1", "assistant", "hi")
    await db.append_message("m2", "user", "hi there")
    # Without session filter: total across DB
    assert await db.message_count() == 3


@pytest.mark.asyncio
async def test_export_session_not_found(db):
    assert await db.export_session("nonexistent") is None


@pytest.mark.asyncio
async def test_export_session_no_messages(db):
    await db.create_session(session_id="e1", source="cli", model="m", model_config={"x": 1}, system_prompt="test")
    blob = await db.export_session("e1")
    assert blob is not None
    assert "id" in blob
    assert blob["id"] == "e1"
    assert "messages" in blob
    assert blob["messages"] == []


@pytest.mark.asyncio
async def test_export_session_with_messages(db):
    await db.create_session(session_id="e1", source="cli", model="m", model_config={"x": 1}, system_prompt="test")
    await db.append_message("e1", "user", "hi")
    await db.append_message("e1", "assistant", "hello")
    blob = await db.export_session("e1")
    assert blob is not None
    assert blob["id"] == "e1"
    assert len(blob["messages"]) == 2
    assert blob["messages"][0]["role"] == "user"
    assert blob["messages"][0]["content"] == "hi"
    assert blob["messages"][1]["role"] == "assistant"
    assert blob["messages"][1]["content"] == "hello"


@pytest.mark.asyncio
async def test_export_all_empty(db):
    all_blob = await db.export_all()
    assert all_blob == []


@pytest.mark.asyncio
async def test_export_all_multiple_sessions(db):
    await db.create_session(session_id="a1", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session(session_id="a2", source="telegram", model="m", model_config={}, system_prompt="")
    await db.append_message("a1", "user", "msg1")
    await db.append_message("a2", "user", "msg2")

    all_blob = await db.export_all()
    assert len(all_blob) == 2
    assert all_blob[0]["id"] in ("a1", "a2")
    assert all_blob[1]["id"] in ("a1", "a2")
    # Check that messages are included
    for item in all_blob:
        assert "messages" in item
        assert len(item["messages"]) == 1


@pytest.mark.asyncio
async def test_export_all_filtered_by_source(db):
    await db.create_session(session_id="a1", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session(session_id="a2", source="cli", model="m", model_config={}, system_prompt="")
    await db.create_session(session_id="a3", source="telegram", model="m", model_config={}, system_prompt="")
    await db.append_message("a1", "user", "msg1")
    await db.append_message("a2", "user", "msg2")
    await db.append_message("a3", "user", "msg3")

    cli_blob = await db.export_all(source="cli")
    assert len(cli_blob) == 2
    assert all(item["source"] == "cli" for item in cli_blob)

    telegram_blob = await db.export_all(source="telegram")
    assert len(telegram_blob) == 1
    assert telegram_blob[0]["source"] == "telegram"

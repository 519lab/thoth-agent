import pytest
import pytest_asyncio
from thoth_state import _AsyncSessionDB


@pytest_asyncio.fixture
async def db(hermes_db_initialized):
    return _AsyncSessionDB()


@pytest.mark.asyncio
async def test_meta_roundtrip(db):
    assert await db.get_meta("k") is None
    await db.set_meta("k", "v")
    assert await db.get_meta("k") == "v"
    await db.set_meta("k", "v2")  # upsert
    assert await db.get_meta("k") == "v2"


@pytest.mark.asyncio
async def test_meta_independent_keys(db):
    await db.set_meta("a", "1")
    await db.set_meta("b", "2")
    assert await db.get_meta("a") == "1"
    assert await db.get_meta("b") == "2"

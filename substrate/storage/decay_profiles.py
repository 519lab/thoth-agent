"""DecayProfileRepo — read-only access to ``substrate_decay_profiles``.

Phase A treats profiles as immutable: the Alembic revision seeds 4
defaults at install time and nothing in Phase A modifies them at
runtime. CRUD (operator-driven profile authoring) lands when there's a
real Curator that consumes profile changes (Phase B+).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
from uuid import UUID

from substrate.storage.rows import _decay_profile_from_row
from substrate.storage.types import DecayProfile, Modality

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


# Stable v5 UUIDs for the seeded profiles. These are mirrored in the
# Alembic revision; if you change one here, change it there.
DEFAULT_TEXT_PROFILE = UUID("00000000-0000-5000-8000-000000000001")
DEFAULT_STRUCTURED_PROFILE = UUID("00000000-0000-5000-8000-000000000002")
DEFAULT_BINARY_PROFILE = UUID("00000000-0000-5000-8000-000000000003")
DEFAULT_SIGNAL_PROFILE = UUID("00000000-0000-5000-8000-000000000004")


_DEFAULT_PROFILE_FOR: dict[Modality, UUID] = {
    Modality.TEXT: DEFAULT_TEXT_PROFILE,
    Modality.STRUCTURED_EVENT: DEFAULT_STRUCTURED_PROFILE,
    Modality.BINARY_BLOB: DEFAULT_BINARY_PROFILE,
    Modality.SIGNAL: DEFAULT_SIGNAL_PROFILE,
}


class DecayProfileRepo:
    """Read-only repository for decay profiles.

    All methods are async and accept an optional ``conn`` — if absent,
    the repo acquires from the shared ``thoth_db.pool()``.
    """

    def __init__(self, pool: "asyncpg.Pool") -> None:
        self._pool = pool

    async def get(
        self,
        profile_id: UUID,
        *,
        conn: "Optional[asyncpg.Connection]" = None,
    ) -> Optional[DecayProfile]:
        """Return the profile or ``None`` if it doesn't exist."""
        row = await self._fetchrow(
            "SELECT * FROM substrate_decay_profiles WHERE profile_id = $1",
            profile_id,
            conn=conn,
        )
        return _decay_profile_from_row(row) if row else None

    async def get_by_name(
        self,
        name: str,
        *,
        conn: "Optional[asyncpg.Connection]" = None,
    ) -> Optional[DecayProfile]:
        row = await self._fetchrow(
            "SELECT * FROM substrate_decay_profiles WHERE name = $1",
            name,
            conn=conn,
        )
        return _decay_profile_from_row(row) if row else None

    @staticmethod
    def default_for_modality(modality: Modality) -> UUID:
        """Return the stable v5 UUID of the default decay profile for a
        given modality. Used by stream auto-registration in
        ``Substrate.boot()`` so it doesn't need a name → ID lookup.
        """
        return _DEFAULT_PROFILE_FOR[modality]

    # ------------------------------------------------------------------
    # Internal: connection acquisition helper.
    # ------------------------------------------------------------------

    async def _fetchrow(
        self,
        query: str,
        *args,
        conn: "Optional[asyncpg.Connection]" = None,
    ):
        if conn is not None:
            return await conn.fetchrow(query, *args)
        async with self._pool.acquire() as c:
            return await c.fetchrow(query, *args)

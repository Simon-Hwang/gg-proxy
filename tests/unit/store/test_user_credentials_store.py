# ruff: noqa: E501 — the test coverage table below intentionally exceeds
# the 100-char limit so each row reads as a single grep-able line.
"""UserCredentialsStore tests — Plan v3 §B.8.1.

Pins the encryption-at-rest, partial-good, allowlist-drift, and
admin-override contracts before any route or manager wires up the
store.

Test coverage map (Plan v3 §B.8.1):

| ID | Test                                                  | What it pins |
|----|-------------------------------------------------------|---|
| a  | test_upsert_then_get_round_trip                       | value decrypts identical |
| b  | test_upsert_idempotent_on_label_env_pair              | second write overwrites |
| c  | test_get_unknown_returns_empty_dict                   | missing user is empty |
| d  | test_list_for_user_no_plaintext_in_metadata           | metadata view never leaks |
| e  | test_allowed_env_names_snapshot                       | drift guard for the allowlist (lives in routers/) |
| f  | test_delete_is_idempotent                             | second delete is no-op |
| g  | test_no_fernet_short_circuits_get_returns_empty       | disabled feature returns {} |
| h  | test_key_fingerprint_recorded_on_upsert               | fp stored verbatim |
| i  | test_get_skips_row_with_mismatched_fingerprint        | bricked row → skip, not raise |
| j  | test_get_returns_partial_when_one_row_is_bricked      | partial-good semantics |
| k  | test_list_bricked_returns_only_mismatched_rows        | bricked-list filter |
| l  | test_get_skips_row_when_decrypt_raises_invalid_token  | tampered ciphertext → skip |
| m  | test_admin_override_keeps_created_by_label_as_admin   | B.8.3.g supporting test (created_by is the most recent writer) |
| n  | test_disabled_feature_raises_on_upsert                | writes fail loud when fernet=None |
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from gg_relay.store.schema import metadata, user_credentials
from gg_relay.store.user_credentials import (
    UserCredentialsFeatureDisabled,
    UserCredentialsStore,
    build_fernet_from_key,
    compute_key_fingerprint,
)


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode("utf-8")


@pytest_asyncio.fixture
async def store(engine, key):
    fernet, fp = build_fernet_from_key(key)
    return UserCredentialsStore(engine, fernet=fernet, key_fingerprint=fp)


@pytest_asyncio.fixture
async def disabled_store(engine):
    return UserCredentialsStore(engine, fernet=None, key_fingerprint=None)


pytestmark = pytest.mark.asyncio


class TestRoundTrip:
    async def test_upsert_then_get_round_trip(self, store):
        """[a] value decrypts identical."""
        await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-alice-secret-12345",
            actor_label="dashboard-alice",
        )
        result = await store.get_for_user("dashboard-alice")
        assert result == {"ANTHROPIC_API_KEY": "sk-alice-secret-12345"}

    async def test_upsert_idempotent_on_label_env_pair(self, store):
        """[b] second write overwrites in place; no UNIQUE crash."""
        await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-first",
            actor_label="dashboard-alice",
        )
        meta = await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-second",
            actor_label="dashboard-alice",
        )
        result = await store.get_for_user("dashboard-alice")
        assert result == {"ANTHROPIC_API_KEY": "sk-second"}
        assert meta["env_name"] == "ANTHROPIC_API_KEY"
        rows = await store.list_for_user("dashboard-alice")
        assert len(rows) == 1, "second upsert should NOT create a duplicate row"

    async def test_get_unknown_returns_empty_dict(self, store):
        """[c] missing user is empty — never raises."""
        result = await store.get_for_user("nobody")
        assert result == {}


class TestMetadataView:
    async def test_list_for_user_no_plaintext_in_metadata(self, store):
        """[d] metadata view returns ``env_name`` etc. but NEVER the value.

        The dashboard reads from list_for_user; if value_encrypted
        or its decrypted form ever leaked here we'd defeat the whole
        encryption-at-rest design.
        """
        await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-secret-must-not-leak",
            actor_label="dashboard-alice",
        )
        rows = await store.list_for_user("dashboard-alice")
        assert len(rows) == 1
        row = rows[0]
        assert "value_encrypted" not in row, (
            "metadata view must not expose ciphertext (a defense-in-depth "
            "measure — even though decrypting requires the key, callers "
            "should never see the column)"
        )
        for v in row.values():
            assert v != "sk-secret-must-not-leak", (
                "decrypted value leaked into metadata projection"
            )


class TestDeleteSemantics:
    async def test_delete_is_idempotent(self, store):
        """[f] second delete is a no-op (returns False)."""
        await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-x",
            actor_label="dashboard-alice",
        )
        assert await store.delete(
            user_label="dashboard-alice", env_name="ANTHROPIC_API_KEY"
        ) is True
        assert await store.delete(
            user_label="dashboard-alice", env_name="ANTHROPIC_API_KEY"
        ) is False


class TestDisabledFeature:
    async def test_no_fernet_short_circuits_get_returns_empty(
        self, disabled_store
    ):
        """[g] reads return ``{}`` when fernet is None — never raises."""
        result = await disabled_store.get_for_user("dashboard-alice")
        assert result == {}

    async def test_disabled_feature_raises_on_upsert(self, disabled_store):
        """[n] writes fail loud — silent no-ops would let operators
        believe they configured the feature when they hadn't."""
        with pytest.raises(UserCredentialsFeatureDisabled):
            await disabled_store.upsert(
                user_label="dashboard-alice",
                env_name="ANTHROPIC_API_KEY",
                value="sk-x",
                actor_label="dashboard-alice",
            )


class TestKeyFingerprint:
    async def test_key_fingerprint_recorded_on_upsert(self, store, key):
        """[h] fp matches the current key's fp."""
        await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-x",
            actor_label="dashboard-alice",
        )
        rows = await store.list_for_user("dashboard-alice")
        assert rows[0]["key_fingerprint"] == compute_key_fingerprint(key)


class TestBrickedRowGracefulDegradation:
    """[i, j, k, l] partial-good semantics — Plan v3 R3 critical contract."""

    async def test_get_skips_row_with_mismatched_fingerprint(
        self, engine, key, caplog
    ):
        """[i] a row whose fingerprint != current key's fp is skipped + logged."""
        new_fernet, new_fp = build_fernet_from_key(key)
        store = UserCredentialsStore(
            engine, fernet=new_fernet, key_fingerprint=new_fp
        )
        # Insert a row whose fp claims to be from an OLD key.
        async with engine.begin() as conn:
            await conn.execute(
                insert(user_credentials).values(
                    user_label="dashboard-alice",
                    env_name="ANTHROPIC_API_KEY",
                    value_encrypted=new_fernet.encrypt(b"sk-x"),
                    key_fingerprint="0123456789abcdef",  # fake old fp
                    created_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    updated_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    created_by_label="dashboard-alice",
                )
            )
        with caplog.at_level("WARNING"):
            result = await store.get_for_user("dashboard-alice")
        assert result == {}
        assert any(
            "bricked" in r.message and "dashboard-alice" in r.message
            for r in caplog.records
        ), "must log a warning identifying the bricked row"

    async def test_get_returns_partial_when_one_row_is_bricked(
        self, engine, key
    ):
        """[j] one bad row does not poison the rest of the user's creds."""
        new_fernet, new_fp = build_fernet_from_key(key)
        store = UserCredentialsStore(
            engine, fernet=new_fernet, key_fingerprint=new_fp
        )
        # Good row through the normal API.
        await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_BASE_URL",
            value="https://example.com",
            actor_label="dashboard-alice",
        )
        # Bricked row inserted manually.
        async with engine.begin() as conn:
            await conn.execute(
                insert(user_credentials).values(
                    user_label="dashboard-alice",
                    env_name="ANTHROPIC_API_KEY",
                    value_encrypted=new_fernet.encrypt(b"sk-bricked"),
                    key_fingerprint="deadbeefdeadbeef",
                    created_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    updated_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    created_by_label="dashboard-alice",
                )
            )
        result = await store.get_for_user("dashboard-alice")
        assert result == {"ANTHROPIC_BASE_URL": "https://example.com"}, (
            "good row must survive the bricked sibling"
        )

    async def test_list_bricked_returns_only_mismatched_rows(
        self, engine, key
    ):
        """[k] list_bricked filters to fp != current."""
        new_fernet, new_fp = build_fernet_from_key(key)
        store = UserCredentialsStore(
            engine, fernet=new_fernet, key_fingerprint=new_fp
        )
        await store.upsert(
            user_label="dashboard-alice",
            env_name="ANTHROPIC_API_KEY",
            value="sk-good",
            actor_label="dashboard-alice",
        )
        async with engine.begin() as conn:
            await conn.execute(
                insert(user_credentials).values(
                    user_label="dashboard-bob",
                    env_name="ANTHROPIC_API_KEY",
                    value_encrypted=new_fernet.encrypt(b"sk-bricked"),
                    key_fingerprint="deadbeefdeadbeef",
                    created_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    updated_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    created_by_label="dashboard-bob",
                )
            )
        bricked = await store.list_bricked()
        assert len(bricked) == 1
        assert bricked[0]["user_label"] == "dashboard-bob"
        assert bricked[0]["key_fingerprint"] == "deadbeefdeadbeef"

    async def test_get_skips_row_when_decrypt_raises_invalid_token(
        self, engine, key, caplog
    ):
        """[l] InvalidToken (tampered ciphertext) is treated like a brick.

        Even when the fingerprint *matches*, an InvalidToken can fire
        if the ciphertext bytes were corrupted in transit / storage.
        The store must not raise; it must skip + log so the rest of
        the user's submit can proceed.
        """
        new_fernet, new_fp = build_fernet_from_key(key)
        store = UserCredentialsStore(
            engine, fernet=new_fernet, key_fingerprint=new_fp
        )
        # Insert a row whose fingerprint matches but ciphertext is garbage.
        async with engine.begin() as conn:
            await conn.execute(
                insert(user_credentials).values(
                    user_label="dashboard-alice",
                    env_name="ANTHROPIC_API_KEY",
                    value_encrypted=b"not-valid-fernet-bytes",
                    key_fingerprint=new_fp,
                    created_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    updated_at=__import__("datetime").datetime.now(
                        __import__("datetime").UTC
                    ),
                    created_by_label="dashboard-alice",
                )
            )
        with caplog.at_level("WARNING"):
            result = await store.get_for_user("dashboard-alice")
        assert result == {}
        assert any(
            "InvalidToken" in r.message
            or "Fernet.decrypt" in r.message
            for r in caplog.records
        )


class TestAdminOverride:
    async def test_admin_override_keeps_created_by_label_as_admin(self, store):
        """[m] After an admin upserts on bob's behalf, the row's
        ``created_by_label`` reflects the admin — supports B.8.3.g.

        The Plan v3 contract: ``created_by_label`` always tracks the
        most-recent writer, which is precisely what the audit story
        needs ("last touched by admin X"). The dashboard surfaces
        this so bob can see when an admin modified one of his rows.
        """
        await store.upsert(
            user_label="dashboard-bob",
            env_name="ANTHROPIC_API_KEY",
            value="sk-bob-self-set",
            actor_label="dashboard-bob",
        )
        before = (await store.list_for_user("dashboard-bob"))[0]
        assert before["created_by_label"] == "dashboard-bob"

        await store.upsert(
            user_label="dashboard-bob",
            env_name="ANTHROPIC_API_KEY",
            value="sk-set-by-admin",
            actor_label="dashboard-admin",
        )
        after = (await store.list_for_user("dashboard-bob"))[0]
        assert after["created_by_label"] == "dashboard-admin", (
            "admin override must overwrite created_by_label so the audit "
            "trail shows the most-recent writer"
        )
        # Value must reflect the admin's write too.
        result = await store.get_for_user("dashboard-bob")
        assert result == {"ANTHROPIC_API_KEY": "sk-set-by-admin"}

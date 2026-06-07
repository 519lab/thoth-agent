"""Tests for _resolve_requests_verify() env var precedence.

Verifies that custom provider `/models` fetches honour the three supported
CA bundle env vars (HERMES_CA_BUNDLE, REQUESTS_CA_BUNDLE, SSL_CERT_FILE)
in the documented priority order, and that non-existent paths are
skipped gracefully rather than breaking the request.

No filesystem or network I/O required — we use tmp_path to create real
CA bundle stand-in files and monkeypatch env vars.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from agent.model_metadata import _resolve_requests_verify


_CA_ENV_VARS = ("HERMES_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")


@pytest.fixture
def clean_env(monkeypatch):
    """Clear all three SSL env vars AND neutralize the OS system-store
    fallback so env-precedence assertions are deterministic regardless of
    the host's installed CA bundles."""
    for var in _CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    import agent.model_metadata as mm

    monkeypatch.setattr(mm, "_SYSTEM_CA_BUNDLES", ())
    return monkeypatch


@pytest.fixture
def bundle_file(tmp_path: Path) -> str:
    """Create a placeholder CA bundle file and return its absolute path."""
    path = tmp_path / "ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n")
    return str(path)


class TestResolveRequestsVerify:
    def test_no_env_returns_true(self, clean_env):
        assert _resolve_requests_verify() is True

    def test_thoth_ca_bundle_returns_path(self, clean_env, bundle_file):
        clean_env.setenv("HERMES_CA_BUNDLE", bundle_file)
        assert _resolve_requests_verify() == bundle_file

    def test_requests_ca_bundle_returns_path(self, clean_env, bundle_file):
        clean_env.setenv("REQUESTS_CA_BUNDLE", bundle_file)
        assert _resolve_requests_verify() == bundle_file

    def test_ssl_cert_file_returns_path(self, clean_env, bundle_file):
        clean_env.setenv("SSL_CERT_FILE", bundle_file)
        assert _resolve_requests_verify() == bundle_file

    def test_priority_thoth_over_requests(self, clean_env, tmp_path, bundle_file):
        other = tmp_path / "other.pem"
        other.write_text("stub")
        clean_env.setenv("HERMES_CA_BUNDLE", bundle_file)
        clean_env.setenv("REQUESTS_CA_BUNDLE", str(other))
        assert _resolve_requests_verify() == bundle_file

    def test_priority_requests_over_ssl_cert_file(self, clean_env, tmp_path, bundle_file):
        other = tmp_path / "other.pem"
        other.write_text("stub")
        clean_env.setenv("REQUESTS_CA_BUNDLE", bundle_file)
        clean_env.setenv("SSL_CERT_FILE", str(other))
        assert _resolve_requests_verify() == bundle_file

    def test_nonexistent_path_falls_through(self, clean_env, tmp_path, bundle_file):
        missing = tmp_path / "does_not_exist.pem"
        clean_env.setenv("HERMES_CA_BUNDLE", str(missing))
        clean_env.setenv("REQUESTS_CA_BUNDLE", bundle_file)
        assert _resolve_requests_verify() == bundle_file

    def test_all_nonexistent_returns_true(self, clean_env, tmp_path):
        missing1 = tmp_path / "a.pem"
        missing2 = tmp_path / "b.pem"
        missing3 = tmp_path / "c.pem"
        clean_env.setenv("HERMES_CA_BUNDLE", str(missing1))
        clean_env.setenv("REQUESTS_CA_BUNDLE", str(missing2))
        clean_env.setenv("SSL_CERT_FILE", str(missing3))
        assert _resolve_requests_verify() is True

    def test_empty_string_env_var_ignored(self, clean_env, bundle_file):
        clean_env.setenv("HERMES_CA_BUNDLE", "")
        clean_env.setenv("REQUESTS_CA_BUNDLE", bundle_file)
        assert _resolve_requests_verify() == bundle_file


class TestResolveCaBundleSystemStore:
    """The system-store fallback: when no env var points at a bundle,
    resolve_ca_bundle() returns the OS trust store (a superset of certifi
    that carries internal CAs) so internal-CA endpoints verify with zero
    config. resolve_ca_bundle() returns None (not True) when nothing is
    found — the caller maps that to certifi."""

    def test_system_store_used_when_no_env(self, clean_env, bundle_file):
        from agent.model_metadata import resolve_ca_bundle
        import agent.model_metadata as mm

        clean_env.setattr(mm, "_SYSTEM_CA_BUNDLES", (bundle_file,))
        assert resolve_ca_bundle() == bundle_file
        # And the requests wrapper surfaces it too.
        assert _resolve_requests_verify() == bundle_file

    def test_env_override_beats_system_store(self, clean_env, tmp_path, bundle_file):
        from agent.model_metadata import resolve_ca_bundle
        import agent.model_metadata as mm

        sys_bundle = tmp_path / "system.pem"
        sys_bundle.write_text("stub")
        clean_env.setattr(mm, "_SYSTEM_CA_BUNDLES", (str(sys_bundle),))
        clean_env.setenv("HERMES_CA_BUNDLE", bundle_file)
        assert resolve_ca_bundle() == bundle_file  # env wins

    def test_nothing_found_returns_none(self, clean_env):
        from agent.model_metadata import resolve_ca_bundle

        # clean_env already sets _SYSTEM_CA_BUNDLES = ()
        assert resolve_ca_bundle() is None
        assert _resolve_requests_verify() is True  # wrapper maps None → True

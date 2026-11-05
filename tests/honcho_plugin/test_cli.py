"""Tests for plugins/memory/honcho/cli.py."""

from types import SimpleNamespace


class TestResolveApiKey:
    """Test _resolve_api_key with various config shapes."""

    def test_returns_api_key_from_root(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        assert honcho_cli._resolve_api_key({"apiKey": "root-key"}) == "root-key"

    def test_returns_api_key_from_host_block(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        cfg = {"hosts": {"thoth": {"apiKey": "host-key"}}, "apiKey": "root-key"}
        assert honcho_cli._resolve_api_key(cfg) == "host-key"

    def test_returns_local_for_base_url_without_api_key(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        cfg = {"baseUrl": "http://localhost:8000"}
        assert honcho_cli._resolve_api_key(cfg) == "local"

    def test_returns_local_for_base_url_env_var(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.setenv("HONCHO_BASE_URL", "http://10.0.0.5:8000")
        assert honcho_cli._resolve_api_key({}) == "local"

    def test_returns_empty_when_nothing_configured(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        assert honcho_cli._resolve_api_key({}) == ""

    def test_rejects_garbage_base_url_without_scheme(self, monkeypatch):
        """Obvious non-URL literals in baseUrl (typos) must not pass the guard."""
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        # Boolean literals, pure digits, and bare identifiers without
        # host-like punctuation are rejected.  Schemeless host:port-style
        # strings are accepted (see test_accepts_legacy_schemeless_host).
        for garbage in ("true", "false", "null", "1", "12345", "localhost"):
            assert honcho_cli._resolve_api_key({"baseUrl": garbage}) == "", \
                f"expected empty for garbage {garbage!r}"

    def test_rejects_non_http_scheme_base_url(self, monkeypatch):
        """file:// / ftp:// / ws:// schemes are rejected as non-HTTP Honcho URLs.

        Note: these DO contain ``.`` or ``:`` so they pass the schemeless
        host fallback.  That's acceptable — the Honcho SDK will still
        reject them when it tries to connect.  If tighter filtering is
        needed later, extend the lowered-literal blocklist or check the
        parsed scheme explicitly.
        """
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        # file:/// parses with scheme='file' but empty netloc, so the
        # http/https guard rejects; the schemeless fallback also rejects
        # because 'file:' starts with a known-non-http scheme prefix.
        # ftp://host/ parses with scheme='ftp', netloc='host' — the
        # http/https guard rejects but the schemeless fallback accepts
        # because 'ftp://host/' contains ':' and '.'.  Behaviour is
        # intentionally lenient: SDK errors out with clearer message.

    def test_accepts_https_base_url(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        assert honcho_cli._resolve_api_key({"baseUrl": "https://honcho.example.com"}) == "local"

    def test_accepts_legacy_schemeless_host(self, monkeypatch):
        """Legacy configs with schemeless host:port must not regress.

        Before scheme validation landed, ``baseUrl: "localhost:8000"`` passed
        the truthy check and flowed through to the SDK.  The lenient
        schemeless fallback preserves that behaviour so self-hosters with
        older configs don't see spurious "no API key configured" errors.
        The SDK itself still rejects malformed URLs at connect time.
        """
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "thoth")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        for legacy in ("localhost:8000", "10.0.0.5:8000", "honcho.local:8080", "host.example.com"):
            assert honcho_cli._resolve_api_key({"baseUrl": legacy}) == "local", \
                f"expected local sentinel for legacy schemeless {legacy!r}"


class TestCmdStatus:
    def test_reports_connection_failure_when_session_setup_fails(self, monkeypatch, capsys, tmp_path):
        import plugins.memory.honcho.cli as honcho_cli

        cfg_path = tmp_path / "honcho.json"
        cfg_path.write_text("{}")

        class FakeConfig:
            enabled = True
            api_key = "root-key"
            workspace_id = "thoth"
            host = "thoth"
            base_url = None
            ai_peer = "thoth"
            peer_name = "eri"
            recall_mode = "hybrid"
            user_observe_me = True
            user_observe_others = False
            ai_observe_me = False
            ai_observe_others = True
            write_frequency = "async"
            session_strategy = "per-session"
            context_tokens = 800
            dialectic_reasoning_level = "low"
            reasoning_level_cap = "high"
            reasoning_heuristic = True

            def resolve_session_name(self):
                return "thoth"

        monkeypatch.setattr(honcho_cli, "_read_config", lambda: {"apiKey": "***"})
        monkeypatch.setattr(honcho_cli, "_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_local_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_active_profile_name", lambda: "default")
        monkeypatch.setattr(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            lambda host=None: FakeConfig(),
        )
        monkeypatch.setattr(
            "plugins.memory.honcho.client.get_honcho_client",
            lambda cfg: object(),
        )

        def _boom(hcfg, client):
            raise RuntimeError("Invalid API key")

        monkeypatch.setattr(honcho_cli, "_show_peer_cards", _boom)
        monkeypatch.setitem(__import__("sys").modules, "honcho", SimpleNamespace())

        honcho_cli.cmd_status(SimpleNamespace(all=False))

        out = capsys.readouterr().out
        assert "FAILED (Invalid API key)" in out
        assert "Connection... OK" not in out

class TestWriteConfigPermissions:
    """honcho.json carries apiKey, so it must be written owner-only."""

    def test_write_config_is_owner_only(self, tmp_path, monkeypatch):
        import os
        import stat
        import sys

        import pytest

        if sys.platform == "win32":
            pytest.skip("POSIX permission bits")
        import plugins.memory.honcho.cli as honcho_cli

        monkeypatch.setattr("thoth_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("thoth_cli.config._is_container", lambda: False)
        path = tmp_path / "honcho.json"
        old_umask = os.umask(0o022)
        try:
            honcho_cli._write_config({"apiKey": "hk-test"}, path)
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_provider_save_config_keeps_api_key_owner_only(self, tmp_path, monkeypatch):
        """HonchoMemoryProvider.save_config rewrites the same honcho.json
        (the generic memory wizard calls it), so it must not re-expose apiKey."""
        import json
        import os
        import stat
        import sys

        import pytest

        if sys.platform == "win32":
            pytest.skip("POSIX permission bits")
        from plugins.memory.honcho import HonchoMemoryProvider

        monkeypatch.setattr("thoth_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("thoth_cli.config._is_container", lambda: False)
        path = tmp_path / "honcho.json"
        path.write_text(json.dumps({"apiKey": "hk-test"}), encoding="utf-8")
        os.chmod(path, 0o644)  # as written before the fix
        old_umask = os.umask(0o022)
        try:
            HonchoMemoryProvider().save_config({"workspace": "w"}, str(tmp_path))
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text(encoding="utf-8")) == {"apiKey": "hk-test", "workspace": "w"}

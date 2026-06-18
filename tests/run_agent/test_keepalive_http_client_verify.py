"""Regression guard: the keepalive httpx client must apply TLS ``verify`` on
the *transport*, not the Client.

``_build_keepalive_http_client`` passes an explicit
``transport=httpx.HTTPTransport(...)`` (for socket keepalives). httpx only
honors a Client-level ``verify`` when it builds the *default* transport — with
a custom transport, ``Client(verify=...)`` is silently ignored and the
transport keeps certifi. The first cut of the CA-trust fix put ``verify`` on
the Client, so it was a no-op unless ``SSL_CERT_FILE`` was also set (the ssl
default context reads that at the env level) — endpoints behind an internal CA
(e.g. a local model server on ``*.skynet.home``) failed with
``CERTIFICATE_VERIFY_FAILED`` surfaced as ``APIConnectionError``. This test
pins that the resolved CA context lands on the HTTPTransport, not the Client.
"""

import ssl

import httpx

from run_agent import AIAgent


def _spy_httpx(monkeypatch):
    seen = {}
    real_transport = httpx.HTTPTransport
    real_client = httpx.Client

    def spy_transport(*args, **kwargs):
        seen["transport_verify"] = kwargs.get("verify", "MISSING")
        return real_transport(*args, **kwargs)

    def spy_client(*args, **kwargs):
        seen["client_has_verify"] = "verify" in kwargs
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", spy_transport)
    monkeypatch.setattr(httpx, "Client", spy_client)
    return seen


def test_verify_context_lands_on_transport_not_client(monkeypatch):
    sentinel = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(
        "agent.model_metadata.resolve_ca_bundle", lambda: "/fake/ca.pem"
    )
    monkeypatch.setattr(ssl, "create_default_context", lambda **kw: sentinel)
    seen = _spy_httpx(monkeypatch)

    client = AIAgent._build_keepalive_http_client("https://model.internal/v1")

    assert client is not None
    # The resolved CA context is applied to the TRANSPORT...
    assert seen["transport_verify"] is sentinel
    # ...and NOT passed to the Client (where a custom transport ignores it).
    assert seen["client_has_verify"] is False


def test_no_ca_bundle_defers_to_certifi_on_transport(monkeypatch):
    """When nothing resolves, verify=True (certifi) is still passed to the
    transport — unchanged default behavior, just in the right place."""
    monkeypatch.setattr("agent.model_metadata.resolve_ca_bundle", lambda: None)
    seen = _spy_httpx(monkeypatch)

    client = AIAgent._build_keepalive_http_client("https://api.example.com/v1")

    assert client is not None
    assert seen["transport_verify"] is True
    assert seen["client_has_verify"] is False


def test_keepalive_failure_falls_back_to_ca_aware_client(monkeypatch):
    """#184: if the keepalive *transport* can't be built, TLS trust must NOT
    be lost. The builder must fall back to a plain client that still carries
    the resolved CA context — losing only socket keepalives, not internal-CA
    verification. The old code returned None here, dropping the OpenAI SDK to
    its certifi default and breaking internal-CA endpoints with a bare
    "Connection error.".
    """
    sentinel = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(
        "agent.model_metadata.resolve_ca_bundle", lambda: "/fake/ca.pem"
    )
    monkeypatch.setattr(ssl, "create_default_context", lambda **kw: sentinel)

    seen = {}
    real_client = httpx.Client

    def boom_transport(*args, **kwargs):
        raise RuntimeError("socket_options unsupported here")

    def spy_client(*args, **kwargs):
        seen["client_verify"] = kwargs.get("verify", "MISSING")
        # Fallback path uses the default transport, so no custom transport= kwarg.
        seen["client_has_transport"] = "transport" in kwargs
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", boom_transport)
    monkeypatch.setattr(httpx, "Client", spy_client)

    client = AIAgent._build_keepalive_http_client("https://model.internal/v1")

    # We still get a usable client...
    assert client is not None
    # ...and it carries the resolved CA context (verify lands on the Client,
    # honored because there's no custom transport on the fallback path).
    assert seen["client_verify"] is sentinel
    assert seen["client_has_transport"] is False


def test_resolve_tls_verify_returns_context_from_bundle(monkeypatch):
    """_resolve_tls_verify() builds an SSLContext from the resolved bundle."""
    sentinel = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(
        "agent.model_metadata.resolve_ca_bundle", lambda: "/fake/ca.pem"
    )
    monkeypatch.setattr(ssl, "create_default_context", lambda **kw: sentinel)

    assert AIAgent._resolve_tls_verify() is sentinel


def test_resolve_tls_verify_never_raises(monkeypatch):
    """Resolution failure degrades to certifi (True), never propagates."""
    def boom():
        raise OSError("trust store unreadable")

    monkeypatch.setattr("agent.model_metadata.resolve_ca_bundle", boom)

    assert AIAgent._resolve_tls_verify() is True

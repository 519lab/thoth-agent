# Langfuse Observability Plugin

This plugin ships bundled with Thoth but is **opt-in** — it only loads when
you explicitly enable it.

## Enable

```bash
pip install langfuse
thoth plugins enable observability/langfuse
```

Or check the box in the interactive `thoth plugins` UI.

## Required credentials

Set these in `~/.thoth/.env`:

```bash
THOTH_LANGFUSE_PUBLIC_KEY=pk-lf-...
THOTH_LANGFUSE_SECRET_KEY=sk-lf-...
THOTH_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Without the SDK or credentials the hooks no-op silently — the plugin fails
open.

## Verify

```bash
thoth plugins list                 # observability/langfuse should show "enabled"
thoth chat -q "hello"              # then check Langfuse for a "Thoth turn" trace
```

## Optional tuning

```bash
THOTH_LANGFUSE_ENV=production       # environment tag
THOTH_LANGFUSE_RELEASE=v1.0.0       # release tag
THOTH_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
THOTH_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
THOTH_LANGFUSE_DEBUG=true           # verbose plugin logging
```

## Disable

```bash
thoth plugins disable observability/langfuse
```

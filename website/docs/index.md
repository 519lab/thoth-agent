---
slug: /
sidebar_position: 0
title: "Thoth Agent Documentation"
description: "The self-improving AI agent — an MIT-licensed fork of Hermes (Nous Research), maintained by 519lab. A built-in learning loop that creates skills from experience, improves them during use, and remembers across sessions."
hide_table_of_contents: true
displayed_sidebar: docs
---

<div className="thoth-hero">
  <p className="thoth-hero__eyebrow">Self-improving · Postgres-native · Yours to run</p>

  <h1 className="thoth-hero__title">The agent with a <em>cognitive substrate</em></h1>

  <p className="thoth-hero__lede">
Thoth is a self-improving AI agent built on a <strong>five-layer memory substrate that consolidates rather than accumulates</strong>. It creates skills from experience, sharpens them in use, and builds a deepening model of who you are across every session — instead of forgetting when the context window closes.
</p>

  <div className="thoth-cta">
    <a className="thoth-btn thoth-btn--gold" href="/docs/getting-started/installation">Get Started →</a>
    <a className="thoth-btn thoth-btn--lapis" href="https://github.com/519lab/thoth-agent">View on GitHub</a>
  </div>

  <div className="substrate-strata">
    <p className="substrate-strata__cap">The five-layer substrate · L0 → L4</p>
    <div className="stratum stratum--l0"><span className="stratum__id">L0</span><span className="stratum__name">Perception</span><span className="stratum__role">every event, captured</span></div>
    <div className="stratum stratum--l1"><span className="stratum__id">L1</span><span className="stratum__name">Entities</span><span className="stratum__role">facts & relationships</span></div>
    <div className="stratum stratum--l2"><span className="stratum__id">L2</span><span className="stratum__name">Associations</span><span className="stratum__role">a weighted graph</span></div>
    <div className="stratum stratum--l3"><span className="stratum__id">L3</span><span className="stratum__name">Patterns</span><span className="stratum__role">generalizations</span></div>
    <div className="stratum stratum--l4"><span className="stratum__id">L4</span><span className="stratum__name">Self-model</span><span className="stratum__role">what it knows it knows</span></div>
  </div>
</div>

An MIT-licensed fork of [Hermes](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com), maintained by [519lab](https://github.com/519lab) — rebuilt around the cognitive substrate.

## Install

**Linux / macOS / WSL2**

```bash
curl -fsSL https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.sh | bash
```

**Windows (native, PowerShell)** — *early beta, [details →](/docs/user-guide/windows-native)*

```powershell
iex (irm https://raw.githubusercontent.com/519lab/thoth-agent/main/scripts/install.ps1)
```

**Android (Termux)** — same curl one-liner as Linux; the installer auto-detects Termux.

See the full **[Installation Guide](/docs/getting-started/installation)** for what the installer does, the per-user vs root layout, and Windows-specific notes.

## What is Thoth Agent?

It's not a coding copilot tethered to an IDE or a chatbot wrapper around a single API. It's an **autonomous agent** that gets more capable the longer it runs. It lives wherever you put it — a $5 VPS, a GPU cluster, or serverless infrastructure (Daytona, Modal) that costs nearly nothing when idle. Talk to it from Telegram while it works on a cloud VM you never SSH into yourself. It's not tied to your laptop.

## Quick Links

| | |
|---|---|
| 🚀 **[Installation](/docs/getting-started/installation)** | Install in 60 seconds on Linux, macOS, WSL2, or native Windows (early beta) |
| 📖 **[Quickstart Tutorial](/docs/getting-started/quickstart)** | Your first conversation and key features to try |
| 🗺️ **[Learning Path](/docs/getting-started/learning-path)** | Find the right docs for your experience level |
| ⚙️ **[Configuration](/docs/user-guide/configuration)** | Config file, providers, models, and options |
| 💬 **[Messaging Gateway](/docs/user-guide/messaging)** | Set up Telegram, Discord, Slack, WhatsApp, Teams, or more |
| 🔧 **[Tools & Toolsets](/docs/user-guide/features/tools)** | 70+ built-in tools and how to configure them |
| 🧠 **[Memory System](/docs/user-guide/features/memory)** | Persistent memory that grows across sessions |
| 📚 **[Skills System](/docs/user-guide/features/skills)** | Procedural memory the agent creates and reuses |
| 🔌 **[MCP Integration](/docs/user-guide/features/mcp)** | Connect to MCP servers, filter their tools, and extend Thoth safely |
| 🧭 **[Use MCP with Thoth](/docs/guides/use-mcp-with-thoth)** | Practical MCP setup patterns, examples, and tutorials |
| 🎙️ **[Voice Mode](/docs/user-guide/features/voice-mode)** | Real-time voice interaction in CLI, Telegram, Discord, and Discord VC |
| 🗣️ **[Use Voice Mode with Thoth](/docs/guides/use-voice-mode-with-thoth)** | Hands-on setup and usage patterns for Thoth voice workflows |
| 🎭 **[Personality & SOUL.md](/docs/user-guide/features/personality)** | Define Thoth's default voice with a global SOUL.md |
| 📄 **[Context Files](/docs/user-guide/features/context-files)** | Project context files that shape every conversation |
| 🔒 **[Security](/docs/user-guide/security)** | Command approval, authorization, container isolation |
| 💡 **[Tips & Best Practices](/docs/guides/tips)** | Quick wins to get the most out of Thoth |
| 🏗️ **[Architecture](/docs/developer-guide/architecture)** | How it works under the hood |
| ❓ **[FAQ & Troubleshooting](/docs/reference/faq)** | Common questions and solutions |

## Key Features

<div className="thoth-cards">
  <div className="thoth-card">
    <div className="thoth-card__glyph">🔄</div>
    <div className="thoth-card__title">A closed learning loop</div>
    <p className="thoth-card__body">Agent-curated memory with periodic nudges, autonomous skill creation, skill self-improvement during use, Postgres full-text cross-session recall with LLM summarization, and <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">🧠</div>
    <div className="thoth-card__title">A five-layer substrate</div>
    <p className="thoth-card__body">Perception → entities → associations → patterns → self-model on Postgres + pgvector, maintained by a roster of always-on sub-agents. Memory that <strong>consolidates, not accumulates</strong>.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">🌍</div>
    <div className="thoth-card__title">Runs anywhere</div>
    <p className="thoth-card__body">Six terminal backends — local, Docker, SSH, Daytona, Singularity, Modal. Daytona and Modal hibernate when idle, costing nearly nothing.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">💬</div>
    <div className="thoth-card__title">Lives where you do</div>
    <p className="thoth-card__body">20+ platforms from one gateway — Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Email, SMS, Microsoft Teams, Home Assistant, and more.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">⏱️</div>
    <div className="thoth-card__title">Scheduled automations</div>
    <p className="thoth-card__body">Built-in cron that delivers results to any connected platform on a schedule you set.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">⛓️</div>
    <div className="thoth-card__title">Delegates &amp; parallelizes</div>
    <p className="thoth-card__body">Spawn isolated subagents for parallel workstreams. Programmatic Tool Calling via <code>execute_code</code> collapses multi-step pipelines into single inference calls.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">🧩</div>
    <div className="thoth-card__title">Open-standard skills</div>
    <p className="thoth-card__body">Compatible with <a href="https://agentskills.io">agentskills.io</a> — skills are portable, shareable, and community-contributed through the Skills Hub.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">🔌</div>
    <div className="thoth-card__title">MCP &amp; full web control</div>
    <p className="thoth-card__body">Connect any MCP server, and search, extract, browse, see (vision), generate images, and speak (TTS) across the open web.</p>
  </div>
  <div className="thoth-card">
    <div className="thoth-card__glyph">🔬</div>
    <div className="thoth-card__title">Research-ready</div>
    <p className="thoth-card__body">Batch processing, trajectory export, and RL training with Atropos — built by model trainers, forked from Hermes by <a href="https://nousresearch.com">Nous Research</a> (the lab behind the Hermes, Nomos, and Psyche models).</p>
  </div>
</div>

## For LLMs and coding agents

Machine-readable entry points to this documentation:

- **[`/llms.txt`](/llms.txt)** — curated index of every doc page with short descriptions. ~17 KB, safe to load into an LLM context.
- **[`/llms-full.txt`](/llms-full.txt)** — every doc page concatenated into a single markdown file for one-shot ingestion. ~1.8 MB.

Both files also resolve at `/docs/llms.txt` and `/docs/llms-full.txt`. Generated fresh on every deploy.

# Substrate-Driven Self-Improvement (the Forge) — Design

> **For Hermes:** This is a design spec, not yet a task-by-task plan. Build it
> phase-by-phase (Tier 1 first); split each phase into reviewed tasks with the
> subagent-driven-development skill before implementing. The whole point of this
> system is that it is *gated* — do not weaken or skip the gate to ship faster.

**Goal:** Let Hermes propose improvements to *itself* — new skills, new tools,
and eventually edits to its own codebase — driven by needs it discovers in its
own memory, without ever editing the code it is currently running and without
being able to remove its own safety limits.

**Architecture:** The substrate (L0→L4) is already a self-model; today it
self-maintains its *knowledge* (the Curator embeds, merges, decays, and releases
L1/L3/L4 — see `substrate/l3/store.py`). Self-improvement extends that same
"detect → propose → validate → apply" loop from *data* to *capability and code*.
Work happens in a **quarantined clone (the Forge)**, never the running install;
it reaches the live system only through a fixed **gate** (plan → implement →
deterministic CI → frontier-model evaluator → human approve → merge → deploy).
A mechanically-enforced **protected core** the agent cannot self-modify keeps the
gate, the evaluator, and the safety rails out of reach.

**Tech Stack:** Existing substrate (pgvector L0–L4, `substrate_telemetry`),
skills system (`skills/`), tool/MCP plugins (`plugins/`, `tools/`), the auxiliary
model client (Claude/GPT via `auxiliary.*` config), git worktrees (`--worktree`),
the test suite (`tests/`), Alembic migrations, GitHub PRs + CI, user systemd units.

---

## 1. The signal — what triggers a proposal

"Based on the user's needs" is not aspirational; the detector already exists. The
substrate accumulates the model that drives *what* to build:

- **L3 patterns** surface *recurring* structure — "Greg repeatedly does X" is
  exactly the "repeated / important / useful" trigger. A high-salience,
  frequently-reinforced pattern about a manual task is a candidate need.
- **L4 + `substrate_telemetry`** are the self-assessment — where the agent is
  failing, slow, or clumsy. A persistent coherence/friction signal is a candidate
  *self*-improvement.

**Gating the trigger (reuse `PatternFinder._should_run` discipline):** a proposal
fires only when the underlying signal is *new and corroborated*, not every tick.
Require (a) a minimum salience/recurrence threshold, (b) the signal is stable
across multiple observations (not a one-off), and (c) no open proposal already
covers it. This is the change-gate we built for the generators, applied to
self-improvement so it doesn't thrash.

**Signal integrity is step zero.** We watched Hermes draw a *wrong* self-conclusion
from the broken `backlog_ratio` metric (issue #107). An agent that acts on a bad
self-model improves itself in the wrong direction. So a proposal trigger must cite
its grounding (which L1/L3 rows, which telemetry) and that grounding is part of
what the evaluator checks. Issue #109 (L1-grounded re-derivation / contradiction
resolution) is therefore a *prerequisite-class* improvement: it hardens the very
signal this system depends on, and is the natural first thing for Hermes to fix
about itself.

## 2. Tiers of self-improvement (build in this order)

| Tier | Improves | Mechanism | Risk | Entry |
|---|---|---|---|---|
| **0. Knowledge** | what it knows | Curator (L0–L4) | low | **done / live** |
| **1. Skills** | what it can do (recipes) | author a bundled skill in `skills/` | low–med | **start here** |
| **2. Tools** | what it can do (new code) | write a tool / MCP server in `tools/`+`plugins/` | med–high | after Tier 1 trusted |
| **3. Self-code** | itself | edit the hermes-agent codebase via PR | high | last; strictest gate |

A **skill is a procedure** (markdown + prompt) that orchestrates tools the agent
*already* has — far smaller blast radius than codegen, which is why Tier 1 is the
entry point. Tiers 2–3 reuse the same Forge + gate with progressively stricter
review. Tier 3 is powerful and we *demonstrated its loop all of this session*
(diagnose from L4 → branch → code → test → PR), so it is feasible — but it stays
human-gated forever.

## 3. The Forge — quarantined work area

The agent must never edit the code it is running. Topology mirrors what we already
live (dev checkout vs. deploy clone), formalized as a third clone:

- **Forge clone:** e.g. `~/.hermes/forge/hermes-agent`, tracking the same upstream.
  The agent **`git pull`s before every task** (drift bit us this session — I worked
  37 commits behind main once); all work is on a branch.
- **Per-task git worktree** (`--worktree`) inside the Forge so concurrent or failed
  attempts don't pollute each other; auto-cleaned if unchanged.
- **Sandbox DB only.** Validation runs the suite against the throwaway
  `postgres-test` container (:5433) — **never prod (:5434)**. Getting this wrong is
  the easy, dangerous mistake.
- **Scoped credentials.** The Forge can *open PRs but not merge* (bot-style token).
  The only path to the live system is human merge + `hermes update`.

**Provisioned at setup**, not improvised: `hermes setup` (or a `hermes forge init`
subcommand) creates the clone, its own venv, the sandbox DB, and the scoped
credentials. The Forge is a full sandboxed dev environment, not just a directory.

Running install (`~/.hermes/hermes-agent`) and Forge **only ever meet through the
gate**: agent writes in the Forge → PR → gate → merge → `hermes update` pulls it
live. They never touch directly.

## 4. The gate — plan → CI → evaluator → human

```
L3/L4/telemetry signal (gated, corroborated)
   → agent writes a PLAN (intent, files, expected tests, rollback, grounding)
   → Forge worktree: implement
   → run suite on sandbox DB (:5433)
   → CI: deterministic (tests / lint / typecheck / migration check)   [hard gate]
   → frontier-model EVALUATOR (diff-vs-plan + guardrail rubric)        [judgment]
   → human approve                                                     [final]
   → merge → hermes update → live
   → telemetry tags the change (trigger, plan, verdict, approver) for audit
```

### 4.1 Plan-first is itself a control
The agent writes a **structured plan before coding**: intent, files it will touch,
tests it expects to add/pass, rollback, and the **grounding** (which substrate rows
/ telemetry justify this). The plan is the contract. It makes scope explicit and
makes "the diff did something other than the stated intent" *detectable*.

### 4.2 The evaluator (Claude/GPT as judge)
A separate, **stronger and different** model than the generator (uncorrelated
failure modes). It is a Critic for *code changes* — architecturally the same shape
as the existing L4 Critic / `critic.assessed` telemetry, but over diffs. It checks:

- **diff-vs-plan** — did it do what the plan said, and *nothing more*? (catches
  scope creep, hidden changes)
- **guardrail rubric** — did it touch a protected path, weaken a permission, delete
  or weaken a test, add an unexpected network/secret/file-exfil call, or broaden the
  agent's own latitude?

The evaluator treats the agent's plan and diff as **untrusted input** (Sentinel
mindset): the thing being judged must not be able to prompt-inject the judge into
approving.

### 4.3 The hard rule: the LLM judge is defense-in-depth, never load-bearing
The evaluator is fallible and jailbreakable, so it **layers on top of** the
deterministic gate, never replaces it:

> deterministic CI (can't be argued out of a red test) → LLM evaluator (judgment)
> → human approve (final merge).

If the LLM judge alone can approve a change, you've built a system that can be
*talked* into modifying itself. CI and the human are the gates that hold; the
evaluator catches the class of problems tests can't express (intent, subtle scope).

## 5. The protected core — what the agent cannot self-modify

The one failure mode that matters is the agent bootstrapping away its own limits.
So an immutable set is **enforced mechanically** (a protected-path / CODEOWNERS
check in CI that *rejects any diff touching it* without explicit heightened human
override — not "the evaluator should notice"):

- the gate pipeline + Forge setup code
- the evaluator's rules / rubric / prompt
- the human-approval requirement and the test requirement themselves
- safety, auth, permission, and sandbox code
- this document's guardrail contract

The agent may *propose* changes to its own protected core, but those get the
strictest path (human-only review, never the LLM-judge-only lane) — because there
the evaluator would be judging changes to its own judge.

## 6. Provenance & reversibility

- Every self-change is **PR-based + checkpointed** → fully reversible.
- Each merged change is **tagged in telemetry**: the triggering signal, the plan,
  the evaluator verdict, the human approver — so self-modification is auditable and
  drift is traceable back to its cause.

## 7. Phased rollout

1. **Tier 1, human-in-the-loop, no autonomy.** Detection (L3/L4 query) → agent
   drafts a skill into `skills/` → **you review/approve** → installed. Goal: make
   the *detect → draft → approve* loop trustworthy. Measure: are the proposals
   actually useful, and is the grounding sound?
2. **Add the evaluator** to the Tier-1 loop (diff/skill-vs-plan + guardrails) as
   advisory, then as a gate, while the human still approves everything.
3. **Tier 2 (tools).** Same gate, capability-scoped tools (narrow permission, not
   blanket shell), sandbox-tested.
4. **Tier 3 (self-code).** Only after 1–3 are trusted. The protected core is
   mandatory before this tier exists. #107 (a self-diagnosed flaw) and #109 are the
   natural first targets.

## 8. Open questions / risks

- **Where authored skills live & how they load** — `skills/` vs a quarantined
  `proposed-skills/` staging dir pending approval; hot-load vs restart.
- **Evaluator model + cost** — which frontier model; per-proposal cost is fine
  given proposals are infrequent and high-stakes.
- **Trigger thresholds** — salience/recurrence/stability bar before a proposal
  fires; tunable like the merge distances (`THOTH_SUBSTRATE_MERGE_*`).
- **Autonomy ceiling** — does Tier 3 ever auto-merge low-risk changes (e.g. docs),
  or is human merge permanent? Default: permanent.
- **Prerequisite:** #109 (signal integrity) should land before any tier acts on
  L4-derived self-flaws.

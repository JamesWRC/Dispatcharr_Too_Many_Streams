# Too Many Streams — design & analysis docs

This folder documents the investigation and design work on the `too_many_streams`
Dispatcharr plugin (June 2026). All Dispatcharr-internals facts were read from the
**deployed production container** (`jflix_dispatcharr`, Dispatcharr **0.21.1**,
`apps.proxy.ts_proxy` namespace) — not from memory or from any other checkout.

## Documents

| Doc | What it is |
|---|---|
| [ANALYSIS-resource-investigation.md](ANALYSIS-resource-investigation.md) | Why the plugin was burning ~3 CPU cores, how it was diagnosed, and the fixes applied (the `-re` runaway, the zombie leak, the directory-fallback DB cost). |
| [REFERENCE-dispatcharr-internals.md](REFERENCE-dispatcharr-internals.md) | Verified reference for how Dispatcharr 0.21.1 selects streams, accounts for connection slots, switches a live channel, and serves the placeholder card — with `file:line` citations. |
| [DECISIONS-startup-stream.md](DECISIONS-startup-stream.md) | Decision record for the "Starting up" stream feature: options weighed, the slot-leak false alarm, the workflow-hallucination episode, and the final locked decisions. |
| [DESIGN-tms-managed-streams.md](DESIGN-tms-managed-streams.md) | The forward-looking design: a registry of dynamic + static managed streams, the "always card then switch" startup flow, leak-free slot accounting, and the phased implementation plan. |

## Current state of the code (June 2026)

**Shipped this session (local working tree + rebuilt `too_many_streams.zip`, NOT yet deployed to prod):**
- `-re` pacing on the placeholder encoder — the runaway from ~3 cores to ~1%.
- `-threads 1` on the software encoder; `wait()` after `kill()` (no more zombie ffmpegs).
- Directory-fallback now sorts/limits in SQL instead of loading ~2806 `Channel` rows.

See [ANALYSIS-resource-investigation.md](ANALYSIS-resource-investigation.md) for details and verification.

**Designed but not yet built:** the multi-managed-stream rework (registry, static
"Starting up" card, always-card-then-switch). See
[DESIGN-tms-managed-streams.md](DESIGN-tms-managed-streams.md) §8 for the phased plan
(Phase 0 — renderer decoupling — is the safe, invisible starting point).

## A note on provenance

During the design phase a multi-agent workflow **hallucinated** a non-existent
Dispatcharr "v0.26.0 / `live_proxy`" codebase with fabricated line numbers and
"corrected" verified facts with invented ones. Every Dispatcharr citation in these
docs was (re-)read from the running 0.21.1 container. Treat any agent-supplied
framework internals as unverified until read from the box.

# Too Many Streams — design & analysis docs

This folder documents the investigation and design work on the `too_many_streams`
Dispatcharr plugin (June–August 2026). All Dispatcharr-internals facts were read from
the **deployed production container** (`jflix_dispatcharr`, Dispatcharr **0.21.1**,
`apps.proxy.ts_proxy` namespace) — not from memory or from any other checkout.

## Documents

| Doc | What it is |
|---|---|
| [ANALYSIS-resource-investigation.md](ANALYSIS-resource-investigation.md) | Why the plugin was burning ~3 CPU cores, how it was diagnosed, and the fixes applied (the `-re` runaway, the zombie leak, the directory-fallback DB cost). |
| [REFERENCE-dispatcharr-internals.md](REFERENCE-dispatcharr-internals.md) | Verified reference for how Dispatcharr 0.21.1 selects streams, accounts for connection slots, switches a live channel, and serves the placeholder card — with `file:line` citations. |
| [DECISIONS-startup-stream.md](DECISIONS-startup-stream.md) | Decision record for the "Starting up" stream feature: options weighed, the slot-leak false alarm, the workflow-hallucination episode, the locked D1/D2 — and **D3**, which deferred them in favour of health-first selection. |
| [DESIGN-tms-managed-streams.md](DESIGN-tms-managed-streams.md) | The forward-looking design (**v3, Aug 2026**): health-first stream selection, card-on-all-bad, and the deferred always-card-then-switch design in Appendix A. |

## Current state of the code

**Shipped, deployed (August 2026, plugin v2.2.4 — commit `0fb8728`):**
- Cold fall-to-card cut from 12.8–47.3 s (sometimes failing outright at 56 s) to
  **~4–6 s**, and off the GPU entirely — a dedicated `TMS Card (copy, fast probe)`
  StreamProfile, the card raised to 10 fps, and the image rendered before the encoder
  starts. See the `tms-card-latency-probe` notes.

**Shipped, local tree only (June 2026, NOT deployed):**
- `-re` pacing on the placeholder encoder — the runaway from ~3 cores to ~1%.
- `-threads 1` on the software encoder; `wait()` after `kill()` (no more zombie ffmpegs).
- Directory-fallback now sorts/limits in SQL instead of loading ~2806 `Channel` rows.

See [ANALYSIS-resource-investigation.md](ANALYSIS-resource-investigation.md) for details and verification.

**Next up** — [DESIGN §7](DESIGN-tms-managed-streams.md) has the roadmap. Four
independent fixes first (a card-serving flag bug, a racy reservation, an impure
`is_streams_maxed` that stops channels, and the renderer decoupling), then
health-first selection. The multi-managed-stream rework and the "Starting up" card
are deferred to v3 — see Appendix A and D3.

## A note on provenance

During the design phase a multi-agent workflow **hallucinated** a non-existent
Dispatcharr "v0.26.0 / `live_proxy`" codebase with fabricated line numbers and
"corrected" verified facts with invented ones. Every Dispatcharr citation in these
docs was (re-)read from the running 0.21.1 container. Treat any agent-supplied
framework internals as unverified until read from the box.

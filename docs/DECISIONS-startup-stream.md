# Decision record — the "Starting up" stream feature

Captures *how* the startup-stream design was reached: the options weighed, the
analysis that overturned an early choice, the false alarm, and the workflow
episode. The resulting architecture lives in
[DESIGN-tms-managed-streams.md](DESIGN-tms-managed-streams.md); the framework facts
in [REFERENCE-dispatcharr-internals.md](REFERENCE-dispatcharr-internals.md).

## The idea

On each channel start, show a "Starting up…" card immediately while a quick
(<10 s) check finds a working source, instead of the viewer staring at a black /
buffering screen while Dispatcharr serially times out on dead streams. This is the
same delivery mechanism as the existing "Too Many Streams" card (a local MPEG-TS
HTTP stream injected as a `Stream` on channels), but for the *startup* phase.

## What made it feasible

The plugin already owns the decision point — `Channel.get_stream()` is overridden,
so the plugin decides which source a channel connects to. And Dispatcharr exposes
live mid-session switching (`ChannelService.change_stream_url`, backed by
owner-update / Redis-pubsub). So "show card now, swap to a real source later" is
mechanically supported. (See [REFERENCE](REFERENCE-dispatcharr-internals.md) §1, §4.)

## Decision 1 — trigger model

### Options
- **Real-first, card-on-failure** — pick a real source as today; show the card only
  when selection fails; a TTL'd card session auto-retries on the next tune. No live
  switch needed. Zero regression for healthy tunes.
- **Always card then switch** — every tune shows the card, a probe switches to a
  healthy source. Matches the original idea literally.
- **Hybrid** — probe with a short budget; card only if the probe runs long.

### The pivot, and the pivot-back
1. An adversarial design review flagged "always card then switch" as having a
   **guaranteed `profile_connections` slot leak** — "no DECR exists anywhere," so the
   probe's manual INCR would never be released. It recommended "real-first."
2. That blocker was **investigated on the box and found FALSE.** The review had only
   grepped the *plugin* repo. Dispatcharr core's `Channel.release_stream()`
   (models.py:533) is the DECR owner and releases exactly the keys a switch writes.
   (See [REFERENCE](REFERENCE-dispatcharr-internals.md) §3.) **No leak.**
3. With the blocker gone, the user chose **always card then switch** (D1).

### The real hazard that *was* found
The switch path **can** double-INCR: `change_stream_url → update_url
(stream_manager.py:1083) → update_stream_profile (models.py:651)` increments
`profile_connections` unless `current == new`. **Mitigation:** write
`stream_profile:{S}=P` *before* calling `change_stream_url`, so it early-returns.
This was missed by the initial human analysis and correctly surfaced by the review
(albeit with hallucinated citations — see below); it was then verified by reading
the real functions.

### Decision (2026-06-14 — later deferred, see D3)
**D1 = "Always card then switch."** Accepted tradeoffs, with mitigations:
- *Healthy-tune regression* (card encode + downstream nvenc + a card→real rebuffer on
  every cold tune) → a **known-good fast-path** (a Redis `tms:health` read) lets warm
  channels skip the card entirely.
- *NVENC-session wall* on a failover wave (the fast-path gives no relief there) → a
  `tms:card_inflight` cap that returns a graceful 503 beyond the GPU's concurrent-
  NVENC ceiling.
- *Autonomous proxy failover* racing the probe → static card + a `channel_stream ==
  CARD` re-check before reserving + a generation token.

> **Superseded 2026-08-12 by D3.** D1 is deferred, not cancelled — the design and
> its accounting analysis survive in [DESIGN Appendix A](DESIGN-tms-managed-streams.md).
> The NVENC-wall mitigation above is **obsolete**: the August copy-profile fix means
> card viewers cost zero GPU sessions, so `tms:card_inflight` and its viewer-facing
> 503s are dropped outright.

## Decision 2 — card placement

### Options
- **Programmatic-only** (no `ChannelStream` row) — safest; the override returns the
  card by id; nothing can mis-select it. But it does **not** literally "add a stream
  to all channels," and isn't visible in the channel UI.
- **Membership row at a high order (9998)** — a real `ChannelStream` on every channel,
  just above the dynamic card at 9999, visible/queryable, with apply/remove parity.
  Safe **iff** the selection loop hard-excludes all managed-card stream ids.

The order-**0** variant was rejected outright: the selection loop reserves the first
order-sorted stream with a free slot and doesn't exclude cards, so an order-0 card
(profile 1, always "free") would always win — every channel would stick on the card.

### Decision
**D2 = membership row at order 9998**, with the loop hard-excluding
`registry.card_stream_ids()`. This matches the user's literal "add a TMS: startup
stream on all channels," gives UI visibility and a terminal fallback, and is made
safe by the exclusion. (See [DESIGN Appendix A.4](DESIGN-tms-managed-streams.md).)

> **Deferred 2026-08-12 with D1** — the 9998 row exists only to deliver D1's card.
> Two things learned since, both recorded in the design:
> - The card's `Stream.name` **must** contain `TooManyStreams`. betterfailover is
>   enabled on this box and leaves today's card alone only via a case-insensitive
>   substring match on that string; a card named `TMS: startup stream` would be
>   classified as a dead static image and failed over mid-probe.
> - The loop exclusion **cannot ship as a standalone refactor**. On prod the card at
>   9999 is served *by the loop reaching it* (profile 1, `max_streams=0`, always
>   free) and the maxed branch is dead code — so adding the exclusion first removes
>   the card's only working delivery path. Exclusion + working maxed branch + the
>   9998 row are one atomic change.

## Decision 3 — phasing (2026-08-12)

Two findings inverted D1's premise.

1. **The card is not instant.** Measured at **~4–6 s to first bytes**, and that
   residual is ts_proxy's own buffer fill — which a startup card would pay
   identically. "Show something immediately" is really "show something at ~4–6 s",
   against a cold tune that then pays a *second* buffer fill on the card→real
   switch. (See the `tms-card-latency-probe` notes and commit `0fb8728`.)
2. **v1 as specced didn't fix the complaint.** The recommended `probe_mode =
   capacity_only` reserves "the first source with a free slot" — precisely what
   today's loop already does. It does not detect dead streams. So Phase 5 would
   have shipped the card, the switch and a rebuffer, and then handed the viewer the
   same dead stream. The thing that actually fixes it — `byte_probe` and
   `tms:health` — was deferred to Phase 6.

The fix for "black screen while Dispatcharr times out on dead streams" is knowing a
source is dead **before** selecting it. That is a *selection* problem, and solving it
needs no card, no live switch and no rebuffer.

### Decision
**D3 = health-first phasing.** Ship selection intelligence before presentation
machinery:
- **v1** — a `tms:health` bad-source **demotion list** (demote, never exclude: an
  all-bad channel must still degrade to today's behaviour, not to nothing).
- **v2** — serve the *existing* card when every source is known bad. That is the one
  case where a card genuinely beats waiting, and it reuses the card path that already
  ships.
- **v3** — D1/D2, gated on measuring how often a tune finds nothing known-good.

**The reorder is also a de-risking.** v1 and v2 add **zero** new coupling to
Dispatcharr internals. D1 adds load-bearing dependencies on `change_stream_url` →
`update_url` → `update_stream_profile` → `release_stream` and on the INCR/DECR
ordering between them — and the double-INCR mitigation fails *silently*: it works
because `update_stream_profile` early-returns on `current == new`, which is an
implementation detail, not a contract. An upgrade that changes it leaks one slot per
switch with no error and no log. That is a fair price for a large win; it is not a
fair price for the win as now measured, so it is gated rather than assumed.

**Open, needs a box session:** where the failure signal comes from. The override
can't observe the failure itself — it returns a stream id and the failure happens
downstream in `StreamManager`. Candidates in
[DESIGN §2.3](DESIGN-tms-managed-streams.md); check first whether ts_proxy already
records this in Redis, in which case v1 is a pure read.

## The workflow episode (a process note)

Two multi-agent workflows were run for the design (a 13-agent generalize-the-plugin
pass, then an 8-agent switch-path design+verification pass). They produced genuinely
useful structure and caught the real double-INCR hazard. **But the second workflow
also hallucinated** a non-existent Dispatcharr "v0.26.0 / `live_proxy`" codebase with
fabricated line numbers (`release_stream@743`, `update_stream_profile@854`,
`manager.py:1164`, `live:channel:…` metadata) and asserted that the SSH-verified
`ts_proxy` / 0.21.1 facts were "stale."

The agents had **no box access** — they could not have read Dispatcharr source — so
those "corrections" were confabulated. The handling: treat the substantive findings
as hypotheses, re-verify each against the running container, keep what's true
(the double-INCR, autonomous failover), and discard the invented citations. Every
Dispatcharr fact in the final docs was (re-)read from the 0.21.1 box.

**Lesson:** multi-agent review is valuable for surfacing failure modes, but any
framework-internal `file:line` claim it produces must be verified against the actual
deployment before it enters a design. Don't let a confident agent overwrite a
first-hand reading.

## Status (2026-08-12)

**D3 supersedes the D1/D2 phasing.** D1 and D2 remain the design of record for a
future v3 and are preserved in [DESIGN Appendix A](DESIGN-tms-managed-streams.md);
they are deferred behind the measurement in DESIGN §7.1.

Current roadmap in [DESIGN §7](DESIGN-tms-managed-streams.md):

- **Now** — four independent fixes, no new coupling: the card-serving flag bug
  (`TooManyStreams.py:347` sets `_tms_serving_card = False` for the card too, which
  short-circuits the copy-profile lookup), the racy reservation (`:339-344`), the
  impure `is_streams_maxed` (`:290-291`, a predicate that stops channels), and the
  renderer decoupling.
- **v1** — health-first selection.
- **v1.5** — active probing, *blocked on a provider-ban tolerance decision*.
- **v2** — card-on-all-bad.
- **v3** — D1/D2, if §7.1 justifies it.

**Dropped outright:** the NVENC-session wall, `tms:card_inflight`, and the
viewer-facing 503 branch — obsoleted by the August copy-profile fix.

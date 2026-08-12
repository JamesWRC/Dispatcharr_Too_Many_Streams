# TMS — stream selection & managed-stream roadmap (v3)

> Revised **2026-08-12**. Supersedes v2 ("always card then switch", 2026-06-14).
>
> **What changed and why:** v2 put the whole card-and-live-switch machinery in v1
> and deferred the health probe to Phase 6. Two things since then inverted that.
> (1) The card was measured at **~4–6 s to first bytes** — it is not instant, and
> the startup card would pay the same ts_proxy buffer fill — so "show something
> immediately" is worth much less than v2 assumed. (2) v2's recommended v1
> `probe_mode=capacity_only` **does not detect dead streams**, which was the
> entire complaint; it reserves "the first source with a free slot", i.e. exactly
> what today's loop already does. v1 as specced would have shipped the card, the
> switch, and a rebuffer, and then handed the viewer the same dead stream.
>
> This revision leads with the thing that actually fixes the complaint —
> **knowing a source is bad before selecting it** — and defers the card/switch
> machinery until measurement shows a residual it is needed for.
>
> All Dispatcharr citations remain verified against the deployed box:
> **0.21.1, `apps.proxy.ts_proxy`**. (An earlier multi-agent pass hallucinated a
> "v0.26.0 / `live_proxy`" tree; those citations were discarded — see §9.)

---

## Decisions

- **D1 — "Always card then switch."** *Locked in v2, now **deferred**.* Not
  cancelled: the design and its hard-won accounting analysis are preserved in
  **Appendix A**, to be built only if §2's measurement shows a gap v1+v2 leave open.
- **D2 — Startup card as a `ChannelStream` row at order 9998.** Deferred with D1
  (it exists only to serve D1's card). See Appendix A and the §3.4 warning about
  why the loop exclusion it requires **cannot** be shipped standalone.
- **D3 (new) — Health-first phasing.** Ship *selection* intelligence before
  *presentation* machinery. v1 = a bad-source demotion list. v2 = serve the
  existing card when everything is known bad. v3 = D1, if still justified.

**Why the reorder is also a de-risking.** v1 and v2 add **zero** new coupling to
Dispatcharr internals — they touch only the existing `get_stream` override and
Redis keys the plugin already owns. D1 adds load-bearing dependencies on
`change_stream_url` → `update_url` → `update_stream_profile` → `release_stream`
and on the exact INCR/DECR ordering across four core functions. That last one
fails *silently*: the double-INCR mitigation works because
`update_stream_profile`'s `current == new` guard early-returns, so a Dispatcharr
upgrade that changes that guard produces a slow `profile_connections` leak with
no error and no log — you would find out when channels stop tuning. See §8.

---

## 1. The problem, restated precisely

A viewer tunes a channel. Dispatcharr's selection loop hands ts_proxy the first
source with a free connection slot, in `channelstream__order`. If that source is
dead, ts_proxy spends its full connect/read timeout on it before anything else
happens, then tries the next. The viewer watches black for the sum of those
timeouts.

Nothing in the current plugin knows a source is dead. `Channel.get_stream` picks
on **capacity**, never on **liveness**. That is the entire bug, and it is a
selection problem, not a presentation problem.

## 2. v1 — health-first selection (a demotion list)

### 2.1 The data

One Redis key family: **`tms:health:{stream_id}`**, written when a source is
observed to have failed, read on the tune path.

v2 specced a two-valued `ok`/`bad` map because `ok` gated its known-good
fast-path. With no card to skip, **v1 only needs the bad half**. Absence of a key
means "unknown", which is treated as neutral. This is a real simplification: no
`ok` writer, no `ok` staleness question, no "is 90 s old evidence still good".

| | value | TTL | meaning |
|---|---|---|---|
| `tms:health:{sid}` | failure count | `BAD_TTL`, backing off | recently observed dead |

**TTL and backoff.** `BAD_TTL` decides how long a dead source stays demoted. Too
short and we retry the dead thing on the next tune (the original complaint); too
long and a recovered source stays buried. Default **300 s**, doubling per
consecutive failure to a **1800 s** ceiling, reset on any successful use. The
key's value is the failure count, so backoff needs no second key.

### 2.2 The selection change — demote, never exclude

```python
card_id = TooManyStreams.get_card_stream_id()
streams = list(self.streams.all().order_by("channelstream__order"))
bad = TooManyStreams.get_bad_stream_ids([s.id for s in streams])   # one MGET
streams.sort(key=lambda s: s.id in bad)                            # stable partition
for stream in streams:
    ...  # unchanged from here
```

**Demotion, not exclusion, is the load-bearing choice.** If every source for a
channel is marked bad, exclusion leaves the channel with nothing to select and
we have invented a new failure mode. A stable partition degrades to exactly
today's behaviour in that case, and improves on it in every other case. There is
no "all sources filtered out" edge case to handle because it cannot arise.

Cost on the tune path: **one Redis `MGET`** over the channel's stream ids
(typically a handful). `Channel.get_stream` currently measures ~35 ms; this is
noise against that.

Note the sort must be **stable** and must run *after* the DB `order_by`, so
within the good group and within the bad group the operator's configured order
is preserved exactly.

### 2.3 The open question: where the failure signal comes from

This is the one genuinely undecided part of v1, and it **needs a box session** —
it depends on what ts_proxy does and logs when a source fails, which cannot be
determined from this repo.

The plugin's `get_stream` override cannot see the failure itself: it returns a
stream id and the failure happens later, downstream, in `StreamManager`.
Candidate signal sources, best first:

**(a) Read something ts_proxy already records.** Check this *first* — if ts_proxy
already tracks per-stream failures in Redis, v1 becomes a pure read and there is
nothing to write at all. Unverified; assume nothing until checked.

**(b) A logging observer on `ts_proxy.stream_manager`.** The plugin **already**
installs a filter on exactly this logger (`_TmsStreamInfoFilter`,
`src/TooManyStreams.py:56`), so the mechanism is proven in this codebase. Watch
for connection-failure lines ("Error opening input", "Connection timed out",
max-retries-exceeded) and mark the channel's current stream bad. Couples to log
*text*, which is fragile — but it fails **safe**: a message change means we stop
learning, never that we break a tune.

**(c) Infer from autonomous failover.** `get_alternate_streams`
(`url_utils.py:279`) is called by the proxy *only* when a stream stalled or
failed. If `channel_stream:{int}` changes from S to S′ without the plugin having
done it, S failed. Observable on the next `get_stream` at zero coupling cost, but
delayed and only fires when the proxy actually failed over.

**Recommendation: (a) if it exists, else (b), with (c) as a cheap always-on
supplement.** Design the writer behind one function —
`TooManyStreams.mark_stream_bad(stream_id)` — so the signal source is swappable
without touching selection.

### 2.4 Active probing — pending a decision

Opening upstream connections to sources nobody is watching would populate
`tms:health` proactively instead of only learning from real viewer failures.
It is strictly additive (same key, same reader) and needs a fleet-global
per-account Redis token bucket (`tms:probe_inflight:{account}` INCR/EXPIRE/DECR
— **not** a process-local semaphore, since `get_stream` runs in every web
worker).

**Not in v1**, pending James's call on provider-ban tolerance. Passive-only
learns more slowly but adds zero upstream load, which is why it is the default.

### 2.5 What v1 does and does not fix

- **Fixes:** any channel with at least one working source. Second and subsequent
  tunes go straight to something that works, skipping the dead-source timeouts.
- **Does not fix:** the *first* tune after a source dies (nothing knows yet — this
  is what §2.4's active probing would close), and a channel where **every** source
  is dead. That second case is v2.

## 3. v2 — serve the card when everything is known bad

When every source for a channel is currently demoted, the viewer is guaranteed a
full round of timeouts. That is the one case where showing a card genuinely beats
waiting — and it is a far narrower and better-justified trigger than v2's "always
card".

**It needs no new Dispatcharr coupling at all.** No `change_stream_url`, no live
switch, no double-INCR ordering invariant, no 9998 membership row, no
autonomous-failover race. It is the card path that already exists and already
works, fired on a different condition, with the next tune re-trying naturally
once the `BAD_TTL`s expire.

Prerequisite: the card-flag bug in §4.1 must be fixed first, or v2 serves the
card through the channel's nvenc profile and re-inherits the 12–47 s wait the
August work removed.

## 4. Build now — independent of all of the above

Each of these stands on its own merits and blocks nothing.

### 4.1 The card-serving flag is wrong on the fresh-selection path (bug)

`src/TooManyStreams.py:347` sets `self._tms_serving_card = False`
**unconditionally** for whatever the loop selected — including the card itself at
order 9999. On prod the loop *does* reach the card directly (account 1 / profile 1
/ `max_streams=0`, so it always has a free slot), so this fires on the real
fall-to-card path.

It matters because `_is_serving_card` (`:201`) returns early on
`flag is not None` — a `False` short-circuits **before** the Redis fallback at
`:209` ever runs. So `get_stream_profile` returns the channel's own profile
(prod: nvenc 1080p 7 Mbit) instead of `TMS Card (copy, fast probe)`, and the card
re-inherits exactly the slow-probe path the August fix removed.

Fix: set the flag from the selected id, matching the restore path at `:318`:

```python
self._tms_serving_card = (stream.id == TooManyStreams.get_card_stream_id())
```

**Needs box verification of the runtime consequence.** The code reading is
unambiguous, but ts_proxy retries `generate_stream_url` for ~3 s
(REFERENCE §2) and the retry hits the *restore* path, which sets the flag
correctly — so the fresh-selection call may or may not be the one that decides
the profile in practice. The August measurement showed no NVENC session on the
card, which suggests a retry does win at least sometimes. Measure before
claiming a user-visible impact; fix regardless.

### 4.2 The reservation is racy (bug)

`src/TooManyStreams.py:339-344` does `GET profile_connections < max` → `SET` →
`INCR`. Under a failover wave, concurrent tunes all read the same pre-INCR value
and over-admit past `max_streams`. Dispatcharr core gets this right
(`_check_and_reserve_profile_slot`, models.py ~408). Mirror it:

```python
def reserve_atomic(P, max_streams, r):
    if max_streams == 0:
        return True                              # unlimited, never INCRs
    n = r.incr(f"profile_connections:{P}")
    if n > max_streams:
        r.decr(f"profile_connections:{P}")       # rollback, net 0
        return False
    return True
```

### 4.3 `is_streams_maxed` is not a pure read (latent landmine)

`src/TooManyStreams.py:290-291` calls `add_stream_to_channel` /
`remove_stream_from_channel` from what reads as a predicate, and
`remove_stream_from_channel` does `ChannelService.stop_channel` +
`proxy_server.stop_channel` (`:262-263`). A predicate that stops channels.

It is **dead code on prod today** — the loop reaches the card at 9999 first, so
the maxed branch at `:353` is never taken — so this is latent, not active. It
becomes live the moment anything changes the card's order or excludes it from the
loop, at which point it is a fleet-stop across ~2806 channels. Make it a pure
read of `tms:maxed_out:{ch}`; membership belongs to the explicit apply/remove
actions.

### 4.4 Renderer decoupling (v2 Phase 0, unchanged)

Replace the process-global `PillowImageGen._last_active_uuids` with an instance
`_last_signature`, **and** have the channel hold one long-lived renderer instance
across updater iterations. The two are inseparable — doing only the first
regresses the dynamic card's change detection. Byte-identical output.

## 5. Deliberately dropped from the v2 design

- **The NVENC-session wall, `tms:card_inflight`, and the 503 branch.** v2 §5/§7
  budgeted one downstream nvenc transcode per card-served channel and capped
  concurrency to protect the GPU, returning 503s to viewers past the cap. The
  August `TMS Card (copy, fast probe)` profile means **card viewers cost zero GPU
  sessions**. The wall does not exist, so neither should the cap or the
  viewer-facing 503s it introduced. Removed regardless of which path is built.

## 6. Constraints discovered since v2

- **betterfailover naming collision (hard constraint if D1 is ever built).**
  betterfailover is enabled on this box and leaves today's card alone only via a
  case-insensitive substring match on `TooManyStreams` in the stream name. A
  startup card named `TMS: startup stream`, as v2 §3.1 specced, would **not**
  match — betterfailover's static-image detector would classify it as a dead
  source and fail the channel over, racing the TMS probe. Any second card must
  carry `TooManyStreams` in its `Stream.name`.

- **`live_proxy` does not exist.** 0.21.1 ships `ts_proxy` / `hls_proxy` /
  `vod_proxy`. The design is `ts_proxy`-specific throughout. This is already true
  of the shipped plugin (it overrides `Channel.get_stream`), so it is a question
  of *coupling depth*, not of kind — which is the §8 argument.

## 7. Roadmap

| | Scope | New Dispatcharr coupling |
|---|---|---|
| **Now** | §4.1 card-flag bug · §4.2 atomic reservation · §4.3 pure `is_streams_maxed` · §4.4 renderer decoupling | none |
| **v1** | §2 health-first selection (bad-list + demotion), signal source per §2.3 | none |
| **v1.5** | §2.4 active probing behind a per-account cap — *pending provider-ban decision* | outbound only |
| **v2** | §3 card-on-all-bad | none |
| **v3** | Appendix A (D1/D2) — **only if measurement shows a residual** | substantial (§8) |

Registry / multi-card / `card_stream_ids()` move to **v3**. They exist to support
a second card, and per §3.4 below the loop exclusion they require is actively
unsafe to ship early.

### 7.1 The measurement that gates v3

Once v1 is live, `tms:health` answers the question v2 could only guess at: **how
often does a tune find nothing known-good?** If that is rare, v3 buys little for
a lot of coupling. If it is common, build v3 with the health data already in
place — which is strictly better than v2's ordering, where the probe was being
built blind.

## 8. Why v3 is genuinely more expensive than it looks

v1 and v2 touch the existing override and Redis keys the plugin already owns. v3
adds hard dependencies on `change_stream_url` (channel_service.py:88) →
`update_url` (stream_manager.py:1060) → `update_stream_profile` (models.py:651) →
`release_stream` (models.py:533), on the `ts_proxy:channel:{uuid}:metadata` field
names, and on the INCR/DECR ordering *between* them.

The double-INCR mitigation is the sharp edge. It is correct today, but it is
correct *because* `update_stream_profile` early-returns when `current == new`.
That is not an API contract — it is an implementation detail of one function. If
an upgrade changes it, every switch leaks one `profile_connections` slot,
silently, with no error and no log, until profiles saturate and channels stop
tuning. A runtime assertion (§A.3.4.5) turns it from silent into loud, but cannot
prevent it.

That is an acceptable price for a large win. It is not an acceptable price for
the win as now measured — which is why it is gated on §7.1 rather than assumed.

## 9. Provenance

Every Dispatcharr citation here was read from the running `jflix_dispatcharr`
container (0.21.1). An earlier multi-agent design pass hallucinated a
"v0.26.0 / `live_proxy`" tree with fabricated line numbers
(`release_stream@743`, `update_stream_profile@854`, `manager.py:1164`,
`live:channel:…` metadata) and asserted the SSH-verified facts were stale. Those
are **not** used anywhere. See
[DECISIONS-startup-stream.md](DECISIONS-startup-stream.md) for the episode and
the lesson.

The one substantive finding that pass got right — the double-INCR via the switch
path — was kept, re-verified against the box, and is preserved in §A.3.4.5.

---

# Appendix A — the deferred "always card then switch" design (D1/D2)

> Preserved verbatim in substance from v2 §3.4/§3.6/§4. Build only if §7.1
> justifies it. Two edits applied: the `tms:card_inflight` cap and its 503 branch
> are removed per §5, and the betterfailover naming constraint (§6) is now
> mandatory.

## A.1 Managed-stream registry (v2 §3.1)

`ManagedStream` dataclass (`key`, `display_name`, `stream_name`, `http_path`,
`kind`, `m3u_account_id`, `apply_order`, `renderer`, `behavior`, …) in a process
-singleton `ManagedStreamRegistry` with `all()/enabled()/get(key)/by_path()/
by_stream_id()/card_stream_ids()`. Built-ins: `tms` (DYNAMIC, `/stream.ts` +
legacy `/`, order 9999) and `startup` (STATIC, order **9998**, disabled by
default).

**Naming (§6):** the startup card's `stream_name` **must** contain
`TooManyStreams` — e.g. `TooManyStreams: Starting up` — or betterfailover treats
it as a dead static image and fails the channel over mid-probe.

## A.2 Multi-stream StreamServer (v2 §3.2/§3.3)

Split into `ManagedStreamChannel` (one lazy encoder + broadcaster, updater for
dynamic only) and `StreamServer` (one `ThreadingHTTPServer` on `:1337`, path
routing, `reload()`). `/healthz` matched before `/` so it warms no encoder. The
host must keep a reference to the server (today it is fire-and-forget). Static
cards have no updater thread. `DynamicRenderer`'s self-exclusion must check **all**
`card_stream_ids()`, not just the `tms` URL, so a startup-card-parked channel
never leaks into the live grid.

## A.3 Runtime flow

### A.3.1 Identifier discipline

| Concern | Identifier | Key / call |
|---|---|---|
| Reservation | channel **int PK** | `channel_stream:{int}` → `stream_profile:{sid}` |
| Slot counter | m3u **profile id** | `profile_connections:{P}` |
| Switch + metadata | channel **UUID** | `change_stream_url`; `ts_proxy:channel:{uuid}:metadata` |
| TMS guards | channel **int PK** | `tms:starting:{int}`, `tms:switch_inflight:{int}` |
| Liveness | channel **UUID** | `proxy_server.check_if_channel_exists(uuid)` |
| Health | **stream id** | `tms:health:{sid}` |

The probe holds the `Channel` (both ids); never derive one from the other.

### A.3.2 Override ordering

```
0. no streams   -> (None, None, "No streams assigned to channel")
1. RESTORE      sid = GET channel_stream:{int}; pid = GET stream_profile:{sid}
                if sid in card_stream_ids(): terminal only while tms:starting exists
                                             (else re-arm — probe died)
                else return (sid, int(pid), None)
2. FAST-PATH    top non-card stream by order; if healthy and has a slot, reserve
                and return it — no card, no probe, no switch
3. ALWAYS-CARD  SET channel_stream:{int}=CARD EX; SET stream_profile:{CARD}=1 EX
                SET tms:starting:{int} <token> NX EX -> spawn probe (web process ONLY)
                return (CARD, 1, None)
4. NORMAL LOOP  by order, `continue` on any id in card_stream_ids()
5. maxed        existing tms branch, reading a PURE is_streams_maxed (§4.3)
```

### A.3.3 Leak-free accounting (Δ = net `profile_connections:{P}`)

| # | Event | Δ(P) |
|---|---|---|
| A | Cold tune → card (profile 1, unlimited) | 0 |
| A′ | Warm tune → fast-path reserve | +1 |
| B | Probe reserves S/P atomically (rollback on overflow) | +1 (0 on rollback) |
| C | Commit switch — keys written **before** `change_stream_url`, so `update_stream_profile` no-ops | 0 |
| D | Teardown on S/P — `release_stream` DECRs | −1 |
| E | Teardown on card | 0 |
| F | Nth concurrent viewer — restore, no write | 0 |
| G | Double `release_stream` — keys gone / HDEL'd | 0 |

Exactly one INCR (B, plugin) ↔ one DECR (D, `release_stream`). The card is
accounting-inert.

### A.3.4 The mandatory ordering, and the assertion that guards it

`change_stream_url(target_stream_id=S, m3u_profile_id=P)` → `update_url`
(stream_manager.py:1060) → `channel.update_stream_profile(P)` (line 1083 →
models.py:651), which **INCRs** `profile_connections:{P}` unless its
`current == new` guard fires.

**Write `SET channel_stream:{int}=S` and `SET stream_profile:{S}=P` strictly
before `change_stream_url`.** Then the guard fires and the plugin's manual INCR
is the only increment.

Per §8 this is an implementation detail, not a contract. Assert
`current_profile == P` immediately pre-switch and **log loudly** on mismatch — it
converts a silent slot leak into a visible one. Add a regression test.

### A.3.5 The probe (web/serving process only — never Celery)

```
P0 generation check: tms:starting token is mine, else ABORT
P1 bounded wait-ready (check_if_channel_exists); EXPIRE tms:starting each iter
P2 candidate by order, excluding card_stream_ids(); CAPACITY PRECHECK before any
   network; per-account fleet-global token bucket gates network
P3 re-check channel_stream:{int} == CARD (autonomous-failover guard); reserve
   atomically; write both keys with SWITCH_TTL
P4 re-check generation + client; (non-owner) HSET metadata STREAM_ID/M3U_PROFILE;
   change_stream_url(...); PERSIST both keys on success
P5 DEL guards; trigger_refresh()
```

Celery workers are `--autoscale=6,1` ephemeral — daemon threads there die on
recycle without running `finally`, so no `finally` may be load-bearing and every
guard key carries a TTL that self-heals.

### A.3.6 Races

- **Client disconnect mid-probe** — before P3's INCR, plain abort. After it,
  `ABORT_RELEASE_REAL`: atomic (Lua/WATCH) DECR of P *only if*
  `channel_stream:{int}` still equals S, deleting the keys in the same
  transaction, so it and `release_stream` cannot double-DECR.
- **Re-tune during probe** — `tms:starting` generation token; stale-token probes
  abort. Re-checked immediately before the irreversible publish.
- **Non-owner switch** — keys + metadata written before publish; a lost pubsub
  event degrades to "stayed on card", never to a leak or a wrong source.
- **Probe-thread death** — `tms:starting` TTL exceeds the full budget and
  self-heals; P3's keys are TTL'd until commit.
- **Autonomous failover** (`get_alternate_streams`, url_utils.py:279) — the proxy
  may switch a stalled card itself. Mitigated by a *static* card (continuous, will
  not trip stall detection), the P3 `channel_stream == CARD` re-check, and the
  generation token.

## A.4 Membership at order 9998 (D2) — and why the exclusion cannot ship early

> ⚠️ **The loop hard-exclusion is not a safe standalone refactor.** On prod the
> card at order 9999 is served *by the normal selection loop reaching it*
> (account 1 / profile 1 / `max_streams=0` ⇒ always a free slot); the maxed
> branch at `TooManyStreams.py:353` is dead code. Adding
> `if stream.id in card_ids: continue` therefore **removes the card's only
> working delivery path** unless the maxed branch is made to work first. The
> exclusion, the maxed branch, and the 9998 row are one atomic change — which is
> why they all sit in v3, not in §4.

```python
card_ids = registry.card_stream_ids()
for stream in self.streams.all().order_by("channelstream__order"):
    if stream.id in card_ids:
        continue
```

Without it, an order-9998 card (always "free") wins the moment the loop reaches
it and every channel sticks on the card permanently. **Verified real, not
theoretical.**

**Apply/remove:** batched `bulk_create(ignore_conflicts)` over
`all_channel_ids − existing`; removal is a bulk delete with `stop_running=False`
by default (never stops the fleet). New channels via a `post_save(Channel,
created=True)` signal plus an idempotent `reconcile_managed_streams` backstop for
`bulk_create` M3U imports that skip `post_save`.

**Override install must precede serving** — install at an app-ready hook, not on
lazy first instantiation, or the override-absent window lets stock `get_stream`
select the 9998 card and park with no probe behind it.

## A.5 Redis keys (v3 additions over v1)

| Key | Id | TTL | Notes |
|---|---|---|---|
| `tms:starting:{int}` | channel int PK | > all budgets, re-asserted per card return | single-flight + sole INCR gate |
| `tms:switch_inflight:{int}` | channel int PK | `SWITCH_TTL` | guards P3→P4 |
| `tms:probe_inflight:{account}` | account id | short | fleet-global network cap |

`tms:health:{sid}` is v1's and is unchanged by v3 — v3 reads the same key,
which is the point of building it first.

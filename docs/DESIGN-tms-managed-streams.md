# Too Many Streams — Multi Managed-Stream Redesign (v2, "always card then switch")

> Design doc, revised 2026-06-14. Generalizes the single hardcoded "Too Many
> Streams" card into a registry of configurable **dynamic** and **static** managed
> placeholder streams, and adds a **"TMS: startup stream"** shown on every fresh
> tune while a background probe live-switches the channel to the first healthy
> real source.
>
> **All code citations are verified against the deployed box: Dispatcharr `0.21.1`,
> `apps.proxy.ts_proxy` namespace.** (An earlier multi-agent pass hallucinated a
> "v0.26.0 / `live_proxy`" tree with wrong line numbers; those were discarded and
> every load-bearing fact below was re-read from the running container.)

---

## Decisions locked in

- **D1 — Trigger model = "ALWAYS card then switch."** Every fresh tune shows the `startup` card immediately; a background probe (web/serving process only) finds the first healthy real source and live-switches to it. A **known-good fast-path** lets warm channels skip the card and connect direct, so the card is in practice only paid by cold/unknown tunes.
- **D2 — The startup card is a real `ChannelStream` membership row at order 9998** on all channels (just above the dynamic `tms` card at 9999), made safe by the selection loop hard-excluding all managed-card stream ids. Visible/queryable in the channel UI; apply/remove parity with the existing card.

**The "slot leak" blocker that earlier review raised is FALSE** — see §9. `Channel.release_stream()` is the verified DECR owner. The real, subtler hazard is a *double*-INCR via the switch path, which §3.4 closes by ordering.

---

## Executive summary

Today the plugin hardcodes one placeholder card. This redesign introduces a **registry of managed placeholder streams** of two kinds — DYNAMIC (the existing live grid, key `tms`) and STATIC (the new "Starting up" card, key `startup`). One HTTP server on `:1337` routes all cards by path; each has its own lazy `-re` encoder; static cards drop the updater thread (strictly cheaper). Every card is UI-configurable and the startup card is fleet apply/removable.

Per your decisions, v1 ships **always-card-then-switch**: the `get_stream` override returns the `startup` card immediately on a fresh tune, a web-process probe health-checks the real sources, reserves the chosen profile, and calls `ChannelService.change_stream_url` to live-switch the channel to it. Slot accounting is **leak-free** because Dispatcharr's `Channel.release_stream()` (models.py:533) decrements `profile_connections` on every teardown, keyed off exactly the `channel_stream`/`stream_profile` keys the switch writes. The one genuine hazard — `update_url`→`update_stream_profile` (stream_manager.py:1083 → models.py:651) performing a *second* INCR — is neutralized by writing the reservation keys **before** the switch so `update_stream_profile` early-returns. A **known-good fast-path** keeps warm tunes byte-identical to today; a `tms:card_inflight` cap bounds the downstream NVENC-session load during failover waves. Strictly additive: prod Stream id 195362, `/stream.ts`, and ~2806 memberships are untouched; startup ships opt-in.

---

## Open questions / decisions for you

The two big forks (trigger model, placement) are decided (D1/D2). Remaining knobs, each with a recommended default:

- **`probe_mode` default.** `capacity_only` (no upstream network probe — just reserve the first source with a free slot; default, safest for provider-ban risk) vs `byte_probe` (open the real URL, require sustained bytes). **Recommended: `capacity_only` in v1**, `byte_probe` behind the fleet-global per-account cap in Phase 6.
- **Known-good fast-path default.** ON (warm channels skip the card; the main lever against per-tune cost) vs OFF (literal "always card"). **Recommended: ON.**
- **`tms:card_inflight` cap value.** Caps concurrent card-served channels to bound downstream NVENC sessions; beyond it the override returns a graceful 503 instead of parking. **Recommended: default to a conservative number (e.g. 6) on a single consumer GPU; expose as a file-only override.**
- **`is_streams_maxed` refactor to a pure read** — required precondition (it currently stops channels). **Recommended: yes.**
- **`tms_enabled=0` behaviour** with live memberships — override skips the card branch → graceful 503, not a mid-stream 404. **Recommended: yes.**
- **plugin.json single source** — generate from a Python field-spec at build time; reconcile the 2.1.3/2.2.3 skew. **Recommended: yes.**
- **Stream-row purge on uninstall** — leave rows (instant re-apply); separate explicit purge action. **Recommended: leave.**
- **Reconcile beat task** — ship the manual action; beat opt-in (Celery is ephemeral). **Recommended: manual + opt-in beat.**

---

## 1. Overview & goals

- **DYNAMIC streams** — re-rendered on content change. The existing TMS grid (active-channels, directory fallback) is the built-in `tms`.
- **STATIC streams** — a fixed configurable image/message. The new `startup` ("TMS: startup stream") is shown on every fresh tune (D1) while the probe finds a real source.

**Goals:** (1) every managed stream UI-configurable; (2) startup fleet apply/removable like the existing card; (3) strictly additive / zero-break for existing installs (Stream 195362, `/stream.ts`, ~2806 order-9999 rows untouched; startup opt-in); (4) one `:1337` server, one hosting process, lazy per-card encoders; (5) room for a 3rd card with minimal code.

**Why "always card" is viable (the three former blockers, resolved):**
- **Slot leak — disproven (§9).** `release_stream` (models.py:533) is the DECR owner; the switch's manual INCR is balanced by it.
- **Double-INCR via the switch — real, neutralized (§3.4.5).** `update_url`→`update_stream_profile` INCRs unless the reservation keys are written first; we order them so it early-returns.
- **Healthy-tune regression — mitigated.** Always-card adds a card encode + downstream nvenc + a card→real rebuffer to every *cold* tune; the known-good fast-path removes it for warm channels. No relief during a fleet-wide failover wave — that case is bounded by a concurrent-card cap (§5), not the fast-path.
- **Order-0 trap — avoided.** The card sits at order **9998**, never 0, and the loop hard-excludes all `card_stream_ids()`.

**Known limitations (accepted):** a single shared encoder/image per card key cannot show per-channel state; "always card" is literal only for cold/unknown channels (the fast-path intentionally bypasses warm ones).

## 2. Current state (verified, 0.21.1)

- **`src/StreamServer.py`** — serves ONE MPEG-TS card on `0.0.0.0:1337` (`/`, `/stream.ts`). Single shared lazy ffmpeg encoder (`-re -loop 1 -framerate 1 -i IMG -re -f lavfi -i anullsrc ... libx264 -preset ultrafast -tune stillimage -threads 1 ... -f mpegts pipe:1`). `_ensure_running` on first client; idle-stop 60s after last. Broadcaster fans stdout to per-client 50-deep drop-on-full queues. **Updater builds a NEW `PillowImageGen()` each iteration and relies on the process-global class attr `_last_active_uuids` to suppress re-encodes.** Fixes preserved: `-re`, `-threads 1`, reap-after-kill.
- **`src/PillowImageGen.py`** — 1920×1080 JPG. `get_active_streams()` SCANs `ts_proxy:channel:*:metadata`, builds the OTHER-active grid (excludes only the `tms` URL today), directory fallback (DB-ordered by indexed `channel_number`, capped 15), else "This Channel is Unavailable". Change-detection vs the class-global `_last_active_uuids`.
- **`src/TooManyStreams.py`** — `Channel.get_stream` override: (1) restore `channel_stream:{int id}`/`stream_profile:{sid}`; (2) iterate `self.streams` by `channelstream__order`, reserve first free-slot profile (`INCR profile_connections:{P}` only if `max_streams>0`), set keys, return; (3) maxed branch returns the card (dead on prod). **`is_streams_maxed` is NOT pure — it calls `remove_stream_from_channel` which `stop_channel`s.** Card identity `Stream(name='TooManyStreams', url=get_stream_url())`.
- **`src/TooManyStreamsConfig.py`** — `get_config()` merges defaults < DB < persistent file < env; **`save_plugin_persistent_config` does raw `json.dump` (bypasses `from_dict`/`dict()`).** `get_stream_url()`→`/stream.ts`.
- **`plugin.py`** — `Plugin` (`version="2.1.3"`), 12 flat `fields[]`, 4 `actions[]`; installs override + log filter everywhere, hosts the server in one non-celery web process (`_should_host_server`+`_can_bind`); **fire-and-forget server thread (no reference kept); `run()` dispatches on action-id only.**
- **`plugin.json`** — duplicates all fields/actions; **version skew 2.2.3 vs 2.1.3** (already drifting).

**Verified Dispatcharr internals (0.21.1, `ts_proxy`):**
- Card = `Stream id=195362, is_custom, m3u_account_id=1`; account 1 profile `id=1, is_default, is_active, max_streams=0` (UNLIMITED). Returning `(195362, 1, None)` is served by `generate_stream_url` (which requires truthy stream_id AND profile_id — url_utils.py:86).
- `Channel.get_stream` reserves via atomic `_check_and_reserve_profile_slot` (INCR-then-check-rollback; max_streams=0 ⇒ no INCR).
- `Channel.release_stream()` (models.py:533): DECRs `profile_connections` on teardown — PRIMARY via `channel_stream:{int}`→`stream_profile:{sid}` (DECR ~627), FALLBACK via metadata hash `ts_proxy:channel:{uuid}:metadata` fields `STREAM_ID`/`M3U_PROFILE` (DECR ~580); HDEL/DELETE guard double-release. Called on every teardown path (views ×4, server ×2, channel_service `stop_channel`, stream_generator).
- `Channel.update_stream_profile(P)` (models.py:651): pipeline DECR-old / SET `stream_profile:{sid}=P` / **INCR `profile_connections:{P}`**; early-returns if current==new or if `stream_profile:{sid}` absent.
- `ChannelService.change_stream_url(channel_id=UUID, new_url, user_agent, target_stream_id, m3u_profile_id)` (channel_service.py:88): owner ⇒ `manager.update_url(new_url, sid, pid)` (stream_manager.py:1060); non-owner ⇒ Redis pubsub to owner. Calls `_update_channel_metadata` (writes `STREAM_ID`/`M3U_PROFILE`). With `new_url` it does not re-select a profile.
- `StreamManager.update_url` (stream_manager.py:1060): if `current_stream_id != stream_id` and `m3u_profile_id` given ⇒ **calls `channel.update_stream_profile(m3u_profile_id)` (line 1083)**.
- `get_alternate_streams(channel_id, current_stream_id)` (url_utils.py:279, used by views.py:269/289): the proxy's **autonomous failover** — picks alternates with available `profile_connections` and can switch a stalled stream on its own.
- Identity: `channel_stream`/`stream_profile`/`profile_connections`/`tms:*` guard keys use the channel **integer PK**; `change_stream_url`/metadata/liveness use the channel **UUID**. `proxy_server.check_if_channel_exists(uuid)` is the liveness primitive.

## 3. Proposed architecture

A single **registry** of `ManagedStream` definitions is the source of truth driving Stream rows, HTTP routing, renderers, channel application, and the override's per-key branching.

### 3.1 Managed-stream model & registry

```python
# src/managed_streams/definition.py
class StreamKind(str, Enum): DYNAMIC="dynamic"; STATIC="static"

@dataclass
class ManagedStream:
    key: str                       # "tms","startup" -> registry id, URL slug, Redis ns
    display_name: str
    stream_name: str               # exact Dispatcharr Stream.name for get_or_create
    http_path: str                 # canonical served path, e.g. "/tms/startup.ts"
    kind: StreamKind
    m3u_account_id: int = 1
    apply_order: Optional[int] = None
    is_custom: bool = True
    stream_url_path: Optional[str] = None
    enabled: bool = True
    builtin: bool = True
    legacy_paths: tuple = ()
    renderer: dict = field(default_factory=dict)
    behavior: dict = field(default_factory=dict)
    stream_id: Optional[int] = field(default=None, compare=False)
    def stored_url(self, host, port): ...
    def all_paths(self): return (self.http_path, *self.legacy_paths)
```

Built-ins: `tms` (DYNAMIC, `stream_url_path="/stream.ts"`, `legacy_paths=("/stream.ts","/")`, `apply_order=9999`, enabled) and `startup` (STATIC, `http_path="/tms/startup.ts"`, **`apply_order=9998`**, `enabled=False`, `behavior={"mode":"always_card","fast_path":True,"probe_mode":"capacity_only"}`).

`ManagedStreamRegistry` — process singleton, lazy load, `reload()` (from `clear_cache()`). Drives everything via `all()/enabled()/get(key)/by_path()/by_stream_id()/card_stream_ids()/refresh_event(key)`. **`card_stream_ids()`** (frozenset of all registry Stream ids) is the single source for the loop exclusion. `get_or_create_stream(ms)` resolves the Stream row (best-effort; back-compatible for the default host/port — non-default `TMS_HOST`/`TMS_PORT` risks a duplicate row, documented).

### 3.2 StreamServer (multi-stream HTTP + encoders)

Split into `ManagedStreamChannel` (one card's lazy encoder + broadcaster, + updater for dynamic only) and `StreamServer` (one `ThreadingHTTPServer` on `:1337`, a `dict[key→channel]`, routing, `reload()`).

Routing resolves against `enabled()` only: `/healthz`→200 (matched before `/`, warms no encoder); `/stream.ts`,`/`→`tms`; `/tms/<key>.ts`→`<key>`; else 404. `video_encoder` is snapshotted per channel (changes apply on `reload()`). **The host keeps a reference to the server** (today it's fire-and-forget); `reload()` is a no-op off-host. **Cross-process ordering invariant:** the host must serve a card's route before any worker returns that card's id — trivially satisfied in v1 (startup's path is bound at startup; the override only returns it when `enabled` and the path is advertised). Static cards have no updater thread.

### 3.3 Renderers (dynamic vs static)

`Renderer` interface. **Phase-0 blocker fix (coupled):** replace process-global `_last_active_uuids` with an instance `_last_signature` AND make the channel hold ONE long-lived renderer instance across updater iterations (the two are inseparable — splitting regresses the dynamic card). `DynamicRenderer` = today's logic intact, but its self-exclusion must check **all** `card_stream_ids()` (so a startup-card-parked channel never leaks into the grid). `StaticRenderer` renders once (passthrough a user image, or draw a card; corrupt image ⇒ card mode, never black). Output dir `/data/plugins/TMS_Persistent_Config/tms_render/<key>.jpg`.

### 3.4 Startup runtime flow (always card then switch)

#### 3.4.1 Identifier discipline (where leaks hide)

| Concern | Identifier | Key / call |
|---|---|---|
| Reservation (restore + reserve) | channel **int PK** `self.id` | `channel_stream:{int}`→sid; `stream_profile:{sid}`→pid |
| Slot counter | m3u **profile id** | `profile_connections:{P}` |
| Switch + metadata | channel **UUID** | `change_stream_url(channel_id=UUID)`; metadata `ts_proxy:channel:{uuid}:metadata` |
| TMS guard keys | channel **int PK** | `tms:starting:{int}`, `tms:switch_inflight:{int}` |
| Liveness | channel **UUID** | `proxy_server.check_if_channel_exists(uuid)` |
| Health | **stream id** | `tms:health:{sid}` |

The probe holds the `Channel` (both ids); never derive one from the other via a lookup.

#### 3.4.2 Override ordering (authoritative)

```
0. no streams        -> (None, None, "No streams assigned to channel")

1. RESTORE SESSION (first; shared by every concurrent viewer; never INCRs):
     sid = GET channel_stream:{int id}; pid = GET stream_profile:{sid}
     if sid and pid:
        if sid in card_stream_ids():
            return (sid, pid, None) IF EXISTS tms:starting:{int id}   # probe owns it
            else fall through (card key outlived its probe -> re-arm)
        else return (sid, int(pid), None)                            # real switched target

2. KNOWN-GOOD FAST-PATH (default ON; kills the healthy-tune regression):
     top = first self.streams EXCLUDING card_stream_ids(), by order
     if GET tms:health:{top.id} == "ok" AND top.profile has a free slot:
        reserve top atomically (3.4.4); return real (top.id, P)      # no card/probe/switch

3. ALWAYS-CARD (D1, cold/unknown):
     if is_kind_active(self, startup):
        CARD = registry.get("startup").stream_id; PID = 1            # unlimited, no INCR
        SET channel_stream:{int id}=CARD EX CARD_SESSION_TTL
        SET stream_profile:{CARD}=1      EX CARD_SESSION_TTL
        if SET tms:starting:{int id} "<token>" NX EX STARTING_TTL:
            if in_web_process(): spawn_probe(self, token)            # NEVER celery
        else: EXPIRE tms:starting:{int id} STARTING_TTL
        return (CARD, 1, None)

4. NORMAL LOOP (only if startup inactive): iterate by order, `continue` on any
   id in card_stream_ids(); reserve first free-slot profile atomically (3.4.4).

5. tms maxed-out -> existing dynamic tms branch, reading a PURE is_streams_maxed (§3.6).
```

`is_kind_active(self, startup)` = `startup.enabled AND startup.stream_id AND self.streams.exclude(id__in=card_stream_ids()).exists()` (must be a real source to switch TO; reuses the loop's queryset, no extra `m3u_account` deref).

#### 3.4.3 Leak-free accounting (Δ = net `profile_connections:{P}` for the real profile P)

| # | Event | Plugin ops | Dispatcharr ops | Δ(P) |
|---|---|---|---|---|
| A | Cold tune → card | SET channel_stream=CARD ex; SET stream_profile:CARD=1 ex; SET tms:starting NX | serves card | 0 (profile 1, no INCR) |
| A′ | Warm tune → fast-path | atomic reserve top (3.4.4) | — | +1 (like today) |
| B | Probe reserves S/P | atomic INCR pc:{P} (rollback if >max) | — | +1 (0 on rollback) |
| C | Commit switch | SET channel_stream=S, **SET stream_profile:S=P** (both before switch), DEL stale stream_profile:CARD; (non-owner) HSET metadata STREAM_ID/M3U_PROFILE; `change_stream_url(...)`; PERSIST on success; DEL guards | `update_stream_profile` **no-ops** (current==P, §3.4.5) | 0 |
| D | Teardown while live on S/P | — | `release_stream` DECR pc:{P} (primary or metadata fallback); DEL/HDEL | −1 |
| E | Teardown while on card | abort: DEL tms:starting | `release_stream`: stream_profile:CARD=1 → DECR pc:1 only if >0 (benign, unlimited) | 0 |
| F | Nth concurrent viewer | restore returns existing CARD/S; no write | — | 0 |
| G | Double release_stream | — | 2nd call: keys gone / metadata HDEL'd → no DECR | 0 |
| H | Rollback in B | INCR then DECR | — | 0 |

Balance: exactly one INCR (B, the plugin) ↔ one DECR (D, `release_stream`). The card is accounting-inert (profile 1 unlimited).

#### 3.4.4 The single atomic reservation (fast-path, probe, AND normal loop)

```python
def reserve_atomic(P, max_streams, r):
    if max_streams == 0: return True            # unlimited
    n = r.incr(f"profile_connections:{P}")
    if n > max_streams:
        r.decr(f"profile_connections:{P}")      # rollback -> net 0
        return False
    return True
```

Replaces the override's current racy `GET … < max ; SET ; INCR` (over-admits past `max_streams` under a wave). Per-channel single-flight (`tms:starting:{int}` SET NX) ensures only one concurrent tune INCRs; losers restore from the winner's `channel_stream`.

#### 3.4.5 Switch identity, ordering & the `update_stream_profile` correction

**Verified:** `change_stream_url(target_stream_id=S, m3u_profile_id=P)` → `update_url(new_url, S, P)` (stream_manager.py:1060) → `channel.update_stream_profile(P)` (line 1083 → models.py:651), which INCRs `profile_connections:{P}` **unless** its `current==new` guard fires.

**Mandatory ordering (makes it a guaranteed no-op):** write `SET channel_stream:{int}=S` and `SET stream_profile:{S}=P` **strictly before** `change_stream_url`. Then `update_stream_profile` reads `current_profile==P==new` and early-returns — no second INCR. The plugin's manual INCR (B) is the only increment. A runtime assertion (`current_profile==P` pre-switch) and a regression test guard this. (Equivalent alternative: skip the manual INCR and let `update_stream_profile` do the single INCR — but that path also requires `stream_profile:{S}` pre-set and is less robust to the current-profile value; we keep the explicit manual-INCR + no-op approach.)

**Ordering invariant consequences:** a re-entrant `get_stream` reads real `(S,P)` (never re-cards); a mid-switch teardown finds a releasable real reservation; on the non-owner path the plugin writes the metadata hash itself so `release_stream`'s fallback is consistent even though the owner applies the switch asynchronously.

#### 3.4.6 The probe (web/serving process only)

```
P0 generation: GET tms:starting:{int} token == mine else ABORT("superseded")
P1 wait-ready (bounded): until proxy_server.check_if_channel_exists(uuid) & state ready;
   abort if client gone; EXPIRE tms:starting each iter; never publish before readiness
P2 select candidate: by order, exclude card_stream_ids(); CAPACITY PRECHECK
   (profile_connections vs max BEFORE any network); per-account Redis token bucket gates
   network; probe_mode==byte_probe ? require sustained bytes : capacity_only
P3 re-check channel_stream:{int}==CARD (autonomous-failover guard); reserve atomically;
   SET channel_stream:{int}=S ex SWITCH_TTL, SET stream_profile:{S}=P ex SWITCH_TTL
P4 re-check generation + client; (non-owner) HSET metadata STREAM_ID/M3U_PROFILE;
   change_stream_url(uuid,new_url,UA,S,P); on success PERSIST the two keys
P5 DEL tms:switch_inflight, DEL tms:starting, trigger_refresh()
```

**Capacity precheck before any network** (failover-wave / provider-ban safety): never open an upstream for a maxed profile; stay on the card instead. **Per-account cap is a fleet-global Redis token bucket** (`tms:probe_inflight:{account}` INCR/EXPIRE/DECR), NOT a process-local semaphore (`get_stream` runs in every web worker). Default `probe_mode=capacity_only` (no network) in v1.

**Known-good fast-path** (step 2): a pure Redis read of `tms:health:{top.id}`. Health is keyed by **stream id**, so one probe of a popular source warms the fast-path for every channel listing it (≈ one probe per distinct healthy stream per `HEALTH_OK_TTL`, not per channel).

#### 3.4.7 Races & guards

- **Probe before init:** bounded `wait_ready` (P1); never publish before `check_if_channel_exists`.
- **Client disconnect during probe:** checked at P1/P2/before-P4. Before P3 INCR → plain ABORT (card released by teardown). After P3 INCR → `ABORT_RELEASE_REAL`: **atomic** (Lua/WATCH) DECR `P` only if `channel_stream:{int}` still equals S, deleting keys in the same txn — so it and `release_stream` can't double-DECR.
- **Re-tune during probe:** `tms:starting` token; stale-token probe ABORTs (or ABORT_RELEASE_REAL if it reserved). Re-checked just before the irreversible publish.
- **Non-owner switch:** keys + metadata written before publish; a lost event degrades to "stayed on card," never a leak/wrong source.
- **Probe-thread death:** `tms:starting` TTL (> full budget) self-heals; P3 keys are TTL'd until commit. Probes never run in Celery; no `finally` is load-bearing.
- **Autonomous proxy failover (`get_alternate_streams`):** the proxy may switch a stalled card on its own (it reserves via `profile_connections` too). Mitigation: route always-card through the **static** card (continuous encoder, won't trip stall detection) and re-check `channel_stream:{int}==CARD` at P3 before reserving; a proxy-initiated switch invalidates the generation token.

#### 3.4.8 Card-session / single-flight lifecycle

`channel_stream:{int}=CARD` carries `CARD_SESSION_TTL`. The single-flight guard `tms:starting:{int}` is the **sole authoritative INCR gate**; its TTL > `WAIT_READY + PROBE + SWITCH` budgets and is **re-asserted (EXPIRE) on every card return** so it can't lapse mid-probe. Restore (step 1) treats a restored card as terminal only while `tms:starting` exists; a card that outlived its probe (probe death) is re-armed on the next tune. This closes both the double-INCR window and the stuck-on-card window.

### 3.5 Configuration & plugin UI

**Namespaced flat keys** (not a JSON blob — Dispatcharr fields are flat/typed). Existing keys unchanged. v1 fields: *shared* `tms_log_level`, `video_encoder`; *dynamic (existing)* `tms_enabled`, `stream_title`, `stream_description`, `stream_channel_cols`, `tms_image_path`, `theme_*`; *startup (new)* `startup_enabled`, `startup_title`, `startup_message`, `startup_image_path`, `startup_bg_color`, `startup_text_color`, `startup_accent_color`. All probe/health knobs (`card_session_ttl`, `starting_ttl`, `probe_mode`, `card_inflight_cap`, timeouts, TTLs, per-account cap) are **file-only overrides**, defaulted in code. Booleans as `number` 0/1 via a `_b` coercer (nonzero→1).

**Blocker fixes:** (1) the Save path must stamp `schema_version=2` and `_migrate_persistent` **before** `json.dump` (it currently bypasses `from_dict`/`dict()`); the migration write-back must be atomic (`tempfile + os.replace`, ideally host-only); (2) startup-colour inheritance ("inherit `theme_*` on first enable") runs in `from_dict` (sees merged DB themes), not in `_migrate_persistent`; (3) **plugin.json must be generated from one field-spec** (or edited in lockstep) and the 2.1.3/2.2.3 skew reconciled. `clear_cache()` → registry `reload()` + RenderManager rebuild + (host only) `server.reload()`. `actions[]` adds `apply_startup_stream`, `remove_startup_stream` (confirm), `reconcile_managed_streams`; `run()` gains explicit per-action dispatch + an `apply(key)`/`remove(key)` helper.

### 3.6 Channel application & ordering (membership @ 9998)

**Placement (D2).** `startup` gets `apply_order=9998`, `m3u_account_id=1` (profile 1 / unlimited), distinct name/path. A real membership row gives: literal "added to all channels," channel-UI visibility/reorder/remove, and a terminal fallback if the override is ever absent (the loop reaches 9998 before 9999 → worst case shows a card, never a 503/black).

**Loop hard-exclusion (load-bearing):**
```python
card_ids = registry.card_stream_ids()      # frozenset({STARTUP_ID, TMS_ID})
for stream in self.streams.all().order_by("channelstream__order"):
    if stream.id in card_ids: continue      # never reserve a managed card as a real source
    ...
```
Without it the order-9998 card (always a "free slot") would be selected the moment the loop reaches it. Guard is on `stream.id` (int PK). 9998<9999 is a fallback-priority guarantee, never a selection mechanism (the loop never reaches a card while the override is present).

**`is_streams_maxed` → PURE read** (HARD precondition). It must NOT call `add/remove_stream_from_channel`/`stop_channel` (today it stops the channel when false — catastrophic across 2806, a fleet-stop storm under D1+D2). Membership owned solely by apply/remove/reconcile; the maxed branch only reads `tms:maxed_out:{ch}`.

**Apply (batched, idempotent, parameterised by key):** `get_or_create_stream` → `bulk_create(ignore_conflicts)` over `all_channel_ids − existing` at `spec.apply_order`. O(1) stream queries; startup@9998 and tms@9999 coexist. **Remove (fleet-safe):** bulk `ChannelStream.filter(stream_id=…).delete()` with `stop_running=False` default (never stops the fleet); `stop_running=True` opt-in, Redis-scoped to parked channels. **New channels:** `post_save(Channel, created=True)` signal (within-process `dispatch_uid`, in web AND celery) + idempotent `reconcile_managed_streams` backstop (covers `bulk_create` M3U imports that skip `post_save`).

**Override install must precede serving:** install at an app-ready hook (not lazy first-instantiation), else the override-absent window lets stock `get_stream` select the 9998 card and park with no probe. Belt-and-braces: on seeing a stock-set `channel_stream:{int}=STARTUP_ID` with no live `tms:starting`, treat as a fresh card and arm a probe.

## 4. Redis keys

| Key | Id | Value | TTL | Writer → Reader | Notes |
|---|---|---|---|---|---|
| `channel_stream:{int}` | channel int PK | sid (CARD or S) | card/probe: TTL'd until commit; real-committed: PERSIST | plugin → override restore, `release_stream` PRIMARY | |
| `stream_profile:{sid}` | stream id | pid (1 / P) | matches its `channel_stream` | plugin (**written before `change_stream_url`**) → `release_stream`, `update_stream_profile` | feeds the no-op |
| `profile_connections:{P}` | profile id | int | none | plugin **INCR once** ↔ core **DECR once** | the leak-critical cell; one writer per direction |
| `tms:starting:{int}` | channel int PK | `<token>` | `STARTING_TTL` > all budgets; re-asserted each card return | plugin → probe generation | single-flight + INCR gate; self-heals |
| `tms:switch_inflight:{int}` | channel int PK | 1 | `SWITCH_TTL` | plugin | guards P3→P4 |
| `tms:health:{sid}` | stream id | ok/bad | ok≈90s / bad≈25s | probe (web) → fast-path | one probe warms the fleet |
| `tms:probe_inflight:{account}` | account id | int | short EX | probe INCR/DECR | **fleet-global** per-account network cap |
| `tms:card_inflight` | — | int | short EX | card serve INCR/DECR | concurrent-card cap (NVENC guard, §5); over cap → 503 |
| `tms:maxed_out:{int}` | channel int PK | int | 30s | pure `is_streams_maxed` | unchanged |
| `ts_proxy:channel:{uuid}:metadata` | channel UUID | hash | proxy-managed | core / plugin (non-owner) → `release_stream` FALLBACK | fields `STREAM_ID`/`M3U_PROFILE` |

## 5. Resource impact

**Plugin side (cheap):** concurrent card encoders = distinct cards viewed, not channels. Many channels on `startup` share ONE encoder via path fan-out (~1% core at 1fps with `-re`; ~30–60 MB; static card has no updater). O(1) in channels.

**System side (the real cost, linear):** each card-served channel = one downstream proxy ffmpeg (nvenc on prod) transcoding the card. 50 card channels = 1 plugin encoder + **50 downstream nvenc transcodes**.

**Healthy tunes under D1:** the known-good fast-path makes warm tunes **byte-identical to today** (Redis read → direct connect). Cold/unknown tunes pay one card encode + one nvenc + one card→real rebuffer, then run real; subsequent tunes within `HEALTH_OK_TTL` are fast-path-direct.

**NVENC-session wall (hard ceiling):** on a failover wave the fast-path gives **zero** relief (health cold/bad fleet-wide), so all affected channels card simultaneously. Consumer GPUs cap concurrent NVENC sessions (~3–8); past that, transcodes **fail** and teardown/retune churn amplifies. **Mitigation (required):** the `tms:card_inflight` counter caps concurrent card-served channels; beyond it the override returns a graceful 503 instead of parking-and-failing. Documented operational limit.

**Steady state:** "0 encoders when idle" holds only with no persistent clients; TiviMate-style persistent connections keep an encoder warm (idle-stop 60s after last client). Quantify warm-fraction empirically. **Mass-failover dynamic-card restarts** scale with active-set transitions (Phase 6 debounce).

## 6. Backward compatibility & migration

Strictly additive: `tms` keeps name/url/order; prod row 195362 + ~2806 rows satisfy identity (best-effort `get_or_create`; non-default host/port risks a duplicate row — documented). `/stream.ts`+`/` serve identical bytes. Startup ships disabled & un-applied. Config v1→v2 additive-only (`_migrate_persistent` stamps + back-fills; atomic write-back; Save path must also stamp/migrate). Packaging: add `src/managed_streams/__init__.py` + `src/renderers/__init__.py` (relative imports); ship the new path + renderers; reconcile plugin.json/version. **Migration ordering on the live box:** operator-driven two-step — `apply_startup_stream` (insert 9998 rows, idempotent, inert behind the loop guard) then flip `startup_enabled`. Reversible.

## 7. Edge cases & failure modes

- **Cold tune:** card → probe → switch; leak-free (§3.4.3). Warm tune: fast-path direct, no card.
- **All real maxed:** capacity precheck → stay on card (Δ=0); re-selects on next tune after `CARD_SESSION_TTL`.
- **Probe-thread death mid-switch:** P3 keys TTL'd; `tms:starting` TTL self-heals.
- **Client disconnect mid-probe:** ABORT (pre-reserve) / atomic ABORT_RELEASE_REAL (post-reserve) — no leak.
- **Autonomous failover races probe:** static card + P3 `channel_stream==CARD` re-check + generation token.
- **Double release_stream:** guarded by Dispatcharr's HDEL/DELETE.
- **`tms_enabled=0` with live rows:** override skips the card branch → graceful 503, not mid-stream 404.
- **Failover wave > NVENC cap:** `tms:card_inflight` → 503 beyond the cap.
- **Disabled key / `/` alias when tms disabled:** 404; `/healthz` matched first.
- **Dynamic grid self-exclusion:** excludes ALL card ids so card-parked channels never leak into the grid.
- **Override-absent window:** app-ready install + stock-card probe-arm.

## 8. Phased implementation plan (switch-path in v1)

- **Phase 0 — Renderer decoupling + long-lived renderer (coupled).** Instance `_last_signature` + one persistent renderer per channel; extract `draw_utils.py`. Byte-identical.
- **Phase 1 — Config model + registry.** schema v2, dataclasses + derived `streams`, `_migrate_persistent`, atomic write-back, **Save-path v2 stamping**, `from_dict` colour inheritance, `card_stream_ids()`. Only `tms` enabled.
- **Phase 2 — Multi-stream StreamServer.** `ManagedStreamChannel` + `StreamServer(registry)`, routing + `/healthz` + `reload()` (no-op off-host); host keeps a server reference. `/stream.ts`+`/` identical.
- **Phase 3 — Loop exclusion + PURE `is_streams_maxed` + atomic reservation + batched apply/remove + signal/reconcile.** (Pure `is_streams_maxed` is a HARD precondition for Phase 5.)
- **Phase 4 — Static `startup` renderer + card served + 9998 membership (no override switch yet).** `StaticRenderer`; enable path (config-disabled default); `apply/remove_startup_stream`; startup fields in plugin.py + plugin.json (generated); override install → app-ready hook.
- **Phase 5 — ALWAYS card then switch (v1 trigger model).** Override always-card branch returning `(STARTUP_ID, 1, None)` with re-asserted single-flight; web-process probe (capacity precheck, fleet-global per-account cap, `capacity_only` default); atomic reservation; `change_stream_url` switch with `stream_profile:{S}=P` written **before** (so `update_stream_profile` no-ops); non-owner metadata write + ordering; atomic `ABORT_RELEASE_REAL`; `tms:card_inflight` cap + 503; known-good fast-path (default ON). Probes NEVER in Celery.
- **Phase 6 — Hardening.** `byte_probe` mode behind the Redis cap; updater debounce; uninstall/rollback; autonomous-failover coalescing; optional reconcile beat.

## 9. Correction note: the slot-leak blocker was a false alarm

An earlier multi-agent review claimed always-card "permanently leaks a `profile_connections` slot because no DECR exists." **False** — that review only grepped the plugin repo, not Dispatcharr core, and additionally hallucinated a "v0.26.0 `live_proxy`" tree (wrong namespace and line numbers). Verified against the deployed **0.21.1 / `ts_proxy`** container:

- **`Channel.release_stream()` (models.py:533) is the DECR owner**, on every teardown path (views ×4, server ×2, `stop_channel`, stream_generator): PRIMARY `channel_stream:{int}`→`stream_profile:{sid}`→DECR `profile_connections` (~627); FALLBACK via metadata `ts_proxy:channel:{uuid}:metadata` `STREAM_ID`/`M3U_PROFILE` (~580); HDEL/DELETE guard double-release. ⇒ a switch that mirrors `get_stream` (SET `channel_stream:{int}=S`, SET `stream_profile:{S}=P`, INCR `profile_connections:{P}`) is released correctly. One manual INCR ↔ one core DECR. **No leak.**
- **The one real subtlety the review surfaced (correctly, despite wrong citations):** the switch path *itself* can INCR — `change_stream_url`→`update_url` (stream_manager.py:1060) calls `channel.update_stream_profile(P)` (line 1083 → models.py:651), which INCRs `profile_connections:{P}` unless `current==new`. **Neutralized by writing `stream_profile:{S}=P` before the switch** so it early-returns (§3.4.5). The leak-free proof therefore depends on *ordering*, not on the (false) "the switch never INCRs."
- **Also confirmed real:** autonomous failover (`get_alternate_streams`, url_utils.py:279) can independently switch a stalled stream — guarded per §3.4.7(f).

### Provenance of facts
Every Dispatcharr citation here was read from the running `jflix_dispatcharr` container (0.21.1). The agent-supplied `live_proxy` / `models.py:743/854` / `manager.py:1164` / `live:channel:` citations were hallucinated and are **not** used.

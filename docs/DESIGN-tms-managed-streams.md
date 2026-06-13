# Too Many Streams — Multi Managed-Stream Redesign (Final)

> Design document produced 2026-06-14. Generalizes the single hardcoded "Too Many
> Streams" card into a registry of configurable **dynamic** and **static** managed
> placeholder streams (including a new "Starting up" card). Grounded in verified
> Dispatcharr internals and hardened by a 5-lens adversarial review (54 issues, 29
> blocker/major). The review **reversed the earlier "always card then switch"
> decision** — see §1 and the Open Questions.

---

## Executive summary

This redesign generalizes the plugin's single hardcoded "Too Many Streams" card into a **registry of managed placeholder streams** with two kinds: DYNAMIC (the existing live grid, key `tms`) and STATIC (a new, opt-in "Starting up" card, key `startup`). One HTTP server on `:1337` routes all cards by path; each card has its own lazy ffmpeg encoder; static cards drop the updater thread so they are strictly cheaper. Every card is fully UI-configurable and the startup card is apply/removable fleet-wide via plugin actions, exactly like today's card.

The headline mechanism change forced by adversarial review: the original "always show card, then probe-and-switch" model is **abandoned as the default** because it (a) permanently leaks a `profile_connections` slot on every switch — there is verifiably no DECR anywhere in the codebase, release is Dispatcharr's job and it never learns of the probe's manual INCR; (b) regresses every healthy tune with a card-encode + downstream nvenc + rebuffer; and (c) breaks the order-iterating selection loop when the card sits at order 0. The finalized model is **"real-source-first, card-on-failure"**: the override selects a real stream as today, and the startup card is shown only when selection fails or a fast health gate flags the top source as known-bad — zero regression for healthy channels, no manual slot accounting, and no order-0 trap. Strictly additive: prod Stream id 195362, `/stream.ts`, and ~2806 memberships are untouched.

---

## Open questions / decisions for the user

- **Startup trigger model (the biggest decision).** The earlier "always card then probe-and-switch" choice is shown by review to leak a connection slot per switch (no DECR exists; Dispatcharr releases only what *it* reserved, and the channel was started on the unlimited card so nothing real was reserved to release), and to regress every healthy tune. **Recommended default: "real-first, card-on-failure"** — the override picks a real source exactly as today; the startup card replaces only the *failure* states (no free slot, or top source flagged bad by a cheap async health gate). This keeps healthy tunes glitch-free, requires no manual `profile_connections` mutation, and avoids the order-0 trap. If you insist on "always card," it can be specified but only with a verified teardown-DECR owner identified first (currently unproven → guaranteed leak).
- **Does the live switch ever happen at all in v1?** With "real-first," the only place a switch is needed is recovering a channel *already* parked on the card when a real source frees up. **Recommended: defer the live `change_stream_url` switch entirely in v1** — a card-parked channel recovers naturally on the viewer's next re-tune (or a short card-session TTL forces re-selection). This removes the single most dangerous code path (out-of-band INCR + cross-identifier switch + non-owner pubsub ordering) from the first shippable version.
- **Startup card placement / order.** Order-0 actively fights the selection loop and becomes a permanent trap if the override is ever absent. **Recommended: do NOT add a membership row at all for `startup`; serve it purely as an override return value (programmatic), like the `tms` maxed-branch.** If apply/remove parity and channel-UI visibility are required, place it at a *high* order (e.g. 9998, just above `tms`) and have the loop hard-exclude all registry card stream-ids. Recommended default: programmatic-only (no membership), with a visibility note.
- **Health gate / fast-path.** Review shows the cache is near-useless for sparsely-tuned channels (cold at 45s TTL) and that a live byte-probe consumes a real provider connection slot, risking account-level bans on a failover wave. **Recommended: make the health gate optional, default OFF in v1**; when on, it is a *capacity-aware* check (read `profile_connections` before any network call) with a per-`m3u_account` concurrency semaphore and a longer ok-TTL.
- **`is_streams_maxed` refactor.** It currently mutates membership and *stops channels*. **Recommended: refactor to a pure read**; membership is owned solely by apply/remove/reconcile.
- **`tms_enabled` kill-switch vs 2806 live memberships.** **Recommended: when `tms_enabled=0`, the override skips the card branch (falls through to a graceful 503) rather than the server 404-ing `/stream.ts` mid-stream;** block disabling-while-applied in the UI help text.
- **Single source of truth for plugin metadata.** `plugin.py` and `plugin.json` duplicate all fields/actions and already drift (v2.1.3 vs v2.2.3). **Recommended: generate `plugin.json` from a single field-spec list at build time**; at minimum, reconcile versions and edit both in lockstep.
- **Probe in Celery.** Daemon threads die without running `finally` on worker recycle. **Recommended: never spawn probes/health work in Celery; only the web process does it** (and only if any health gate is enabled at all).
- **Stream-row purge on uninstall.** **Recommended: leave rows (cheap, instant re-apply);** offer a separate explicit "purge" action. Deleting id=195362 is irreversible.
- **Hourly reconcile beat task.** **Recommended: ship the manual action; make the beat task opt-in** (Celery here is ephemeral).
- **Cross-key image dedup / user-defined cards.** **Recommended: both deferred** — confirm acceptable.

---

## 1. Overview & goals

Today the plugin hardcodes exactly one placeholder "card" (the dynamic "Too Many Streams" grid). This redesign generalizes that single hardcoded identity into a **registry of configurable managed placeholder streams**, supporting two kinds:

- **DYNAMIC streams** — re-rendered when content changes. The existing TMS card (live grid of currently-active channels, with a directory fallback) is the built-in dynamic stream `tms`.
- **STATIC streams** — a fixed, configurable image/message. A new built-in static stream `startup` ("TMS: startup stream") is shown when a channel cannot immediately deliver a real source.

**Goals:**

1. Every managed stream is **user-configurable** (text, image, colours, behaviour, enable/disable) via the plugin UI.
2. The startup stream is **apply-able to ALL channels and removable** via plugin actions, exactly like the existing TMS card.
3. **Strictly additive / zero-break** for existing installs: the dynamic card (Stream id 195362, `/stream.ts`, ~2806 channel memberships at order 9999) keeps working untouched; the startup stream defaults to disabled & un-applied (opt-in).
4. One HTTP server on `:1337` serves all managed streams (routed by path); one hosting process; lazy per-card encoders.
5. The architecture leaves room for a 3rd+ managed stream with minimal new code.

**Trigger-model decision (revised from the draft).** The draft's "always show the startup card on every tune, then probe and live-switch to a healthy real source" is **withdrawn as the default.** Adversarial review established three independently fatal problems, each verified against the code:

- **Slot leak (blocker).** There is **no `DECR` of `profile_connections` anywhere in the plugin or, per grounding, in `change_stream_url`.** Release is Dispatcharr's teardown job, keyed off the channel's *own* reservation. On a fresh tune the channel starts on the startup card (profile 1, unlimited, **no INCR**). A probe that manually `INCR`s a *real* profile creates an increment that teardown will never match → **one permanently leaked slot per startup-switched channel**, eventually saturating `max_streams` and wedging real playback. No teardown-DECR counterpart could be identified, so the manual reservation is a guaranteed leak.
- **Healthy-tune regression (major).** The card is transcoded twice (plugin libx264, then the downstream proxy ffmpeg — universally nvenc on prod) on **every** tune, adds cold-start latency to the hot path, and forces a client-visible rebuffer (card→real input re-init) on every healthy tune, where today a healthy channel tunes straight through.
- **Order-0 trap (major).** The selection loop reserves the first order-sorted stream with a free profile slot and **does not exclude managed cards**; a card at order 0 on profile 1 (unlimited) always wins → every applied channel sticks on the card.

**Finalized model: "real-source-first, card-on-failure."** The override selects a real stream exactly as it does today. The startup card is returned **only** when real selection cannot immediately succeed (no free slot, or an *optional, default-off* health gate flags the top candidate as known-bad). This is **zero-regression for healthy channels**, requires **no manual `profile_connections` mutation** (the card uses profile 1 / unlimited, which never INCRs and never needs release), and never places a card where the order loop can mis-select it. The live `change_stream_url` switch and the out-of-band probe-reservation — the riskiest paths — are **deferred out of v1** (see §3.4); a card-parked channel recovers on the next re-tune or via a short card-session TTL.

**Known limitations (accepted):**
- A single shared encoder/image per card key **cannot show per-channel state** — one channel "starting" while another shows "unavailable" cannot both render on one shared image. The redesign makes the two *states* into two distinct *cards* on distinct routes (`startup` vs `tms`), which is the right granularity; within a single key it remains one shared image, as today.
- The startup card is a distinct failure-state surface, not a guarantee that any given tune is instant; "instant card on every tune" is explicitly **not** a goal of the finalized model.

## 2. Current state (verified recap)

- **`src/StreamServer.py`** — serves ONE MPEG-TS card on `0.0.0.0:1337` (`do_GET` accepts `/` and `/stream.ts`, else 404). Single shared LAZY ffmpeg encoder (`-re -loop 1 -framerate 1 -i IMG.jpg -re -f lavfi -i anullsrc ... libx264 -preset ultrafast -tune stillimage -threads 1 ... -f mpegts pipe:1`; nvenc/qsv branches present). Started on first client (`_ensure_running`), idle-stopped 60s after the last client (`IDLE_SHUTDOWN_GRACE`). Broadcaster fans encoder stdout into per-client 50-deep drop-on-full queues, reading `1316*16` bytes. **The updater constructs a NEW `PillowImageGen()` every loop iteration** and relies on the **process-global class attr `_last_active_uuids`** surviving across those instances to suppress re-encodes; on change it calls `_start_ffmpeg`. Default image: `img/too_many_streams2.jpg`. Hard-won fixes preserved: `-re` pacing, `-threads 1`, reap-after-kill, "restart only if still current and still watched". `do_GET` warms the encoder for **both** `/` and `/stream.ts`.
- **`src/PillowImageGen.py`** — renders 1920×1080 JPG. `get_active_streams()` SCANs `ts_proxy:channel:*:metadata`, filters valid UUIDs, builds an OTHER-channels grid via `ChannelStatus.get_basic_channel_info`, **excludes channels whose `info["url"] == get_stream_url()` — i.e. only the `tms` URL**; falls back to a directory of channels with the `tms` card applied (DB-ordered by indexed `channel_number`, capped 15); else "This Channel is Unavailable". Change-detection compares `self._current_uuids` to the **class-global `_last_active_uuids`**. Default render file `too_many_streams.jpg`; logo cache `/tmp/tms_logos`.
- **`src/TooManyStreams.py`** — `Channel.get_stream` override: (1) restore from `channel_stream:{self.id}` (**integer PK**) / `stream_profile:{stream_id}`; (2) iterate `self.streams.all().order_by("channelstream__order")`, reserve first profile with a free slot — `INCR profile_connections:{P}` only when `max_streams>0`, **no DECR anywhere**; write Redis keys; return `(stream.id, profile.id, None)`; (3) maxed branch returns `(custom_stream.id, None, None)` (dead on prod per grounding). `apply_to_all_channels()` is N+1. **`is_streams_maxed` is NOT pure: it calls `add_stream_to_channel` when true and `remove_stream_from_channel` when false — the latter STOPS the channel** (`ChannelService.stop_channel` + `proxy_server.stop_channel`). Identity: `Stream.objects.filter(name='TooManyStreams', url=get_stream_url())`.
- **`src/TooManyStreamsConfig.py`** — `get_config()` seeds **only 4 keys** (title/description/cols/log_level), then layers DB `settings`, then the persistent **file (overrides DB)**, then a small env override; caches a `PluginConfig`. **`save_plugin_persistent_config` does raw `json.dump(settings)` — it never constructs `PluginConfig` and never calls `dict()`.** `get_stream_url()` → `http://127.0.0.1:1337/stream.ts`. `get_host_and_port()` reads `TMS_HOST`/`TMS_PORT` env.
- **`src/schemas.py`** — `PluginConfig` dataclass; `from_dict` is hand-written field-by-field; `dict()` is `asdict`. No `schema_version`.
- **`plugin.py`** — `Plugin` (`version="2.1.3"`), flat `fields[]` (12), `actions[]` (4). `initialize()` installs override + log filter in EVERY process; hosts `StreamServer` in exactly one non-celery web process (`_should_host_server` + `_can_bind` precheck — TOCTOU between precheck and bind, handled only by catching `OSError`). **The host does NOT keep a reference to the `StreamServer` (fire-and-forget thread).** `run()` dispatches solely on the action-id string; **no `key`/params plumbing**.
- **`plugin.json`** — **duplicates all 12 fields and 4 actions verbatim** and is shipped by `build.sh`. **Version skew: `plugin.json` is `2.2.3`, `plugin.py` is `2.1.3`** — proof the two are hand-maintained and already drift.
- **`build.sh`** — `cp -r src` (new subpackages picked up automatically *if they have `__init__.py`*), `cp plugin.json`, deletes the stale zip before rebuilding.

**Verified Dispatcharr internals (authoritative):** card is `Stream id=195362, is_custom=true, m3u_account_id=1, stream_profile_id=NULL`; account 1 profile id=1 (`is_default, is_active, max_streams=0`=UNLIMITED). `generate_stream_url` requires truthy `stream_id` AND `profile_id` (else 503). `change_stream_url` does NOT touch `profile_connections` in either owner (`manager.update_url` in place) or non-owner (pubsub) path; with `new_url` passed it is used directly. `get_stream` runs in web AND celery.

## 3. Proposed architecture

A single **registry** of `ManagedStream` definitions is the source of truth that drives: the Dispatcharr `Stream` rows, HTTP routing in `StreamServer`, which renderer runs per path, channel application/order, and the `get_stream` override's per-key branching.

### 3.1 Managed-stream model & registry

A `ManagedStream` is a pure-data **definition** plus a resolved runtime `stream_id`. It owns no threads and does no I/O at construction. The `renderer`/`behavior` config is an opaque-to-the-registry **dict** (different kinds need different shapes).

```python
# src/managed_streams/definition.py
class StreamKind(str, Enum):
    DYNAMIC = "dynamic"
    STATIC  = "static"

@dataclass
class ManagedStream:
    key: str                       # "tms", "startup" -> registry id, URL slug, Redis namespace
    display_name: str
    stream_name: str               # exact Dispatcharr Stream.name for get_or_create
    http_path: str                 # canonical served path, e.g. "/tms/startup.ts"
    kind: StreamKind
    m3u_account_id: int = 1
    apply_order: Optional[int] = None   # None => NOT added as a ChannelStream (programmatic-only)
    is_custom: bool = True
    stream_profile_id: Optional[int] = None
    stream_url_path: Optional[str] = None   # override the URL stored in the Stream row
    enabled: bool = True
    builtin: bool = True
    legacy_paths: tuple = ()
    renderer: dict = field(default_factory=dict)
    behavior: dict = field(default_factory=dict)
    stream_id: Optional[int] = field(default=None, compare=False)

    def stored_url(self, host, port) -> str:
        path = self.stream_url_path or self.http_path
        h = "127.0.0.1" if host == "0.0.0.0" else host
        return f"http://{h}:{port}{path}"
    def all_paths(self) -> tuple: return (self.http_path, *self.legacy_paths)
    def redis_ns(self) -> str: return f"tms:ms:{self.key}"
```

**Back-compat URL encoding.** `tms` keeps stored `Stream.url=/stream.ts` (`stream_url_path="/stream.ts"`) and lists `/stream.ts` + `/` in `legacy_paths`; new defs get clean `/tms/<key>.ts` URLs. The two built-ins:

```python
# src/managed_streams/builtins.py
def _builtin_defs():
    return [
        ManagedStream(
            key="tms", display_name="Too Many Streams (dynamic grid)",
            stream_name="TooManyStreams", http_path="/tms/tms.ts",
            stream_url_path="/stream.ts", legacy_paths=("/stream.ts", "/"),
            kind=StreamKind.DYNAMIC, apply_order=9999, builtin=True, enabled=True,
            renderer={...}, behavior={"mode": "maxed_fallback"},
        ),
        ManagedStream(
            key="startup", display_name="Starting up",
            stream_name="TMS: startup stream", http_path="/tms/startup.ts",
            kind=StreamKind.STATIC,
            apply_order=None,            # programmatic-only by default (see 3.6 / OQ)
            builtin=True, enabled=False, # opt-in
            renderer={...},
            behavior={"mode": "first_failure",  # FINALIZED model, not "always_then_probe"
                      "health_gate_enabled": False,  # default OFF (review-driven)
                      "card_session_ttl_sec": 20},
        ),
    ]
```

**`ManagedStreamRegistry`** — process-wide singleton, lazy `_load()`, `reload()` (called by `TooManyStreamsConfig.clear_cache()`). Accessors drive every other lane: `all() / enabled() / get(key) / by_path(path) / by_stream_id(id) / card_stream_ids() / refresh_event(key)`. `card_stream_ids()` returns the set of all registry Stream ids and is the **single source** the override loop uses to exclude managed cards.

```python
def get_or_create_stream(self, ms):
    host, port = TooManyStreamsConfig.get_host_and_port()
    url = ms.stored_url(host, port)
    row, _ = Stream.objects.get_or_create(
        name=ms.stream_name, url=url,
        defaults=dict(is_custom=ms.is_custom, channel_group=None,
                      stream_profile_id=ms.stream_profile_id))
    ms.stream_id = row.id
    return row
```

**Atomicity caveat (review-driven):** `get_or_create` is only race-free against a DB unique index. The codebase does **not** confirm a unique constraint on `Stream(name, url)`. We therefore treat `get_or_create` as **best-effort**; it remains strictly better than the current get-then-create, and is back-compatible because `(name="TooManyStreams", url="/stream.ts")` matches the prod row **for the default host/port only**. **Host/port coupling:** `Stream.url` embeds host:port; an install with a non-default `TMS_PORT`/`TMS_HOST` already has a different stored URL and would get a *second* row on upgrade — this fragility exists today and is documented, not introduced. Recommendation: document that `TMS_HOST`/`TMS_PORT` must be stable across upgrades.

| Today | After |
|---|---|
| `TooManyStreams.STREAM_NAME` | `registry.get("tms").stream_name` |
| `get_or_create_stream()` | `registry.get_or_create_stream(registry.get("tms"))` |
| `get_stream_url()` | `registry.get("tms").stored_url(...)` (kept as back-compat shim) |
| global `REFRESH_SIGNAL` | `registry.refresh_event(key)` per dynamic key |
| single-path `StreamServer` | one `StreamServer` routing all `enabled()` paths |
| (none) | `registry.card_stream_ids()` — loop exclusion set |

### 3.2 StreamServer (multi-stream HTTP + encoders)

Split into:

- **`ManagedStreamChannel`** — owns ONE card's lazy encoder + broadcaster (+ updater for dynamic only); today's `StreamServer` body minus the HTTP server, parameterised by a `ManagedStream`. All hard-won behaviour preserved per card verbatim.
- **`StreamServer`** — owns the single `ThreadingHTTPServer` on `0.0.0.0:1337`, a `dict[key -> ManagedStreamChannel]`, routing, and `reload()`.

**Routing — resolves against `enabled()` only (so disabled keys 404 without warming an encoder):**

| Path | Resolves to | Notes |
|---|---|---|
| `/healthz` | 200 `ok` | matched FIRST, before the `/` alias; warms no encoder (dead-`:1337` self-check) |
| `/stream.ts`, `/` | `tms` (if enabled) | back-compat aliases; **404 if `tms` disabled** |
| `/tms/<key>.ts` | `<key>` (if enabled) | strict `^/tms/([a-z0-9_]{1,32})\.ts$`; disabled/unknown ⇒ 404 |
| anything else | 404 | |

**Encoder freshness:** `_get_ffmpeg_cmd` today re-reads `config.video_encoder` live at each encoder start. After the split, `video_encoder` is **snapshotted at `ManagedStreamChannel` construction**; an encoder change takes effect on `reload()`. The carry-over predicate (below) **includes `encoder`**, so an encoder edit *does* rebuild the channel.

**dynamic vs static** differ only in lifecycle around the image (ffmpeg command identical):
- **Dynamic** = encoder + broadcaster + **updater** (re-renders on active-set change → one-discontinuity restart; blocks on the per-key refresh `Event` when idle). 3 threads warm.
- **Static** = encoder + broadcaster, **no updater** (image never changes ⇒ no Redis/DB polling, no re-render). 2 threads warm. User image fed directly (Pillow bypassed); else rendered once into the per-key file and treated immutable.

**`reload()`** rebuilds the dict; removed/disabled cards `shutdown()`; warm cards whose **`key`+`kind`+`image_path`+`encoder`** are unchanged are carried over (an unrelated edit never bounces an in-use card). **The host MUST keep a reference to the `StreamServer`** (today it is fire-and-forget) and `reload()` is a **no-op in non-hosting workers** (they hold no server). **Cross-process ordering invariant (review-driven):** because `get_stream` returns card ids in *all* processes but only the host serves `:1337`, the host must `server.reload()` to add a new card's route **before** any worker can return that card's id. In v1 this is trivially satisfied: the only new card is `startup`, served at a fixed path that the host binds at startup; enabling it is a config flag that both the registry (everywhere) and the host's server (via `reload`) read from the same persisted config, and the override only returns a `startup` id when `enabled AND` the server already advertises the path. If a worker ever returns a card id whose path the live server doesn't serve, the proxy 503s and retries (no crash).

### 3.3 Renderers (dynamic vs static)

A `Renderer` interface generalizes today's `PillowImageGen`. Each managed stream gets its own renderer instance, its own output file (`<render_dir>/<key>.jpg`), and **per-instance change-detection state**.

**Mandatory blocker fix:** replace the **process-global** `PillowImageGen._last_active_uuids` with an **instance** `_last_signature`. **Coupling correction (review-driven):** the current updater creates a *new* `PillowImageGen()` every loop and depends on the class-global surviving across instances. Moving the signature to instance state in isolation would make every updater iteration re-render+restart. Therefore the signature fix is **inseparable from the long-lived-renderer ownership change** — `ManagedStreamChannel`/`RenderManager` must hold ONE renderer instance across iterations. The phased plan (§8) reflects this: the two land **together** in Phase 0, not split across phases.

- **`DynamicRenderer`** wraps today's logic intact (`poll()`==`get_active_streams()`, `render()`==`generate()`); changes: per-instance signature, and config read from the per-stream renderer blob.
- **`StaticRenderer`** renders a fixed card once (re-rendered only on config/mtime/size change; `poll()` does no Redis/DB and returns `False`):
  - **Passthrough mode** (user `background_image_path`): normalize/letterbox to 1920×1080 JPEG (or `copyfile` if already so) into `out_path`; corrupt/missing ⇒ card mode (never a black screen).
  - **Card mode**: solid bg + centred title + message + optional static motif. No animation.

Shared font/`_hex_to_rgb`/centred-text helpers factor into `src/renderers/draw_utils.py` (no behavioural change to the dynamic path).

**Self-exclusion deviation (required, review-driven):** today `get_active_streams` excludes only the `tms` URL. With `startup` live, a channel showing the startup card would leak into the dynamic grid. The dynamic renderer's exclusion **must check ALL managed-card URLs/ids** via `registry.card_stream_ids()` / `registry.by_path`, an **explicit deviation from "byte-for-byte."**

`RenderManager` owns one renderer per enabled key (`render_path / renderer / ensure_initial / reload`), carries over `_last_signature`, writes to the **persistent volume** `/data/plugins/TMS_Persistent_Config/tms_render/<key>.jpg`. **Migration of the existing image:** the dynamic card's first post-upgrade frame is produced by `RenderManager.ensure_initial("tms")` (force-render) into `tms_render/tms.jpg`; the bundled `img/too_many_streams2.jpg` remains the *fallback seed* only if rendering fails, and a user `tms_image_path` continues to be honored exactly as today. Logo cache stays ephemeral at `/tmp/tms_logos`.

### 3.4 Startup runtime flow (real-first, card-on-failure)

The override is generalized but its **healthy path is unchanged**. The normal loop gains **one hard invariant** and the failure branches gain the `startup` card.

**Override ordering (authoritative, finalized):**

```
0. no streams        -> (None, None, "No streams assigned to channel")
1. restore session   -> channel_stream:{int id} + stream_profile:{sid} present? return them
2. normal loop       -> iterate self.streams by channelstream__order, EXCLUDING any
                        stream id in registry.card_stream_ids(); reserve first real
                        profile with a free slot (INCR only if max_streams>0). UNCHANGED
                        except the exclusion. (This is the healthy path; zero regression.)
3a. optional health gate (default OFF) -> if startup active AND the chosen real stream's
                        top profile is flagged tms:health=="bad" (cheap Redis read, NO
                        network in the hot path), treat as "no immediate source".
3b. maxed / no-immediate-source -> if startup is_kind_active(channel):
                        return (startup_card_id, 1, None)  [profile 1 = unlimited, NO INCR]
                        write channel_stream:{int id}=startup_card_id,
                              stream_profile:{startup_card_id}=1, with a SHORT TTL
                              (card_session_ttl_sec, default 20s) so a later re-tune
                              re-enters selection instead of restoring the card forever.
4. tms maxed-out     -> existing dynamic tms card branch (UNCHANGED behaviour), now
                        reading a PURE is_streams_maxed (membership mutation removed).
```

**Why this resolves the blockers:**
- **No slot leak.** The card is returned with profile 1 (unlimited) ⇒ **no INCR, nothing to release.** The plugin performs **no manual `profile_connections` mutation** anywhere. The only INCR remains the existing normal-loop reservation, which Dispatcharr's teardown already balances exactly as today.
- **No healthy-tune regression.** Healthy channels take the normal loop and tune straight to the real source — no card, no double-transcode, no switch, no rebuffer.
- **No order-0 trap.** `startup` is **programmatic-only** (`apply_order=None`, no `ChannelStream` row) — it is returned by the override, never reached by the order loop. The loop additionally **excludes all `card_stream_ids()`** so even the `tms` order-9999 row (and any future card row) can never be mis-selected as a real source.

**`is_kind_active(channel, ms)`** = `ms.enabled AND` (for programmatic-only startup) `ms.behavior` active. Because `startup` has no membership, "applied to all channels" is expressed as a **global enable flag** plus an optional explicit allow/deny scope; the apply/remove actions toggle that flag (and, if the user opts into membership placement per the OQ, manage rows at a high order with the same exclusion guaranteeing safety). The cheap candidate-existence question ("does this channel have ≥1 real stream?") is answered **without extra DB work** by reusing the same `self.streams` queryset the normal loop already evaluates and subtracting `card_stream_ids()` — no `m3u_account` dereference in the hot path.

**Card-session lifecycle (review-driven, closes the "stuck on card" bug).** The card session keys get an explicit **TTL** (`card_session_ttl_sec`). This is the critical divergence from today's untTL'd `channel_stream` write: it guarantees a channel parked on the startup card **re-enters selection on the next tune after the TTL**, so a channel whose first attempt found everything maxed automatically retries when a slot frees up — without any live switch, probe, or manual accounting. The `stream_profile:{startup_card_id}=1` orphan is harmless (the card is always profile 1) and also expires.

**Deferred to a later phase (explicitly NOT in v1), pending the OQ decision on "always-card":** the out-of-band probe, manual profile reservation, and `change_stream_url` live switch. If proactive recovery of a *currently-parked* channel is later chosen (rather than on re-tune), that feature must first identify a **verified teardown-DECR owner** for any manually-reserved slot, pin the **channel-id identity** for `change_stream_url` (the session keys are integer-PK; the switch identifier must be confirmed, not assumed UUID), bound the **all-inclusive time budget** (`wait_ready + health + reserve-retries + switch < guard_ttl`, extending the guard via `EXPIRE`), add a **per-`m3u_account` probe concurrency semaphore** plus a **capacity precheck before any network probe** (read `profile_connections` first), guard against **daemon-thread death in Celery** (TTL-based self-heal, never rely on `finally`), and define the **non-owner pubsub ordering/ack** and **false-`ok` health poisoning** mitigations.

**Optional health gate (default OFF).** When enabled, it is purely a **Redis read** of `tms:health:{stream_id}` in the hot path (no network). The `bad`/`ok` entries are populated by a **separate, web-process-only, capacity-aware, per-account-throttled background check** (never from a Celery daemon thread, never before checking `profile_connections`). A live byte-probe consumes a real provider connection slot and risks account bans on a failover wave, so on an "all maxed" profile the gate **must not probe** — it simply lets the maxed branch show the card. False-`ok` poisoning is mitigated by a stricter ok-criterion (sustained bytes) and a conservative ok-TTL; this is why the gate ships **off by default.**

### 3.5 Configuration & plugin UI

**Decision: namespaced flat keys, NOT a JSON blob** (Dispatcharr `fields[]` are flat/typed/single-value; a blob loses labels/help and corrupts trivially). Existing keys are **not renamed** (renaming silently resets prod). New `startup_*` keys get a clean prefix.

**Single-source-of-truth fix (blocker).** `plugin.py` `fields[]`/`actions[]` are **duplicated in `plugin.json`** and already drift (2.1.3 vs 2.2.3). **Every field/action change MUST land in both**, and `build.sh` ships `plugin.json`. **Recommendation: generate `plugin.json` from one Python field-spec list** at build time (a `gen_plugin_json.py` step in `build.sh`); at minimum reconcile the versions and edit in lockstep.

**Reduced UI surface (review-driven UX fix).** The 9 internal probe knobs are **NOT** top-level fields. v1 exposes only:

*Shared:* `tms_log_level`, `video_encoder`.
*Dynamic (existing, UNCHANGED keys):* `tms_enabled` (number 0/1, default 1), `stream_title`, `stream_description`, `stream_channel_cols`, `tms_image_path`, `theme_bg_color`, `theme_card_bg_color`, `theme_card_border_color`, `theme_text_color`, `theme_accent_color`, `theme_accent_text_color`.
*Startup (new):* `startup_enabled` (number 0/1, default 0), `startup_title`, `startup_message`, `startup_image_path`, `startup_bg_color`, `startup_text_color`, `startup_accent_color`.

All probe/health-gate knobs (`card_session_ttl`, gate on/off, timeouts, TTLs, min_bytes, per-account concurrency) are **persistent-file-only overrides**, defaulted in code, never shown in the form. Booleans encoded as `number` 0/1 are normalized by an `_b` coercer (**any nonzero → 1**).

**`tms_enabled=0` interaction (review-driven).** When `tms_enabled=0` with ~2806 live order-9999 rows still applied: the override **skips the card branch and falls through to a graceful 503** (the existing "No compatible profile" error), rather than the server 404-ing `/stream.ts` mid-stream. `help_text` warns that disabling without removing leaves dead membership.

**Config model & save path (blocker fix).** Two concrete code changes the draft omitted:
1. `save_plugin_persistent_config` (or `run()`'s save branch) **must stamp `schema_version=2` and run `_migrate_persistent` on the raw dict before `json.dump`** — the current path bypasses `from_dict`/`dict()` entirely.
2. The **startup-colour inheritance** ("inherit existing `theme_*` on first enable") **must run in `from_dict`** (which sees the fully merged `final_data` incl. DB `theme_*`), **not in `_migrate_persistent`** (which sees only the file).

`PluginConfig` keeps a **flat face** (so `config.stream_title` etc. keep working) and gains a **derived `streams` projection** rebuilt every `from_dict` (never persisted; `dict()` pops it). The registry overlays its `renderer`/`behavior` blobs from `config.stream(key)`. `clear_cache()` additionally calls `ManagedStreamRegistry.reload()` and rebuilds `RenderManager`, and — **only in the hosting process** — `server.reload()`. The **file-overrides-DB** precedence is a pre-existing UX hazard now amplified by more keys; documented in `help_text`.

**`actions[]`** — keep the four existing; add `apply_startup_stream`, `remove_startup_stream` (confirm), `reconcile_managed_streams` (no confirm). `run()` gains **explicit dispatch branches per action id**; a small `apply(key)` / `remove(key)` helper takes the registry key.

### 3.6 Channel application, ordering & migration

**Startup is programmatic-only by default (no membership row).** This is the safest resolution of the order-0 trap: the card is returned by the override, never reached by the order loop, and there is no inert row to become a trap if the override is ever absent. "Apply to all channels" for startup is a **global enable + optional scope**, toggled by `apply_startup_stream`/`remove_startup_stream`. *If* channel-edit-UI visibility / `ChannelStream`-queryability is required (OQ), startup is placed at a **high order (9998)** and the loop's `card_stream_ids()` exclusion guarantees it is never mis-selected; it is **never** placed at order 0.

**`is_streams_maxed` refactored to a pure read** (resolves the §3.4-vs-§3.6 contradiction). Membership is owned solely by apply/remove/reconcile; the maxed branch no longer mutates membership or stops channels. `tms` continues to be applied at order 9999.

**Apply (batched, idempotent — replaces N+1)** for any membership-bearing card:

```python
def apply_to_all_channels(spec, batch_size=1000):
    if spec.apply_order is None: return            # programmatic-only; nothing to insert
    stream = registry.get_or_create_stream(spec)
    all_ids  = set(Channel.objects.values_list("id", flat=True))
    existing = set(ChannelStream.objects.filter(stream_id=stream.id).values_list("channel_id", flat=True))
    rows = [ChannelStream(channel_id=c, stream_id=stream.id, order=spec.apply_order)
            for c in (all_ids - existing)]
    for i in range(0, len(rows), batch_size):
        ChannelStream.objects.bulk_create(rows[i:i+batch_size], ignore_conflicts=True, batch_size=batch_size)
```

O(1) stream queries; idempotent; `ignore_conflicts` guards concurrent apply. Re-applying `tms` adds 0 rows.

**Remove (fleet-safe bulk):** `ChannelStream.filter(stream_id=stream.id).delete()`. Default `stop_running=False` (does NOT stop the fleet — today's per-channel remove stops channels, catastrophic across 2806). `stop_running=True` opt-in, scoped via Redis to channels currently on this card.

**New-channel handling.** `post_save(Channel, created=True)` signal (primary) + manual `reconcile_managed_streams` (backstop). **`dispatch_uid` dedups receivers WITHIN a process**, **not across processes** — cross-process double-apply is moot because a `Channel.save()` only fires receivers in the saving process. The signal **is installed in Celery too** (like the override) so single-row creates in Celery tasks apply correctly; the **real gap is `bulk_create` M3U imports** (which skip `post_save`), covered by reconcile. (For programmatic-only startup, the signal is a no-op — there is no row to add.)

**Disable** (`<kind>.enabled=False`) is a config flag, cheap and reversible. For `tms` it leaves the order-9999 rows inert and the override skips the card branch (graceful 503, not 404 mid-stream). For programmatic-only startup, disable is simply the gate going inactive — there are no inert rows to become a trap.

## 4. Data model & Redis keys

**Dispatcharr `Stream` rows:**

| key | Stream.name | stored Stream.url | served paths | apply_order |
|---|---|---|---|---|
| `tms` | `TooManyStreams` | `http://127.0.0.1:1337/stream.ts` | `/stream.ts`, `/`, `/tms/tms.ts` | 9999 |
| `startup` | `TMS: startup stream` | `http://127.0.0.1:1337/tms/startup.ts` | `/tms/startup.ts` | None (programmatic-only) |

**Redis keys:**

| Key | Value | TTL | Notes |
|---|---|---|---|
| `channel_stream:{int channel_id}` | stream_id | none for real (proxy lifecycle); **short TTL (`card_session_ttl`, ~20s) when set to a card** | keyed by **integer PK**; card writes get a TTL so parked channels re-select |
| `stream_profile:{stream_id}` | m3u_profile_id (1 for card) | matches its `channel_stream` lifecycle | card variant orphan is harmless |
| `profile_connections:{profile_id}` | int | none | **INCR only by the normal loop when `max_streams>0`; no DECR in plugin** (release is Dispatcharr's) — card never touches it |
| `tms:maxed_out:{channel_id}` | int | 30s (existing) | unchanged; now read by a **pure** `is_streams_maxed` |
| `tms:health:{stream_id}` | `ok`/`bad` | conservative ok-TTL / short bad-TTL | **only used when the optional gate is enabled**; populated by the web-process, capacity-aware background check |
| `ts_proxy:channel:{uuid}:metadata` | hash | proxy-managed | read by dynamic grid / log filter |

**No untTL'd plugin-written card session** — the explicit card-session TTL is the mechanism that prevents "stuck on card forever." (Probe-specific guard/single-flight keys belong to the deferred feature and are not in v1.)

**Persistent config (v2, flat):** `schema_version: 2` + all flat keys. Rendered images at `/data/plugins/TMS_Persistent_Config/tms_render/<key>.jpg`.

## 5. Resource impact (honest accounting)

**Per-warm-encoder (plugin side):** ~1% of one core at 1 fps **because of `-re`** (without it, ~3 cores); ~30–60 MB RSS + the JPG; 1 ffmpeg + 1 broadcaster (+ updater for dynamic). **Plus per-client queue memory:** up to 50 × `1316*16` ≈ 1 MB/client; a 50-client wave ≈ ~50 MB of buffered TS in the plugin process, plus thread stacks.

**Plugin-side vs system-side (review-driven separation):**
- **Plugin side** — concurrent encoders = number of **distinct cards currently being viewed** (not channels, not configured cards). Many channels on one card share one encoder via path fan-out. This part is genuinely cheap.
- **System side** — each maxed/parked channel is a **separate downstream proxy ffmpeg (nvenc on prod)** pulling `:1337`. 50 concurrent channels on the card = 1 plugin encoder + **50 downstream nvenc transcodes** of the (low-bitrate, GOP-1) card. The card being cheap per-transcode keeps each small, but **system cost scales linearly with concurrent card-served channels** — it is **not** scale-free.

**Why the finalized model is much cheaper than "always card":** the startup card is served **only on failure**, not on every tune. Healthy tunes incur **zero** card cost (plugin or downstream), zero switch, zero rebuffer — identical to today. The card encoder warms only during genuine maxed/failure conditions.

**Steady-state correction (review-driven).** "0 encoders when nothing maxed" is true only with no persistent clients. Per prod reality, TiviMate often **holds a persistent connection** to a card-fed channel, keeping that encoder warm indefinitely (idle-stop is 60s after the *last* client). Quantify warm-fraction empirically rather than asserting 0.

**Mass-failover restarts (review-driven).** The dynamic `tms` encoder restarts **once per active-set transition** during a wave (each flip changes the grid → re-render + ffmpeg respawn), **not once total.** A debounce/coalesce window is a recommended hardening (Phase 6), not a v1 requirement.

## 6. Backward compatibility & migration

**Strictly additive.** `tms` keeps `name="TooManyStreams"`, stored `url="/stream.ts"`, `order=9999`; prod row id=195362 and its ~2806 rows satisfy the registry identity (best-effort `get_or_create` returns the existing row **for default host/port**; non-default `TMS_PORT`/`TMS_HOST` risks a duplicate row — documented). `/stream.ts` and `/` serve identical bytes. `get_stream_url()` keeps its `/stream.ts` return. Existing actions keep ids. Startup ships **disabled & un-applied** ⇒ no behavioural change until opt-in.

**Config migration (v1→v2), additive-only & idempotent.** `_migrate_persistent(data)` only **stamps and back-fills** (never drops/rewrites): if `version<2`, add new keys (startup disabled; `tms_enabled=1`), set `schema_version=2`. **Startup-colour inheritance is performed in `from_dict`** (sees merged DB `theme_*`), not in the migration. The migration runs in `get_plugin_persistent_config()` (called at import in **every** process) — so the write-back **must be atomic** (`write tempfile + os.replace`) and ideally guarded to the hosting process. **The Save path must also explicitly stamp v2 + migrate before `json.dump`.**

**Build/packaging.** `cp -r src` ships new subpackages **only if `src/managed_streams/__init__.py` and `src/renderers/__init__.py` exist** — add them. `plugin.json` must be rebuilt in lockstep with fields (ideally generated). Reconcile the existing 2.1.3/2.2.3 skew. New `/tms/<key>.ts` paths + renderer package must be in the shipped zip or channels point at paths the running server doesn't serve.

## 7. Edge cases & failure modes

- **All real candidates maxed:** override returns the card with profile 1 (no INCR); the **card-session TTL** ensures the next tune re-selects when a slot frees — no stuck-on-card, no leak, no live switch.
- **Channel parked on card, source recovers:** recovers on next re-tune after the card-session TTL expires (v1); proactive recovery is the deferred switch feature.
- **`tms_enabled=0` with live memberships:** override skips the card branch → graceful 503, not a mid-stream 404.
- **Static user image swapped on disk:** picked up **only** at cold start (post idle-stop) or explicit `reload()`; while warm the image is frozen.
- **Bad/corrupt/missing user image:** `StaticRenderer` falls back to card mode — never a black screen.
- **Disabled key:** override doesn't return it; server 404s the path; `/` alias 404s if `tms` disabled; `/healthz` matched before `/`.
- **Two workers race a Stream row / apply:** `get_or_create` (best-effort) + `bulk_create(ignore_conflicts)` keep both safe; a duplicate row only arises under non-default host/port.
- **Dynamic grid self-exclusion:** excludes **all** managed-card ids so card-parked channels never leak into the grid.
- **Mass failover encoder churn:** restarts scale with active-set transitions; debounce is a Phase 6 hardening.
- **Optional health gate (if enabled):** false-`ok` poisoning and provider-connection consumption are explicitly bounded (web-process-only, capacity-aware, per-account-throttled, sustained-bytes ok-criterion). Default OFF.
- **Dead-`:1337`-listener:** `/healthz` lets the host self-check without warming an encoder; paired with `_can_bind`/single-host gating.

## 8. Phased implementation plan

Each phase is independently shippable; nothing before its phase changes runtime behaviour for existing installs.

**Phase 0 — Renderer decoupling + long-lived renderer (coupled, per review).** Replace the class-global `_last_active_uuids` with instance `_last_signature` **and** make the updater/`ManagedStreamChannel` hold **one persistent renderer instance** across iterations (the two are inseparable — splitting them regresses the dynamic card). Extract `draw_utils.py`. Output byte-for-byte identical. *Ships invisibly; unblocks everything.*

**Phase 1 — Config model + registry (no new streams served).** Add `CONFIG_SCHEMA_VERSION`, the config dataclasses + derived `streams` projection + `tms_enabled`, `_migrate_persistent`, **atomic write-back**, **save-path v2 stamping**, **from_dict colour inheritance**. Add `src/managed_streams/` (with `__init__.py`), only `tms` enabled. Wire `clear_cache()`→`reload()` + `card_stream_ids()`. *UI/behaviour unchanged.*

**Phase 2 — Multi-stream StreamServer (still only `tms`).** Split into `ManagedStreamChannel` + `StreamServer(registry)`; path routing + `/healthz` (matched before `/`) + `reload()` (no-op off-host); `RenderManager`; **host keeps a server reference**. `/stream.ts` + `/` serve `tms` identically. *Verified identical bytes.*

**Phase 3 — Loop exclusion + pure `is_streams_maxed` + batched apply/remove + signal/reconcile.** Add the `card_stream_ids()` exclusion to the normal loop (safe: only `tms` at 9999, behaviour unchanged). Refactor `is_streams_maxed` to pure. Generalize apply/remove (batched, idempotent, fleet-safe remove); keep `apply/remove_from_all_channels` as `tms` wrappers. Install `post_save` signal (within-process `dispatch_uid`) + `reconcile_managed_streams`. *Dynamic card unaffected; startup not enabled.*

**Phase 4 — Static renderer + `startup` card served (no override change).** Implement `StaticRenderer` (card + passthrough). Enable the `startup` built-in path (config-disabled by default). Add startup `fields[]` (both `plugin.py` and `plugin.json`/generator) + `apply_startup_stream`/`remove_startup_stream`. Because `startup` is **programmatic-only (no membership)**, applying it cannot wedge the order loop. Serving `/tms/startup.ts` works for preview; no runtime selection yet. *Configure/preview the card.*

**Phase 5 — Startup runtime flow (real-first, card-on-failure).** Add the override's failure-branch return of `(startup_card_id, 1, None)` with the **TTL'd card session**, gated on `is_kind_active(startup)`. No probe, no live switch, no manual reservation. *The card appears on genuine maxed/failure tunes for opted-in installs; zero healthy-path change.*

**Phase 6 — Hardening / optional features.** Per-card encoder override; updater debounce/coalesce; uninstall/rollback (disconnect signal, restore `Channel.get_stream`, optional row purge); **and — only if proactive recovery over re-tune is chosen (OQ) — the deferred probe+switch feature**, which must first deliver: verified teardown-DECR owner, pinned channel-id identity for `change_stream_url`, all-inclusive time budget with guard `EXPIRE`, per-account probe semaphore + capacity precheck, Celery-thread-death TTL self-heal, non-owner pubsub ordering/ack, and false-`ok` poisoning mitigation. Optional capacity-aware health gate. Optional hourly reconcile beat.

## 9. Open questions / decisions for the user

See the **Open questions / decisions for the user** section near the top (trigger model, live-switch deferral, card placement/order, health-gate default, `is_streams_maxed` refactor, `tms_enabled` kill-switch behaviour, plugin.json single-source, Celery probe policy, row purge, reconcile beat, image dedup, user-defined cards). Recommended defaults are stated inline.

---

### Review issues resolved (what changed from the draft)

- **Blockers (slot leak):** the "always card then probe-and-switch + manual INCR" mechanism is **removed from v1.** The finalized "real-first, card-on-failure" model performs **no manual `profile_connections` mutation** (card = profile 1, unlimited, no INCR, nothing to release), eliminating the leak class entirely. Any future proactive-recovery switch is deferred behind a hard prerequisite to first identify a verified teardown-DECR owner.
- **Blockers (session write before switch, channel-id identity, non-owner pubsub):** the live switch is **deferred**; v1 has no out-of-band session write racing a switch and no cross-identifier hazard. Card sessions are written under the **integer PK** with an explicit **TTL**.
- **Blocker plugin.json drift + version skew:** elevated to a first-class requirement — edit both / generate `plugin.json` from a field-spec; reconcile 2.1.3/2.2.3.
- **Blocker save-path bypass:** documented that Save uses raw `json.dump`; specified the required v2-stamp + migrate-before-write change.
- **Major fast-path/order-0/normal-loop mis-selection:** the normal loop now **hard-excludes `card_stream_ids()`**; `startup` is **programmatic-only**; the fragile fast-path is removed (the optional health gate is a hot-path Redis read only, default off).
- **Major `is_streams_maxed` mutation contradiction:** refactored to a **pure read**; membership owned by apply/remove/reconcile; maxed branch no longer stops channels.
- **Major resource accounting:** §5 rewritten to separate plugin-side from system-side cost, correct "0 encoders" and "restart once"; the card now costs nothing on healthy tunes because it is failure-only; provider-connection and health-gate risks bounded; gate ships off by default.
- **Major Phase-0 entanglement:** the instance-signature fix is **merged with long-lived-renderer ownership** into Phase 0.
- **Major reload/registry desync, signal-in-Celery, fresh-tune DB cost, apply hot-path tax:** host keeps a server reference, `reload()` no-op off-host with a stated cross-process ordering invariant; `dispatch_uid` rationale corrected (within-process); candidate-existence check reuses the loop's queryset with no extra `m3u_account` deref.
- **Minors/nits:** encoder-snapshot freshness, enabled-only routing, static-image freshness, stuck-on-card TTL, self-exclusion of all card URLs, atomic migration write-back, `get_or_create` non-atomicity, host/port-in-url duplicate-row risk, default-image path mapping, `__init__.py` packaging, `tms_enabled=0` 404→503, run() dispatch wiring, queue memory accounting, UI knob reduction + `_b` coercer — all incorporated.

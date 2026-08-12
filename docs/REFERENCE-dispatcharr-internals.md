# Reference — Dispatcharr 0.21.1 internals the plugin depends on

Everything here was **read from the running production container**
(`jflix_dispatcharr`, Dispatcharr `0.21.1`, namespace `apps.proxy.ts_proxy`).
Paths are inside the container at `/app/...`. Line numbers are for 0.21.1 and will
drift across Dispatcharr versions — **re-verify before relying on them.**

> ⚠️ A multi-agent design pass hallucinated a "v0.26.0 / `live_proxy`" tree with
> different line numbers (`release_stream@743`, `update_stream_profile@854`,
> `live:channel:…` metadata). That tree **does not exist on this box.** Do not use
> those citations.

## 0. The placeholder card as a Dispatcharr object

| Field | Value |
|---|---|
| `Stream.id` | **195362** |
| `Stream.name` | `TooManyStreams` |
| `is_custom` | `true` |
| `m3u_account_id` | **1** (the auto-created "Custom Streams" account) |
| `stream_profile_id` | `NULL` |
| `url` | `http://127.0.0.1:1337/stream.ts` |
| applied as `ChannelStream` | `order = 9999` on ~2806 channels |

Account 1's profile: `id=1`, `name="custom Default"`, `is_default`, `is_active`,
**`max_streams=0` (UNLIMITED)**.

**Why this matters:** the card has a real M3U account/profile, so the normal stream
selection loop hands it profile 1 and returns `(195362, 1, None)` — a *real*
profile id — which `generate_stream_url` accepts and serves. The card is therefore
**not** profile-less; it is just unlimited (so it never consumes an enforced slot).

## 1. Stream selection — `Channel.get_stream()`

`apps/channels/models.py:435` (the plugin **overrides** this; the override mirrors
the original and adds the TMS branches — `src/TooManyStreams.py:188`).

Returns `(stream_id, profile_id, error_reason)`. Flow:

1. **Restore session:** `GET channel_stream:{int channel_id}` → `GET
   stream_profile:{stream_id}`; if both present, return them (no new reservation).
2. **Select & reserve:** iterate `self.streams.all().order_by("channelstream__order")`;
   for the first stream whose active M3U profile has a free slot, **reserve it
   atomically** (`_check_and_reserve_profile_slot`, models.py ~408: `INCR
   profile_connections:{P}`; if the new count `> max_streams`, `DECR` to roll back;
   `max_streams=0` ⇒ unlimited ⇒ **no INCR**), then `SET channel_stream:{int}=sid`,
   `SET stream_profile:{sid}=P`, and return `(sid, P, None)`.
3. Maxed/none → `(None, None, error_reason)`.

A stream with **no `m3u_account` is skipped** (`if not m3u_account: continue`).

## 2. Serving — `generate_stream_url()` / `stream_ts`

- `apps/proxy/ts_proxy/url_utils.py:86` — calls `channel.get_stream()` and
  **requires both `stream_id` AND `profile_id` to be truthy**, else returns `None`
  (→ the endpoint 503s after retries). Builds the URL via
  `transform_url(stream.url, profile.search_pattern, profile.replace_pattern)`;
  user-agent via `m3u_account.get_user_agent()`; the transcode flag from
  `channel.get_stream_profile()` (the default `core_streamprofile` — the
  ffmpeg/proxy profile, **separate** from the M3U connection profile).
- `apps/proxy/ts_proxy/views.py:47` `stream_ts` — the serving endpoint. On (re)init
  it retries `generate_stream_url` for ~3 s; reads `channel_stream:{ch}` /
  `stream_profile:{sid}` from Redis. Runs in **gevent uwsgi workers**.

## 3. Connection-slot accounting (`profile_connections`)

This is the leak-critical subsystem. **One INCR must be balanced by one DECR.**

### INCR sites
- `Channel.get_stream` / `_check_and_reserve_profile_slot` (models.py ~408) — the
  normal reservation.
- **`Channel.update_stream_profile(new_profile_id)` (models.py:651)** — an atomic
  Redis-pipeline **profile switch**: reads `channel_stream:{int}`→`stream_profile:{sid}`,
  and **if the current profile ≠ new**, `DECR` old / `SET stream_profile:{sid}=new` /
  **`INCR profile_connections:{new}`**. Early-returns (no change) if current==new, or
  if `stream_profile:{sid}` is absent.

### DECR site — `Channel.release_stream()` (models.py:533)
The release owner, called on **every** teardown path (`views.py` client
disconnect/failed-init ×4, `server.py` cleanup ×2, `channel_service.stop_channel`,
`stream_generator`):
- **PRIMARY:** `GET channel_stream:{int}` → `GET stream_profile:{sid}` → if
  `profile_connections:{pid} > 0`, **`DECR`** it (~627); then `DELETE` the two keys
  and `HDEL` the metadata `STREAM_ID`/`M3U_PROFILE` fields.
- **FALLBACK** (primary key already gone): recover `stream_id`+`profile_id` from the
  channel metadata hash `ts_proxy:channel:{uuid}:metadata` fields `STREAM_ID` /
  `M3U_PROFILE`, **`DECR`** (~580), then `HDEL`.
- The `DELETE`/`HDEL` after a DECR is the **double-release guard** — a second
  `release_stream` finds nothing and won't DECR again.

**Consequence for the plugin:** a manual reservation (`SET channel_stream:{int}=S`,
`SET stream_profile:{S}=P`, `INCR profile_connections:{P}`) is **released correctly
by `release_stream`** on teardown. There is no leak — the earlier "no DECR exists"
claim was based on grepping only the plugin repo.

## 4. Live channel switching — `ChannelService.change_stream_url()`

`apps/proxy/ts_proxy/services/channel_service.py:88`. Signature: `(channel_id=UUID,
new_url=None, user_agent=None, target_stream_id=None, m3u_profile_id=None)`.

- If `new_url` is omitted but `target_stream_id` is given, it calls
  `get_stream_info_for_switch` (url_utils.py:163) which **only reads** capacity
  (it does *not* INCR) to pick a profile.
- **If this worker owns the channel:** `manager.update_url(new_url, stream_id,
  m3u_profile_id)` in place. **Else:** publishes a Redis **pubsub** switch event for
  the owner to apply.
- It updates the channel metadata hash via `_update_channel_metadata(...)` (writes
  `STREAM_ID`/`M3U_PROFILE` — this is what feeds `release_stream`'s fallback).

### The switch-path INCR gotcha
`StreamManager.update_url` (`apps/proxy/ts_proxy/stream_manager.py:1060`) — when
`self.current_stream_id != stream_id` and an `m3u_profile_id` is given — calls
**`channel.update_stream_profile(m3u_profile_id)` (line 1083)**, which (per §3)
**INCRs** `profile_connections` unless `current==new`.

⇒ If the plugin manually reserves `P` **and** calls `change_stream_url`, a second
INCR happens **unless** `stream_profile:{S}=P` is written **before** the switch so
`update_stream_profile` early-returns. This ordering is the load-bearing invariant
in the design (see [DESIGN §3.4.5](DESIGN-tms-managed-streams.md)).

## 5. Autonomous failover — `get_alternate_streams()`

`apps/proxy/ts_proxy/url_utils.py:279` (used by `views.py:269` and `:289`). The
proxy can, on its own, find alternate streams **with available `profile_connections`**
and switch a stalled stream to one — independently of the plugin. Any plugin flow
that parks a channel on the card and then switches it must assume the proxy might
switch first (guard: a continuous/static card won't trip stall detection; re-check
`channel_stream:{int}` still points at the card before reserving).

## 6. Redis keys & identifier discipline

| Key | Identifier | Meaning |
|---|---|---|
| `channel_stream:{id}` | channel **integer PK** | active stream id for the channel |
| `stream_profile:{sid}` | stream id | the M3U profile id reserved for that stream |
| `profile_connections:{pid}` | M3U profile id | live connection count (vs `max_streams`) |
| `tms:maxed_out:{id}` | channel **integer PK** | plugin's 30 s maxed marker |
| `ts_proxy:channel:{uuid}:metadata` | channel **UUID** | proxy metadata hash (`STREAM_ID`, `M3U_PROFILE`, state, owner…) |

**Identity split (a real footgun):** the reservation keys
(`channel_stream`/`stream_profile`) and the plugin's own guard keys use the channel
**integer PK** (`self.id`). `change_stream_url`, the metadata hash, and
`proxy_server.check_if_channel_exists()` use the channel **UUID** (`self.uuid`).
Code that switches a channel holds the `Channel` object (both ids) and must use the
right one per call — never derive one from the other via a lookup.

## 7. Process & deployment facts

- The plugin's `get_stream` override + log filter install in **every** process (web
  + every Celery worker). The `:1337` HTTP server is hosted by exactly one
  non-celery web process (`_should_host_server` + `_can_bind`).
- Celery workers are **ephemeral** (`--autoscale=6,1`); daemon threads there die on
  recycle without running `finally`. Anything that must survive (the server, any
  background probe) belongs in the long-lived web process, with TTL-based self-heal.
- Container/process layout (see the `dispatcharr-prod-setup` memory): container
  `jflix_dispatcharr`; plugin at `/data/plugins/too_many_streams/`; persistent config
  at `/data/plugins/TMS_Persistent_Config/`.

## Quick re-verification commands

```bash
# version + namespace
docker exec jflix_dispatcharr sh -c "cat /app/version.py; ls /app/apps/proxy/"
# the accounting functions
docker exec jflix_dispatcharr sh -c "grep -nE 'def release_stream|def update_stream_profile|def get_stream' /app/apps/channels/models.py"
docker exec jflix_dispatcharr sh -c "grep -nE 'def update_url|update_stream_profile' /app/apps/proxy/ts_proxy/stream_manager.py"
docker exec jflix_dispatcharr sh -c "grep -nE 'def change_stream_url|def get_alternate_streams' /app/apps/proxy/ts_proxy/services/channel_service.py /app/apps/proxy/ts_proxy/url_utils.py"
# the card row + its unlimited profile (psql via base64 to dodge quote-mangling)
docker exec jflix_dispatcharr sh -c "psql -tA -U dispatch -d dispatcharr -c 'SELECT id,name,is_custom,m3u_account_id FROM dispatcharr_channels_stream WHERE id=195362'"
```

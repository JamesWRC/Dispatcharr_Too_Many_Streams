# Known Issues — maintainer notes

> **Status: BROKEN on Dispatcharr >= 0.25.0.** The plugin fails to import and is
> non-functional. It must be ported before it can be re-enabled.
>
> **It is not, however, a cause of instance-wide HTTP 500s.** This file originally
> claimed it was. That was tested on 2026-08-17 by disabling the plugin and
> restarting: the 500s persisted, so the plugin is ruled out. This matches the
> mechanics — Dispatcharr's loader isolates plugin import failures
> (`loader.py:243`), so a plugin that dies at import has no reach beyond its own
> registry entry. See §2 for the disproven hypothesis, kept because the
> logging-handler leak it describes is a real bug worth fixing on its own.
>
> Investigated 2026-08-17 against Dispatcharr `0.29.0`. Line references below are
> to upstream `Dispatcharr/Dispatcharr` at that version.

---

## 1. Root cause: `apps.proxy.ts_proxy` no longer exists

Dispatcharr 0.25.0 (2026-05-21) renamed the live-streaming proxy package:

> `ts_proxy` module refactored and renamed to `live_proxy`. The live-streaming
> proxy was reorganized into a structured package (`apps/proxy/live_proxy/`)…
> — upstream `CHANGELOG.md:483`

There is no compatibility shim (`apps/proxy/__init__.py` is a one-line docstring).
Every one of our imports is dead:

| Our import (`src/`) | Now lives at |
|---|---|
| `apps.proxy.ts_proxy.server.ProxyServer` | `apps.proxy.live_proxy.server:34` |
| `apps.proxy.ts_proxy.services.channel_service.ChannelService` | `apps.proxy.live_proxy.services.channel_service:25` |
| `apps.proxy.ts_proxy.channel_status.ChannelStatus` | `apps.proxy.live_proxy.channel_status:13` |

So `plugin.py:22` raises `ModuleNotFoundError`, is caught at `plugin.py:31`, and
re-raised. The `Plugin` class body never executes.

Confirm in the UI (plugin shows as **not loaded**) or in `docker logs dispatcharr`:

```
Error importing module ... ModuleNotFoundError: No module named 'apps.proxy.ts_proxy'
```

## 2. Logging handler leak (real bug — but NOT a cause of instance-wide 500s)

> **Disproven as an outage cause.** This section originally argued the repeated
> failed imports were taking the instance down. Disabling the plugin and
> restarting did **not** clear the 500s, so that is wrong. The leak below is
> still a genuine bug — it wastes fds and disk — but it is not an outage.

Dispatcharr's loader isolates import failures — `loader.py:243` catches the
exception and substitutes a `loaded=False` placeholder. What follows is what we
do **before** the failing import, which survives that isolation:

```python
# plugin.py:15-17 — runs at module top level, before the try block
logger = logging.getLogger('plugins.too_many_streams')
file_handler = logging.FileHandler(os.path.join(os.path.dirname(__file__), "debug.log"))
logger.addHandler(file_handler)
```

`logging.getLogger()` returns a **process-global singleton** that survives module
reload, and we never remove the handler. Meanwhile the loader re-imports us on
every `discover_plugins(force_reload=True)` — plugin reload, install, enable/disable,
settings save (`api_views.py:130,412,502,569,1362`) — in each of the 4 uWSGI workers.

Consequences, all cumulative and never reset until the container restarts:

- **Quadratic log growth.** Import attempt *N* has *N* handlers attached, so the
  `logger.error(..., exc_info=True)` at `plugin.py:32` writes the full traceback
  *N* times. After *N* attempts, `debug.log` holds `N(N+1)/2` tracebacks. A full
  `/data` volume makes Django and Postgres throw on essentially every request.
- **File-descriptor leak.** One never-closed FD on `debug.log` per attempt. At the
  usual 1024 limit the worker can no longer open sockets to Postgres or Redis.

Disabling the plugin stops both, because `loader.py:204` short-circuits *before*
`_load_plugin()` for disabled plugins — the module is never imported at all.

**Fix:** build the logger inside `initialize()`, guard with
`if not logger.handlers:`, and log to stdout / Dispatcharr's logger rather than a
file inside the plugin directory.

---

## 3. Everything else that must be fixed before re-enabling

Repointing the imports to `live_proxy` is **not sufficient** — the plugin would then
load and immediately cause a different set of 500s. Full checklist:

### 3.1 `Channel.get_stream()` now returns a 4-tuple — **would 500 every stream request**

```python
# apps/channels/models.py:692 -> (stream_id, profile_id, error_reason, slot_reserved)
# apps/proxy/live_proxy/url_utils.py:116
stream_id, profile_id, error_reason, slot_reserved = channel.get_stream()
```

Our override (`src/TooManyStreams.py:191`) returns 3-tuples on all five return
paths → `ValueError: not enough values to unpack (expected 4, got 3)`. The 4th
element arrived in 0.27.0 (`CHANGELOG.md:294`).

### 3.2 Background threads leak DB connections — **the classic app-wide 500**

From upstream `Plugins.md:310-319`:

> Dispatcharr uses `django-db-geventpool` with a bounded per-uWSGI-worker pool
> (`MAX_CONNS=8`). Each greenlet or OS thread that runs ORM code checks out a
> connection until Django closes it.
>
> **Background threads or greenlets you spawn:** each thread/greenlet that uses the
> ORM must call `close_old_connections()` (or `connection.close()`) in its own
> `finally` block when done.

uWSGI runs gevent with early monkey-patching (`docker/uwsgi.ini:50-54`), so our
`threading.Thread` calls create greenlets. `StreamServer._image_updater_loop` runs
`Channel.objects.filter(...)` and `ChannelStatus.get_basic_channel_info()` in an
infinite loop and never releases its checkout — permanently burning one of the 8
slots per worker. When the pool empties,
`dispatcharr/db/backends/postgresql_psycopg3/pool.py:47` calls `self.pool.get()`
**with no timeout** and the request greenlet blocks forever:

> without it, greenlets and OS threads keep connections checked out until the pool
> blocks on `pool.get()` — `CHANGELOG.md:327`
>
> XC/`player_api` hung while `/api/core/version/` stayed fast — `CHANGELOG.md:167`

### 3.3 Blocking the gevent hub

`Plugins.md:318` warns that `time.sleep`, sync HTTP, or large CPU work "can freeze
the whole worker". We do all three:

- `StreamServer._broadcaster_loop` busy-waits on `time.sleep`.
- `PillowImageGen._get_cached_logo` does a synchronous `requests.get` per logo.
- `PillowImageGen.generate()` renders and JPEG-encodes 1920x1080 in Pillow's C code,
  which never yields — stalling all 400 greenlets in that worker.

Move image generation to a Celery task (`Plugins.md:318` recommends `.delay()`).

### 3.4 Redis key namespace changed

`ts_proxy:channel:{id}:metadata` is now `live:channel:{id}:metadata`
(`apps/proxy/live_proxy/redis_keys.py:8-10`). Affects the scan pattern in
`PillowImageGen.get_active_streams()` and `_TmsStreamInfoFilter._is_tms_channel()`.
Both fail silently — they just match nothing.

### 3.5 Connection accounting bypasses `reserve_profile_slot()`

Our override uses raw `redis_client.get` / `incr`, reintroducing the
GET-check-INCR race upstream fixed in 0.26.0 (`CHANGELOG.md:1129`). Route through
`reserve_profile_slot()` / `release_profile_slot()`
(`apps/m3u/connection_pool.py:280`), per `CHANGELOG.md:294`. We also skip
`_stream_assignment_is_reusable` / `_release_stale_stream_assignment`, leaving stale
`channel_stream:` keys — upstream notes these let a second stream reach the provider
and fail validation (`apps/channels/models.py:709-711`).

### 3.6 Smaller bugs

- **`plugin.py:186`** — `self.initialized = True` sets an *instance* attribute,
  shadowing the class attribute at `plugin.py:40`. The loader builds a fresh
  `Plugin()` on every discovery (`loader.py:360`), so `initialize()` re-runs every
  time; it only avoids double-starting the server because `_can_bind` fails once the
  port is held.
- **No `stop()` method.** `loader.py:592` looks for one. Without it the ffmpeg
  process, HTTP server, threads, and the `Channel.get_stream` monkey-patch are never
  cleaned up on reload or disable.
- **`PillowImageGen.py:118`** logs at INFO *inside a sort comparator* — one line per
  comparison, on every regeneration.
- **`TooManyStreamsConfig.get_persistent_storage_path`** writes to
  `/data/plugins/TMS_Persistent_Config`, which is inside the plugin discovery
  directory (`loader.py:51`). It gets scanned as a plugin folder and surfaces as a
  bogus placeholder entry. Move it outside `/data/plugins`.

---

## 4. Version timeline

| Dispatcharr | Date | Change | Effect on this plugin |
|---|---|---|---|
| <= 0.24.x | — | — | Works |
| 0.25.0 | 2026-05-21 | `ts_proxy` -> `live_proxy` (`CHANGELOG.md:483`) | **Import fails; plugin dead** |
| 0.26.0 | 2026-06-07 | `django-db-geventpool`, `MAX_CONNS=8` (`CHANGELOG.md:384`) | Thread DB leaks become fatal |
| 0.27.0 | 2026-06-16 | `get_stream()` -> 4-tuple, `reserve_profile_slot()` (`CHANGELOG.md:294`) | Override signature wrong |
| 0.29.0 | 2026-08-09 | current | — |

`README.md` still claims "Dispatcharr v0.19+" — update once ported.

## 5. Suggested order of work

1. Fix the logging handler leak (§2) — smallest change, stops the outage mechanism.
2. Port imports and Redis keys to `live_proxy` (§1, §3.4).
3. Fix the `get_stream` override: 4-tuple + `reserve_profile_slot()` (§3.1, §3.5).
4. Add `close_old_connections()` to every background loop (§3.2).
5. Move image generation to Celery (§3.3).
6. Add `stop()`; fix the `initialized` attribute; move the persistent config dir (§3.6).
7. Re-test against 0.29.0 and bump the minimum supported version in `README.md`.

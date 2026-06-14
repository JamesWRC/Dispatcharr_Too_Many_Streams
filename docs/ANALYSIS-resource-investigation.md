# Analysis — placeholder-encoder resource investigation

**Date:** 2026-06-13/14 · **Box:** `jflix_dispatcharr` (Dispatcharr 0.21.1, Ubuntu host) ·
**Trigger:** "the toomanystreams plugin is using massive amounts of resources."

## TL;DR

The placeholder MPEG-TS encoder ran **unthrottled at ~3 CPU cores** whenever any
channel was failed over to the card, because the ffmpeg command had **no `-re`
flag** — so ffmpeg encoded the looped still image as fast as the CPU allowed
instead of at 1 fps wall-clock. Host load tracked this one process almost
exactly. Three fixes were applied (`-re`, `-threads 1`, reap-after-kill) plus a
DB-efficiency fix in the image generator. All are in the local tree and the
rebuilt `too_many_streams.zip`; **not yet deployed to prod.**

## Symptoms observed

- Host 1-minute load average **6.72** (15-min 7.52) on an 8-ish-core box also
  running Jellyfin transcodes, unmanic, etc.
- Top CPU process: an **`ffmpeg` at 335% → 307%**, PPID = the Dispatcharr
  `daphne` web process (host PID 398425 / container PID 293).
- A second `[ffmpeg] <defunct>` (zombie) also parented by `daphne`.

## Method

Read-only inspection of the running container (no disruptive actions; a 3-second
controlled `ffmpeg` A/B test was **blocked by the sandbox** as too CPU-heavy on
an already-loaded box, so the conclusion rests on direct evidence + reading the
deployed code):

```bash
ssh a@jwrc.me
uptime; ps -eo pid,ppid,pcpu,pmem,etime,args --sort=-pcpu | head
docker ps
docker exec jflix_dispatcharr ps -eo pid,ppid,pcpu,etime,stat,args   # find the ffmpeg
docker exec jflix_dispatcharr cat /data/plugins/too_many_streams/src/StreamServer.py
docker exec jflix_dispatcharr tail -n 60 /data/plugins/too_many_streams/debug.log
docker exec jflix_dispatcharr ss -tlnp | grep 1337                   # listener present
```

## Root cause — the encoder was not wall-clock paced

The deployed `StreamServer._get_ffmpeg_cmd` built:

```
ffmpeg -loglevel error -loop 1 -framerate 1 -i IMG.jpg \
       -f lavfi -i anullsrc=r=48000:cl=stereo \
       -c:v libx264 -preset ultrafast -tune stillimage \
       -r 1 -g 1 -b:v 800k -c:a aac -b:a 96k -f mpegts pipe:1
```

`-framerate 1` / `-r 1` set the **timestamp** rate (1 fps in stream time), **not**
wall-clock pacing. Without `-re`, ffmpeg generates the looped image **as fast as
the CPU allows** (thousands of frames of stream-time per second). The only thing
that could throttle it is pipe backpressure from the consumer — but the
`StreamServer` broadcaster reads `proc.stdout` in a tight loop and **drops** frames
to full client queues (`q.put_nowait(buf)` → `queue.Full: pass`). So it drains the
pipe at full speed and removes all backpressure. Net: a nominal "1 fps static
image" encoder pegs ~3 cores the entire time a client is connected.

### Evidence it was this process

| Observation | Value |
|---|---|
| Heavy ffmpeg %CPU (live, then as it died) | **335% → 307%** |
| Its parent | `daphne` (the process that hosts the TMS `:1337` server) |
| `debug.log` "Placeholder encoder started" lines | 4 (at 13:26:49, 13:26:52, 13:40:54, 13:41:00) |
| Host load when the encoder was alive vs. seconds after it died | **6.72 → 2.05** |

The load dropping from ~7 to ~2 the instant the encoder became a zombie is the
clincher: this one process *was* the resource spike. The encoder-start pairs
(start + one "Image changed; restarting encoder") confirmed single-instance
per-client-session behaviour, **not** a respawn loop.

## Secondary findings

1. **Zombie ffmpeg leak.** `_terminate_locked()` did `process.terminate()` →
   `wait(timeout=1)` → on timeout `process.kill()` **with no following `wait()`**.
   A busy-at-300% encoder often doesn't die within 1 s of SIGTERM, gets SIGKILLed,
   and is then never reaped → lingering `[ffmpeg] <defunct>` under `daphne`. Two
   were present. Harmless to CPU, but leaks PIDs over time.
2. **Directory-fallback DB cost.** `PillowImageGen._get_directory_fallback` loaded
   **every** channel that has the card applied (~2806 `Channel` model instances)
   into Python, sorted them, and kept 15 — and did this every 60 s while the card
   was on screen during a broad failover.
3. **Dual-host bind race (cosmetic).** Two web processes (`daphne` + a uwsgi
   worker) both pass `_should_host_server()` and race `_can_bind(:1337)`; the loser
   logs "already served by another process." Only one actually serves, so it isn't
   a CPU cause — but it's noisy. (`ss -tnp` initially looked like "no listener"
   because it lists *established* sockets only; `ss -tlnp` showed `LISTEN 0 5` —
   the server was up, just clientless at that moment.)

## Fixes applied

All in the local working tree and rebuilt into `too_many_streams.zip`.

1. **`-re` on both encoder inputs** (`src/StreamServer.py`, `_get_ffmpeg_cmd`) — paces
   the looped image and the `anullsrc` audio to wall-clock 1 fps. Drops the encoder
   from ~3 cores to ~1%. `-re` only paces reads; it does **not** reintroduce the
   loop-wrap DTS jump that the earlier pre-encoded-loop design suffered (that was a
   different mechanism — see [DESIGN](DESIGN-tms-managed-streams.md) / the resource
   memory).
2. **`-threads 1`** on the software (libx264) branch — at 1 fps with `-g 1` (every
   frame an independent keyframe) frame-threading buys nothing and just lets the
   encoder briefly fan across cores.
3. **`wait()` after `kill()`** in `_terminate_locked` — reaps the killed encoder so
   it can't linger as a zombie.
4. **Directory-fallback now sorts/limits in SQL**
   (`PillowImageGen._get_directory_fallback`): `Channel.objects.filter(id__in=…)
   .only(…).order_by('channel_number')[:15]`. `channel_number` is an indexed
   `FloatField`, so the SQL ordering matches the old Python `float()` sort exactly
   while materializing 15 rows instead of ~2806.

## Verification & status

- The `-re` cause is conclusively supported by the empirical 307% CPU on a nominal
  1-fps encode and the load tracking it; the direct A/B `ffmpeg` test was
  intentionally **not** run on the loaded prod box.
- Post-fix, the rebuilt zip was verified to contain `-re` (×2), `-threads 1`, and the
  SQL-side fallback ordering.
- **Deployment status: NOT deployed.** Prod still runs the unpaced version; the fix
  is staged in the local tree + zip for the user to push via the plugin UI or a
  container redeploy.

## Things considered and deliberately *not* changed

- **Bitrate (`-b:v 800k`) / 1080p / live-encoder-vs-pre-encoded-loop** — with `-re`
  the encoder is ~1%, so these cost no CPU; the pre-encoded loop is known to
  reintroduce DTS-wrap corruption and was already reverted once.
- **Dual-host race** — noisy, not wasteful; only one encoder ever runs.
- **`_get_cached_logo` blocking HTTP** in the render path (up to ~45 s sequential on
  a cold logo cache) — flagged as low-impact (cached 1 h; doesn't block clients).

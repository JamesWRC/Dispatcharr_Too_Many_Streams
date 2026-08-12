"""Measure what a channel's sources are actually carrying, and remember it.

The problem this solves: a channel's streams are ordered by hand (`channelstream__
order`), and that order says nothing about whether a source is currently *carrying
the event*. Providers leave sources up that connect fine, stream real bytes, and
show a frozen "THIS STREAM IS NOT ACTIVE" slate. Every liveness check that only
looks at the connection -- has a free slot, opens, delivers bytes -- scores those
as healthy and hands the viewer a still image.

What separates them is CONTENT, and the cheapest content signal is bitrate: a
frozen slate compresses to a fraction of live video. Measured on ch1013, the slate
sat at 444 kbps against 6265 and 2280 for the live sources -- an obvious outlier,
and one we can see with `-c copy` byte counting alone, no decoding. Freeze
detection is kept as a secondary demotion signal only: it costs a full decode and,
measured back-to-back on the same sources, it disagreed with itself, so it is not
trusted to promote anything -- only to push a suspect down.

Slot discipline is the other half. These probes open REAL provider connections that
Dispatcharr's accounting knows nothing about, and the account here allows 4 total.
So scanning takes ONE slot at a time, fleet-wide per account, guarded by a Redis
lock -- a scan must never be the reason a viewer can't watch.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from core.utils import RedisClient

logger = logging.getLogger('plugins.too_many_streams.QualityScanner')

# Per-stream verdict. Keyed by stream id, so one scan warms every channel that
# lists that source -- the fleet shares the knowledge.
QUALITY_KEY = "tms:quality:{sid}"

# How long a verdict is trusted. Long enough that most tunes read cache instead of
# scanning; short enough that a source coming back on air is noticed. A provider
# slate can flip to live content at kickoff, so this is deliberately not hours.
QUALITY_TTL = 15 * 60

# Inconclusive verdicts (couldn't reach it -- possibly our own fault) are retried
# soon rather than shaping selection for a quarter of an hour.
INCONCLUSIVE_TTL = 90

# One probe connection per m3u account, fleet-wide. Held for the length of a single
# stream's sample, then released. TTL is the crash guard.
SCAN_LOCK_KEY = "tms:scan_slot:{account_id}"
SCAN_LOCK_TTL = 30

# Minimum gap between probes on the same account. Providers keep a session alive for
# a while after we disconnect, so back-to-back probes burn the account's allowance
# and start getting refused -- which looks exactly like a dead source. Measured on
# this box: probing four sources back-to-back reported two of them as "no video
# stream"; re-probed 90s later with 20s spacing, one was a genuine frozen slate and
# the other was live at 4524 kbps. Without this gap the scanner invents dead streams
# and, worse, competes with real viewers for connections.
PROBE_SPACING = 20
COOLDOWN_KEY = "tms:scan_cooldown:{account_id}"

# Seconds of stream to sample. Enough to measure a stable bitrate without holding
# the slot long. Connection setup dominates anyway (measured 5-9s).
SAMPLE_SECONDS = 3

# Below this, a source is almost certainly a slate rather than live video. Only used
# to mark `suspect`; ranking is comparative, because "low" is relative to the other
# sources on the same channel.
SLATE_KBPS = 800


class QualityScanner:

    @staticmethod
    def _ffmpeg():
        return shutil.which("ffmpeg") or "/usr/lib/jellyfin-ffmpeg/ffmpeg"

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #
    @staticmethod
    def get_quality(stream_id):
        """Cached verdict for a stream, or None if never scanned / expired."""
        try:
            raw = RedisClient.get_client().get(QUALITY_KEY.format(sid=stream_id))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    @staticmethod
    def _store(stream_id, verdict):
        try:
            # An inconclusive verdict expires quickly -- it is a "come back to this",
            # not a finding, and we do not want a transient refusal shaping selection
            # for the next quarter of an hour.
            ttl = QUALITY_TTL if verdict.get("conclusive", True) else INCONCLUSIVE_TTL
            RedisClient.get_client().setex(
                QUALITY_KEY.format(sid=stream_id), ttl, json.dumps(verdict)
            )
        except Exception as e:
            logger.warning("TMS scan: could not store verdict for %s: %s", stream_id, e)

    # ------------------------------------------------------------------ #
    # The single shared probe slot
    # ------------------------------------------------------------------ #
    @staticmethod
    def _acquire_slot(account_id):
        try:
            return bool(RedisClient.get_client().set(
                SCAN_LOCK_KEY.format(account_id=account_id), "1", nx=True, ex=SCAN_LOCK_TTL
            ))
        except Exception:
            return False

    @staticmethod
    def _release_slot(account_id):
        try:
            RedisClient.get_client().delete(SCAN_LOCK_KEY.format(account_id=account_id))
        except Exception:
            pass

    @staticmethod
    def _cooldown_elapsed(account_id):
        """True if enough time has passed since the last probe on this account."""
        try:
            return bool(RedisClient.get_client().set(
                COOLDOWN_KEY.format(account_id=account_id), "1", nx=True, ex=PROBE_SPACING
            ))
        except Exception:
            return True   # never let a Redis hiccup block scanning entirely

    # ------------------------------------------------------------------ #
    # Measuring one source
    # ------------------------------------------------------------------ #
    @staticmethod
    def scan_stream(stream, seconds=SAMPLE_SECONDS, detect_freeze=True):
        """Sample one source and record what it is carrying.

        Returns a verdict dict, and stores it. `ok` means we saw positive evidence
        of live video -- a video stream, real bytes, and no freeze. Absence of a
        freeze report is NOT evidence: a source that fails to open reports no freeze
        either, and treating that as healthy is exactly how a dead source gets
        ranked first.
        """
        account_id = getattr(stream, "m3u_account_id", None)
        if not account_id:
            return None

        if not QualityScanner._acquire_slot(account_id):
            logger.debug("TMS scan: probe slot busy for account %s; skipping %s", account_id, stream.id)
            return None

        if not QualityScanner._cooldown_elapsed(account_id):
            logger.debug("TMS scan: account %s still cooling down; skipping %s", account_id, stream.id)
            QualityScanner._release_slot(account_id)
            return None

        tmp = os.path.join(tempfile.gettempdir(), f"tms_probe_{stream.id}.ts")
        verdict = {"kbps": 0.0, "ok": False, "frozen": False, "res": None,
                   "reason": "", "conclusive": True, "ts": int(time.time())}
        try:
            cmd = [
                QualityScanner._ffmpeg(), "-hide_banner", "-loglevel", "info",
                "-user_agent", "VLC/3.0.21 LibVLC/3.0.21",
                "-rw_timeout", "8000000", "-t", str(seconds), "-i", stream.url,
            ]
            if detect_freeze:
                # Decode only the video stream, only to look for a frozen picture.
                cmd += ["-map", "0:v:0", "-vf", "freezedetect=n=-60dB:d=2", "-f", "null", "-"]
            # The bitrate measurement: straight copy, no decode, count the bytes.
            cmd += ["-map", "0", "-c", "copy", "-f", "mpegts", "-y", tmp]

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 25)
            err = proc.stderr or ""

            size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            verdict["kbps"] = round(size * 8 / seconds / 1000, 1)
            verdict["frozen"] = "freeze_start" in err

            m = re.search(r"Video: (\w+).*?, (\d+x\d+)", err)
            verdict["res"] = m.group(2) if m else None

            if not m:
                # Inconclusive, NOT bad. A refused connection and a dead source look
                # identical from here, and we are a plausible cause of the refusal.
                verdict["reason"] = "no video stream"
                verdict["conclusive"] = False
            elif size == 0:
                verdict["reason"] = "no data"
                verdict["conclusive"] = False
            elif verdict["frozen"]:
                # Positive evidence: we decoded frames and they did not change.
                verdict["reason"] = "frozen picture"
            else:
                verdict["ok"] = True
                verdict["reason"] = "live"
                if verdict["kbps"] < SLATE_KBPS:
                    # Still ranked above a confirmed slate, but flagged: a low
                    # bitrate on its own is suggestive, not conclusive.
                    verdict["reason"] = "live (low bitrate)"

        except subprocess.TimeoutExpired:
            verdict["reason"] = "timeout"
        except Exception as e:
            verdict["reason"] = f"error: {e}"
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            QualityScanner._release_slot(account_id)

        QualityScanner._store(stream.id, verdict)
        logger.info("TMS scan: stream %s -> %s (%.0f kbps, %s)",
                    stream.id, verdict["reason"], verdict["kbps"], verdict["res"] or "?")
        return verdict

    # ------------------------------------------------------------------ #
    # Ranking
    # ------------------------------------------------------------------ #
    @staticmethod
    def rank_key(stream_id, fallback_order):
        """Sort key: known-live first (best bitrate wins), then unknown, then bad.

        Unknown sits ABOVE known-bad deliberately. An unscanned source might be
        carrying the event; a source we watched freeze is not. Within a tier the
        channel's hand-ordering breaks ties, so an unscanned channel behaves
        exactly as it does today.
        """
        q = QualityScanner.get_quality(stream_id)
        if q is None:
            return (1, 0, fallback_order)          # never scanned
        if q.get("ok"):
            return (0, -q.get("kbps", 0), fallback_order)   # live, best bitrate first
        if not q.get("conclusive", True):
            # We failed to reach it, but we may well have been the problem. Treat as
            # unknown so it keeps its hand-ordering rather than being buried.
            return (1, 0, fallback_order)
        return (2, 0, fallback_order)              # positively bad: frozen picture

    @staticmethod
    def order_streams(pairs):
        """`pairs` is [(stream, channelstream_order)] -> best-first list of streams."""
        return [s for s, _ in sorted(pairs, key=lambda p: QualityScanner.rank_key(p[0].id, p[1]))]

    # ------------------------------------------------------------------ #
    # Scanning a whole channel, one slot at a time
    # ------------------------------------------------------------------ #
    @staticmethod
    def scan_channel(streams, seconds=SAMPLE_SECONDS, force=False, on_result=None, stop=None):
        """Sample each source in turn, newest verdict first, using ONE slot.

        Sequential on purpose. Probing in parallel is self-defeating here: four
        concurrent probes are this account's entire allowance, so they compete with
        real viewers and with each other -- in testing that produced a source that
        returned zero bytes purely because the provider refused the connection, and
        it was then indistinguishable from a genuinely dead one.

        `on_result` is called after each verdict, so a caller can act on the first
        good source (switch a waiting viewer to it) while the sweep carries on
        filling in the rest for next time. `stop` is a callable that aborts the
        sweep -- e.g. the viewer gave up.
        """
        results = {}
        probed = 0
        for stream in streams:
            if stop and stop():
                logger.debug("TMS scan: sweep aborted")
                break
            if not force:
                cached = QualityScanner.get_quality(stream.id)
                if cached is not None:
                    results[stream.id] = cached
                    if on_result:
                        on_result(stream, cached)
                    continue

            # Pace ourselves rather than tripping our own cooldown. The first probe
            # goes immediately -- that one is on the critical path, a viewer may be
            # waiting on the card for it -- and only the background continuation
            # pays the spacing.
            if probed:
                waited = 0.0
                while waited < PROBE_SPACING:
                    if stop and stop():
                        return results
                    time.sleep(1.0)
                    waited += 1.0

            verdict = QualityScanner.scan_stream(stream, seconds=seconds)
            if verdict is None:
                continue                       # another scanner holds the slot
            probed += 1
            results[stream.id] = verdict
            if on_result:
                on_result(stream, verdict)
        return results

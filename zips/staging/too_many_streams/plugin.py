# plugin.py
"""Too Many Streams Plugin for Dispatcharr"""
# -*- coding: utf-8 -*-
# Python imports
import itertools
import os
import logging
import sys
import threading

# Dispactharr imports
from apps.channels.models import Logo
from apps.proxy.config import TSConfig  # ensure correct model

# Too ManyStreams imports NOTE: must use relative import, else Dispatcharr fails to load the plugin
from .src.TooManyStreams import TooManyStreams  # ensure correct import

# ---- Global default UA (used if UI field empty and no env/CoreSettings UA) ----
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

class Plugin:
    name = "too_many_streams"
    version = "1.0.0"
    description = "Handles scenarios where too many streams are open and what users see."


    def __init__(self):
        # Create a logger for this plugin
        self.logger = logging.getLogger('plugins.too_many_streams.Plugin')
        self.logger.setLevel(logging.DEBUG)
        pass


    # Settings rendered by UI
    fields = [
        {
            "id": "lineup_ids",
            "label": "Lineup IDs",
            "type": "text",  # multiline
            "default": "",
            "placeholder": "One per line, or comma/space separated (e.g. USA-DITV501-X, CAN-0005993-X, USA-OTA63601)",
            "help_text": "Only enter lineupId strings. Country and ZIP/postal are auto-derived per lineup.",
        },
        {
            "id": "merge_lineups",
            "label": "Merge multiple lineups into a single XMLTV",
            "type": "boolean",
            "default": True,
            "help_text": "If enabled, all generated lineups are merged into one XML and one EPG Source.",
        },
        {
            "id": "timespan",
            "label": "Total hours to fetch",
            "type": "number",
            "default": 72,
            "help_text": "Total hours of guide data to fetch (requested in 6-hour chunks).",
        },
        {
            "id": "delay",
            "label": "Delay between requests (sec)",
            "type": "number",
            "default": 5,
            "help_text": "Seconds to sleep between chunk requests to Zap2it.",
        },
        {
            "id": "user_agent",
            "label": "HTTP User-Agent (optional)",
            "type": "string",
            "default": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "help_text": "Override HTTP User-Agent when fetching from Zap2it. Leave blank to use the plugin's global default.",
        },
        {
            "id": "refresh_interval_hours",
            "label": "Refresh Interval (hours)",
            "type": "number",
            "default": 24,
            "help_text": "Auto-refresh interval for the EPG source (0 to disable).",
        },
        {
            "id": "auto_download_enabled",
            "label": "Auto-Download XML on Schedule",
            "type": "boolean",
            "default": True,
            "help_text": "When enabled, download a new XMLTV file on the saved interval.",
        },
        {
            "id": "epg_name",
            "label": "EPG Name",
            "type": "string",
            "default": "Zap2XML",
            "help_text": "Base name for the EPG Source(s) added to Dispatcharr.",
        },
        {
            "id": "confirm",
            "label": "Require confirmation",
            "type": "boolean",
            "default": True,
            "help_text": "Confirm before generating and importing the EPG",
        },
    ]

    actions = [
        {
            "id": "download_epg",
            "label": "Download EPG XML",
            "description": "Fetch Zap2it data and write XML under /data/epgs. Updates/creates the EPG source(s).",
            "confirm": {
                "required": True,
                "title": "Download EPG XML?",
                "message": "This fetches a new XMLTV file using the listed lineup IDs and updates the EPG source(s).",
            },
        },
        {
            "id": "apply_channel_mappings",
            "label": "Set Channels + Logos",
            "description": "Match channels by name to EPG tvg_ids and set logos.",
            "confirm": {
                "required": True,
                "title": "Set channels + logos?",
                "message": "Matches by channel name. Sets EPG and logos from channels.db or XMLTV icons.",
            },
        },
    ]    

    # This is called when an action is triggered. See action list above.
    def run(self, action: str = None, params: dict = None, context: dict = None, *args, **kwargs):
        self.logger.info("Running the Too Many Streams plugin.")
        self.logger.info(f"Signature seen: action={action!r}, params keys={list((params or {}).keys())}, "
                        f"context keys={list((context or {}).keys())}, extra args={args}, kwargs={kwargs}")
        # ...do work...
        self.logger.info("Running the Too Many Streams plugin.")
        self.logger.info(f"Action: {action}")
        self.logger.info(f"Params: {params}")
        self.logger.info(f"Context: {context}")
        pass

    def _resolve_python(self):
        candidates = []
        ve = os.environ.get("VIRTUAL_ENV")
        if ve:
            candidates.extend([os.path.join(ve, "bin", "python"), os.path.join(ve, "bin", "python3")])
        candidates.extend(["python", "python3", sys.executable])
        for c in candidates:
            if not c:
                continue
            ok = os.path.exists(c) if os.path.sep in c else (shutil.which(c) is not None)
            if not ok:
                continue
            if "uwsgi" in os.path.basename(c).lower():
                continue
            return c
        return sys.executable

    def _cleanup_plugin_workspace(self, logger=None):
        """Remove transient artifacts from the plugin folder."""
        try:
            plugin_dir = os.path.dirname(__file__)
            for name in os.listdir(plugin_dir):
                if name.startswith("xmltv_") and name.endswith(".xml"):
                    try:
                        os.remove(os.path.join(plugin_dir, name))
                    except Exception:
                        pass
            cache_dir = os.path.join(plugin_dir, "cache")
            if os.path.isdir(cache_dir):
                for root, dirs, files in os.walk(cache_dir, topdown=False):
                    for n in files:
                        try: os.remove(os.path.join(root, n))
                        except Exception: pass
                    for n in dirs:
                        try: os.rmdir(os.path.join(root, n))
                        except Exception: pass
                try: os.rmdir(cache_dir)
                except Exception: pass
            if logger:
                try: logger.info("[zap2xml] workspace cleaned")
                except Exception: pass
        except Exception:
            pass


def _plugin_key():
    try:
        return __name__.split(".")[0]
    except Exception:
        return os.path.basename(os.path.dirname(__file__)).replace(" ", "_").lower()

# Expose schema for UIs that look at module-level
fields = Plugin.fields
actions = Plugin.actions

s = TooManyStreams()
s.install_get_stream_override()
# s.install_overrides()
# s.install_generate_stream_url_override()
s.install_generate_patch()
# s.install_stream_ts_return_override()


def on_join():
    # do logging, analytics, DB lookups, etc.
    print(">>> New client connected; rotating image…")

    return next(itertools.cycle([
    os.path.join(os.path.dirname(__file__), "no_streams.jpg"),
    os.path.join(os.path.dirname(__file__), "no_streams2.jpg"),
    os.path.join(os.path.dirname(__file__), "no_streams3.jpg"),
]))  # return the image to use for THIS client

t = threading.Thread(
    target=TooManyStreams.stream_still_mpegts_http,
    args=(os.path.join(os.path.dirname(__file__), "no_streams.jpg"),),
    kwargs={"on_client_start": on_join, "host": "127.0.0.1", "port": 8081},
    daemon=True,  # dies when the main program exits
)
t.start()
s.TMS_generate_stream_url()
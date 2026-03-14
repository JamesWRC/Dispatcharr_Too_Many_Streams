# plugin.py
"""Too Many Streams Plugin for Dispatcharr"""
# -*- coding: utf-8 -*-
# Python imports
import os
import logging
import socket
import threading
import sys
import inspect

# Configure logging as early as possible
logger = logging.getLogger('plugins.too_many_streams')
log_file = os.path.join(os.path.dirname(__file__), "debug.log")
file_handler = logging.FileHandler(log_file)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)
logger.setLevel(logging.INFO)

try:
    # Too ManyStreams imports
    from .src.TooManyStreams import TooManyStreams
    from .src.TooManyStreamsConfig import TooManyStreamsConfig
    
    # Safely log the source path
    try:
        source_path = inspect.getfile(TooManyStreamsConfig)
        logger.info(f"Imported TooManyStreamsConfig from {source_path}")
    except Exception:
        logger.info("Imported TooManyStreamsConfig (could not determine source path)")
        
except Exception as e:
    logger.error(f"Failed to import plugin components: {e}", exc_info=True)
    raise


class Plugin:
    name = "too_many_streams"
    version = "2.1.3"
    description = "Handles scenarios where too many streams are open and what users see."
    initialized = False

    # LOAD FILE-BASED DEFAULTS FOR THE UI FROM USER SPECIFIED PATH
    _file_config = TooManyStreamsConfig.get_plugin_persistent_config()

    fields = [
        {
            "id": "stream_title",
            "label": "Stream Title",
            "type": "string",
            "default": _file_config.get("stream_title", "Sorry, this channel is unavailable."),
            "placeholder": "The title displayed on the 'Too Many Streams' image.",
            "help_text": "The title displayed on the 'Too Many Streams' image.",
        },
        {
            "id": "stream_description",
            "label": "Stream Description",
            "type": "string",
            "default": _file_config.get("stream_description", "While this channel is not currently available, here are some other channels you can watch."),
            "placeholder": "The description displayed on the 'Too Many Streams' image.",
            "help_text": "The description displayed on the 'Too Many Streams' image.",
        },
        {
            "id": "stream_channel_cols",
            "label": "Number of channel columns",
            "type": "number",
            "default": int(_file_config.get("stream_channel_cols", 5)),
            "placeholder": "The number of columns of channels to display on the 'Too Many Streams' image.",
            "help_text": "The number of columns of channels to display on the 'Too Many Streams' image.",
        },
        {
            "id": "tms_image_path",
            "label": "Static Image Path",
            "type": "string",
            "default": _file_config.get("tms_image_path", None),
            "placeholder": "Path to a static image to use instead of the dynamic image.",
            "help_text": "Path to a static image to use instead of the dynamic image.",
        },
        {
            "id": "tms_log_level",
            "label": "Log Level",
            "type": "string",
            "default": _file_config.get("tms_log_level", "INFO"),
            "placeholder": "Log level for the plugin.",
            "help_text": "Log level for the plugin.",
        },
        {
            "id": "video_encoder",
            "label": "Video Encoder",
            "type": "string",
            "default": _file_config.get("video_encoder", "libx264"),
            "placeholder": "libx264",
            "help_text": "FFmpeg encoder (e.g., libx264, h264_nvenc, h264_qsv, h264_omx, h264_videotoolbox). Use libx264 if unsure.",
        },
        {
            "id": "theme_bg_color",
            "label": "Background Color",
            "type": "string",
            "default": _file_config.get("theme_bg_color", "#0F172A"),
            "placeholder": "#0F172A",
            "help_text": "Hex code for the main background.",
        },
        {
            "id": "theme_card_bg_color",
            "label": "Card Background Color",
            "type": "string",
            "default": _file_config.get("theme_card_bg_color", "#1E293B"),
            "placeholder": "#1E293B",
            "help_text": "Hex code for the channel card background.",
        },
        {
            "id": "theme_card_border_color",
            "label": "Card Border Color",
            "type": "string",
            "default": _file_config.get("theme_card_border_color", "#334155"),
            "placeholder": "#334155",
            "help_text": "Hex code for the card border.",
        },
        {
            "id": "theme_text_color",
            "label": "Text Color",
            "type": "string",
            "default": _file_config.get("theme_text_color", "#F8FAFC"),
            "placeholder": "#F8FAFC",
            "help_text": "Hex code for the main text.",
        },
        {
            "id": "theme_accent_color",
            "label": "Accent Color",
            "type": "string",
            "default": _file_config.get("theme_accent_color", "#38BDF8"),
            "placeholder": "#38BDF8",
            "help_text": "Hex code for accents (e.g., channel number pill).",
        },
        {
            "id": "theme_accent_text_color",
            "label": "Accent Text Color",
            "type": "string",
            "default": _file_config.get("theme_accent_text_color", "#0F172A"),
            "placeholder": "#0F172A",
            "help_text": "Hex code for text inside accent pills.",
        },
    ]

    actions = [
        {
            "id": "apply_too_many_streams",
            "label": "Apply 'Too Many Streams' to channels",
            "description": "Adds the 'Too Many Streams' stream to the bottom of all channels.",
            "confirm": {
                "required": True,
                "title": "Apply 'Too Many Streams'?",
                "message": "This adds the 'Too Many Streams' stream to the bottom of all channels.",
            },
        },
        {
            "id": "remove_too_many_streams",
            "label": "Remove 'Too Many Streams' from channels",
            "description": "Removes the 'Too Many Streams' stream from all channels.",
            "confirm": {
                "required": True,
                "title": "Remove 'Too Many Streams'?",
                "message": "Removes the 'Too Many Streams' stream from all channels.",
            },
        },
        {
            "id": "save_plugin_config",
            "label": "Save Plugin Config",
            "description": "Saves the current plugin configuration to persistent storage. So if you ever update/reinstall the plugin, your settings are retained.",
            "confirm": {
                "required": True,
                "title": "Save Plugin Config to disk?",
                "message": "Saves the current plugin configuration to persistent storage.",
            },
        },
        {
            "id": "search_for_config",
            "label": "Search for Persistent Config",
            "description": "Manually searches for and reloads the persistent configuration file from disk.",
        },
    ]    

    def __init__(self):
        self.initialize()

    def initialize(self):
        if self.initialized:
            return
            
        config = TooManyStreamsConfig.get_config()
        logger.setLevel(config.tms_log_level)

        HOST, PORT = TooManyStreamsConfig.get_host_and_port()
        image_to_use = config.tms_image_path

        TooManyStreams.install_get_stream_override()

        if not self._can_bind(HOST, PORT):
            logger.error(f"Too Many Streams: Could not bind to {HOST}:{PORT}. Port might be in use.")
            return

        if not TooManyStreams.check_requirements_met():
            TooManyStreams.install_requirements()

        threading.Thread(
            target=TooManyStreams.stream_still_mpegts_http_thread,
            args=(image_to_use,),
            kwargs={"host": HOST, "port": PORT},
            daemon=True,
        ).start()
            
        self.initialized = True
        logger.info("Too Many Streams plugin initialized.")

    @staticmethod
    def _can_bind(host, port) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            s.close()
            return True
        except OSError:
            s.close()
            return False

    def run(self, action: str = None, params: dict = None, context: dict = None, *args, **kwargs):
        self.initialize()
        logger.info(f"Running action: {action}")
        
        if action == "apply_too_many_streams":
            TooManyStreams.apply_to_all_channels()
        elif action == "remove_too_many_streams":
            TooManyStreams.remove_from_all_channels()
        elif action == "save_plugin_config":
            settings = (context or {}).get("settings") or (context or {}).get("config") or (params or {})
            logger.info(f"Saving settings: {settings}")
            if settings:
                TooManyStreamsConfig.save_plugin_persistent_config(settings)
            else:
                logger.warning("No settings found to save.")
        elif action == "search_for_config":
            logger.info("Manually searching for and reloading config...")
            TooManyStreamsConfig.clear_cache()
            TooManyStreamsConfig.get_config()

        return {"status": "ok"}

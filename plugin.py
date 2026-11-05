# plugin.py
"""Too Many Streams Plugin for Dispatcharr"""
# -*- coding: utf-8 -*-
# Python imports
import logging

logger = logging.getLogger('plugins.too_many_streams')
logger.setLevel(logging.INFO)

try:
    # Too ManyStreams imports
    from .src.TooManyStreams import TooManyStreams
    from .src.TooManyStreamsConfig import TooManyStreamsConfig
    
    logger.info("Imported TooManyStreams components")
        
except Exception as e:
    logger.error(f"Failed to import plugin components: {e}", exc_info=True)
    raise


class Plugin:
    name = "too_many_streams"
    version = "2.3.0"
    description = "Handles scenarios where too many streams are open and what users see."
    initialized = False

    # LOAD FILE-BASED DEFAULTS FOR THE UI FROM USER SPECIFIED PATH
    try:
        _file_config = TooManyStreamsConfig.get_plugin_persistent_config()
    except Exception as e:
        logger.warning(
            "Failed to load persistent plugin config at import time; "
            "falling back to in-memory defaults. Error: %s",
            e,
        )
        _file_config = {}

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
            "label": "Prepare automatic capacity warning",
            "description": "Prepares warning metadata; no channel associations are added.",
            "confirm": {
                "required": True,
                "title": "Apply 'Too Many Streams'?",
                "message": "Prepare automatic capacity warning without changing channels?",
            },
        },
        {
            "id": "remove_too_many_streams",
            "label": "Remove 'Too Many Streams' from channels",
            "description": "Removes legacy warning associations only; does not stop healthy channels.",
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
        self.initialized = False
        self.initialize()

    def initialize(self):
        if self.initialized:
            return
        config = TooManyStreamsConfig.get_config()
        logger.setLevel(config.tms_log_level)
        host, port = TooManyStreamsConfig.get_host_and_port()
        TooManyStreams.start(config.tms_image_path, host=host, port=port)
        self.initialized = True
        logger.info("Too Many Streams plugin initialized.")

    def stop(self, context=None):
        """Dispatcharr's loader invokes this before disable/reload/removal."""
        TooManyStreams.stop()
        self.initialized = False

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

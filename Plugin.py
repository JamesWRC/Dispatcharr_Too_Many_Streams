# plugin.py
"""Too Many Streams Plugin for Dispatcharr"""
# -*- coding: utf-8 -*-
# Python imports
import os
import logging
import socket
import threading

# Too ManyStreams imports NOTE: must use relative import, else Dispatcharr fails to load the plugin
from .src.TooManyStreams import TooManyStreams  # ensure correct import
from .src.TooManyStreamsConfig import TooManyStreamsConfig, DEFAULT_CSS  # ensure correct import




class Plugin:
    name = "too_many_streams"
    version = "1.1.0"
    description = "Handles scenarios where too many streams are open and what users see."


    def __init__(self):
        # Create a logger for this plugin
        self.logger = logging.getLogger('plugins.too_many_streams.Plugin')
        self.logger.setLevel(os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper())

        # Check if we can bind to the desired host and port
        HOST, PORT = TooManyStreamsConfig.get_host_and_port()
        
        image_to_use = None # None = Dynamical image generation, with current stream info
        image_to_use = os.environ.get("TMS_IMAGE_PATH", None)


        # if not os.path.exists(TooManyStreams.TMS_MAXED_PKL): # Only initialize if the pickle path exists
        #     _pkl_path = os.path.dirname(TooManyStreams.TMS_MAXED_PKL)
        #     if not os.path.exists(_pkl_path):
        #         os.makedirs(_pkl_path, exist_ok=True)
        #     pickle.dump({}, open(TooManyStreams.TMS_MAXED_PKL, "wb"))

        # Patch the Stream.get_stream method to return our custom stream when requested
        TooManyStreams.install_get_stream_override()

        ### 
        # The below code should only have one instance. It may be called multiple times, but the server / threads should only start once.
        ###
        if not self._can_bind(HOST, PORT):
            return

        # Check and install required packages
        if not TooManyStreams.check_requirements_met():
            TooManyStreams.install_requirements()

        TooManyStreams.start_maxed_channel_cleanup_thread()
        # Start the HTTP server thread to serve the "Too Many Streams" image
        threading.Thread(
            target=TooManyStreams.stream_still_mpegts_http_thread,
            args=(image_to_use,),
            kwargs={"host": HOST, "port": PORT},
            daemon=True,  # dies when the main program exits
        ).start()
            

        self.logger.info("Too Many Streams plugin initialized.")

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

    _persisted_config = TooManyStreamsConfig.get_plugin_persistent_config()
    _title_default = _persisted_config.get("stream_title", "Sorry, this channel is unavailable.")
    _description_default = _persisted_config.get("stream_description", "While this channel is not currently available, here are some other channels you can watch.")
    _cols_default = _persisted_config.get("stream_channel_cols", 5)
    _css_default = _persisted_config.get("stream_channel_css", DEFAULT_CSS)
    # Settings rendered by UI
    fields = [
        {
            "id": "stream_title",
            "label": "Stream Title",
            "type": "string",  # multiline
            "default": _title_default,
            "placeholder": "The title displayed on the 'Too Many Streams' image.",
            "help_text": "The title displayed on the 'Too Many Streams' image.",
        },
        {
            "id": "stream_description",
            "label": "Stream Description",
            "type": "string",  # multiline
            "default": _description_default,
            "placeholder": "The description displayed on the 'Too Many Streams' image.",
            "help_text": "The description displayed on the 'Too Many Streams' image.",
        },
        {
            "id": "stream_channel_cols",
            "label": "number of channel columns",
            "type": "number",
            "default": _cols_default,
            "placeholder": "The number of columns of channels to display on the 'Too Many Streams' image.",
            "help_text": "The number of columns of channels to display on the 'Too Many Streams' image.",
        },
        {
            "id": "stream_channel_css",
            "label": "Custom CSS for channel layout",
            "type": "text",
            "default": _css_default,
            "help_text": "You can customize the classes in the rendered HTML",
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
            "description": f"Saves the current plugin configuration to persistent storage: {TooManyStreamsConfig.get_persistent_storage_path()}. So if you ever update/reinstall the plugin, your settings are retained.",
            "confirm": {
                "required": True,
                "title": "Save Plugin Config to disk?",
                "message": f"Saves the current plugin configuration to persistent storage: {TooManyStreamsConfig.get_persistent_storage_path()}",
            },
        },
    ]    

    # This is called when an action is triggered. See action list above.
    def run(self, action: str = None, params: dict = None, context: dict = None, *args, **kwargs):
        self.logger.info("Running the Too Many Streams plugin.")
        self.logger.info(f"Signature seen: action={action!r}, params keys={list((params or {}).keys())}, "
                        f"context keys={list((context or {}).keys())}, extra args={args}, kwargs={kwargs}")

        # TooManyStreams.delete_stream()  # pass logger to TooManyStreams class
        if action == "apply_too_many_streams":
            TooManyStreams.apply_to_all_channels()
        elif action == "remove_too_many_streams":
            TooManyStreams.remove_from_all_channels()
        elif action == "save_plugin_config":
            TooManyStreamsConfig.save_plugin_persistent_config(TooManyStreamsConfig.get_plugin_config())

        pass

    
# Expose schema for UIs that look at module-level
fields = Plugin.fields
actions = Plugin.actions
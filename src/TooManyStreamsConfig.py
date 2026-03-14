# Holds env vars and config for the TooManyStreams plugin

import os
import json
import logging

logger = logging.getLogger('plugins.too_many_streams.TooManyStreamsConfig')

from .schemas import PluginConfig

class TooManyStreamsConfig:
    _STREAM_URL = 'http://{host}:{port}/stream.ts'
    PLUGIN_KEY = 'too_many_streams'
    _cached_config = None
    
    @staticmethod
    def get_persistent_storage_path() -> str:
        # User specified path: data/plugins/TMS_Persistent_Config
        target_dir = "/data/plugins/TMS_Persistent_Config"
        os.makedirs(target_dir, exist_ok=True)
        return os.path.join(target_dir, "too_many_streams_persistent_config.json")

    @staticmethod
    def get_plugin_persistent_config() -> dict:
        config_path = TooManyStreamsConfig.get_persistent_storage_path()
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    data = json.load(f)
                    logger.info(f"Persistent config file FOUND at {config_path}")
                    return data
            except Exception as e:
                logger.error(f"Error loading config from {config_path}: {e}")
        else:
            logger.info(f"No persistent config file found at {config_path}")
        return {}

    @staticmethod
    def clear_cache():
        """Clears the cached configuration, forcing a reload on the next get_config() call."""
        TooManyStreamsConfig._cached_config = None
        logger.info("Plugin configuration cache cleared.")

    @staticmethod
    def get_config() -> PluginConfig:
        # 0. Check cache first
        if TooManyStreamsConfig._cached_config is not None:
            return TooManyStreamsConfig._cached_config

        from apps.plugins.models import PluginConfig as DbPluginConfig
        
        # Start with hardcoded defaults from the class
        final_data = {
            "stream_title": PluginConfig.stream_title,
            "stream_description": PluginConfig.stream_description,
            "stream_channel_cols": PluginConfig.stream_channel_cols,
            "tms_log_level": PluginConfig.tms_log_level,
        }

        # 1. Load from database (Dispatcharr UI settings)
        try:
            if db_config := DbPluginConfig.objects.filter(key=TooManyStreamsConfig.PLUGIN_KEY).first():
                if db_config.settings:
                    logger.info("Merging settings from DB")
                    final_data.update(db_config.settings)
        except Exception as e:
            logger.error(f"Error retrieving plugin config from DB: {e}")

        # 2. Load from persistent config file (USER SPECIFIED LOCATION - HIGH PRIORITY)
        persistent_data = TooManyStreamsConfig.get_plugin_persistent_config()
        if persistent_data:
            logger.info("Overriding with file settings from TMS_Persistent_Config")
            final_data.update(persistent_data)

        # 3. Load from environment variables (FINAL OVERRIDE)
        env_config = {
            "tms_image_path": os.environ.get("TMS_IMAGE_PATH"),
            "tms_log_level": os.environ.get("TMS_LOG_LEVEL", os.environ.get("DISPATCHARR_LOG_LEVEL", "INFO")).upper(),
        }
        env_config = {k: v for k, v in env_config.items() if v is not None}
        if env_config:
            final_data.update(env_config)

        logger.info(f"FINAL RESOLVED CONFIG: {final_data}")
        TooManyStreamsConfig._cached_config = PluginConfig.from_dict(final_data)
        return TooManyStreamsConfig._cached_config

    @staticmethod
    def get_plugin_config(config_key:str=None):
        """DEPRECATED: Use get_config() instead."""
        config = TooManyStreamsConfig.get_config()
        if config_key is None:
            return config.dict()
        return getattr(config, config_key, None)

    @staticmethod
    def get_host_and_port() -> tuple[str, int]:
        _host = os.environ.get("TMS_HOST", "0.0.0.0")
        _port = int(os.environ.get("TMS_PORT", 1337))
        return (_host, _port)
    
    @staticmethod
    def get_stream_url() -> str:
        host, port = TooManyStreamsConfig.get_host_and_port()
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        return TooManyStreamsConfig._STREAM_URL.format(host=display_host, port=port)
            
    @staticmethod
    def save_plugin_persistent_config(config: dict):
        config_path = TooManyStreamsConfig.get_persistent_storage_path()
        try:
            with open(config_path, "w") as f:
                json.dump(config, f, indent=4)
            logger.info(f"Successfully saved config to {config_path}")
            TooManyStreamsConfig.clear_cache()
        except Exception as e:
            logger.error(f"Error saving config to {config_path}: {e}")

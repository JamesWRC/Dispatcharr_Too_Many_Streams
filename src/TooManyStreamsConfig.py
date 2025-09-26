# Holds env vars and config for the TooManyStreams plugin

import os
import json


DEFAULT_CSS = """
    html, body {
    width: 1920px; height: 1080px; margin: 0; background: #fff; color: #111;
    font-family: Arial, "Segoe UI", Roboto, sans-serif; /* simple stack */
    }

    /* Center whole block */
    body {
    text-align: center; /* minimal, avoids flex */
    }
    .wrap {
    width: 92%;
    max-width: 1680px;
    margin: 0 auto;
    display: block;
    }

    h1 {
    font-size: 48px;  /* fixed size; avoid clamp() */
    margin: 0 0 12px;
    }
    .desc {
    width: 82%;
    margin: 0 auto 20px;
    font-size: 20px;    /* fixed */
    line-height: 1.45;
    color: #333;
    }

    /* ------- Grid replacement (row/column without CSS Grid) ------- */
    /* Set columns in your template (C = self.html_cols) */
    /* Each card becomes an inline-block with percentage width */
    .grid { font-size: 0; /* remove gaps between inline-blocks */ }
    .card {
    display: inline-block;
    vertical-align: top;
    width: REPLACE_WITH_PERCENT%;  /* = 100 / C, e.g., 50% for 2 cols, 33.333% for 3. This value is calculated via python using the number of channel cols */
    box-sizing: border-box;
    padding: 16px 22px;
    margin: 7px 9px;               /* simulate gap */
    background: #ffffff;
    border: 1px solid #e6e9ef;
    border-radius: 16px;
    box-shadow: 0 1px 2px rgba(16,24,40,0.04);
    font-size: 16px; /* restore text size */
    text-align: left;
    }

    /* darker stripe for “even” rows: add class server-side */
    .card_even {
    display: inline-block;
    vertical-align: top;
    width: REPLACE_WITH_PERCENT%;  /* = 100 / C, e.g., 50% for 2 cols, 33.333% for 3. This value is calculated via python using the number of channel cols */
    box-sizing: border-box;
    padding: 16px 22px;
    margin: 7px 9px;               /* simulate gap */
    background: #e2e2e2;
    border: 1px solid #e6e9ef;
    border-radius: 16px;
    box-shadow: 0 1px 2px rgba(16,24,40,0.04);
    font-size: 16px; /* restore text size */
    text-align: left;
    }

    /* channel number pill */
    .chan {
    display: inline-block;
    font-weight: 600;
    font-size: 18px;
    padding: 6px 10px;
    border-radius: 999px;
    background: #e1e1e1;
    color: #7294f2;
    border: 1px solid #dbe5ff;
    white-space: nowrap;
    margin-right: 12px;
    }

    /* logo box (avoid object-fit) */
    .icon {
    display: inline-block;
    width: 120px; height: 120px;   /* smaller for wkhtml; adjust as needed */
    overflow: hidden;
    border-radius: 12px;
    border: 1px solid #e6e9ef;
    vertical-align: middle;
    margin-right: 14px;
    background: transparent;
    }
    .icon img {
    max-width: 100%;
    max-height: 100%;
    display: block;
    background: transparent;
    }

    .name {
    display: inline-block;
    vertical-align: middle;
    max-width: calc(100% - 150px); /* crude but works in wkhtml */
    font-size: 24px;
    line-height: 1.35;
    font-weight: 600;
    letter-spacing: 0.01em;
    color: #0b1220;
    white-space: normal;
    word-break: break-word;
    /* avoid text-wrap/hyphens for compatibility */
    }
"""

class TooManyStreamsConfig:
    _STREAM_URL = 'http://{host}:{port}/stream.ts'
    PLUGIN_KEY = 'too_many_streams'
    PERSISTENT_CONFIG_FOLDER = "persistent_config"

    @staticmethod
    def get_host_and_port() -> tuple[str, int]:
        """
        Returns the host and port for the "Too Many Streams" service.
        Uses the TMS_HOST and TMS_PORT environment variables if set, otherwise defaults to "
        """
        _host = os.environ.get("TMS_HOST", "0.0.0.0")
        _port = os.environ.get("TMS_PORT", 1337)

        assert isinstance(_host, str)
        assert isinstance(_port, (str, int)) and str(_port).isdigit(), "TMS_PORT must be an integer"

        return (_host, _port)
    
    @staticmethod
    def get_stream_url() -> str:
        """
        Returns the URL where the "Too Many Streams" image can be accessed.
        Uses the host and port from environment variables or defaults.
        """
        host, port = TooManyStreamsConfig.get_host_and_port()
        return TooManyStreamsConfig._STREAM_URL.format(host=host, port=port)
    

    @staticmethod
    def get_plugin_config(config_key:str=None):
        """
        Retrieves the plugin configuration from the database for the given config_key.
        If config_key is None, returns the entire settings dictionary.
        Args:
            config_key (str): The specific configuration key to retrieve. If None, returns the entire settings dict.
        Returns:
            The value associated with the config_key, the entire settings dictionary if config_key is None, or None if not found."""
        from apps.plugins.models import PluginConfig
        try:
            if cfg := PluginConfig.objects.filter(key=TooManyStreamsConfig.PLUGIN_KEY).first():
                print(f"cfg.settings={cfg.settings}")
                st = dict(cfg.settings or {})
                # If no config key is provided, return the whole settings dict
                if config_key is None:
                    return st
                
                val = st.get(config_key, None)
                print(f"TooManyStreamsConfig: Found plugin config for key {TooManyStreamsConfig.PLUGIN_KEY}, returning {config_key}={val}")
                return val
            else:
                print(f"TooManyStreamsConfig: No plugin config found for key {TooManyStreamsConfig.PLUGIN_KEY}")
                return TooManyStreamsConfig.get_plugin_persistent_config().get(config_key, None)

        except Exception as e:
            print(f"TooManyStreamsConfig: Error retrieving plugin config for key {TooManyStreamsConfig.PLUGIN_KEY}: {e}")
            TooManyStreamsConfig.get_plugin_persistent_config().get(config_key, None)
            return None
            

    @staticmethod
    def get_persistent_storage_path() -> str:
        """
        Returns the path to the persistent storage file for the plugin configuration.
        The file is located two directories above this file's directory.
        """
        plugin_root_dir = os.path.dirname(os.path.abspath(__file__))
        # go up 2 directories
        plugin_dir = os.path.dirname(os.path.dirname(plugin_root_dir))
        config_file = os.path.join(plugin_dir, TooManyStreamsConfig.PERSISTENT_CONFIG_FOLDER, "too_many_streams_persistent_config.json")
        if not os.path.exists(os.path.dirname(config_file)):
            os.makedirs(os.path.dirname(config_file), exist_ok=True)
        return config_file

    @staticmethod
    def get_plugin_persistent_config():
        """
        Loads and returns the plugin persistent config from the persistent storage path as a dictionary.
        If the file does not exist or cannot be read, returns an empty dictionary.
        """
        config_path = TooManyStreamsConfig.get_persistent_storage_path()
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    return json.load(f)
            except Exception as e:
                print(f"TooManyStreamsConfig: Error loading config from {config_path}: {e}")
                return {}
        else:
            return {}
    @staticmethod
    def save_plugin_persistent_config(config: dict):
        """
        Saves the provided config dictionary to the persistent storage path as JSON.
        """
        config_path = TooManyStreamsConfig.get_persistent_storage_path()
        try:
            with open(config_path, "w") as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            print(f"TooManyStreamsConfig: Error saving config to {config_path}: {e}")

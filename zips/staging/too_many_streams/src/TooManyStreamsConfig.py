# Holds env vars and config for the TooManyStreams plugin

import os

class TooManyStreamsConfig:
    _STREAM_URL = 'http://{host}:{port}/stream.ts'

    @staticmethod
    def get_host_and_port() -> tuple[str, int]:
        _host = os.environ.get("TMS_HOST", "0.0.0.0")
        _port = os.environ.get("TMS_PORT", 1337)

        assert isinstance(_host, str)
        assert isinstance(_port, (str, int)) and str(_port).isdigit(), "TMS_PORT must be an integer"

        return (_host, _port)
    
    @staticmethod
    def get_stream_url() -> str:
        host, port = TooManyStreamsConfig.get_host_and_port()
        return TooManyStreamsConfig._STREAM_URL.format(host=host, port=port)
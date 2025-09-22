# Custom exception for the TooManyStreams plugin

class TooManyStreamsException(Exception):
    """Base exception for TooManyStreams plugin"""
    pass

class TMS_CustomStreamNotFound(TooManyStreamsException):
    """Raised when a custom stream is not found"""
    pass
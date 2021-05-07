import logging
from logging.handlers import RotatingFileHandler


file_format_string = "%(asctime)s.%(msecs)03d %(name)s:%(lineno)d [%(levelname)s]  %(message)s"


stream_format_string = "%(asctime)s %(name)s:%(lineno)d [%(levelname)s]  %(message)s"


def set_file_logger(filename,
                    name='funcx',
                    level=logging.DEBUG,
                    format_string=None,
                    max_bytes=100 * 1024 * 1024,
                    backup_count=1):
    """Add a stream log handler.

    Args:
        - filename (string): Name of the file to write logs to
        - name (string): Logger name
        - level (logging.LEVEL): Set the logging level.
        - format_string (string): Set the format string
        - maxBytes: The maximum bytes per logger file, default: 100MB
        - backupCount: The number of backup (must be non-zero) per logger file, default: 1

    Returns:
       -  None
    """
    if format_string is None:
        format_string = file_format_string

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(filename, maxBytes=max_bytes, backupCount=backup_count)
    handler.setLevel(level)
    formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


def set_stream_logger(name='funcx', level=logging.DEBUG, format_string=None):
    """Add a stream log handler.

    Args:
         - name (string) : Set the logger name.
         - level (logging.LEVEL) : Set to logging.DEBUG by default.
         - format_string (string) : Set to None by default.

    Returns:
         - None
    """
    if format_string is None:
        format_string = stream_format_string

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


logging.getLogger('funcx').addHandler(logging.NullHandler())

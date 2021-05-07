""" funcX : Fast function serving for clouds, clusters and supercomputers.

"""
import logging
from funcx.sdk.version import VERSION

__author__ = "The funcX team"
__version__ = VERSION

from funcx.sdk.client import FuncXClient
from funcx.utils.loggers import set_file_logger, set_stream_logger

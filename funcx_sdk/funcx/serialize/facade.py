import atexit
import multiprocessing as mp
import subprocess
import uuid
import time
from funcx.serialize.concretes import *
from funcx.serialize.base import METHODS_MAP_DATA, METHODS_MAP_CODE
from funcx.serialize.off_process_checker import OffProcessClient
import logging
logger = logging.getLogger(__name__)


class FuncXSerializer(object):
    """ Wraps several serializers for one uniform interface
    """

    def __init__(self, use_offprocess_checker=False):
        """ Instantiate the appropriate classes
        """

        # Do we want to do a check on header size here ? Probably overkill
        headers = list(METHODS_MAP_CODE.keys()) + list(METHODS_MAP_DATA.keys())
        self.header_size = len(headers[0])
        self.use_offprocess_checker = use_offprocess_checker

        if self.use_offprocess_checker:
            self.serialize_lock = mp.Lock()

            logger.debug("Starting off_process_checker")
            try:
                port = self._start_off_process_checker()
            except Exception:
                logger.exception("Off_process_checker instantiation failed")
                self.use_offprocess_checker = False
            else:
                self.off_proc_client = OffProcessClient(port)
                self.off_proc_client.connect()
                atexit.register(self.cleanup)

        self.methods_for_code = {}
        self.methods_for_data = {}

        for key in METHODS_MAP_CODE:
            self.methods_for_code[key] = METHODS_MAP_CODE[key]()
        for key in METHODS_MAP_DATA:
            self.methods_for_data[key] = METHODS_MAP_DATA[key]()

    def _start_off_process_checker(self):
        """ Kicks off off_process_checker using subprocess and returns the port on which it
        is listening.
        """
        std_file = f'/tmp/{uuid.uuid4()}'
        logger.debug(f"Writing off_process_checker logs to: {std_file}")
        std_out = open(std_file, 'w')
        self.serialize_proc = subprocess.Popen(['off_process_checker.py'], stdout=std_out, stderr=subprocess.STDOUT)
        with open(std_file) as f:
            for i in range(200, 3000, 200):
                time.sleep(i / 100)  # Sleep incrementally waiting for process start
                for line in f.readlines():
                    if 'BINDING TO' in line:
                        _, port = line.strip().split(':')
                        port = int(port)
                        return port

        raise Exception("Failed to determine off_process_checker's port")

    def _list_methods(self):
        return self.methods_for_code, self.methods_for_data

    def cleanup(self):
        logger.debug("Cleaning up")
        if self.use_offprocess_checker:
            self.off_proc_client.close()
            self.serialize_proc.terminate()

    def deserialize_check(self, serialized_msg, timeout=1):
        """
        Parameters
        ----------
        serialized_msg

        timeout : int
           timeout in seconds to wait for result
        """
        if not self.use_offprocess_checker:
            return False

        with self.serialize_lock:
            status, info = self.off_proc_client.send_recv(serialized_msg)

            if status == 'SUCCESS':
                return True
            if status == 'PONG':
                return status
            else:
                logger.exception("Got exception while attempting deserialization")
                raise Exception(info)

    def serialize(self, data):
        serialized = None
        serialized_flag = False
        last_exception = None

        if callable(data):
            for method in self.methods_for_code.values():
                try:
                    serialized = method.serialize(data)
                    self.deserialize_check(serialized)
                except Exception as e:
                    logger.debug(f"[NON-CRITICAL] Method {method} failed with exception: {e}")
                    last_exception = e
                    continue
                else:
                    serialized_flag = True
                    break

        else:
            for method in self.methods_for_data.values():
                try:
                    serialized = method.serialize(data)
                except Exception as e:
                    logger.exception("Method {} did not work".format(method))
                    last_exception = e
                    continue
                else:
                    serialized_flag = True
                    break

        if serialized_flag is False:
            raise last_exception

        return serialized

    def deserialize(self, payload):
        """
        Parameters
        ----------
        payload : str
           Payload object to be deserialized

        """
        header = payload[0:self.header_size]
        if header in self.methods_for_code:
            result = self.methods_for_code[header].deserialize(payload)
        elif header in self.methods_for_data:
            result = self.methods_for_data[header].deserialize(payload)
        else:
            raise Exception("Invalid header: {} in data payload".format(header))

        return result

    def pack_buffers(self, buffers):
        """
        Parameters
        ----------
        buffers : list of \n terminated strings
        """
        packed = ''
        for buf in buffers:
            s_length = str(len(buf)) + '\n'
            packed += s_length + buf

        return packed

    def unpack_buffers(self, packed_buffer):
        """
        Parameters
        ----------
        packed_buffers : packed buffer as string
        """
        unpacked = []
        while packed_buffer:
            s_length, buf = packed_buffer.split('\n', 1)
            i_length = int(s_length)
            current, packed_buffer = buf[:i_length], buf[i_length:]
            unpacked.extend([current])

        return unpacked

    def unpack_and_deserialize(self, packed_buffer):
        """ Unpacks a packed buffer and returns the deserialized contents
        Parameters
        ----------
        packed_buffers : packed buffer as string
        """
        unpacked = []
        while packed_buffer:
            s_length, buf = packed_buffer.split('\n', 1)
            i_length = int(s_length)
            current, packed_buffer = buf[:i_length], buf[i_length:]
            deserialized = self.deserialize(current)
            unpacked.extend([deserialized])

        assert len(unpacked) == 3, "Unpack expects 3 buffers, got {}".format(len(unpacked))

        return unpacked

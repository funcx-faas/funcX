#!/usr/bin/env python3

import zmq
import time
import pickle
import logging

from funcx import set_file_logger
from funcx_endpoint.executors.high_throughput.messages import Message

logger = logging.getLogger(__name__)


class CommandClient(object):
    """ CommandClient
    """

    def __init__(self, ip_address, port_range):
        """
        Parameters
        ----------

        ip_address: str
           IP address of the client (where Parsl runs)
        port_range: tuple(int, int)
           Port range for the comms between client and interchange

        """
        self.context = zmq.Context()
        self.zmq_socket = self.context.socket(zmq.DEALER)
        self.zmq_socket.set_hwm(0)
        self.port = self.zmq_socket.bind_to_random_port("tcp://{}".format(ip_address),
                                                        min_port=port_range[0],
                                                        max_port=port_range[1])

    def run(self, message):
        """ This function needs to be fast at the same time aware of the possibility of
        ZMQ pipes overflowing.

        The timeout increases slowly if contention is detected on ZMQ pipes.
        We could set copy=False and get slightly better latency but this results
        in ZMQ sockets reaching a broken state once there are ~10k tasks in flight.
        This issue can be magnified if each the serialized buffer itself is larger.
        """
        self.zmq_socket.send(message.pack(), copy=True)
        reply = self.zmq_socket.recv()
        return Message.unpack(reply)

    def close(self):
        self.zmq_socket.close()
        self.context.term()


class TasksOutgoing(object):
    """ Outgoing task queue from the executor to the Interchange
    """

    def __init__(self, ip_address, port_range):
        """
        Parameters
        ----------

        ip_address: str
           IP address of the client (where Parsl runs)
        port_range: tuple(int, int)
           Port range for the comms between client and interchange

        """
        self.context = zmq.Context()
        self.zmq_socket = self.context.socket(zmq.DEALER)
        self.zmq_socket.set_hwm(0)
        self.port = self.zmq_socket.bind_to_random_port("tcp://{}".format(ip_address),
                                                        min_port=port_range[0],
                                                        max_port=port_range[1])
        self.poller = zmq.Poller()
        self.poller.register(self.zmq_socket, zmq.POLLOUT)

    def put(self, message, max_timeout=1000):
        """ This function needs to be fast at the same time aware of the possibility of
        ZMQ pipes overflowing.

        The timeout increases slowly if contention is detected on ZMQ pipes.
        We could set copy=False and get slightly better latency but this results
        in ZMQ sockets reaching a broken state once there are ~10k tasks in flight.
        This issue can be magnified if each the serialized buffer itself is larger.

        Parameters
        ----------

        message : py object
             Python object to send
        max_timeout : int
             Max timeout in milliseconds that we will wait for before raising an exception

        Raises
        ------

        zmq.EAGAIN if the send failed.

        """
        timeout_ms = 0
        current_wait = 0
        while current_wait < max_timeout:
            socks = dict(self.poller.poll(timeout=timeout_ms))
            if self.zmq_socket in socks and socks[self.zmq_socket] == zmq.POLLOUT:
                # The copy option adds latency but reduces the risk of ZMQ overflow
                self.zmq_socket.send(message, copy=True)
                return
            else:
                timeout_ms += 1
                logger.debug("Not sending due to full zmq pipe, timeout: {} ms".format(timeout_ms))
            current_wait += timeout_ms

        # Send has failed.
        logger.debug("Remote side has been unresponsive for {}".format(current_wait))
        raise zmq.error.Again

    def close(self):
        self.zmq_socket.close()
        self.context.term()


class ResultsIncoming(object):
    """ Incoming results queue from the Interchange to the executor
    """

    def __init__(self, ip_address, port_range):
        """
        Parameters
        ----------

        ip_address: str
           IP address of the client (where Parsl runs)
        port_range: tuple(int, int)
           Port range for the comms between client and interchange

        """
        self.context = zmq.Context()
        self.results_receiver = self.context.socket(zmq.DEALER)
        self.results_receiver.set_hwm(0)
        self.port = self.results_receiver.bind_to_random_port("tcp://{}".format(ip_address),
                                                              min_port=port_range[0],
                                                              max_port=port_range[1])

    def get(self, block=True, timeout=None):
        block_messages = self.results_receiver.recv()
        try:
            res = pickle.loads(block_messages)
        except pickle.UnpicklingError:
            try:
                res = Message.unpack(block_messages)
            except Exception:
                logger.exception(f"Message in results queue is not pickle/Message formatted:{block_messages}")
        return res

    def request_close(self):
        status = self.results_receiver.send(pickle.dumps(None))
        time.sleep(0.1)
        return status

    def close(self):
        self.results_receiver.close()
        self.context.term()

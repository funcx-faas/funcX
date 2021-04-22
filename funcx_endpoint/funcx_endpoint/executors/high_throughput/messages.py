import json
import uuid
from abc import ABC, abstractmethod
from enum import Enum, auto
from struct import Struct
from typing import Tuple
MESSAGE_TYPE_FORMATTER = Struct('b')


class MessageType(Enum):
    HEARTBEAT_REQ = auto()
    HEARTBEAT = auto()
    EP_STATUS_REPORT = auto()
    MANAGER_STATUS_REPORT = auto()
    TASK = auto()

    def pack(self):
        return MESSAGE_TYPE_FORMATTER.pack(self.value)

    @classmethod
    def unpack(cls, buffer):
        mtype, = MESSAGE_TYPE_FORMATTER.unpack_from(buffer, offset=0)
        return MessageType(mtype), buffer[MESSAGE_TYPE_FORMATTER.size:]


class TaskStatusCode(int, Enum):
    WAITING_FOR_NODES = auto()
    WAITING_FOR_LAUNCH = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()


COMMAND_TYPES = {
    MessageType.HEARTBEAT_REQ
}


class Message(ABC):
    def __init__(self):
        self._payload = None
        self._header = None

    @property
    def header(self):
        return self._header

    @property
    def type(self):
        raise NotImplementedError()

    @property
    def payload(self):
        return self._payload

    @payload.setter
    def payload(self, p):
        return

    @classmethod
    def unpack(cls, msg):
        message_type, remaining = MessageType.unpack(msg)
        if message_type is MessageType.HEARTBEAT_REQ:
            return HeartbeatReq.unpack(remaining)
        elif message_type is MessageType.HEARTBEAT:
            return Heartbeat.unpack(remaining)
        elif message_type is MessageType.EP_STATUS_REPORT:
            return EPStatusReport.unpack(remaining)
        elif message_type is MessageType.MANAGER_STATUS_REPORT:
            return ManagerStatusReport.unpack(remaining)
        elif message_type is MessageType.TASK:
            return Task.unpack(remaining)

        raise Exception(f"Unknown Message Type Code: {message_type}")

    @abstractmethod
    def pack(self):
        raise NotImplementedError()


class Task(Message):
    """
    Task message from the forwarder->interchange
    """
    type = MessageType.TASK

    def __init__(self, task_id: str, container_id: str, task_buffer: str, raw_buffer=None):
        super().__init__()
        self.task_id = task_id
        self.container_id = container_id
        self.task_buffer = task_buffer
        self.raw_buffer = raw_buffer

    def pack(self) -> bytes:

        if self.raw_buffer is None:
            add_ons = f'TID={self.task_id};CID={self.container_id};{self.task_buffer}'
            self.raw_buffer = add_ons.encode('utf-8')

        return self.type.pack() + self.raw_buffer

    @classmethod
    def unpack(cls, raw_buffer: bytes):
        b_tid, b_cid, task_buf = raw_buffer.decode('utf-8').split(';', 2)
        return cls(b_tid[4:], b_cid[4:], task_buf.encode('utf-8'), raw_buffer=raw_buffer)

    def set_local_container(self, container_id):
        self.local_container = container_id


class HeartbeatReq(Message):
    """
    Synchronous request for a Heartbeat.  This is sent from the Forwarder to the endpoint on start to get
    an initial connection and ensure liveness.
    """
    type = MessageType.HEARTBEAT_REQ

    @property
    def header(self):
        return None

    @property
    def payload(self):
        return None

    @classmethod
    def unpack(cls, msg):
        return cls()

    def pack(self):
        return self.type.pack()


class Heartbeat(Message):
    """
    Generic Heartbeat message, sent in both directions between Forwarder and Interchange.
    """
    type = MessageType.HEARTBEAT

    def __init__(self, endpoint_id):
        super().__init__()
        self.endpoint_id = endpoint_id

    @classmethod
    def unpack(cls, msg):
        return cls(msg.decode("ascii"))

    def pack(self):
        return self.type.pack() + self.endpoint_id.encode("ascii")


class EPStatusReport(Message):
    """
    Status report for an endpoint, sent from Interchange to Forwarder.  Includes EP-wide info such as utilization,
    as well as per-task status information.
    """
    type = MessageType.EP_STATUS_REPORT

    def __init__(self, endpoint_id, ep_status_report, task_statuses):
        super().__init__()
        self._header = uuid.UUID(endpoint_id).bytes
        self.ep_status = ep_status_report
        self.task_statuses = task_statuses
        # self._payload = task_statuses

    @classmethod
    def unpack(cls, msg):
        endpoint_id = str(uuid.UUID(bytes=msg[:16]))
        msg = msg[16:]
        jsonified = msg.decode("ascii")
        ep_status, task_statuses = json.loads(jsonified)
        return cls(endpoint_id, ep_status, task_statuses)

    def pack(self):
        # TODO: do we want to do better than JSON?
        jsonified = json.dumps([self.ep_status, self.task_statuses])
        return self.type.pack() + self._header + jsonified.encode("ascii")


class ManagerStatusReport(Message):
    """
    Status report sent from the Manager to the Interchange, which mostly just amounts to saying which tasks are now
    RUNNING.
    """
    type = MessageType.MANAGER_STATUS_REPORT

    def __init__(self, task_statuses, container_switch_count):
        super().__init__()
        self.task_statuses = task_statuses
        self.container_switch_count = container_switch_count

    @classmethod
    def unpack(cls, msg):
        container_switch_count = int.from_bytes(msg[:10], 'little')
        msg = msg[10:]
        jsonified = msg.decode("ascii")
        task_statuses = json.loads(jsonified)
        return cls(task_statuses, container_switch_count)

    def pack(self):
        # TODO: do better than JSON?
        jsonified = json.dumps(self.task_statuses)
        return self.type.pack() + self.container_switch_count.to_bytes(10, 'little') + jsonified.encode("ascii")

from funcx_endpoint.executors.high_throughput.funcx_manager import Manager
from funcx_endpoint.executors.high_throughput.messages import Task
import queue
import logging
import pickle
import zmq
import os
import shutil
import pytest


class TestManager:

    @pytest.fixture(autouse=True)
    def test_setup_teardown(self):
        os.makedirs(os.path.join(os.getcwd(), 'mock_uid'))
        yield
        shutil.rmtree(os.path.join(os.getcwd(), 'mock_uid'))

    def test_remove_worker_init(self):
        manager = Manager(logdir='./', uid="mock_uid")
        manager.worker_map.to_die_count["RAW"] = 0
        manager.task_queues["RAW"] = queue.Queue()

        manager.remove_worker_init("RAW")
        task = manager.task_queues["RAW"].get()
        assert isinstance(task, Task)
        assert task.task_id == "KILL"
        assert task.task_buffer == "KILL"

    def test_manager_worker(self):
        manager = Manager(logdir='./', uid="mock_uid")
        manager.worker_map.to_die_count["RAW"] = 0
        manager.task_queues["RAW"] = queue.Queue()
        manager.logdir = "./"
        manager.worker_type = 'RAW'

        # Manager::start()
        worker = manager.worker_map.add_worker(worker_id="0",
                                               worker_type=manager.worker_type,
                                               address=manager.address,
                                               debug=logging.DEBUG,
                                               uid=manager.uid,
                                               logdir=manager.logdir,
                                               worker_port=manager.worker_port)
        # The worker process should have been spawned above,
        # and it should have sent out the registration message
        manager.worker_procs.update(worker)
        assert len(manager.worker_procs) == 1

        # Manager::pull_tasks()
        poller = zmq.Poller()
        poller.register(manager.funcx_task_socket, zmq.POLLIN)

        # Manager::pull_tasks()
        # We want to catch the worker's registration message
        w_id, m_type, message = manager.funcx_task_socket.recv_multipart()
        reg_info = pickle.loads(message)
        assert reg_info['worker_id'] == '0'
        assert reg_info['worker_type'] == 'RAW'
        manager.worker_map.register_worker(w_id, reg_info['worker_type'])

        # Begin testing removing worker process
        manager.remove_worker_init("RAW")
        task = manager.task_queues["RAW"].get()
        worker_id = manager.worker_map.get_worker("RAW")
        to_send = [worker_id, pickle.dumps(task.task_id), pickle.dumps(task.container_id), task.pack()]
        manager.funcx_task_socket.send_multipart(to_send)

        # We want to catch the worker's response to the remove message
        w_id, m_type, message = manager.funcx_task_socket.recv_multipart()
        assert m_type == b'WRKR_DIE'
        assert pickle.loads(message) is None

        manager.worker_map.remove_worker(w_id)
        manager.worker_procs.pop(w_id.decode())
        assert len(manager.worker_procs) == 0

import logging
import shutil
import uuid
from queue import Queue

import pytest
from globus_compute_common import messagepack
from globus_compute_endpoint.engines import GlobusComputeEngine
from globus_compute_sdk.serialize import ComputeSerializer
from parsl.executors.errors import BadStateException
from parsl.providers import SlurmProvider
from tests.utils import double, ez_pack_function

logger = logging.getLogger(__name__)


@pytest.fixture
def gc_engine_bad_submit_command(tmp_path):
    ep_id = uuid.uuid4()
    sbatch_path = shutil.which("sbatch")
    assert not sbatch_path, "Tests should be run on a Non slurm machine"
    engine = GlobusComputeEngine(
        address="127.0.0.1",
        heartbeat_period=1,
        heartbeat_threshold=2,
        provider=SlurmProvider(
            init_blocks=1,
            min_blocks=0,
            max_blocks=1,
        ),
        strategy="simple",
        job_status_kwargs={"max_idletime": 0, "strategy_period": 0.1},
    )
    queue = Queue()
    engine.start(endpoint_id=ep_id, run_dir=str(tmp_path), results_passthrough=queue)

    yield engine
    engine.shutdown()


def test_broken_provider(gc_engine_bad_submit_command, caplog):
    engine = gc_engine_bad_submit_command
    queue = engine.results_passthrough

    task_id = uuid.uuid4()
    serializer = ComputeSerializer()
    task_body = ez_pack_function(
        serializer,
        double,
        (5,),
        {},
    )
    task_message = messagepack.pack(
        messagepack.message_types.Task(task_id=task_id, task_buffer=task_body)
    )
    future = engine.submit(task_id=task_id, packed_task=task_message)

    with pytest.raises(BadStateException):
        future.result(timeout=60)

    report = str(future.exception())
    logger.warning(f"********** Got {report=}")

    assert "127" in report
    assert "not found" in report
    assert "Failed to start block 0: Cannot launch job" in report

    flag = False
    for _i in range(10):
        q_msg = queue.get(timeout=5)
        assert isinstance(q_msg, dict)

        packed_result_q = q_msg["message"]
        result = messagepack.unpack(packed_result_q)
        if isinstance(result, messagepack.message_types.Result):
            assert result.task_id == task_id
            assert result.error_details
            assert result.data
            assert "127" in report
            assert "not found" in result.data
            assert "Failed to start block 0: Cannot launch job" in result.data
            flag = True
            break

    assert flag, "Expected BadStateException in failed result.data, but none received"
    engine.shutdown()

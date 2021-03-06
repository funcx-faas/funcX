from funcx_endpoint.endpoint.utils.config import Config
from funcx_endpoint.executors import HighThroughputExecutor
from parsl.providers import TorqueProvider
from parsl.launchers import AprunLauncher
from parsl.addresses import address_by_hostname

# PLEASE UPDATE user_opts BEFORE USE
user_opts = {
    'bluewaters': {
        'worker_init': 'module load bwpy;source anaconda3/etc/profile.d/conda.sh;conda activate funcx_testing_py3.7',
        'scheduler_options': '',
    }
}

config = Config(
    executors=[
        HighThroughputExecutor(
            max_workers_per_node=1,
            worker_debug=False,
            address=address_by_hostname(),
            provider=TorqueProvider(
                queue='normal',
                launcher=AprunLauncher(overrides="-b -- bwpy-environ --"),
                # string to prepend to #SBATCH blocks in the submit
                scheduler_options=user_opts['bluewaters']['scheduler_options'],

                # Command to be run before starting a worker, such as:
                # 'module load bwpy; source activate parsl_env'.
                worker_init=user_opts['bluewaters']['worker_init'],
                init_blocks=1,
                max_blocks=1,
                min_blocks=1,
                nodes_per_block=2,
                walltime='00:30:00'
            ),
        )

    ],
)

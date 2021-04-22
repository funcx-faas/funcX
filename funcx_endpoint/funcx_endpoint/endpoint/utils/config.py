from parsl.utils import RepresentationMixin
from funcx_endpoint.strategies.simple import SimpleStrategy
from funcx_endpoint.executors import HighThroughputExecutor


class Config(RepresentationMixin):
    """ Specification of FuncX configuration options.

    Parameters
    ----------

    max_workers_per_node : int
        Maximum # of worker per node. Default: inf

    cores_per_worker : float
        cores to be assigned to each worker. Oversubscription is possible
        by setting cores_per_worker < 1.0. Default=1

    mem_per_worker : float
        GB of memory required per worker. If this option is specified, the node manager
        will check the available memory at startup and limit the number of workers such that
        the there's sufficient memory for each worker. Default: None

    working_dir : str
        Working dir to be used by the executor. Default to the endpoint directory if not specified

    worker_debug : Bool
        Enables worker debug logging.

    worker_mode : str
        Select the mode of operation from no_container, singularity_reuse, singularity_single_use
        Default: no_container

    scheduler_mode : str
        Select the mode of how the container is managed from hard, soft
        Default: hard

    container_type : str
        Select the type of container from Docker, Singularity, Shifter
        Default: None

    scaling_enabled : Bool
        Allow Interchange to manage resource provisioning. If set to False, interchange
        will not do any scaling.
        Default: True

    stdout : str
        Path where the endpoint's stdout should be written
        Default: ./interchange.stdout

    stderr : str
        Path where the endpoint's stderr should be written
        Default: ./interchange.stderr

    detach_endpoint : Bool
        Should the endpoint deamon be run as a detached process? This is good for
        a real edge node, but an anti-pattern for kubernetes pods
        Default: True


    """

    def __init__(self,

                 # Execution backed
                 executors: list = [HighThroughputExecutor()],

                 # Scaling mechanics
                 scaling_enabled=True,

                 # Connection info
                 worker_ports=None,
                 worker_port_range=(54000, 55000),
                 funcx_service_address='https://api2.funcx.org/v2',

                 # Container info
                 worker_mode='no_container',
                 scheduler_mode='hard',
                 container_type=None,

                 # Tuning info
                 prefetch_capacity=10,
                 heartbeat_period=30,
                 heartbeat_threshold=120,
                 poll_period=10,
                 detach_endpoint=True,
                 # Logging info
                 log_max_bytes=256 * 1024 * 1024,  # in bytes
                 log_backup_count=1,
                 working_dir=None,
                 stdout="./interchange.stdout",
                 stderr="./interchange.stderr",
                 worker_debug=False):

        # Execution backends
        self.executors = executors  # List of executors

        # Scaling mechanics
        self.scaling_enabled = scaling_enabled

        # Connection info
        self.worker_ports = worker_ports
        self.worker_port_range = worker_port_range
        self.funcx_service_address = funcx_service_address

        # Container info
        self.worker_mode = worker_mode
        self.scheduler_mode = scheduler_mode
        self.container_type = container_type

        # Tuning info
        self.prefetch_capacity = prefetch_capacity
        self.heartbeat_period = heartbeat_period
        self.heartbeat_threshold = heartbeat_threshold
        self.poll_period = poll_period
        self.detach_endpoint = detach_endpoint

        # Logging info
        self.log_max_bytes = log_max_bytes
        self.log_backup_count = log_backup_count
        self.working_dir = working_dir
        self.worker_debug = worker_debug
        self.stdout = stdout
        self.stderr = stderr

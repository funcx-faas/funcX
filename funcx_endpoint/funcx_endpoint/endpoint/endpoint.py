import glob
from importlib.machinery import SourceFileLoader
import json
import logging
import os
import pathlib
import random
import shutil
import signal
import sys
import time
import uuid
from string import Template

import daemon
import daemon.pidfile
import psutil
import requests
import texttable as tt
import typer
from retry import retry

import funcx
from funcx.utils.errors import *
from funcx_endpoint.executors.high_throughput import default_config as endpoint_default_config
from funcx_endpoint.executors.high_throughput import global_config as funcx_default_config
from funcx_endpoint.executors.high_throughput.interchange import Interchange
from funcx.sdk.client import FuncXClient


FUNCX_CONFIG_FILE_NAME = 'config.py'

app = typer.Typer()


class State:
    DEBUG = False
    FUNCX_DIR = '{}/.funcx'.format(pathlib.Path.home())
    FUNCX_CONFIG_FILE = os.path.join(FUNCX_DIR, FUNCX_CONFIG_FILE_NAME)
    FUNCX_DEFAULT_CONFIG_TEMPLATE = funcx_default_config.__file__
    FUNCX_CONFIG = {}


def version_callback(value):
    if value:
        from funcx_endpoint.endpoint import VERSION
        typer.echo("FuncX endpoint version: {}".format(VERSION))
        raise typer.Exit()


def complete_endpoint_name():
    config_files = glob.glob('{}/*/config.py'.format(State.FUNCX_DIR))
    for config_file in config_files:
        yield os.path.basename(os.path.dirname(config_file))


def check_pidfile(filepath, match_name='funcx-endpoint', endpoint_name='', log=False):
    """ Helper function to identify possible dead endpoints

    Returns a record with 'exists' and 'active' fields indicating
    whether the pidfile exists, and whether the process is active if it does exist
    (The endpoint can only start correctly if there is no pidfile)

    Parameters
    ----------
    filepath : str
        Path to the pidfile

    match_name : str
       Name of the process to check for if pidfile exists

    endpoint_name : str
        endpoint name for debugging purposes

    log : bool
        whether or not to log instructions for user if pidfile exists

    """
    if not os.path.exists(filepath):
        return {
            "exists": 0,
            "active": 0,
        }

    older_pid = int(open(filepath, 'r').read().strip())

    proc_found = False
    try:
        proc = psutil.Process(older_pid)
        if proc.name() == match_name:
            # this is the only case where the endpoint is active.
            # If the process name does not match or no process exists,
            # it means the endpoint has been terminated without proper cleanup
            proc_found = True

    except psutil.NoSuchProcess:
        pass

    if log:
        if proc_found:
            logger.info("Endpoint is already active")
        else:
            logger.info("A prior Endpoint instance appears to have been terminated without proper cleanup")
            logger.info('''Please cleanup using:
    $ funcx-endpoint stop {}'''.format(endpoint_name))

    if proc_found:
        return {
            "exists": 1,
            "active": 1,
        }

    return {
        "exists": 1,
        "active": 0,
    }


def init_endpoint_dir(endpoint_name, endpoint_config=None):
    """ Initialize a clean endpoint dir

    Returns if an endpoint_dir already exists

    Parameters
    ----------
    endpoint_name : str
        Name of the endpoint, which will be used to name the dir
        for the endpoint in the FUNCX_DIR

    endpoint_config : str
       Path to a config file to be used instead of the funcX default config file

    """
    endpoint_dir = os.path.join(State.FUNCX_DIR, endpoint_name)
    logger.debug(f"Creating endpoint dir {endpoint_dir}")
    os.makedirs(endpoint_dir, exist_ok=True)

    endpoint_config_target_file = os.path.join(endpoint_dir, FUNCX_CONFIG_FILE_NAME)
    if endpoint_config:
        shutil.copyfile(endpoint_config, endpoint_config_target_file)
        return endpoint_dir

    endpoint_config = endpoint_default_config.__file__
    with open(endpoint_config) as r:
        endpoint_config_template = Template(r.read())

    endpoint_config_template = endpoint_config_template.substitute(name=endpoint_name)
    with open(endpoint_config_target_file, "w") as w:
        w.write(endpoint_config_template)

    return endpoint_dir


def init_endpoint():
    """Setup funcx dirs and default endpoint config files

    TODO : Every mechanism that will update the config file, must be using a
    locking mechanism, ideally something like fcntl https://docs.python.org/3/library/fcntl.html
    to ensure that multiple endpoint invocations do not mangle the funcx config files
    or the lockfile module.
    """
    _ = FuncXClient()

    if os.path.exists(State.FUNCX_CONFIG_FILE):
        typer.confirm(
            "Are you sure you want to initialize this directory? "
            f"This will erase everything in {State.FUNCX_DIR}", abort=True
        )
        logger.info("Wiping all current configs in {}".format(State.FUNCX_DIR))
        backup_dir = State.FUNCX_DIR + ".bak"
        try:
            logger.debug(f"Removing old backups in {backup_dir}")
            shutil.rmtree(backup_dir)
        except OSError:
            pass
        os.renames(State.FUNCX_DIR, backup_dir)

    if os.path.exists(State.FUNCX_CONFIG_FILE):
        logger.debug("Config file exists at {}".format(State.FUNCX_CONFIG_FILE))
        return

    try:
        os.makedirs(State.FUNCX_DIR, exist_ok=True)
    except Exception as e:
        print("[FuncX] Caught exception during registration {}".format(e))

    shutil.copyfile(State.FUNCX_DEFAULT_CONFIG_TEMPLATE, State.FUNCX_CONFIG_FILE)
    init_endpoint_dir("default")


def register_with_hub(endpoint_uuid, endpoint_dir, address,
                      redis_host='funcx-redis.wtgh6h.0001.use1.cache.amazonaws.com'):
    """ This currently registers directly with the Forwarder micro service.

    Can be used as an example of how to make calls this it, while the main API
    is updated to do this calling on behalf of the endpoint in the second iteration.
    """
    print("Picking source as a mock site")
    sites = ['128.135.112.73', '140.221.69.24',
             '52.86.208.63', '129.114.63.99',
             '128.219.134.72', '134.79.129.79']
    ip_addr = random.choice(sites)
    try:
        r = requests.post(address + '/register',
                          json={'endpoint_id': endpoint_uuid,
                                'endpoint_addr': ip_addr,
                                'redis_address': redis_host})
    except requests.exceptions.ConnectionError:
        logger.critical("Unable to reach the funcX hub at {}".format(address))
        exit(-1)

    if r.status_code != 200:
        print(dir(r))
        print(r)
        raise RegistrationError(r.reason)

    with open(os.path.join(endpoint_dir, 'endpoint.json'), 'w+') as fp:
        json.dump(r.json(), fp)
        logger.debug("Registration info written to {}/endpoint.json".format(endpoint_dir))

    return r.json()


@app.command(name="configure", help="Configure an endpoint")
def configure_endpoint(
        name: str = typer.Argument("default", help="endpoint name", autocompletion=complete_endpoint_name),
        endpoint_config: str = typer.Option(None, "--endpoint-config", help="endpoint config file")
):
    """Configure an endpoint

    Drops a config.py template into the funcx configs directory.
    The template usually goes to ~/.funcx/<ENDPOINT_NAME>/config.py
    """
    endpoint_dir = os.path.join(State.FUNCX_DIR, name)
    new_config_file = os.path.join(endpoint_dir, FUNCX_CONFIG_FILE_NAME)

    if not os.path.exists(endpoint_dir):
        init_endpoint_dir(name, endpoint_config=endpoint_config)
        print('''A default profile has been create for <{}> at {}
Configure this file and try restarting with:
    $ funcx-endpoint start {}'''.format(name,
                                        new_config_file,
                                        name))
        return


@app.command(name="start", help="Start an endpoint by name")
def start_endpoint(
        name: str = typer.Argument("default", autocompletion=complete_endpoint_name),
        endpoint_uuid: str = typer.Option(None, help="The UUID for the endpoint to register with")
):
    """Start an endpoint

    This function will do:
    1. Connect to the broker service, and register itself
    2. Get connection info from broker service
    3. Start the interchange as a daemon


    |                      Broker service       |
    |               -----2----> Forwarder       |
    |    /register <-----3----+   ^             |
    +-----^-----------------------+-------------+
          |     |                 |
          1     4                 6
          |     v                 |
    +-----+-----+-----+           v
    |      Start      |---5---> Interchange
    |     Endpoint    |        daemon
    +-----------------+

    Parameters
    ----------
    name : str
    endpoint_uuid : str
    """

    funcx_client = FuncXClient()

    endpoint_dir = os.path.join(State.FUNCX_DIR, name)
    endpoint_json = os.path.join(endpoint_dir, 'endpoint.json')

    if not os.path.exists(endpoint_dir):
        print('''Endpoint {0} is not configured!
1. Please create a configuration template with:
   $ funcx-endpoint configure {0}
2. Update configuration
3. Start the endpoint.
        '''.format(name))
        return

    endpoint_config = SourceFileLoader('config',
                                       os.path.join(endpoint_dir, FUNCX_CONFIG_FILE_NAME)).load_module()

    funcx_client = FuncXClient(funcx_service_address=endpoint_config.config.funcx_service_address)

    # If pervious registration info exists, use that
    if os.path.exists(endpoint_json):
        with open(endpoint_json, 'r') as fp:
            logger.debug("Connection info loaded from prior registration record")
            reg_info = json.load(fp)
            endpoint_uuid = reg_info['endpoint_id']
    elif not endpoint_uuid:
        endpoint_uuid = str(uuid.uuid4())

    logger.info(f"Starting endpoint with uuid: {endpoint_uuid}")

    pid_path = os.path.join(endpoint_dir, 'daemon.pid')

    # if the pidfile exists, we should return early because we don't
    # want to attempt to create a new daemon when one is already
    # potentially running with the existing pidfile
    if check_pidfile(pid_path, endpoint_name=name, log=True)['exists']:
        return

    # Create a daemon context
    # If we are running a full detached daemon then we will send the output to
    # log files, otherwise we can piggy back on our stdout
    if endpoint_config.config.detach_endpoint:
        stdout = open(os.path.join(endpoint_dir, endpoint_config.config.stdout), 'w+')
        stderr = open(os.path.join(endpoint_dir, endpoint_config.config.stderr), 'w+')
    else:
        stdout = sys.stdout
        stderr = sys.stderr

    try:
        context = daemon.DaemonContext(working_directory=endpoint_dir,
                                       umask=0o002,
                                       # lockfile.FileLock(
                                       pidfile=daemon.pidfile.PIDLockFile(pid_path),
                                       stdout=stdout,
                                       stderr=stderr,
                                       detach_process=endpoint_config.config.detach_endpoint
                                       )
    except Exception as e:
        logger.critical("Caught exception while trying to setup endpoint context dirs")
        logger.critical("Exception : ", e)

    # TODO : we need to load the config ? maybe not. This needs testing
    endpoint_config = SourceFileLoader(
        'config',
        os.path.join(endpoint_dir, FUNCX_CONFIG_FILE_NAME)).load_module()

    with context:
        while True:
            # Register the endpoint
            logger.info("Registering endpoint")
            if State.FUNCX_CONFIG.get('broker_test', False) is True:
                logger.warning("**************** BROKER State.DEBUG MODE *******************")
                reg_info = register_with_hub(endpoint_uuid,
                                             endpoint_dir,
                                             State.FUNCX_CONFIG['broker_address'],
                                             State.FUNCX_CONFIG['redis_host'])
            else:
                reg_info = register_endpoint(funcx_client, name, endpoint_uuid, endpoint_dir)

            logger.info("Endpoint registered with UUID: {}".format(reg_info['endpoint_id']))

            # Configure the parameters for the interchange
            optionals = {}
            optionals['client_address'] = reg_info['address']
            optionals['client_ports'] = reg_info['client_ports'].split(',')
            if 'endpoint_address' in State.FUNCX_CONFIG:
                optionals['interchange_address'] = State.FUNCX_CONFIG['endpoint_address']

            optionals['logdir'] = endpoint_dir

            if State.DEBUG:
                optionals['logging_level'] = logging.DEBUG

            ic = Interchange(endpoint_config.config, endpoint_id=endpoint_uuid, **optionals)
            ic.start()
            ic.stop()

            logger.critical("Interchange terminated.")
            time.sleep(10)


# Avoid a race condition when starting the endpoint alongside the web service
@retry(delay=5, logger=logging.getLogger('funcx'))
def register_endpoint(funcx_client, endpoint_name, endpoint_uuid, endpoint_dir):
    """Register the endpoint and return the registration info.

    Parameters
    ----------

    funcx_client : FuncXClient
        The auth'd client to communicate with the funcX service

    endpoint_name : str
        The name to register the endpoint with

    endpoint_uuid : str
        The uuid to register the endpoint with

    endpoint_dir : str
        The directory to write endpoint registration info into.

    """
    logger.debug("Attempting registration")
    logger.debug(f"Trying with eid : {endpoint_uuid}")
    from funcx_endpoint.endpoint.version import VERSION as ENDPOINT_VERSION
    reg_info = funcx_client.register_endpoint(endpoint_name,
                                              endpoint_uuid,
                                              endpoint_version=ENDPOINT_VERSION)

    with open(os.path.join(endpoint_dir, 'endpoint.json'), 'w+') as fp:
        json.dump(reg_info, fp)
        logger.debug("Registration info written to {}/endpoint.json".format(endpoint_dir))

    return reg_info


@app.command(name="stop")
def stop_endpoint(name: str = typer.Argument("default", autocompletion=complete_endpoint_name)):
    """ Stops an endpoint using the pidfile

    """

    endpoint_dir = os.path.join(State.FUNCX_DIR, name)
    pid_file = os.path.join(endpoint_dir, "daemon.pid")

    if os.path.exists(pid_file):
        logger.debug(f"{name} has a daemon.pid file")
        pid = None
        with open(pid_file, 'r') as f:
            pid = int(f.read())
        # Attempt terminating
        try:
            logger.debug("Signalling process: {}".format(pid))
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.1)
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.1)
            # Wait to confirm that the pid file disappears
            if not os.path.exists(pid_file):
                logger.info("Endpoint <{}> is now stopped".format(name))

        except OSError:
            logger.warning("Endpoint {} could not be terminated".format(name))
            logger.warning("Attempting Endpoint {} cleanup".format(name))
            os.remove(pid_file)
            sys.exit(-1)
    else:
        logger.info("Endpoint <{}> is not active.".format(name))


@app.command(name="restart")
def restart_endpoint(name: str = typer.Argument("default", autocompletion=complete_endpoint_name)):
    """Restarts an endpoint"""
    stop_endpoint(name)
    start_endpoint(name)


@app.command(name="list")
def list_endpoints():
    """ List all available endpoints
    """
    table = tt.Texttable()

    headings = ['Endpoint Name', 'Status', 'Endpoint ID']
    table.header(headings)

    config_files = glob.glob('{}/*/config.py'.format(State.FUNCX_DIR))
    for config_file in config_files:
        endpoint_dir = os.path.dirname(config_file)
        endpoint_name = os.path.basename(endpoint_dir)
        status = 'Initialized'
        endpoint_id = None

        endpoint_json = os.path.join(endpoint_dir, 'endpoint.json')
        if os.path.exists(endpoint_json):
            with open(endpoint_json, 'r') as f:
                endpoint_info = json.load(f)
                endpoint_id = endpoint_info['endpoint_id']
            if check_pidfile(os.path.join(endpoint_dir, 'daemon.pid'))['active']:
                status = 'Active'
            else:
                status = 'Inactive'

        table.add_row([endpoint_name, status, endpoint_id])

    s = table.draw()
    print(s)


@app.command(name="delete")
def delete_endpoint(
        name: str = typer.Argument(..., autocompletion=complete_endpoint_name),
        autoconfirm: bool = typer.Option(False, "-y", help="Do not ask for confirmation to delete.")
):
    """Deletes an endpoint and its config."""
    if not autoconfirm:
        typer.confirm(f"Are you sure you want to delete the endpoint <{name}>?", abort=True)

    endpoint_dir = os.path.join(State.FUNCX_DIR, name)

    # If endpoint currently running, stop it.
    pid_file = os.path.join(endpoint_dir, "daemon.pid")
    active = os.path.exists(pid_file)
    if active:
        stop_endpoint(name)

    shutil.rmtree(endpoint_dir)


@app.callback()
def main(
        ctx: typer.Context,
        _: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
        debug: bool = typer.Option(False, "--debug", "-d"),
        config_dir: str = typer.Option(State.FUNCX_DIR, "--config_dir", "-c", help="override default config dir")
):
    # Note: no docstring here; the docstring for @app.callback is used as a help message for overall app.
    # Sets up global variables in the State wrapper (debug flag, config dir, default config file).
    # For commands other than `init`, we ensure the existence of the config directory and file.

    global logger
    funcx.set_stream_logger(level=logging.DEBUG if debug else logging.INFO)
    logger = logging.getLogger('funcx')
    logger.debug("Command: {}".format(ctx.invoked_subcommand))

    # Set global state variables, to avoid passing them around as arguments all the time
    State.DEBUG = debug
    State.FUNCX_DIR = config_dir
    State.FUNCX_CONFIG_FILE = os.path.join(State.FUNCX_DIR, FUNCX_CONFIG_FILE_NAME)

    # Otherwise, we ensure that configs exist
    if not os.path.exists(State.FUNCX_CONFIG_FILE):
        logger.info(f"No existing configuration found at {State.FUNCX_CONFIG_FILE}. Initializing...")
        init_endpoint()

    logger.debug("Loading config files from {}".format(State.FUNCX_DIR))

    funcx_config = SourceFileLoader('global_config', State.FUNCX_CONFIG_FILE).load_module()
    State.FUNCX_CONFIG = funcx_config.global_options


def cli_run():
    """Entry point for setuptools to point to"""
    app()


if __name__ == '__main__':
    app()

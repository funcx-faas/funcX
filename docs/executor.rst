Globus Compute Executor
=======================

The |Executor|_ class, a subclass of Python's |ConcurrentFuturesExecutor|_, is the
preferred approach to collecting results from the Globus Compute web services.  Over
polling (the historical approach) where the web service must be repeatedly
queried for the status of tasks and results eventually collected in bulk, the
|Executor|_ class instantiates an AMQPS connection that streams results
directly -- and immediately -- as they arrive at the server.  This is a far
more efficient paradigm, simultaneously in terms of bytes over the wire, time
spent waiting for results, and boilerplate code to check for results.

For most "simple" interactions with Globus Compute, this class will likely be the
quickest and easiest avenue to submit tasks and acquire results.  An
example interaction:

.. code-block:: python
    :caption: globus_compute_executor_basic_example.py

    from globus_compute_sdk import Executor

    def double(x):
        return x * 2

    tutorial_endpoint_id = '4b116d3c-1703-4f8f-9f6f-39921e5864df'
    with Executor(endpoint_id=tutorial_endpoint_id) as gce:
        fut = gce.submit(double, 7)

        print(fut.result())

This example is only a quick-reference, showing the basic mechanics of how to
use the |Executor|_ class and submitting a single task.  However, there
are a number of details to observe.  The first is that a |Executor|_
instance is associated with a specific endpoint.  We use the "well-known"
tutorial endpoint in this example, but that can point to any endpoint to which
you have access.

.. note::
    A friendly FYI: the tutorial endpoint is public -- available for any
    (authenticated) user.  You are welcome to use it, but please limit the size
    and number of functions you send to this endpoint.  It is hosted on a small
    VM with limited CPU and memory, intentionally underpowered.   Its primary
    intended purpose is for an introduction to the Globus Compute toolset.

    This endpoint has been made public by the Globus Compute team for tutorial
    use, but endpoints created by users can not be shared publicly.

Second, the waiting -- or "blocking" -- for a result is automatic.  The
|.submit()|_ call returns a |Future|_ immediately; the actual HTTP call to the
Globus Compute web-services will not have occurred yet, and neither will the task even
been executed (remotely), much less a result received.  The |.result()|_ call
blocks ("waits") until all of that has completed, and the result has been
received from the upstream services.

Third, |Executor|_ objects can be used as context managers (the ``with``
statement).  Underneath the hood, the |Executor|_ class uses threads to
implement the asynchronous interface -- a thread to coalesce and submit tasks,
and a thread to watch for incoming results.  The |Executor|_ logic cannot
determine when it will no longer receive tasks (i.e., no more |.submit()|_
calls) and so cannot prematurely shutdown.  Thus, it must be told, either
explicitly with a call to |.shutdown()|_, or implicitly when used as a context
manager.

Multiple Function Invocations
-----------------------------

Building on the asynchronous behavior of |.submit()|_, we can easily create
multiple tasks and wait for them all.  The simplest case is a for-loop over
submission and results.  As an example, consider the `Collatz conjecture`_,
alternatively known as the :math:`3n + 1` problem.  The conjecture is that
given a starting integer and two generation rules, the outcome sequence will
always end with 1.  The rules are:

- If :math:`N_i` is even, then :math:`N_{i+1} = N_i / 2`
- If :math:`N_i` is odd, then :math:`N_{i+1} = 3 N_i + 1`

To verify all of the sequences through 100, one brute-force approach is:

.. code-block:: python
    :caption: globus_compute_executor_collatz.py

    from globus_compute_sdk import Executor

    def generate_collatz_sequence(N: int, sequence_limit = 10_000):
        seq = [N]
        while N != 1 and len(seq) < sequence_limit:
            if N % 2:
                N = 3 * N + 1
            else:
                N //= 2  # okay because guaranteed integer result
            seq.append(N)
        return seq

    ep_id = "<your_endpoint_id>"

    generate_from = 1
    generate_through = 100
    futs, results, disproof_candidates = [], [], []
    with Executor(endpoint_id=ep_id) as gce:
        for n in range(generate_from, generate_through + 1):
            futs.append(gce.submit(generate_collatz_sequence, n))
        print("Tasks all submitted; waiting for results")

    # The futures were appended to the `futs` list in order, so one could wait
    # for each result in turn to get a submission-ordered set of results:
    for f in futs:
        r = f.result()
        results.append(r)
        if r[-1] != 1:
            # of course, given the conjecture, we don't expect this branch
            disproof_candidates.append(r[0])

    print(f"All sequences generated (from {generate_from} to {generate_through})")
    for res in results:
        print(res)

    if disproof_candidates:
        print("Possible conjecture disproving integers:", disproof_candidates)

Checking the Status of a Result
-------------------------------

Sometimes, it is desirable not to wait for a result, but just to check on the
status.  Futures make this simple with the |.done()|_ method:

.. code-block:: python

    ...
    future = gce.submit(generate_collatz_sequence, 1234567890)

    # Use the .done() method to check the status of the function without
    # blocking; this will return a Bool indicating whether the result is ready
    print("Status: ", future.done())


Handling Exceptions
-------------------

Assuming that a future will always have a result will lead to broken scripts.
Exceptions happen, whether from a condition the task function does not handle
or from an external execution error.  To robustly handle task exceptions, wrap
|.result()|_ calls in a ``try`` block.  The following code has updated the
sequence generator to throw an exception after ``sequence_limit`` steps rather
than summarily return, and the specific number chosen starts a sequence that
takes more than 100 steps to complete.

.. code-block:: python
    :caption: globus_compute_executor_handle_result_exceptions.py

    from globus_compute_sdk import Executor

    def generate_collatz_sequence(N: int, sequence_limit=100):
        seq = [N]
        while N != 1 and len(seq) < sequence_limit:
            if N % 2:
                N = 3 * N + 1
            else:
                N //= 2  # okay because guaranteed integer result
            seq.append(N)
        if N != 1:
            raise ValueError(f"Sequence not terminated in {sequence_limit} steps")
        return seq

    with Executor(endpoint_id=ep_id) as gce:
        future = gce.submit(generate_collatz_sequence, 1234567890)

    try:
        print(future.result())
    except Exception as exc:
        print(f"Oh no!  The task raised an exception: {exc})


Receiving Results Out of Order
------------------------------

So far, we've shown simple iteration through the list of Futures, but that's
not generally the most performant approach for overall workflow completion.
In the previous examples, a result may return early at the end of the list, but
the script will not recognize it until it "gets there," waiting in the meantime
for the other tasks to complete.  (Task functions are not guaranteed to be
scheduled in order, nor are they guaranteed to take the same amount of time to
finish.)  There are a number of ways to work with results as they arrive; this
example uses `concurrent.futures.as_completed`_:

.. code-block:: python
    :caption: globus_compute_executor_results_as_arrived.py

    import concurrent.futures

    def double(x):
        return f"{x} -> {x * 2}"

    def slow_double(x):
        import random, time
        time.sleep(x * random.random())
        return f"{x} -> {x * 2}"

    with Executor(endpoint_id=endpoint_id) as gce:
        futs = [gce.submit(double, i) for i in range(10)]

        # The futures were appended to the `futs` list in order, so one could
        # wait for each result in turn to get an ordered set:
        print("Results:", [f.result() for f in futs])

        # But often acting on the results *as they arrive* is more desirable
        # as results are NOT guaranteed to arrive in the order they were
        # submitted.
        #
        # NOTA BENE: handling results "as they arrive" must happen before the
        # executor is shutdown.  Since this executor was used in a `with`
        # statement, then to stream results, we must *stay* within the `with`
        # statement.  Otherwise, at the unindent, `.shutdown()` will be
        # implicitly invoked (with default arguments) and the script will not
        # continue until *all* of the futures complete.
        futs = [fx.submit(slow_double, i) for i in range(10, 20)]
        for f in concurrent.futures.as_completed(futs):
            print("Received:", f.result())

Reloading Tasks
---------------
Waiting for incoming results with the |Executor|_ requires an active
connection -- which is often at odds with closing a laptop clamshell (e.g.,
heading home for the weekend).  For longer running jobs like this, the
|Executor|_ offers the |.reload_tasks()|_ method.  This method will reach
out to the Globus Compute web-services to collect all of the tasks associated with the
|.task_group_id|_, create a list of associated futures, finish
(call |.set_result()|_) any previously finished tasks, and watch the unfinished
futures.  Consider the following (contrived) example:

.. code-block:: python
    :caption: globus_compute_executor_reload_tasks.py

    # execute initially as:
    # $ python globus_compute_executor_reload_tasks.py
    #  ... this Task Group ID: <TG_UUID_STR>
    #  ...
    # Then run with the Task Group ID as an argument:
    # $ python globus_compute_executor_reload_tasks.py <TG_UUID_STR>

    import os, signal, sys, time, typing as t
    from globus_compute_sdk import Executor
    from globus_compute_sdk.sdk.executor import ComputeFuture

    task_group_id = sys.argv[1] if len(sys.argv) > 1 else None

    def task_kernel(num):
        return f"your Globus Compute logic result, from task: {num}"

    ep_id = "<YOUR_ENDPOINT_UUID>"
    with Executor(endpoint_id=ep_id) as gce:
        futures: t.Iterable[ComputeFuture]
        if task_group_id:
            print(f"Reloading tasks from Task Group ID: {task_group_id}")
            gce.task_group_id = task_group_id
            futures = gce.reload_tasks()

        else:
            # Save the task_group_id somewhere.  Perhaps in a file, or less
            # robustly "as mere text" on your console:
            print(
                "New session; creating Globus Compute tasks; if this script dies, rehydrate"
                f" futures with this Task Group ID: {gce.task_group_id}"
            )
            num_tasks = 5
            futures = [gce.submit(task_kernel, i + 1) for i in range(num_tasks)]

            # Ensure all tasks have been sent upstream ...
            while gce.task_count_submitted < num_tasks:
                time.sleep(1)
                print(f"Tasks submitted upstream: {gce.task_count_submitted}")

            # ... before script death for [silly reason; did you lose power!?]
            bname = sys.argv[0]
            if sys.argv[0] != sys.orig_argv[0]:
                bname = f"{sys.orig_argv[0]} {bname}"

            print("Simulating unexpected process death!  Now reload the session")
            print("by rerunning this script with the task_group_id:\n")
            print(f"  {bname} {gce.task_group_id}\n")
            os.kill(os.getpid(), signal.SIGKILL)
            exit(1)  # In case KILL takes split-second to process

    # Get results:
    results, exceptions = [], []
    for f in futures:
        try:
            results.append(f.result(timeout=10))
        except Exception as exc:
            exceptions.append(exc)
    print("Results:\n ", "\n  ".join(results))

For a slightly more advanced usage, one could manually submit a batch of tasks
with the |Client|_, and wait for the results at a future time.  Submitting
the results might look like:

.. code-block:: python
    :caption: globus_compute_client_submit_batch.py

    from globus_compute_sdk import Client

    def expensive_task(task_arg):
        import time
        time.sleep(3600 * 24)  # 24 hours
        return "All done!"

    ep_id = "<endpoint_id>"
    gcc = Client()

    print(f"Task Group ID for later reloading: {gcc.session_task_group_id}")
    fn_id = gcc.register_function(expensive_task)
    batch = gcc.create_batch()
    for task_i in range(10):
        batch.add(fn_id, ep_id, args=(task_i,))
    gcc.batch_run(batch)

And ~24 hours later, could reload the tasks with the executor to continue
processing:

.. code-block:: python
    :caption: globus_compute_executor_reload_batch.py

    from globus_compute_sdk import Executor

    ep_id = "<endpoint_id>"
    tg_id = "Saved task group id from 'yesterday'"
    with Executor(endpoint_id=ep_id, task_group_id=tg_id) as gce:
        futures = gce.reload_tasks()
        for f in concurrent.futures.as_completed(futs):
            print("Received:", f.result())

.. include:: bash_functions.rst

AMQP Port
---------

As of v2.11.0, newly configured endpoints use AMQP over port 443 by default,
since firewall rules usually allow outbound HTTPS.

The port that the Executor uses to fetch task results remains the default AMQP
5671, but can be overridden via the ``amqp_port`` parameter during
instantiation.  This may be necessary in cases where outbound 5671 is also
unavailable in the task submission environment, such as some cloud VMs
like BinderHub:

.. code-block:: python

  >>> from globus_compute_sdk import Executor
  >>> gce = Executor(amqp_port=443)


.. |Client| replace:: ``Client``
.. _Client: reference/client.html
.. |Executor| replace:: ``Executor``
.. _Executor: reference/executor.html
.. |Future| replace:: ``Future``
.. _Future: https://docs.python.org/3/library/concurrent.futures.html#future-objects
.. |ConcurrentFuturesExecutor| replace:: ``concurrent.futures.Executor``
.. _ConcurrentFuturesExecutor: https://docs.python.org/3/library/concurrent.futures.html#executor-objects
.. |.shutdown()| replace:: ``.shutdown()``
.. _.shutdown(): reference/executor.html#globus_compute_sdk.Executor.shutdown
.. |.submit()| replace:: ``.submit()``
.. _.submit(): reference/executor.html#globus_compute_sdk.Executor.submit
.. |.result()| replace:: ``.result()``
.. _.result(): https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future.result
.. |.done()| replace:: ``.done()``
.. _.done(): https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future.done
.. |.set_result()| replace:: ``.set_result()``
.. _.set_result(): https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future.set_result
.. |.reload_tasks()| replace:: ``.reload_tasks()``
.. _.reload_tasks(): reference/executor.html#globus_compute_sdk.Executor.reload_tasks
.. |.task_group_id| replace:: ``.task_group_id``
.. _.task_group_id: reference/executor.html#globus_compute_sdk.Executor.task_group_id
.. _Collatz conjecture: https://en.wikipedia.org/wiki/Collatz_conjecture
.. _concurrent.futures.as_completed: https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.as_completed

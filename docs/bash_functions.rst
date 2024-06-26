Bash Functions
--------------

|BashFunction|_ is the solution to executing commands remotely using Globus Compute.
The |BashFunction|_ class allows for the specification of a command string, along with
runtime details such as a run directory, per-task sandboxing, walltime, etc and returns a
|BashResult|_. |BashResult|_ encapsulates the outputs from executing the command line string
by wrapping the returnode and snippets from the standard streams (`stdout` and `stderr`).

Here's a basic example that demonstrates specifying a |BashFunction|_ that is to be
formatted with a list of values at launch time.

.. code-block:: python

   from globus_compute_sdk import BashFunction, Executor

   ep_id = <SPECIFY_ENDPOINT_ID>
   # The cmd will be formatted with kwargs at invocation time
   bf = BashFunction("echo '{message}'")
   with Executor(endpoint_id=ep_id) as ex:

       for msg in ["hello", "hola", "bonjour"]:
           future = ex.submit(bf, message=msg)
           bash_result = future.result()  # BashFunctions return BashResults
           print(bash_result.stdout)

   # Executing the above prints:
   hello

   hola

   bonjour


The |BashResult|_ object captures outputs relevant to simplify debugging when execution
failures. By default, |BashFunction|_ captures 1000 lines of stdout and stderr, but this
can be changed via the `BashFunction(snippet_lines)` kwarg.

.. |BashFunction| replace:: ``BashFunction``
.. _BashFunction: reference/bash_function.html

.. |BashResult| replace:: ``BashResult``
.. _BashResult: reference/bash_function.html#globus_compute_sdk.sdk.bash_function.BashResult

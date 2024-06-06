import typing as t

from .bash_function import BashFunction


class MPIFunction(BashFunction):

    def __call__(
        self,
        stdout: str | None = None,
        stderr: str | None = None,
        rundir: str | None = None,
        resource_specification: dict[str, t.Any] = None,
        **kwargs,
    ):
        assert (
            resource_specification
        ), "MPIFunction requires kwarg:resource_specification"
        import copy

        self.stdout = stdout or self.stdout
        self.stderr = stderr or self.stderr
        self.rundir = rundir or self.rundir

        # Copy to avoid mutating the class vars
        format_args = copy.copy(vars(self))
        format_args.update(kwargs)
        cmd_line = "$PARSL_MPI_PREFIX " + self.cmd.format(**format_args)
        return self.execute_cmd_line(cmd_line)

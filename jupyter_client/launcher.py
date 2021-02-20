"""Utilities for launching kernels"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import asyncio
import os
import sys
from subprocess import Popen, PIPE
from typing import Any, Dict, Tuple

from traitlets.log import get_logger


def launch_kernel(cmd, stdin=None, stdout=None, stderr=None, env=None,
                  independent=False, cwd=None, **kw):
    """ Launches a localhost kernel, binding to the specified ports.

    Parameters
    ----------
    cmd : Popen list,
        A string of Python code that imports and executes a kernel entry point.

    stdin, stdout, stderr : optional (default None)
        Standards streams, as defined in subprocess.Popen.

    env: dict, optional
        Environment variables passed to the kernel

    independent : bool, optional (default False)
        If set, the kernel process is guaranteed to survive if this process
        dies. If not set, an effort is made to ensure that the kernel is killed
        when this process dies. Note that in this case it is still good practice
        to kill kernels manually before exiting.

    cwd : path, optional
        The working dir of the kernel process (default: cwd of this process).

    **kw: optional
        Additional arguments for Popen

    Returns
    -------

    subprocess.Popen instance for the kernel subprocess
    """

    kwargs, interrupt_event = prepare_process_args(stdin=stdin, stdout=stdout, stderr=stderr, env=env,
                  independent=independent, cwd=cwd, **kw)

    try:
        # Allow to use ~/ in the command or its arguments
        cmd = list(map(os.path.expanduser, cmd))

        proc = Popen(cmd, **kwargs)
    except Exception as exc:
        msg = (
            "Failed to run command:\n{}\n"
            "    PATH={!r}\n"
            "    with kwargs:\n{!r}\n"
        )
        # exclude environment variables,
        # which may contain access tokens and the like.
        without_env = {key:value for key, value in kwargs.items() if key != 'env'}
        msg = msg.format(cmd, env.get('PATH', os.defpath), without_env)
        get_logger().error(msg)
        raise

    finish_process_launch(proc, stdin, interrupt_event)

    return proc


async def async_launch_kernel(cmd, stdin=None, stdout=None, stderr=None, env=None,
                              independent=False, cwd=None, **kw):
    """ Launches a localhost kernel, binding to the specified ports using async subprocess.

    Parameters
    ----------
    cmd : list,
        A string of Python code that imports and executes a kernel entry point.

    stdin, stdout, stderr : optional (default None)
        Standards streams, as defined in subprocess.Popen.

    env: dict, optional
        Environment variables passed to the kernel

    independent : bool, optional (default False)
        If set, the kernel process is guaranteed to survive if this process
        dies. If not set, an effort is made to ensure that the kernel is killed
        when this process dies. Note that in this case it is still good practice
        to kill kernels manually before exiting.

    cwd : path, optional
        The working dir of the kernel process (default: cwd of this process).

    **kw: optional
        Additional arguments for Popen

    Returns
    -------

    asyncio.subprocess.Process instance for the kernel subprocess
    """

    kwargs, interrupt_event = prepare_process_args(stdin=stdin, stdout=stdout, stderr=stderr, env=env,
                  independent=independent, cwd=cwd, **kw)

    try:
        # Allow to use ~/ in the command or its arguments
        cmd = list(map(os.path.expanduser, cmd))

        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    except Exception as exc:
        msg = (
            "Failed to run command:\n{}\n"
            "    PATH={!r}\n"
            "    with kwargs:\n{!r}\n"
        )
        # exclude environment variables,
        # which may contain access tokens and the like.
        without_env = {key:value for key, value in kwargs.items() if key != 'env'}
        msg = msg.format(cmd, env.get('PATH', os.defpath), without_env)
        get_logger().error(msg)
        raise

    finish_process_launch(proc, stdin, interrupt_event)

    return proc


def prepare_process_args(stdin=None, stdout=None, stderr=None, env=None,
                         independent=False, cwd=None, **kw) -> Tuple[Dict[str, Any], Any]:

    # Popen will fail (sometimes with a deadlock) if stdin, stdout, and stderr
    # are invalid. Unfortunately, there is in general no way to detect whether
    # they are valid.  The following two blocks redirect them to (temporary)
    # pipes in certain important cases.

    # If this process has been backgrounded, our stdin is invalid. Since there
    # is no compelling reason for the kernel to inherit our stdin anyway, we'll
    # place this one safe and always redirect.
    redirect_in = True
    _stdin = PIPE if stdin is None else stdin

    # If this process in running on pythonw, we know that stdin, stdout, and
    # stderr are all invalid.
    redirect_out = sys.executable.endswith('pythonw.exe')
    if redirect_out:
        blackhole = open(os.devnull, 'w')
        _stdout = blackhole if stdout is None else stdout
        _stderr = blackhole if stderr is None else stderr
    else:
        _stdout, _stderr = stdout, stderr

    env = env if (env is not None) else os.environ.copy()

    kwargs = kw.copy()
    main_args = dict(
        stdin=_stdin,
        stdout=_stdout,
        stderr=_stderr,
        cwd=cwd,
        env=env,
    )
    kwargs.update(main_args)

    # Spawn a kernel.
    if sys.platform == 'win32':
        if cwd:
            kwargs['cwd'] = cwd

        from .win_interrupt import create_interrupt_event
        # Create a Win32 event for interrupting the kernel
        # and store it in an environment variable.
        interrupt_event = create_interrupt_event()
        env["JPY_INTERRUPT_EVENT"] = str(interrupt_event)
        # deprecated old env name:
        env["IPY_INTERRUPT_EVENT"] = env["JPY_INTERRUPT_EVENT"]

        try:
            from _winapi import DuplicateHandle, GetCurrentProcess, \
                DUPLICATE_SAME_ACCESS, CREATE_NEW_PROCESS_GROUP
        except:
            from _subprocess import DuplicateHandle, GetCurrentProcess, \
                DUPLICATE_SAME_ACCESS, CREATE_NEW_PROCESS_GROUP

        # create a handle on the parent to be inherited
        if independent:
            kwargs['creationflags'] = CREATE_NEW_PROCESS_GROUP
        else:
            pid = GetCurrentProcess()
            handle = DuplicateHandle(pid, pid, pid, 0,
                                     True, # Inheritable by new processes.
                                     DUPLICATE_SAME_ACCESS)
            env['JPY_PARENT_PID'] = str(int(handle))

        # Prevent creating new console window on pythonw
        if redirect_out:
            kwargs['creationflags'] = kwargs.setdefault('creationflags', 0) | 0x08000000 # CREATE_NO_WINDOW

        # Avoid closing the above parent and interrupt handles.
        # close_fds is True by default on Python >=3.7
        # or when no stream is captured on Python <3.7
        # (we always capture stdin, so this is already False by default on <3.7)
        kwargs['close_fds'] = False
    else:
        interrupt_event = None  # N/A
        # Create a new session.
        # This makes it easier to interrupt the kernel,
        # because we want to interrupt the whole process group.
        # We don't use setpgrp, which is known to cause problems for kernels starting
        # certain interactive subprocesses, such as bash -i.
        kwargs['start_new_session'] = True
        if not independent:
            env['JPY_PARENT_PID'] = str(os.getpid())

    return kwargs, interrupt_event


def finish_process_launch(subprocess, stdin, interrupt_event) -> None:
    """Finishes the process launch by patching the interrupt event (windows) and closing stdin. """

    if sys.platform == 'win32':
        # Attach the interrupt event to the Popen objet so it can be used later.
        subprocess.win32_interrupt_event = interrupt_event

    # Clean up pipes created to work around Popen bug.
    if stdin is None:
        subprocess.stdin.close()


__all__ = [
    'launch_kernel',
    'async_launch_kernel'
]

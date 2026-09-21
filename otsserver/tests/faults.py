# Copyright (C) 2026 ab21tor
#
# This file is part of the OpenTimestamps Server.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of the OpenTimestamps Server including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

"""Fault mechanisms the tests share (workflow two, the self-stamp;
workflow three, the watcher; workflow four, restore and migration): an
exception injected at the n-th call of a named function, a stop the code
under test does not handle, and a path made unreadable for the test's
duration. The third use decided each extraction: the helpers were
identical in shape at every site. The child processes each module pauses
and kills stay with their module, because the boundaries they wrap are
that tool's own functions and a shared runner would only carry the
wrapper script as a parameter. Every test says which fault model it uses;
this file only supplies the mechanism, and none of it is a power cut."""

import contextlib
import errno
import os
import stat
import unittest
from unittest import mock


class Stop(BaseException):
    """A stop the code under test does not handle: the process is gone at
    that call. (An OSError there would be a fault the code handles, a
    different case.) Not an Exception, so no `except Exception` on the way
    up mistakes it for a failure to log and carry on from."""


@contextlib.contextmanager
def fail_on_call(module_obj, name, n, exc=None):
    """Patch module_obj.name so that its n-th call raises (the run stops
    there); every other call goes through. Yields a record that says how
    many calls were made and whether the n-th one happened, so a sweep can
    tell a boundary it exercised from one it never reached."""
    real = getattr(module_obj, name)
    record = {'calls': 0, 'fired': False}

    def wrapper(*args, **kwargs):
        record['calls'] += 1
        if record['calls'] == n:
            record['fired'] = True
            raise exc or OSError(errno.EIO, 'injected stop at %s call %d' % (name, n))
        return real(*args, **kwargs)
    with mock.patch.object(module_obj, name, wrapper):
        yield record


def unreadable(path):
    """chmod 000; returns the function that restores the mode (a file the
    tool has since renamed is left alone). Skipped as root, who reads
    anything."""
    if os.geteuid() == 0:
        raise unittest.SkipTest('running as root: permission faults cannot be injected')
    mode = os.stat(path).st_mode

    def restore():
        try:
            os.chmod(path, stat.S_IMODE(mode))
        except FileNotFoundError:
            pass
    os.chmod(path, 0)
    return restore

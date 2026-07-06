"""CommandRunner tests. Run: python -m pytest, or python tests/test_commands.py"""

import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent.commands import CommandRunner  # noqa: E402


def test_methods_do_not_shadow_thread_internals():
    # Python 3.13's threading.Thread sets instance attributes such as ``_handle``
    # (a _thread._ThreadHandle). A CommandRunner method sharing that name gets
    # shadowed, so calling it raises "'_thread._ThreadHandle' object is not
    # callable" and silently kills the command runner every poll. Guard against
    # any custom method colliding with a Thread internal (``run`` is the one
    # intentional override).
    reserved = set(dir(threading.Thread()))
    reserved.discard("run")
    ours = {n for n in vars(CommandRunner) if not n.startswith("__") and callable(getattr(CommandRunner, n))}
    clash = ours & reserved
    assert not clash, f"CommandRunner methods shadow Thread internals: {sorted(clash)}"


def test_dispatch_is_callable_on_instance():
    # The bug manifested only on a live instance (where Thread.__init__ has set
    # self._handle). Ensure our dispatch entrypoint is still a bound method.
    runner = CommandRunner(storage=None)
    assert callable(runner._dispatch)
    assert not callable(getattr(runner, "_handle", None))  # that's the ThreadHandle, not us


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("All command tests passed.")

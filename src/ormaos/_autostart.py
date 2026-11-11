"""Start the recorder in this Python process.

``ormaos.pth`` imports this module at interpreter start, only when the
environment variable ``ORMAOS_DIR`` is set (``ormaos run`` sets it). Any
failure here is silent: the observed program must run unchanged.
"""

import os
import sys


def _start() -> None:
    directory = os.environ.get("ORMAOS_DIR")
    if not directory or not hasattr(sys, "monitoring"):
        return
    if not os.path.isdir(directory):
        return
    from ormaos._recorder import Recorder

    Recorder(directory).start()


try:
    _start()
except Exception:
    pass

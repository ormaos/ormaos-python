"""Allow ``python -m ormaos``."""

import sys

from ormaos.cli import main

if __name__ == "__main__":
    sys.exit(main())

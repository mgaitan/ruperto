"""Run the Ruperto CLI with `python -m ruperto`."""

from __future__ import annotations

import sys

from ruperto import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

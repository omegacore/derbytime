"""`python -m racemgr`: the same CLI as the `racemgr` script, for machines
where the venv's script directory isn't on PATH."""
import sys

from .cli import main

sys.exit(main())

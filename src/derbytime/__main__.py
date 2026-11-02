"""`python -m derbytime`: the same CLI as the `derbytime` script, for machines
where the venv's script directory isn't on PATH."""
import sys

from .cli import main

sys.exit(main())

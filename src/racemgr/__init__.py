"""Derby Time."""
from importlib.metadata import PackageNotFoundError, version


def _version() -> str:
    # The distribution was called "racemgr" before the rename; an editable
    # install made then keeps that name until it is reinstalled.
    for dist in ("derbytime", "racemgr"):
        try:
            return version(dist)
        except PackageNotFoundError:
            pass
    return "0.0.0+unknown"  # running from a source tree that was never installed


__version__ = _version()

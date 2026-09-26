"""Frankfurt-inspired ATC research benchmark (not an operational flight simulator)."""

__version__ = "0.3.0"

from .environment import AirTrafficEnv

__all__ = ["AirTrafficEnv", "__version__"]

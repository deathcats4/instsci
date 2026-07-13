"""InstSci - academic paper fetcher with institutional access support."""

from .config import Config
from .models import FetchResult, NextAction, Paper

__all__ = ["PaperFetcher", "Paper", "FetchResult", "NextAction", "Config"]
__version__ = "0.2.0a1"


def __getattr__(name: str):
    if name == "PaperFetcher":
        from .fetcher import PaperFetcher

        return PaperFetcher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

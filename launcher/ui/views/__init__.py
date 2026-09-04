"""The three gallery compositions.

Each view is a pure renderer plus a navigation rule.  They share the manifest,
the selection index and every component in :mod:`launcher.ui.components`, but
they lay the screen out very differently on purpose.
"""

from __future__ import annotations

from .base import SUMMARY_BUCKETS, GalleryView, VIEWS, view_for
from .carousel import CarouselView
from .coverflow import CoverFlowView
from .grid import GridView

__all__ = [
    "GalleryView",
    "GridView",
    "CarouselView",
    "CoverFlowView",
    "VIEWS",
    "view_for",
    "SUMMARY_BUCKETS",
]

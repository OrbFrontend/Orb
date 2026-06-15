"""Vertical feature slices — each feature is a self-contained folder.

A slice may import **downward** (``core``, ``inference``, ``analysis``,
``database``) but never from another slice, ``pipeline/``, ``workflows/``, or
``api/``. Each slice follows the Standard Slice Shape: a facade ``__init__.py``,
optional ``contracts.py``, pure ``<logic>.py``, and ``<integration>.py`` wiring.
"""

"""Prose-quality detectors — pure functions over text + database.models shapes.

Each detector is independently testable and depends only on its siblings here
(``text_segmentation``) and ``database.models`` (downward). The consolidated
runner lives one level up in ``analysis.audit``; the public result types are
re-exported through the ``analysis`` facade.
"""

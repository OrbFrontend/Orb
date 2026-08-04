"""Shared text primitives — the foundation layer of the analysis package.

Text operations that everything else in ``analysis`` builds on. Generic
sentence/quotation policy is imported from ``core``; analysis-specific prose
blocks remain here. Two modules:

- ``lexical`` — word-level: tokenizing, normalizing, n-grams, token-sequence
  comparison, and the stopword/content-word floor.
- ``text_segmentation`` — paragraph/sentence/dialogue splitting and block
  extraction.

They have no dependencies on detectors or rewriters, so they sit below both.
"""

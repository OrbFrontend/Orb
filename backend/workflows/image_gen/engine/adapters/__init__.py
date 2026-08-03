"""Image backend adapters.

Deliberately re-exports nothing: every importer names the adapter module it
wants, and eagerly importing them here would defeat the router's per-adapter
ImportError guard by pulling each one's dependencies in through `adapters.base`.
"""

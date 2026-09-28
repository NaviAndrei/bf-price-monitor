"""Offline anomaly-detection pilot (T-29, #36).

Offline only: nothing in this package is imported by scrape.py, analyze.py,
notify.py or run_loop.py, and it can't change an alert decision or a
notification. `config`, `dataset` and `metrics` are pure Python; `model`
needs the optional `anomaly` extra (scikit-learn, ruptures).
"""

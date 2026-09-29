"""Kubernetes stress test: node load test with temperature monitoring."""
import logging

__version__ = "1.15.0"

# without a configured debug log, log messages are silently discarded
logging.getLogger(__name__).addHandler(logging.NullHandler())

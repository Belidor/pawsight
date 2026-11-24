"""Tests. Run from the project root:

    .venv/bin/python -m unittest discover -s tests -t .

The NullHandler silences warning output: with no handler at all, logging
would print them to stderr via lastResort. Propagate is NOT turned off;
otherwise the secret-leak test would not see records that reach the root
logger, and that is exactly where they have to be caught.
"""
import logging

logging.getLogger("pawsight").addHandler(logging.NullHandler())

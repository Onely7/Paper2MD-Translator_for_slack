"""
The tests (``python -m unittest discover -s tests -t .``).

Everything the tests and the code under test write to the temporary directory
goes to one directory per run (``p2m-tests-*``), which is removed when the run
ends; the commands the tests run get it as ``TMPDIR``.
"""

import atexit
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="p2m-tests-")
tempfile.tempdir = _ROOT
os.environ["TMPDIR"] = _ROOT
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)

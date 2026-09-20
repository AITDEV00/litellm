"""Memray profiling wrapper for the LiteLLM gateway.

memray run needs a real script file; `litellm` is a console script
(pyproject.toml -> litellm:run_server). This wrapper calls it with the
original CLI args. Shipped in the profiler image at /app/docker/memray_wrapper.py.
"""
import sys

sys.argv[0] = "litellm"

from litellm import run_server  # noqa: E402

run_server()

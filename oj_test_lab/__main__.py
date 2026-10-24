import sys

from .cli import main

# Piped/redirected output on Windows defaults to a legacy code page that cannot print Vietnamese.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.exit(main())

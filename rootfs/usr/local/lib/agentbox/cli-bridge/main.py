#!/usr/bin/python3 -I
"""cli-bridge entry point (run with python3 -I: no PYTHON* variables, no user site).

  main.py serve              the HTTP service (s6 service cli-bridge)
  main.py status             adapters, logins, CLI versions, models
  main.py login ADAPTER      log the bridge's own copy of a CLI in
  main.py check [MODEL ...]  one real call per model through the running service
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli_bridge import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main(sys.argv[1:]))

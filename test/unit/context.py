"""Puts cli_bridge on sys.path: the checkout's copy, or the image's."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STUBS = os.path.normpath(os.path.join(HERE, "..", "stubs"))
for candidate in (
    os.path.join(HERE, "..", "..", "rootfs", "usr", "local", "lib", "agentbox", "cli-bridge"),
    "/usr/local/lib/agentbox/cli-bridge",
):
    if os.path.isdir(os.path.join(candidate, "cli_bridge")):
        sys.path.insert(0, os.path.normpath(candidate))
        break

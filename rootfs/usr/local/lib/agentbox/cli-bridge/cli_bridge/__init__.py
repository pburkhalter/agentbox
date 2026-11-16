"""cli-bridge: an OpenAI-compatible HTTP front for the coding CLIs in agentbox.

Each request runs one Claude Code or Codex process as a plain language model
(no tools, no MCP, no project state) and translates its event stream into a
chat completion. The bridge keeps no conversation state.
"""

__version__ = "1.0.0"

"""AI Messaging — local MCP server (the client layer).

Runs next to each agent and talks to the central server inside the
tailnet. Holds the client-side state (identity, assigned ID, followed
chats, read checkpoints) in a local user_config file. See docs/design.md.
"""

__version__ = "0.3.0"

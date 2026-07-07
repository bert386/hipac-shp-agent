"""HiPAC-SHP Raspberry Pi agent.

Scans the local network for receivers, logs into each over SSH, captures the
`receiver_cli` TUI screen, parses the Receiver + Node properties, stores the
results locally and pushes them to the central Laravel server.
"""

__version__ = "0.8.0"

"""herdr-fleet: start and manage a team of named coding agents from a
per-repository fleet file (``.herdr/fleet.toml``).

Modules, by responsibility (``docs/design.md`` section 9; AGENTS.md has a map):

- ``cli``: argparse, dispatch, output, exit codes.
- ``ops``: the operations (up, restart, swap, kickoff, down, ui, install-cli)
  and their refusals; ``bringup``: workspace resolution, classifying and
  starting seats; ``placement``: where new panes go (chain or grid);
  ``layout``: the pure grid plan; ``tile``: re-tile live seats with pane moves;
  ``checks``: read-only drift, status, doctor.
- ``seats``: the per-seat lifecycle against herdr (shell check, start with
  retry, the only prompt path, the quit procedure).
- ``herdr``: the only module that executes the herdr binary.
- ``fleetfile`` / ``state``: the two files on disk; ``kickoff``: templates.
- ``context``: per-invocation context and preconditions; ``menu``: the popup;
  ``notify``: ``herdr notification show``.
- ``messages``: ``send``, ``ack``, the message log and owed replies;
  ``contacts``: peers the team messages but never manages; ``statenote``:
  ``state`` sections; ``permissions``: the advisory Claude Code permission
  check in ``doctor``.
- ``worktrees``: the read-only ``worktrees`` listing; ``gitinfo``: read-only git
  helpers (also the doctor's ``fleet file vs git`` check); ``context_size``: the
  transcript reader behind ``status --context`` (usage numbers only).
- ``scaffold``: ``init``, which writes a starter fleet file (no herdr calls).
"""

import os

__version__ = "0.5.0"
PLUGIN_ID = "yarrasys.fleet"
MIN_HERDR_VERSION = (0, 9, 4)
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

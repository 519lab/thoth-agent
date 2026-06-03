"""Substrate CLI subcommands. Wired into Thoth's argparse tree by
``thoth_cli/main.py`` via :func:`substrate.cli.inspect.add_subparser`.

Phase A surface is a single debug command::

    thoth substrate
    thoth substrate streams
    thoth substrate slices --stream NAME --limit 20
    thoth substrate pending
    thoth substrate profiles
"""

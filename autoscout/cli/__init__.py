"""AutoScout command-line layer: one module per command group.

``autoscout.__main__`` builds the top-level parser, asks each group to
register its subcommands, and dispatches to the chosen command's handler.
"""

"""Small argparse helpers shared by governance CLIs.

The common failure this module exists to absorb is agent-written commands such
as ``task-record status --format json``. Plain argparse treats root options as
belonging before the subcommand, while humans and AI agents often put output or
state options next to the action they are taking.

Do not replace this with ``parse_intermixed_args()`` for subcommand CLIs:
Python documents intermixed parsing as incompatible with subparsers. A Click or
Typer migration would also be a broad command-framework change for a narrow
compatibility issue. The scoped fix is to register selected root options on
both the parent parser and each subparser.

When adding a root option copy to a subparser, always suppress its default. A
normal subparser default would silently overwrite the parent value when the user
passed the option in the old, still-supported order. Do not use this helper for
commands where arbitrary argv payloads are parsed with argparse.REMAINDER, such
as shell adapters; there, option order may be part of the payload command.
Also avoid it when a command has the same option name with different choices or
semantics, for example a formatter that also supports ``markdown``.

Relevant upstream notes:
- https://docs.python.org/3/library/argparse.html#intermixed-parsing
- https://click.palletsprojects.com/en/stable/commands-and-groups/
- https://typer.tiangolo.com/tutorial/subcommands/
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable


COMMON_GLOBAL_ARG_NAMES = ("root", "db", "format")


def suppressible_default(value: object, suppress: bool) -> object:
    return argparse.SUPPRESS if suppress else value


def add_root_arg(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    parser.add_argument(
        "--root",
        default=suppressible_default(".", suppress_default),
        help="Repository root. Default: current directory.",
    )


def add_db_arg(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    parser.add_argument(
        "--db",
        default=suppressible_default(None, suppress_default),
        help="SQLite database path. Default: <ai-client-project>/state/aicg.db.",
    )


def add_format_arg(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default=suppressible_default("text", suppress_default),
    )


def add_common_global_args(
    parser: argparse.ArgumentParser,
    *,
    names: Iterable[str] = COMMON_GLOBAL_ARG_NAMES,
    suppress_default: bool = False,
) -> None:
    """Add shared root options.

    Use ``suppress_default=True`` only for duplicate registrations on
    subparsers, so old-order parent values still survive parsing. Only include
    names whose defaults, choices, and meanings are identical at both parser
    levels.
    """
    for name in names:
        if name == "root":
            add_root_arg(parser, suppress_default=suppress_default)
        elif name == "db":
            add_db_arg(parser, suppress_default=suppress_default)
        elif name == "format":
            add_format_arg(parser, suppress_default=suppress_default)
        else:
            raise ValueError(f"unknown common argparse global: {name}")

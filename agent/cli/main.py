#!/usr/bin/env python3
"""
Main ThoughtMachine CLI entry point.

Provides unified command-line interface for all ThoughtMachine operations.
"""

import argparse
import sys

from agent.cli.rag_commands import add_index_codebase_subparser


def main() -> int:
    """Main entry point for ThoughtMachine CLI."""
    parser = argparse.ArgumentParser(
        prog="thoughtmachine",
        description="ThoughtMachine - AI agent for code understanding and automation",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands"
    )

    # Add subcommands from modules
    add_index_codebase_subparser(subparsers)
    # Future: add other subcommands here

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
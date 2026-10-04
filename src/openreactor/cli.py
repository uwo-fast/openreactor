from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from openreactor import __version__
from openreactor.config import ConfigError, load_config


def check_config(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except OSError as e:
        print(f"error: cannot read {args.config}: {e.strerror}", file=sys.stderr)
        return 1
    except ConfigError as e:
        for problem in e.problems:
            print(f"error: {problem}", file=sys.stderr)
        print(f"{args.config}: {len(e.problems)} problem(s)", file=sys.stderr)
        return 1

    channels = sum(max(len(d.channels), 1) for d in config.devices)
    print(f"{args.config}: OK, {len(config.devices)} device(s), {channels} channel(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openreactor", description="Supervise a benchtop lab reactor."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    check = commands.add_parser("check-config", help="validate a config file and exit")
    check.add_argument("config", help="path to the TOML config file")
    check.set_defaults(func=check_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

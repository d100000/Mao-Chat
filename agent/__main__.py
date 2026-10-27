"""Unified CLI: database foundation and evidence-first runtime."""
from __future__ import annotations

import sys


DB_COMMANDS = {"init-db", "ingest", "index", "search", "trace-page", "vector-index", "vector-build", "vector-search", "relations-import", "relations-review"}
RUNTIME_COMMANDS = {"ask", "trace-quote", "trace-event", "web-search", "config", "doctor"}


def _move_config_flag(argv):
    """Accept ``--config`` before or after a subcommand.

    ``argparse`` only recognizes the global option before the subparser.  The
    unified CLI historically routed ``search --config ...`` to runtime and
    then failed parsing it, so normalize both forms before dispatch.
    """
    args = list(argv)
    for index, token in enumerate(args):
        if token == "--config" and index + 1 < len(args):
            value = args[index + 1]
            del args[index:index + 2]
            return ["--config", value, *args]
        if token.startswith("--config="):
            value = token.split("=", 1)[1]
            del args[index]
            return ["--config", value, *args]
    return args


def main(argv=None):
    argv = _move_config_flag(list(sys.argv[1:] if argv is None else argv))
    # A leading --config belongs to runtime. DB commands accept --db.
    command = next((x for x in argv if x in DB_COMMANDS or x in RUNTIME_COMMANDS), None)
    # `search` exists in both CLIs. A config flag means the user requested the
    # runtime Agent search (hybrid/date-aware); otherwise keep the raw DB tool.
    if command == "search" and "--config" in argv:
        from .runtime_cli import main as runtime_main
        return runtime_main(argv)
    if command in DB_COMMANDS:
        from .cli import main as db_main
        return db_main(argv)
    if command in RUNTIME_COMMANDS:
        from .runtime_cli import main as runtime_main
        return runtime_main(argv)
    print("可用命令：init-db ingest index vector-index vector-build vector-search relations-import relations-review search trace-page ask trace-quote trace-event web-search config doctor")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

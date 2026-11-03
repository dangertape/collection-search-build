"""Entry point.

    collection-search            start the app (window + tray + MCP server)
    collection-search --mcp      stdio shim for Claude Desktop
    collection-search cli ...    command-line harness (index, search, eval, status)
"""

import sys


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--mcp"]:
        from .shim import main as shim_main  # stdlib only: starts fast, no Qt

        return shim_main()
    if args[:1] == ["cli"]:
        from .cli import main as cli_main

        return cli_main(args[1:])
    from .ui.app import main as app_main

    return app_main(args)


if __name__ == "__main__":
    raise SystemExit(main())

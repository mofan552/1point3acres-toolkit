"""Entry points of the installed package: `pip install 1point3acres-toolkit` or `uvx 1point3acres-toolkit`.

A checkout runs mcp_server.py and cli.py as scripts, so the flat modules import each other by bare name. The wheel
ships this directory as one package; these functions restore the script layout before importing anything else, so
an installed copy runs exactly the code the checkout runs."""
import sys
from pathlib import Path


def _script_layout():
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    _script_layout()
    import mcp_server
    mcp_server.main()


def cli():
    _script_layout()
    import cli as command_line
    command_line.run()


if __name__ == '__main__':
    main()

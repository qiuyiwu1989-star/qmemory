import sys

from qmemory.cli import main as cli_main
from qmemory.desktop import main as desktop_app_main
from qmemory.mcp_server import run as mcp_run


if __name__ == "__main__":
    if "--mcp" in sys.argv:
        mcp_run()
    elif len(sys.argv) > 1 and "--smoke-test" not in sys.argv:
        raise SystemExit(cli_main(sys.argv[1:]))
    else:
        raise SystemExit(desktop_app_main())

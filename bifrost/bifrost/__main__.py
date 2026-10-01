"""`bifrost` launches the GUI; `selftest [--live]` checks the stack; `cd info|rip` reads or rips a disc."""

import sys


def run() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        from . import selftest
        return selftest.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "cd":
        from . import cdcli
        return cdcli.main(sys.argv[2:])
    from . import app
    return app.main()


if __name__ == "__main__":
    sys.exit(run())

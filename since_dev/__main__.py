import sys

from .cli import run
from .realio import RealIo


def main():
    for stream in (sys.stdout, sys.stderr):
        # A Windows console in a legacy code page would fail on a title outside it; replace what it cannot show.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        code = run(sys.argv[1:], RealIo())
    except KeyboardInterrupt:
        code = 130
    except Exception as error:  # noqa: BLE001 - the last line of defense: say what happened in one line
        sys.stderr.write("since: %s\n" % error)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()

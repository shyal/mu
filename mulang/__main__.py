"""mu [--fmt] FILE|-, or --version. Also python -m mulang."""

import sys

from . import VERSION, MuError, fmt, profile, transpile


def main(args):
    if args == ["--version"]:
        sys.exit(print(f"mu {VERSION}"))
    if args in (["-h"], ["--help"]):
        sys.exit(print(__doc__))
    run = transpile
    if args[:1] == ["--fmt"]:
        run, args = fmt, args[1:]
    if len(args) != 1:
        sys.exit(__doc__)
    try:
        src = sys.stdin.read() if args[0] == "-" else open(args[0]).read()
        if run is transpile:
            profile()
        sys.stdout.write(run(src))
    except MuError as err:
        sys.exit(f"{args[0]}: {err}")


def cli():
    main(sys.argv[1:])


if __name__ == "__main__":
    cli()

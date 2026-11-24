import sys

from shellpilot_cli import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["--interactive"]))

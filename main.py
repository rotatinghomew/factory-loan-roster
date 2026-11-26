"""Module entry point alias: ``python main.py``."""

from shared_labor.server import create_server, main, route  # noqa: F401

if __name__ == "__main__":
    main()

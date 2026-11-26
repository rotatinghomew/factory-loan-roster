"""Top-level alias for environments that expect ``python server.py``."""

from shared_labor.server import create_handler, create_server, main, route  # noqa: F401

if __name__ == "__main__":
    main()

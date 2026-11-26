"""Convenience entry point: ``python app.py --port 8080``."""

from shared_labor.server import create_server, main

app = None  # Created by the standard-library server factory when needed.


def create_app(*args, **kwargs):
    return create_server(*args, **kwargs)


if __name__ == "__main__":
    main()

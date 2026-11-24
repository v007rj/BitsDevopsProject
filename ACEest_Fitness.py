"""Assignment entry point; the Assignment 1 implementation remains in app.py."""

import os

from app import app, create_app  # noqa: F401

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

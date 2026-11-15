"""Vercel entry point: every request goes to the Flask app in web/app.py (see vercel.json)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"))

from app import app  # noqa: E402,F401

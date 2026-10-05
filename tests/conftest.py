"""Biến môi trường cho test: không cần GitHub App thật, và dùng SQLite trong bộ nhớ thay Postgres."""
import os

os.environ.setdefault("GITHUB_APP_ID", "1")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "x")
os.environ["DATABASE_URL"] = "sqlite://"

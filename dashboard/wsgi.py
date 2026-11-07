"""Entry point: flask --app dashboard.wsgi:app run (see infrastructure/dashboard-stack.yaml)."""

from dashboard import create_app

app = create_app()

"""gunicorn entry point: gunicorn dashboard.wsgi:app"""

from dashboard import create_app

app = create_app()

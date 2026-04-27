# Ре-экспорт WSGI-приложения из passenger_wsgi.py.
# Sprinthost uWSGI часто ищет именно wsgi.py / app.py.
from passenger_wsgi import application  # noqa: F401

app = application

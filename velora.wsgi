"""WSGI entrypoint для uWSGI на Sprinthost (.wsgi handler)."""
import os
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Подключаем site-packages из локального venv
VENV = os.path.join(HERE, "venv")
_lib = os.path.join(VENV, "lib")
if os.path.isdir(_lib):
    for _d in os.listdir(_lib):
        if _d.startswith("python"):
            _sp = os.path.join(_lib, _d, "site-packages")
            if os.path.isdir(_sp):
                site.addsitedir(_sp)
            break

from velora.web.server import app as application  # noqa: E402, F401

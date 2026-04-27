"""WSGI entrypoint для uWSGI на Sprinthost."""
import os
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Сбрасываем кеш velora-модулей при каждой перезагрузке site.wsgi
# (mod_wsgi embedded mode кеширует sys.modules — без этого Config
#  хранит пустые значения из старого импорта до рестарта Apache).
for _k in list(sys.modules.keys()):
    if _k.startswith("velora"):
        del sys.modules[_k]

VENV = os.path.join(HERE, "venv")
_lib = os.path.join(VENV, "lib")
if os.path.isdir(_lib):
    for _d in os.listdir(_lib):
        if _d.startswith("python"):
            _sp = os.path.join(_lib, _d, "site-packages")
            if os.path.isdir(_sp):
                site.addsitedir(_sp)
            break

from velora.web.server import app as _app  # noqa: E402


def application(environ, start_response):
    """Прячем `/site.wsgi` из URL и нормализуем PATH_INFO для Flask."""
    script = environ.get("SCRIPT_NAME", "")
    path = environ.get("PATH_INFO", "")
    if script.endswith("/site.wsgi"):
        environ["SCRIPT_NAME"] = script[: -len("/site.wsgi")]
    if "/site.wsgi" in path:
        path = path.replace("/site.wsgi", "", 1)
    if not path:
        path = "/"
    environ["PATH_INFO"] = path
    return _app(environ, start_response)

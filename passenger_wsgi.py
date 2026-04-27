"""WSGI entrypoint для Sprinthost (Passenger / uWSGI)."""
import os
import site
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Подключаем site-packages из локального venv (нужно для uWSGI Sprinthost'а,
# который запускает сам интерпретатор и не активирует venv).
VENV = os.path.join(HERE, "venv")
if os.path.isdir(os.path.join(VENV, "lib")):
    for _d in os.listdir(os.path.join(VENV, "lib")):
        if _d.startswith("python"):
            _sp = os.path.join(VENV, "lib", _d, "site-packages")
            if os.path.isdir(_sp):
                site.addsitedir(_sp)
            break

# Для Passenger — переключаемся на venv'овый python (для uWSGI это no-op).
_INTERP = os.path.join(VENV, "bin", "python")
if (
    os.path.exists(_INTERP)
    and sys.executable != _INTERP
    and os.environ.get("VELORA_VENV_OK") != "1"
):
    os.environ["VELORA_VENV_OK"] = "1"
    try:
        os.execl(_INTERP, _INTERP, *sys.argv)
    except OSError:
        pass  # под uWSGI execl недоступен — продолжаем с system python + addsitedir

from velora.web.server import app  # noqa: E402

# Sprinthost uWSGI обычно ищет `application`, Passenger тоже умеет.
application = app

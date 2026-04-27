"""Запуск веб-версии Velora Sound."""
from velora.web.server import run_web

if __name__ == "__main__":
    print("Velora Sound web → http://127.0.0.1:5000")
    run_web(host="127.0.0.1", port=5000, debug=False)
        
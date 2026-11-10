"""Entrypoint: ``python -m keepsake``. Web server plus the serial job worker.

The process never exits for want of a mount: a missing source or iCloud folder is
reported on the Folders page ("unavailable") and retried, because a container
that crash-loops hides the real problem behind restart noise.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from typing import List, Optional

from keepsake import __version__
from keepsake.auth import reset_password_command, stored_password_exists
from keepsake.config import Config, ConfigError
from keepsake.diagnostics import install_ring
from keepsake.server import serve
from keepsake.service import Service

logger = logging.getLogger("keepsake")
MIN_PASSWORD = 10


def worker(svc: Service, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            ran = svc.run_next() or svc.run_due_check()
            svc.housekeeping()
        except Exception:  # noqa: BLE001 - the loop must outlive any one bug
            logger.exception("worker error")
            ran = False
        if not ran:
            svc.wake.wait(timeout=30)
            svc.wake.clear()


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in ("-V", "--version"):
        print(f"keepsake-for-icloud {__version__}")
        return 0
    if argv and argv[0] == "reset-password":
        return reset_password_command(argv[1:])
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    install_ring()  # the last lines of the log, for the diagnostics download
    try:
        cfg = Config.from_env()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        cfg = cfg.with_settings()
        stored = stored_password_exists(cfg.data_dir)  # set in the page or by reset-password
        if cfg.needs_password and not stored and len(cfg.password) < MIN_PASSWORD:
            raise ConfigError(
                f"refusing to start: Keepsake is set to listen on {cfg.host}, "
                f"which other devices can reach, so it needs a password of at "
                f"least {MIN_PASSWORD} characters (KEEPSAKE_PASSWORD or "
                f"KEEPSAKE_PASSWORD_FILE), or set KEEPSAKE_HOST=127.0.0.1")
        svc = Service(cfg)
        httpd = serve(svc)
    except (ConfigError, OSError) as exc:
        logger.error("%s", exc)
        return 2
    stop = threading.Event()
    threading.Thread(target=worker, args=(svc, stop), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
        target=httpd.shutdown, daemon=True).start())
    logger.info("Keepsake %s up on http://%s:%d, roots: %s, password: %s",
                __version__, cfg.host, cfg.port, ",".join(sorted(cfg.roots)),
                "required" if cfg.password or stored else "none (loopback only)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    if svc.status_publisher:
        svc.status_publisher.close()
    httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

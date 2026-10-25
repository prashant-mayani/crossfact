"""Production entrypoint: `python -m app.server`.

Kubernetes sends SIGTERM when a pod is replaced. Plain uvicorn would stop at
once and kill the background threads running consensus jobs. Here the first
SIGTERM instead:
  1. flips the app to draining: /ask answers 503 "restarting", /readyz fails,
     but page loads and the UI's polling keep working;
  2. waits for in-flight runs to finish (at most server.drain_timeout_s, which
     must stay below the pod's terminationGracePeriodSeconds);
  3. then lets uvicorn shut down normally.
A second SIGTERM or SIGINT skips the wait.
"""
from __future__ import annotations

import logging
import signal
import threading

import uvicorn

from .config import cfg
from .lifecycle import LIFECYCLE

log = logging.getLogger("crossfact.server")


class DrainingServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, drain_timeout_s: float):
        super().__init__(config)
        self.drain_timeout_s = drain_timeout_s
        self._drain_started = False

    def handle_exit(self, sig, frame) -> None:
        if self._drain_started or sig == signal.SIGINT:
            return super().handle_exit(sig, frame)
        self._drain_started = True
        log.warning("received %s: draining up to %.0fs before shutdown", signal.Signals(sig).name,
                    self.drain_timeout_s)

        def finish() -> None:
            LIFECYCLE.drain(self.drain_timeout_s)
            uvicorn.Server.handle_exit(self, sig, frame)

        threading.Thread(target=finish, name="drain", daemon=True).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server_cfg = cfg()["server"]
    config = uvicorn.Config("app.main:app", host=server_cfg["host"], port=int(server_cfg["port"]),
                            proxy_headers=True, forwarded_allow_ips="*", log_level="info",
                            timeout_graceful_shutdown=20)
    DrainingServer(config, float(server_cfg["drain_timeout_s"])).run()


if __name__ == "__main__":
    main()

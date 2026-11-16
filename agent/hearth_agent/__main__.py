"""Run the agent: `python -m hearth_agent`."""

import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(level=os.environ.get("HEARTH_LOG_LEVEL", "INFO"))
    uvicorn.run(
        create_app(),
        host=os.environ.get("HEARTH_AGENT_HOST", "0.0.0.0"),
        port=int(os.environ.get("HEARTH_AGENT_PORT", "8080")),
        ssl_certfile=os.environ.get("HEARTH_TLS_CERT") or None,
        ssl_keyfile=os.environ.get("HEARTH_TLS_KEY") or None,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

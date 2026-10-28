"""`python -m src` -> uvicorn on C3_PORT (default 8002), single worker.

One worker per container on purpose: each worker would load its own copy of
the model. Scale out with more replicas (c3-inference-N), not workers.
"""

import uvicorn

from .config import Settings

if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.port,
        workers=1,
        log_level=settings.log_level.lower(),
        access_log=False,  # one structured log line per frame is emitted by the app
    )

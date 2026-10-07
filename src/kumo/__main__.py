import logging
import os

import uvicorn

from .web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=os.environ.get("KUMO_HOST", "0.0.0.0"), port=int(os.environ.get("KUMO_PORT", "8115")))


if __name__ == "__main__":
    main()

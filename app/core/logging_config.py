import logging


def configure_logging() -> None:
    # Request/message bodies and credentials must never be logged.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

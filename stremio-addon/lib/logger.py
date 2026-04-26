import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cricfy")


def log_error(component: str, message: str) -> None:
    logger.error(f"[{component}] {message}")


def log_info(component: str, message: str) -> None:
    logger.info(f"[{component}] {message}")

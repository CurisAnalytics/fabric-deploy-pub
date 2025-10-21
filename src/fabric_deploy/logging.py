import logging
import re
from typing import ClassVar, override

from fabric_cicd._common._color import Fore, Style
from fabric_cicd._common._logging import print_header

print_header = print_header


class CustomFormatter(logging.Formatter):
    LEVEL_COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": Fore.BLACK,
        "INFO": Fore.WHITE + Style.BRIGHT,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "CRITICAL": Style.BRIGHT + Fore.RED,
    }

    @override
    def format(self, record: logging.LogRecord) -> str:
        level_color = self.LEVEL_COLORS.get(record.levelname, "")
        level_name = {
            "WARNING": "warn",
            "DEBUG": "debug",
            "INFO": "info",
            "ERROR": "error",
            "CRITICAL": "crit",
        }.get(record.levelname, "unknown")
        level_name = f"{level_color}[{level_name}]"
        message = f"{record.getMessage()}{Style.RESET_ALL}"
        # Calculate visual length by removing ANSI escape codes
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        # Get visual length of level_name without ANSI codes
        visual_level_length = len(ansi_escape.sub("", level_name))
        # Pad to 8 visual characters
        padding = " " * max(0, 8 - visual_level_length)
        full_message = f"{level_name}{padding} {message}"
        return full_message


def get_logger(name: str) -> logging.Logger:
    """Configure and return logger with CustomFormatter."""
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(CustomFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    return log

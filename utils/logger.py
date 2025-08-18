# logger.py

import logging
import os

# buffer for latest logs (per request)
log_buffer = []

class BrowserLogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        log_buffer.append(msg)

def get_logger(name):
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"{name}.log")

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.INFO)

        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)

        # add custom handler that stores logs in memory
        bh = BrowserLogHandler()
        bh.setLevel(logging.INFO)
        bh.setFormatter(formatter)

        logger.addHandler(fh)
        logger.addHandler(ch)
        logger.addHandler(bh)

    return logger

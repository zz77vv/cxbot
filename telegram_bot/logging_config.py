import logging
import re
from logging.handlers import RotatingFileHandler


class RedactFilter(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = tuple(s for s in secrets if s)

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, '[REDACTED]')
        message = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{20,}', '[REDACTED]', message)
        record.msg, record.args = message, ()
        # Exception strings may contain HTTP URLs or user query payloads.
        if record.exc_info:
            record.msg += f' error_type={record.exc_info[0].__name__}'
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging(cfg):
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(cfg.log_file, maxBytes=20 * 1024 * 1024,
                                  backupCount=5, encoding='utf-8')
    handler.addFilter(RedactFilter((cfg.bot_token, cfg.api_hash)))
    handler.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for name in ('httpx', 'httpcore', 'telethon'):
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler

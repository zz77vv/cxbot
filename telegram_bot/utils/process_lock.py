import os
from pathlib import Path


class ProcessLock:
    """OS releases the advisory lock on crash. The lock file is never unlinked."""
    def __init__(self, database: Path):
        self.path = Path(str(database) + '.instance.lock')
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError('该数据库已有机器人进程运行') from None

    def close(self):
        if self.file:
            self.file.close()
            self.file = None

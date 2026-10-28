"""Persist only authorized keys; exclusive publication never replaces a Session."""
import os
import uuid
from pathlib import Path
from telethon.sessions import SQLiteSession


def save_authorized_session(root: Path, key: str, session) -> Path:
    if not key.isascii() or not key.isdigit():
        raise ValueError('invalid_phone')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = root / '.pending'
    pending.mkdir(mode=0o700, exist_ok=True)
    if pending.is_symlink():
        raise ValueError('unsafe_session_directory')
    pending.chmod(0o700)
    temporary = pending / (uuid.uuid4().hex + '.session')
    final = root / (key + '.session')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        saved = SQLiteSession(str(temporary))
        try:
            saved.set_dc(session.dc_id, session.server_address, session.port)
            saved.auth_key = session.auth_key
            saved.save()
        finally:
            saved.close()
        temporary.chmod(0o600)
        # Same filesystem atomic no-clobber publication, unlike rename/replace.
        os.link(temporary, final)
        return final
    finally:
        for suffix in ('', '-journal', '-wal', '-shm'):
            temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)

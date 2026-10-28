import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from telegram_bot.config import Config
from telegram_bot.storage.database import Database
from telegram_bot.storage.repositories.account_repository import AccountRepository
from telegram_bot.clients.userbot_manager import UserbotManager
from telegram_bot.models.account import Account
from telegram_bot.models.enums import AccountState


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError('Tests must not contact external services')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)


@pytest.fixture
def cfg(tmp_path):
    return Config('test-token', 123, 456, 'test-hash', tmp_path/'bot.db', tmp_path/'sessions',
                  tmp_path/'logs/bot.log', tmp_path/'cache', reply_timeout=0.1, export_timeout=0.1,
                  retry_base=0.001, reconnect_interval=0.01, ocr_timeout=1)


@pytest.fixture
async def db(cfg):
    database = await Database(cfg.database).open()
    yield database
    await database.close()


def message(text='', id=11, buttons=None, **extra):
    attrs = dict(id=id, text=text, buttons=buttons, out=False, media=None, document=None,
                 file=None, sender_id=999, reply_to_msg_id=None, edit_date=None,
                 click=AsyncMock(return_value=SimpleNamespace(message='')))
    attrs.update(extra)
    return SimpleNamespace(**attrs)


def button(text):
    return SimpleNamespace(text=text)


@pytest.fixture
def client():
    return SimpleNamespace(is_connected=Mock(return_value=True), connect=AsyncMock(),
        disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
        send_message=AsyncMock(return_value=message(id=10)), get_messages=AsyncMock(return_value=[]),
        download_media=AsyncMock())


@pytest.fixture
def manager(cfg, db, client):
    manager = UserbotManager(cfg, AccountRepository(db), factory=lambda p: client)
    manager.accounts['10001'] = Account('10001', cfg.sessions/'10001.session', client, AccountState.ONLINE)
    return manager


@pytest.fixture
def bot():
    return SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock(),
                           delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())

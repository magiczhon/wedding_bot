"""Тесты адаптеров на настоящих pyTelegramBotAPI и vk_api с подменой сетевых запросов"""
import json

import pytest

telebot = pytest.importorskip('telebot')
pytest.importorskip('vk_api')

from telebot import apihelper, types  # noqa: E402

from adapters.telegram_adapter import TelegramMessenger  # noqa: E402
from adapters.vk_adapter import VkMessenger  # noqa: E402

TG_USER = {'id': 100, 'is_bot': False, 'first_name': 'Петр', 'last_name': 'Иванов', 'username': 'petr'}
TG_BOT = {'id': 999, 'is_bot': True, 'first_name': 'Wedding', 'username': 'wedding_bot'}
PRIVATE = {'id': 100, 'type': 'private'}


class FakeResponse:
    def __init__(self, payload):
        self.status_code, self.payload = 200, payload
        self.text = json.dumps(payload)
        self.content = b''
        self.reason = 'OK'

    def json(self):
        return self.payload


@pytest.fixture
def tg(monkeypatch):
    calls = []

    def sender(method, url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        calls.append((name, kwargs.get('params') or {}))
        result = {'message_id': len(calls) + 500, 'date': 0, 'chat': PRIVATE, 'from': TG_BOT}
        if name == 'getMe':
            result = TG_BOT
        return FakeResponse({'ok': True, 'result': result})

    monkeypatch.setattr(apihelper, 'CUSTOM_REQUEST_SENDER', sender)
    received = []
    return TelegramMessenger('123:ABC', received.append), calls, received


def tg_message(**fields):
    data = {'message_id': 7, 'date': 0, 'chat': PRIVATE, 'from': TG_USER}
    data.update(fields)
    return types.Message.de_json(data)


def test_telegram_parses_start_payload_reply_and_photo(tg):
    messenger, calls, received = tg
    inc = messenger.to_incoming(tg_message(text='/start join_ABC123'))
    assert (inc.text, inc.start_payload, inc.display_name, inc.username) == ('/start', 'join_ABC123', 'Петр Иванов', 'petr')

    reply = {'message_id': 55, 'date': 0, 'chat': PRIVATE, 'from': TG_BOT, 'text': 'вопрос'}
    inc = messenger.to_incoming(tg_message(text='ответ', reply_to_message=reply))
    assert inc.reply_to_id == '55'

    photo = [{'file_id': 'small', 'file_unique_id': 's', 'width': 10, 'height': 10},
             {'file_id': 'big', 'file_unique_id': 'b', 'width': 800, 'height': 600}]
    inc = messenger.to_incoming(tg_message(photo=photo, caption='арка'))
    assert inc.text == 'арка' and [a.kind for a in inc.attachments] == ['photo']

    voice = {'file_id': 'v', 'file_unique_id': 'v', 'duration': 3}
    inc = messenger.to_incoming(tg_message(voice=voice))
    assert inc.attachments[0].kind == 'other' and inc.attachments[0].description == 'голосовое сообщение'


def test_telegram_send_keyboard_remove_and_long_text(tg):
    messenger, calls, received = tg
    mid = messenger.send('100', 'Меню', [['A', 'B'], ['C']])
    name, params = calls[-1]
    assert name == 'sendMessage' and mid.isdigit()
    markup = json.loads(params['reply_markup'])
    assert [[b['text'] for b in row] for row in markup['keyboard']] == [['A', 'B'], ['C']]

    messenger.send('100', 'Без кнопок', [])
    assert json.loads(calls[-1][1]['reply_markup']) == {'remove_keyboard': True}

    before = len(calls)
    messenger.send('100', ('строка\n' * 1000))  # ~7000 символов → два сообщения
    assert len(calls) - before == 2
    assert 'reply_markup' not in calls[-1][1]

    assert 'start=join_XYZ' in messenger.invite_hint('XYZ') and 'wedding_bot' in messenger.invite_hint('XYZ')


@pytest.fixture
def vk(monkeypatch):
    calls = []
    messenger = VkMessenger('vk-token', 42, on_message=lambda inc: None)

    def method(name, values=None, raw=False):
        values = dict(values or {})
        calls.append((name, values))
        if name == 'messages.send':
            return [{'peer_id': int(values['peer_ids']), 'message_id': 900 + len(calls),
                     'conversation_message_id': len(calls)}]
        if name == 'users.get':
            return [{'id': values['user_ids'], 'first_name': 'Олег', 'last_name': 'Ведущий', 'screen_name': 'oleg'}]
        if name == 'photos.getMessagesUploadServer':
            return {'upload_url': 'https://upload.example/photo'}
        if name == 'photos.saveMessagesPhoto':
            return [{'owner_id': -42, 'id': 7, 'access_key': 'k'}]
        if name == 'groups.getById':
            return {'groups': [{'id': 42, 'screen_name': 'our_wedding'}]}
        raise AssertionError(f'unexpected method {name}')

    monkeypatch.setattr(messenger.session, 'method', method)
    monkeypatch.setattr(messenger.upload.http, 'post',
                        lambda url, files=None: FakeResponse({'server': 1, 'photo': '[]', 'hash': 'h'}))
    return messenger, calls


def test_vk_parses_message_with_reply_and_attachments(vk):
    messenger, calls = vk
    msg = {'from_id': 20, 'peer_id': 20, 'conversation_message_id': 15, 'text': 'Конечно!',
           'reply_message': {'from_id': -42, 'conversation_message_id': 3, 'text': 'вопрос'},
           'attachments': [
               {'type': 'photo', 'photo': {'sizes': [{'width': 100, 'height': 100, 'url': 'small'},
                                                     {'width': 1000, 'height': 800, 'url': 'big'}]}},
               {'type': 'doc', 'doc': {'title': 'смета.pdf', 'url': 'doc-url', 'ext': 'pdf'}},
               {'type': 'audio_message', 'audio_message': {}}]}
    inc = messenger.to_incoming(msg)
    assert (inc.user_id, inc.chat_id, inc.message_id, inc.reply_to_id) == ('20', '20', '15', '3')
    assert (inc.username, inc.display_name) == ('oleg', 'Олег Ведущий')
    assert [a.kind for a in inc.attachments] == ['photo', 'document', 'other']
    assert inc.attachments[1].filename == 'смета.pdf'

    own_reply = dict(msg, reply_message={'from_id': 20, 'conversation_message_id': 9})
    assert messenger.to_incoming(own_reply).reply_to_id is None  # reply на своё же сообщение — не ответ


def test_vk_send_returns_conversation_id_and_builds_keyboard(vk):
    messenger, calls = vk
    mid = messenger.send('20', 'Меню', [['✅ Подтвердить присутствие'], ['A' * 60, 'B']])
    name, params = calls[-1]
    assert name == 'messages.send' and params['peer_ids'] == '20' and mid == str(len(calls))
    keyboard = json.loads(params['keyboard'])
    labels = [[b['action']['label'] for b in row] for row in keyboard['buttons']]
    assert labels == [['✅ Подтвердить присутствие'], ['A' * 40, 'B']]

    messenger.send('20', 'Убрать', [])
    assert json.loads(calls[-1][1]['keyboard'])['buttons'] == []
    messenger.send('20', 'Без изменений')
    assert 'keyboard' not in calls[-1][1]

    messenger.send_photo('20', b'PNG')
    assert calls[-1][1]['attachment'] == 'photo-42_7_k'
    assert 'vk.me/our_wedding' in messenger.invite_hint('XYZ')


def test_end_to_end_guest_in_telegram_question_reaches_host_in_vk(tg, vk, tmp_path, monkeypatch):
    import yaml
    from pathlib import Path
    from core.bot import WeddingBot
    from database.db import Database

    tg_messenger, tg_calls, _ = tg
    vk_messenger, vk_calls = vk
    event = yaml.safe_load((Path(__file__).parent.parent / 'event.example.yaml').read_text(encoding='utf-8'))
    db = Database(str(tmp_path / 'e2e.db'))
    bot = WeddingBot(db, event, admins={'telegram': ['1']})
    bot.register(tg_messenger)
    bot.register(vk_messenger)

    code = db.invite_codes(['host'])['host']
    host_msg = lambda text, cmid, **kw: vk_messenger.to_incoming(
        {'from_id': 20, 'peer_id': 20, 'conversation_message_id': cmid, 'text': text, **kw})
    bot.handle(host_msg(f'код {code}', 1))
    bot.handle(host_msg('Олег Ведущий', 2))

    bot.handle(tg_messenger.to_incoming(tg_message(text='/start')))
    bot.handle(tg_messenger.to_incoming(tg_message(text='Петр Иванов')))
    bot.handle(tg_messenger.to_incoming(tg_message(text='🏠 Главное меню')))
    bot.handle(tg_messenger.to_incoming(tg_message(text=event['guest_channels']['host']['button'])))
    bot.handle(tg_messenger.to_incoming(tg_message(text='Можно песню для мамы?')))

    question = [c for c in vk_calls if c[0] == 'messages.send'][-1]
    assert 'песню для мамы' in question[1]['message'] and 'Петр Иванов' in question[1]['message']
    question_cmid = next(i + 1 for i, c in enumerate(vk_calls) if c is question)

    bot.handle(host_msg('Конечно!', 50, reply_message={'from_id': -42, 'conversation_message_id': question_cmid}))
    answer = [c for c in tg_calls if c[0] == 'sendMessage'][-1]
    assert 'Конечно!' in answer[1]['text'] and 'Олег Ведущий' in answer[1]['text']

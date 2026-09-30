"""Общие фикстуры: фейковые мессенджеры, база во временной папке, бот и помощники сценариев"""
import pytest

from core import bot as b
from core.bot import WeddingBot
from core.event_config import default_config
from core.messenger import Incoming, Messenger
from database.db import Database

PRIVACY = {'operator': 'ИП Тестов Т.Т.', 'contacts': 'privacy@example.ru', 'version': '1'}


class FakeMessenger(Messenger):
    def __init__(self, name, can_copy):
        self.name, self.title, self.can_copy = name, name.upper(), can_copy
        self.log, self.counter = [], 0

    def _id(self):
        self.counter += 1
        return f'{self.name}-{self.counter}'

    def send(self, chat_id, text, keyboard=None):
        mid = self._id()
        self.log.append(('text', str(chat_id), text, keyboard, mid))
        return mid

    def send_location(self, chat_id, lat, lon):
        self.log.append(('location', str(chat_id), (lat, lon), None, self._id()))

    def send_photo(self, chat_id, data, caption=''):
        mid = self._id()
        self.log.append(('photo', str(chat_id), data, None, mid))
        return mid

    def send_document(self, chat_id, data, filename, caption=''):
        mid = self._id()
        self.log.append(('document', str(chat_id), (filename, data), None, mid))
        return mid

    def copy_native(self, incoming, chat_id):
        if not self.can_copy:
            return None
        mid = self._id()
        self.log.append(('copy', str(chat_id), incoming.message_id, None, mid))
        return mid

    def invite_hint(self, code):
        return f'{self.name}-join:{code}'

    def guest_hint(self, code):
        return f'{self.name}-guest:{code}'

    def guest_link(self, code):
        return f'https://{self.name}.example/{code}'

    # помощники для тестов
    def to(self, chat_id):
        return [e for e in self.log if e[1] == str(chat_id)]

    def last(self, chat_id):
        return self.to(chat_id)[-1]

    def texts(self, chat_id):
        return [e[2] for e in self.to(chat_id) if e[0] == 'text']


class Env:
    def __init__(self, tmp_path):
        self.db = Database(str(tmp_path / 'test.db'))
        self.bot = WeddingBot(self.db, privacy=PRIVACY, public_url='https://svc.example')
        self.tg, self.vk = FakeMessenger('telegram', can_copy=True), FakeMessenger('vk', can_copy=False)
        self.bot.register(self.tg)
        self.bot.register(self.vk)
        self.counter = 0

    def m(self, platform):
        return self.tg if platform == 'telegram' else self.vk

    def event(self, title='Свадьба Анны и Ивана', **kwargs):
        return self.db.create_event(title, default_config(), **kwargs)

    def say(self, platform, uid, text='', reply_to=None, attachments=None, start_payload=None):
        self.counter += 1
        self.bot.handle(Incoming(platform=platform, user_id=str(uid), chat_id=str(uid),
                                 message_id=f'in-{self.counter}', text=text, username=f'user{uid}',
                                 display_name=f'User {uid}', reply_to_id=reply_to,
                                 attachments=attachments or [], start_payload=start_payload,
                                 native=object() if attachments else None))

    def open_code(self, platform, uid, code):
        if platform == 'telegram':
            self.say(platform, uid, '/start', start_payload=f'ev_{code}')
        else:
            self.say(platform, uid, f'код {code}')

    def guest(self, platform, uid, name, event, rsvp=None):
        """Гость открывает приглашение, соглашается, представляется и отвечает на вопрос о присутствии"""
        self.open_code(platform, uid, event['code'])
        self.say(platform, uid, b.BTN_AGREE)
        self.say(platform, uid, name)
        if rsvp == 'yes':
            self.say(platform, uid, b.RSVP_YES)
            self.say(platform, uid, '2')
        elif rsvp == 'no':
            self.say(platform, uid, b.RSVP_NO)
        else:
            self.say(platform, uid, b.BTN_MENU)

    def staff(self, platform, uid, name, event, role):
        code = self.db.invite_codes(event['id'], [role])[role]
        if platform == 'telegram':
            self.say(platform, uid, '/start', start_payload=f'join_{code}')
        else:
            self.say(platform, uid, f'код {code}')
        user = self.db.get_or_create_user(platform, str(uid), str(uid), f'user{uid}', f'User {uid}')
        if user['state'] == 'consent':
            self.say(platform, uid, b.BTN_AGREE)
        user = self.db.get_user(user['id'])
        if not user['full_name']:
            self.say(platform, uid, name)

    def team(self, event):
        """Организатор (tg 1), ведущий (vk 20), невеста (tg 30), декоратор (tg 40)"""
        self.staff('telegram', 1, 'Мария Организатор', event, 'organizer')
        self.staff('vk', 20, 'Олег Ведущий', event, 'host')
        self.staff('telegram', 30, 'Анна Невеста', event, 'couple')
        self.staff('telegram', 40, 'Дина Декор', event, 'decorator')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'SEND_DELAY', 0)
    return Env(tmp_path)

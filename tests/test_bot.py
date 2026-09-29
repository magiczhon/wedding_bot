"""Сценарные тесты ядра на фейковых мессенджерах: запуск — `pytest` из корня проекта"""
import sqlite3
from io import BytesIO
from pathlib import Path

import openpyxl
import pytest
import yaml

from core import bot as b
from core.bot import WeddingBot
from core.messenger import Attachment, Incoming, Messenger
from database.db import Database

EVENT = yaml.safe_load((Path(__file__).parent.parent / 'event.example.yaml').read_text(encoding='utf-8'))


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
        return f'{self.name}:{code}'

    # помощники для тестов
    def to(self, chat_id):
        return [e for e in self.log if e[1] == str(chat_id)]

    def last(self, chat_id):
        return self.to(chat_id)[-1]

    def texts(self, chat_id):
        return [e[2] for e in self.to(chat_id) if e[0] == 'text']


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'SEND_DELAY', 0)
    db = Database(str(tmp_path / 'test.db'))
    bot = WeddingBot(db, EVENT, admins={'telegram': ['1']}, test_users={'telegram': ['1']})
    tg, vk = FakeMessenger('telegram', can_copy=True), FakeMessenger('vk', can_copy=False)
    bot.register(tg)
    bot.register(vk)
    counter = [0]

    def say(platform, uid, text='', reply_to=None, attachments=None, start_payload=None):
        counter[0] += 1
        bot.handle(Incoming(platform=platform, user_id=str(uid), chat_id=str(uid), message_id=f'in-{counter[0]}',
                            text=text, username=f'user{uid}', display_name=f'User {uid}', reply_to_id=reply_to,
                            attachments=attachments or [], start_payload=start_payload,
                            native=object() if attachments else None))

    return bot, db, tg, vk, say


def register_guest(say, platform, uid, name, rsvp=None):
    say(platform, uid, '/start')
    say(platform, uid, name)
    if rsvp == 'yes':
        say(platform, uid, b.RSVP_YES)
        say(platform, uid, '2')
    elif rsvp == 'no':
        say(platform, uid, b.RSVP_NO)
    else:
        say(platform, uid, b.BTN_MENU)


def join_team(bot, db, say, platform, uid, role, name):
    code = db.invite_codes([role])[role]
    if platform == 'telegram':
        say(platform, uid, '/start', start_payload=f'join_{code}')
    else:
        say(platform, uid, f'код {code}')
    say(platform, uid, name)


def setup_team(bot, db, say):
    register_guest(say, 'telegram', 1, 'Мария Организатор')  # админ из config → организатор
    join_team(bot, db, say, 'vk', 20, 'host', 'Олег Ведущий')
    join_team(bot, db, say, 'telegram', 30, 'couple', 'Анна Невеста')
    join_team(bot, db, say, 'telegram', 40, 'decorator', 'Дина Декор')


def test_guest_registration_rsvp_and_preferences_without_duplicates(env):
    bot, db, tg, vk, say = env
    say('vk', 100, 'Начать')
    assert 'имя и фамилию' in vk.texts(100)[-1]
    say('vk', 100, 'Петр')  # одно слово — просим ещё раз
    assert 'И фамилию' in vk.texts(100)[-1]
    say('vk', 100, 'Петр Иванов')
    assert vk.last(100)[3] == [[b.RSVP_YES, b.RSVP_NO], [b.RSVP_MAYBE], [b.BTN_MENU]]
    say('vk', 100, b.RSVP_YES)
    say('vk', 100, '3')
    guest = db.guests('yes')[0]
    assert (guest['full_name'], guest['party_size'], guest['platform']) == ('Петр Иванов', 3, 'vk')

    for name, food, drink in [(b.FOR_ME_PREFIX + 'Петр Иванов', '🐟 Рыба', '🥂 Вино белое'),
                              ('Ольга Иванова', '🥩 Мясо', b.BTN_SKIP),
                              (b.FOR_ME_PREFIX + 'Петр Иванов', '🥩 Мясо', '🥃 Коньяк')]:
        say('vk', 100, b.BTN_PREFS)
        say('vk', 100, name)
        say('vk', 100, food)
        say('vk', 100, drink)
    prefs = {p['guest_name']: (p['food'], p['drink']) for p in db.preferences()}
    assert prefs == {'Петр Иванов': ('🥩 Мясо', '🥃 Коньяк'), 'Ольга Иванова': ('🥩 Мясо', 'не указано')}


def test_guest_does_not_see_staff_actions(env):
    bot, db, tg, vk, say = env
    register_guest(say, 'telegram', 100, 'Петр Иванов')
    say('telegram', 100, b.BTN_EXPORT)
    assert tg.texts(100)[-1].startswith('Не понял')
    assert not [e for e in tg.to(100) if e[0] == 'document']
    say('telegram', 100, b.BTN_PLACE)
    assert tg.last(100)[0] == 'location'


def test_team_joins_by_code_and_organizer_is_notified(env):
    bot, db, tg, vk, say = env
    setup_team(bot, db, say)
    roles = {u['full_name']: u['role'] for u in db.users_by_roles(list(bot.roles))}
    assert roles == {'Мария Организатор': 'organizer', 'Олег Ведущий': 'host',
                     'Анна Невеста': 'couple', 'Дина Декор': 'decorator'}
    assert any('Олег Ведущий присоединился' in t and 'VK' in t for t in tg.texts(1))
    say('telegram', 1, b.BTN_INVITES)
    assert 'vk:' in tg.texts(1)[-1] and 'telegram:' in tg.texts(1)[-1]
    say('telegram', 50, 'код ZZZZZZ')
    assert 'не найден' in tg.texts(50)[-1]


def test_question_to_host_across_messengers_and_answers(env):
    bot, db, tg, vk, say = env
    setup_team(bot, db, say)
    register_guest(say, 'telegram', 100, 'Петр Иванов')
    guest_id = db.guests()[0]['id']

    say('telegram', 100, EVENT['guest_channels']['host']['button'])
    say('telegram', 100, 'Можно заказать песню для мамы?')
    assert tg.texts(100)[-1].startswith('✅ Передано')
    question = vk.last(20)
    assert question[0] == 'text' and f'гостя №{guest_id}' in question[2] and 'песню для мамы' in question[2]
    assert not any('песню' in t for t in tg.texts(1))  # организатору вопросы ведущему не приходят

    say('vk', 20, 'Конечно, после торта!', reply_to=question[4])  # ответ через reply
    answer = tg.last(100)
    assert 'Олег Ведущий' in answer[2] and 'после торта' in answer[2]
    assert 'отправлен гостю Петр Иванов' in vk.texts(20)[-1]

    say('telegram', 100, b.BTN_MENU)
    say('telegram', 100, 'Спасибо!', reply_to=answer[4])  # гость отвечает на ответ из главного меню
    assert 'Спасибо!' in vk.texts(20)[-1]

    say('vk', 20, f'ответ {guest_id} И вам!')  # ответ по номеру, без reply
    assert 'И вам!' in tg.texts(100)[-1]


def test_host_broadcast_needs_confirmation_and_guest_reply_goes_to_host(env):
    bot, db, tg, vk, say = env
    setup_team(bot, db, say)
    register_guest(say, 'telegram', 100, 'Петр Иванов')
    register_guest(say, 'vk', 101, 'Ирина Смирнова')

    say('vk', 20, b.BTN_WRITE_GUESTS)
    say('vk', 20, 'Кто хочет участвовать в конкурсе?')
    assert 'всем гостям (2 чел.)' in vk.texts(20)[-1]
    say('vk', 20, b.BTN_CANCEL)
    assert not any('конкурсе' in t for t in tg.texts(100))

    say('vk', 20, 'Кто хочет участвовать в конкурсе?')
    say('vk', 20, b.BTN_CONFIRM)
    assert 'Отправлено: 2' in vk.texts(20)[-1]
    assert vk.last(20)[3] == [[b.BTN_MENU]]  # ведущий остаётся в режиме «писать гостям»
    broadcast = tg.last(100)
    assert 'конкурсе' in broadcast[2] and 'конкурсе' in vk.texts(101)[-1]

    say('telegram', 100, 'Я хочу!', reply_to=broadcast[4])
    assert 'Я хочу!' in vk.texts(20)[-1] and 'Петр Иванов' in vk.texts(20)[-1]


def test_contractor_room_reaches_only_its_members_with_media(env):
    bot, db, tg, vk, say = env
    setup_team(bot, db, say)

    say('telegram', 40, b.BTN_ROOMS)
    rooms = [row[0] for row in tg.last(40)[3]]
    assert rooms == ['Общий чат команды', 'Декор', b.BTN_MENU]
    say('telegram', 40, 'Декор')
    assert 'Мария Организатор' in tg.texts(40)[-1] and 'Олег' not in tg.texts(40)[-1]
    photo = Attachment('photo', load=lambda: b'PNG', filename='arka.jpg')
    say('telegram', 40, 'Вариант арки', attachments=[photo])
    # внутри Telegram — копия сообщения целиком
    for uid in (1, 30):
        kinds = [e[0] for e in tg.to(uid)[-2:]]
        assert kinds == ['text', 'copy'] and 'Дина Декор' in tg.to(uid)[-2][2]
    assert not any('арки' in t for t in vk.texts(20))  # ведущего нет в чате «Декор»

    # ответ невесты (reply) на пересланное сообщение возвращается в чат «Декор»
    say('telegram', 30, 'Нравится!', reply_to=tg.to(30)[-1][4])
    assert 'Нравится!' in tg.texts(40)[-1] and 'Декор' in tg.texts(40)[-1]

    # в общем чате фото из Telegram уходит ведущему во ВКонтакте файлом
    say('telegram', 40, b.BTN_MENU)
    say('telegram', 40, b.BTN_ROOMS)
    say('telegram', 40, 'Общий чат команды')
    say('telegram', 40, 'Схема зала', attachments=[photo])
    assert [e[0] for e in vk.to(20)[-2:]] == ['text', 'photo'] and vk.to(20)[-1][2] == b'PNG'
    assert 'Схема зала' in vk.to(20)[-2][2]


def test_organizer_broadcast_to_unanswered_guests_and_summary_export(env):
    bot, db, tg, vk, say = env
    setup_team(bot, db, say)
    register_guest(say, 'telegram', 100, 'Петр Иванов', rsvp='yes')
    register_guest(say, 'vk', 101, 'Ирина Смирнова')
    register_guest(say, 'telegram', 102, 'Сергей Козлов', rsvp='no')

    say('telegram', 1, b.BTN_BROADCAST)
    say('telegram', 1, 'Кто не ответил')
    say('telegram', 1, 'Напоминаем ответить до пятницы')
    assert '(1 чел.)' in tg.texts(1)[-1]
    say('telegram', 1, b.BTN_CONFIRM)
    assert 'до пятницы' in vk.texts(101)[-1]
    assert not any('до пятницы' in t for t in tg.texts(100) + tg.texts(102))
    # ответ гостя на рассылку попадает организатору и молодожёнам
    say('vk', 101, 'Приду!', reply_to=vk.last(101)[4])
    assert 'Приду!' in tg.texts(1)[-1] and 'Приду!' in tg.texts(30)[-1]

    say('telegram', 1, b.BTN_SUMMARY)
    summary = tg.texts(1)[-1]
    assert 'Придут: 1 (всего человек: 2)' in summary and 'Не ответили: 1' in summary

    say('telegram', 30, b.BTN_EXPORT)  # молодожёны тоже видят выгрузку
    kind, _, (filename, data), _, _ = tg.last(30)
    assert kind == 'document' and filename == 'guests.xlsx'
    wb = openpyxl.load_workbook(BytesIO(data))
    assert wb.sheetnames == ['Гости', 'Пожелания', 'Команда', 'Сообщения']
    names = [r[1] for r in wb['Гости'].iter_rows(min_row=2, values_only=True) if r[0]]
    assert sorted(names) == ['Ирина Смирнова', 'Петр Иванов', 'Сергей Козлов']
    assert any('Приду!' in (r[5] or '') for r in wb['Сообщения'].iter_rows(min_row=2, values_only=True))


def test_staff_guest_view_and_back(env):
    bot, db, tg, vk, say = env
    setup_team(bot, db, say)
    say('telegram', 30, b.BTN_GUEST_VIEW)
    assert [b.BTN_RSVP] in tg.last(30)[3] and [b.BTN_STAFF_VIEW] in tg.last(30)[3]
    say('telegram', 30, b.BTN_DRESS)  # в режиме гостя работают гостевые кнопки
    assert 'Дресс-код' in tg.texts(30)[-1]
    say('telegram', 30, b.BTN_STAFF_VIEW)
    assert [b.BTN_ROOMS] == tg.last(30)[3][0][:1]


def test_legacy_database_is_migrated(tmp_path):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE guests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, chat_id INTEGER,
                             username TEXT, tg_name TEXT, full_name TEXT, updated_at TIMESTAMP);
        CREATE TABLE guest_preferences (id INTEGER PRIMARY KEY AUTOINCREMENT, guest_id INTEGER, guest_name TEXT,
                                        food TEXT, drink TEXT, updated_at TIMESTAMP);
        INSERT INTO guests (user_id, chat_id, username, tg_name, full_name) VALUES (555, 555, 'petr', 'Петя', 'Петр Иванов');
        INSERT INTO guest_preferences (guest_id, guest_name, food, drink) VALUES (1, 'Петр Иванов', 'Рыба', 'Водка');
        INSERT INTO guest_preferences (guest_id, guest_name, food, drink) VALUES (1, 'Петр Иванов', 'Мясо', 'Водка');
    ''')
    conn.commit()
    conn.close()
    db = Database(path)
    guest = db.guests()[0]
    assert (guest['platform'], guest['platform_user_id'], guest['full_name']) == ('telegram', '555', 'Петр Иванов')
    assert [(p['guest_name'], p['food']) for p in db.preferences()] == [('Петр Иванов', 'Мясо')]
    Database(path)  # повторный запуск ничего не ломает
    assert len(db.guests()) == 1

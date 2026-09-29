"""Логика бота, общая для всех мессенджеров.

Сценарии:
- гость: регистрация по имени, подтверждение присутствия, пожелания по меню,
  информация о мероприятии, вопросы ведущему и организатору;
- ведущий: получает вопросы гостей, отвечает конкретному гостю или пишет всем гостям;
- команда (организатор, молодожёны, подрядчики): рабочие чаты по ролям,
  где сообщение участника получают все остальные участники, в любом мессенджере;
- организатор: выгрузка, сводка, рассылки, коды приглашения для команды.
"""
import re
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional

from core.export import build_excel
from core.messenger import Incoming, Keyboard, Messenger
from database.db import Database
from utils.logger import logger

BTN_MENU = '🏠 Главное меню'
BTN_RSVP = '✅ Подтвердить присутствие'
BTN_PREFS = '🍽 Пожелания по меню'
BTN_DRESS = '👗 Дресс-код'
BTN_PLACE = '📍 Место и время'
BTN_PROGRAM = '📅 Программа'
BTN_ROOMS = '💬 Рабочие чаты'
BTN_WRITE_GUESTS = '📢 Написать гостям'
BTN_EXPORT = '📋 Выгрузка гостей'
BTN_SUMMARY = '📊 Сводка'
BTN_BROADCAST = '📨 Рассылка'
BTN_INVITES = '🔑 Приглашения команды'
BTN_GUEST_VIEW = '👤 Меню гостя'
BTN_STAFF_VIEW = '🧑‍💼 Меню команды'

RSVP_YES, RSVP_NO, RSVP_MAYBE = '✅ Приду', '❌ Не смогу', '🤔 Пока не знаю'
RSVP_LABELS = {'yes': 'придёт', 'no': 'не придёт', 'maybe': 'пока не знает', None: 'не ответил'}
BTN_CONFIRM, BTN_CANCEL = '✅ Отправить', '❌ Отмена'
BTN_SKIP = 'Пропустить'
FOR_ME_PREFIX = 'Для меня: '

AUDIENCES = {
    'Всем гостям': ('guests', 'any'),
    'Кто придёт': ('guests', 'yes'),
    'Кто не ответил': ('guests', None),
    'Всей команде': ('team', None),
    'Тестовым': ('test', None),
}

ANSWER_RE = re.compile(r'^(?:/reply|/ответ|ответ)\s+(\d+)\s+(.+)$', re.S | re.I)
CODE_RE = re.compile(r'^(?:/join|/start\s+join_|код|code)[\s_:]*([A-Za-z0-9]{4,12})$', re.I)
SEND_DELAY = 0.05  # пауза между сообщениями рассылки, чтобы не упереться в лимиты мессенджеров


class WeddingBot:
    def __init__(self, db: Database, event: Dict, admins: Dict[str, List], test_users: Optional[Dict] = None):
        self.db = db
        self.event = event
        self.admins = {platform: {str(i) for i in ids} for platform, ids in (admins or {}).items()}
        self.test_users = test_users or {}
        self.messengers: Dict[str, Messenger] = {}
        self.roles: Dict[str, str] = event.get('roles', {})
        self.channels: Dict[str, Dict] = event.get('guest_channels', {})
        self.rooms: Dict[str, Dict] = {r['id']: r for r in event.get('rooms', [])}
        # Сообщения, ожидающие подтверждения рассылки: user_id -> (Incoming, параметры)
        self.pending: Dict[int, tuple] = {}
        self.pending_lock = threading.Lock()
        self.states: Dict[str, Callable] = {
            'ask_name': self._on_name,
            'rsvp': self._on_rsvp,
            'party_size': self._on_party_size,
            'pref_name': self._on_pref_name,
            'pref_food': self._on_pref_food,
            'pref_drink': self._on_pref_drink,
            'in_channel': self._on_channel_message,
            'room_select': self._on_room_select,
            'broadcast_audience': self._on_broadcast_audience,
            'broadcast_text': self._on_broadcast_text,
            'confirm_send': self._on_confirm_send,
        }

    def register(self, messenger: Messenger):
        self.messengers[messenger.name] = messenger

    # ================= вход =================

    def handle(self, inc: Incoming):
        try:
            self._handle(inc)
        except Exception:
            logger.exception(f'Error handling message from {inc.platform}:{inc.user_id}')
            try:
                self.messengers[inc.platform].send(inc.chat_id, 'Что-то пошло не так 😔 Попробуйте ещё раз.')
            except Exception:
                pass

    def _handle(self, inc: Incoming):
        user = self._user(inc)
        text = (inc.text or '').strip()

        code = self._extract_code(inc, text)
        if code:
            return self._join(user, code)

        if text.lower() in ('/start', 'начать', 'start'):
            if user['full_name']:
                self.db.set_state(user['id'], None)
                return self.show_menu(user, 'Рады вас снова видеть! 🥂')
            return self._ask_name(user, self.event['event'].get('welcome', 'Добро пожаловать! 🎉'))

        if not user['full_name']:
            if user['state'] == 'ask_name':
                return self._on_name(user, inc, text)
            return self._ask_name(user, self.event['event'].get('welcome', 'Добро пожаловать! 🎉'))

        if text in (BTN_MENU, '/menu') or text.lower() == 'меню':
            self._drop_pending(user)
            self.db.set_state(user['id'], None)
            return self.show_menu(user, 'Главное меню')

        if inc.reply_to_id:
            relay = self.db.find_relay(inc.platform, inc.chat_id, inc.reply_to_id)
            if relay and self._route_reply(user, inc, relay):
                return

        answer = ANSWER_RE.match(text)
        if answer and self.is_staff(user):
            return self._answer_by_number(user, int(answer.group(1)), answer.group(2), inc)

        handler = self.states.get(user['state'] or '')
        if handler:
            return handler(user, inc, text)

        action = self._menu_actions(user).get(text)
        if action:
            return action(user)
        self.show_menu(user, 'Не понял 🙂 Выберите пункт меню.')

    def _user(self, inc: Incoming) -> Dict:
        is_admin = str(inc.user_id) in self.admins.get(inc.platform, set())
        user = self.db.get_or_create_user(inc.platform, inc.user_id, inc.chat_id, inc.username,
                                          inc.display_name, role='organizer' if is_admin else 'guest')
        if is_admin and user['role'] != 'organizer':
            self.db.update_user(user['id'], role='organizer')
            user['role'] = 'organizer'
        return user

    # ================= вспомогательное =================

    def is_staff(self, user: Dict) -> bool:
        return user['role'] != 'guest'

    def guest_view(self, user: Dict) -> bool:
        return user['role'] == 'guest' or self.db.state_data(user).get('guest_view', False)

    def role_title(self, role: str) -> str:
        return 'Гость' if role == 'guest' else self.roles.get(role, role)

    def platform_title(self, platform: str) -> str:
        m = self.messengers.get(platform)
        return m.title if m else platform

    def send(self, user: Dict, text: str, keyboard: Optional[Keyboard] = None) -> Optional[str]:
        m = self.messengers.get(user['platform'])
        if m is None:
            logger.warning(f"No messenger for platform {user['platform']}")
            return None
        return m.send(user['chat_id'], text, keyboard)

    def _set_idle(self, user: Dict):
        """Сброс сценария с сохранением выбранного вида меню (гостя или команды)"""
        guest_view = self.db.state_data(user).get('guest_view', False)
        self.db.set_state(user['id'], None, {'guest_view': True} if guest_view else None)
        user['state'], user['state_data'] = None, ('{"guest_view": true}' if guest_view else None)

    def _set_state(self, user: Dict, state: str, **data):
        if self.db.state_data(user).get('guest_view'):
            data['guest_view'] = True
        self.db.set_state(user['id'], state, data)

    def _drop_pending(self, user: Dict):
        with self.pending_lock:
            self.pending.pop(user['id'], None)

    @staticmethod
    def _extract_code(inc: Incoming, text: str) -> Optional[str]:
        if inc.start_payload and inc.start_payload.lower().startswith('join_'):
            return inc.start_payload[5:]
        m = CODE_RE.match(text)
        return m.group(1) if m else None

    # ================= меню =================

    def menu_keyboard(self, user: Dict) -> Keyboard:
        if self.guest_view(user):
            rows = [[BTN_RSVP], [BTN_PREFS, BTN_DRESS]]
            rows.append([BTN_PLACE, BTN_PROGRAM] if self.event['event'].get('program') else [BTN_PLACE])
            buttons = [c['button'] for c in self.channels.values()]
            rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            if self.is_staff(user):
                rows.append([BTN_STAFF_VIEW])
            return rows
        rows = [[BTN_ROOMS]]
        if self._broadcast_channel(user):
            rows[0].append(BTN_WRITE_GUESTS)
        if user['role'] in self.event.get('export_roles', ['organizer']):
            rows.append([BTN_EXPORT, BTN_SUMMARY])
        if user['role'] == 'organizer':
            rows.append([BTN_BROADCAST, BTN_INVITES])
        rows.append([BTN_PLACE, BTN_GUEST_VIEW])
        return rows

    def show_menu(self, user: Dict, text: str):
        self.send(user, text, self.menu_keyboard(user))

    def _menu_actions(self, user: Dict) -> Dict[str, Callable]:
        actions = {BTN_PLACE: self.show_place}
        if self.guest_view(user):
            actions.update({BTN_RSVP: self.ask_rsvp, BTN_PREFS: self.start_preferences,
                            BTN_DRESS: self.show_dresscode, BTN_PROGRAM: self.show_program})
            for cid, c in self.channels.items():
                actions[c['button']] = lambda u, cid=cid: self.enter_guest_channel(u, cid)
            if self.is_staff(user):
                actions[BTN_STAFF_VIEW] = self.switch_to_staff_view
        else:
            actions.update({BTN_ROOMS: self.choose_room, BTN_GUEST_VIEW: self.switch_to_guest_view})
            if self._broadcast_channel(user):
                actions[BTN_WRITE_GUESTS] = self.enter_guest_broadcast
            if user['role'] in self.event.get('export_roles', ['organizer']):
                actions.update({BTN_EXPORT: self.send_export, BTN_SUMMARY: self.send_summary})
            if user['role'] == 'organizer':
                actions.update({BTN_BROADCAST: self.start_broadcast, BTN_INVITES: self.show_invites})
        return actions

    def switch_to_guest_view(self, user: Dict):
        self.db.set_state(user['id'], None, {'guest_view': True})
        user['state_data'] = '{"guest_view": true}'
        self.show_menu(user, 'Так бот выглядит для гостей. Вернуться — «Меню команды».')

    def switch_to_staff_view(self, user: Dict):
        self.db.set_state(user['id'], None)
        user['state_data'] = None
        self.show_menu(user, 'Меню команды')

    # ================= регистрация и приглашения =================

    def _ask_name(self, user: Dict, intro: str):
        self._set_state(user, 'ask_name')
        self.send(user, f'{intro}\n\nПожалуйста, напишите ваше имя и фамилию:', [])

    def _on_name(self, user: Dict, inc: Incoming, text: str):
        if len(text.split()) < 2 or len(text) > 100:
            return self.send(user, 'Пожалуйста, напишите имя И фамилию (можно и отчество):')
        self.db.update_user(user['id'], full_name=text)
        user['full_name'] = text
        self._set_idle(user)
        logger.info(f"Registered {user['platform']}:{user['platform_user_id']} as {text!r}, role={user['role']}")
        if self.is_staff(user):
            self._notify_join(user)
            return self.show_menu(user, f'Спасибо, {text}! Вы в команде: {self.role_title(user["role"])}.\n'
                                        f'Рабочие чаты — в меню «{BTN_ROOMS}».')
        self.send(user, f'Спасибо, {text}! Вы зарегистрированы 🎉')
        self.ask_rsvp(user)

    def _join(self, user: Dict, code: str):
        role = self.db.role_by_code(code)
        if role is None:
            return self.send(user, 'Код приглашения не найден. Проверьте его у организатора.')
        if user['role'] == 'organizer' and role != 'organizer':
            return self.send(user, 'Вы организатор — роль по коду не меняется.')
        self.db.update_user(user['id'], role=role)
        user['role'] = role
        self.db.set_state(user['id'], None)
        user['state_data'] = None
        if not user['full_name']:
            return self._ask_name(user, f'Вы присоединяетесь к команде: {self.role_title(role)}.')
        self._notify_join(user)
        self.show_menu(user, f'Готово! Вы в команде: {self.role_title(role)}.')

    def _notify_join(self, user: Dict):
        for org in self.db.users_by_roles(['organizer']):
            if org['id'] != user['id']:
                self.send(org, f"👋 {user['full_name']} присоединился к команде: {self.role_title(user['role'])} "
                               f"({self.platform_title(user['platform'])})")

    def show_invites(self, user: Dict):
        team_roles = [r for r in self.roles if r != 'organizer']
        codes = self.db.invite_codes(team_roles)
        lines = ['🔑 Коды приглашения. Отправьте нужный код человеку — он станет участником команды '
                 'и получит доступ к своим рабочим чатам:\n']
        for role in team_roles:
            lines.append(f'{self.role_title(role)} — код {codes[role]}')
            for m in self.messengers.values():
                lines.append(f'   • {m.title}: {m.invite_hint(codes[role])}')
        lines.append('\nГостям код не нужен: достаточно открыть бота.')
        self.send(user, '\n'.join(lines))

    # ================= информация =================

    def show_place(self, user: Dict):
        ev, place = self.event['event'], self.event['event'].get('place', {})
        text = f"📍 {place.get('name', '')}\n{place.get('address', '')}\n🕓 {ev.get('date_time', '')}"
        if place.get('how_to_get'):
            text += f"\n\n{place['how_to_get']}"
        self.send(user, text)
        if place.get('lat') is not None and place.get('lon') is not None:
            self.messengers[user['platform']].send_location(user['chat_id'], place['lat'], place['lon'])

    def show_dresscode(self, user: Dict):
        self.send(user, self.event['event'].get('dresscode', 'Дресс-код не указан'))

    def show_program(self, user: Dict):
        self.send(user, '📅 Программа\n\n' + self.event['event'].get('program', 'Программа появится позже'))

    # ================= подтверждение присутствия =================

    def ask_rsvp(self, user: Dict):
        cfg, ev = self.event.get('rsvp', {}), self.event['event']
        current = f"\nСейчас отмечено: {RSVP_LABELS[user.get('rsvp')]}." if user.get('rsvp') else ''
        deadline = f"\nПросим ответить {ev['rsvp_deadline']}." if ev.get('rsvp_deadline') else ''
        self._set_state(user, 'rsvp')
        self.send(user, f"{cfg.get('question', 'Сможете ли вы прийти?')}{current}{deadline}",
                  [[RSVP_YES, RSVP_NO], [RSVP_MAYBE], [BTN_MENU]])

    def _on_rsvp(self, user: Dict, inc: Incoming, text: str):
        answer = {RSVP_YES: 'yes', RSVP_NO: 'no', RSVP_MAYBE: 'maybe'}.get(text)
        if answer is None:
            return self.send(user, 'Выберите вариант кнопкой ниже 👇')
        if answer == 'yes':
            self.db.update_user(user['id'], rsvp='yes')
            max_size = int(self.event.get('rsvp', {}).get('max_party_size', 5))
            numbers = [str(i) for i in range(1, max_size + 1)]
            self._set_state(user, 'party_size')
            return self.send(user, 'Ура! 🎉 Сколько вас будет, включая вас?',
                             [numbers[i:i + 5] for i in range(0, len(numbers), 5)] + [[BTN_MENU]])
        self.db.update_user(user['id'], rsvp=answer, party_size=0 if answer == 'no' else None)
        user['rsvp'] = answer
        self._set_idle(user)
        self.show_menu(user, 'Очень жаль, что не получится 😔 Спасибо, что предупредили!' if answer == 'no'
                       else 'Хорошо! Ответить можно в любой момент кнопкой «Подтвердить присутствие».')

    def _on_party_size(self, user: Dict, inc: Incoming, text: str):
        max_size = int(self.event.get('rsvp', {}).get('max_party_size', 5))
        if not text.isdigit() or not 1 <= int(text) <= max_size:
            return self.send(user, f'Выберите число от 1 до {max_size} 👇')
        self.db.update_user(user['id'], party_size=int(text))
        self._set_idle(user)
        self.show_menu(user, f'Спасибо! Ждём вас ({text} чел.) 🥂\n'
                             f'Не забудьте указать пожелания по меню — кнопка «{BTN_PREFS}».')

    # ================= пожелания по меню =================

    def start_preferences(self, user: Dict):
        existing = self.db.preferences(user['id'])
        text = 'Для кого заполняем пожелания? Напишите имя и фамилию гостя или выберите себя.'
        if existing:
            text = ('Уже указано:\n' + '\n'.join(f"• {p['guest_name']}: {p['food']}, {p['drink']}" for p in existing)
                    + '\n\nЧтобы изменить — выберите того же гостя ещё раз.\n\n' + text)
        me = (FOR_ME_PREFIX + user['full_name'])[:40]
        others = [[p['guest_name'][:40]] for p in existing if p['guest_name'] != user['full_name']][:5]
        self._set_state(user, 'pref_name')
        self.send(user, text, [[me]] + others + [[BTN_MENU]])

    def _on_pref_name(self, user: Dict, inc: Incoming, text: str):
        name = user['full_name'] if text.startswith(FOR_ME_PREFIX) else text
        if len(name) < 2 or len(name) > 100:
            return self.send(user, 'Напишите имя и фамилию гостя:')
        self._ask_option(user, 'pref_food', 'food', guest_name=name)

    def _ask_option(self, user: Dict, state: str, key: str, **data):
        cfg = self.event['menu'][key]
        options = list(cfg['options'])
        self._set_state(user, state, **data)
        self.send(user, cfg['question'], [options[i:i + 2] for i in range(0, len(options), 2)] + [[BTN_SKIP]])

    def _option_value(self, key: str, text: str) -> Optional[str]:
        if text == BTN_SKIP:
            return 'не указано'
        return text if text in self.event['menu'][key]['options'] else None

    def _on_pref_food(self, user: Dict, inc: Incoming, text: str):
        food = self._option_value('food', text)
        if food is None:
            return self.send(user, 'Выберите вариант кнопкой ниже 👇')
        data = self.db.state_data(user)
        self._ask_option(user, 'pref_drink', 'drink', guest_name=data['guest_name'], food=food)

    def _on_pref_drink(self, user: Dict, inc: Incoming, text: str):
        drink = self._option_value('drink', text)
        if drink is None:
            return self.send(user, 'Выберите вариант кнопкой ниже 👇')
        data = self.db.state_data(user)
        self.db.upsert_preference(user['id'], data['guest_name'], data['food'], drink)
        self._set_idle(user)
        self.show_menu(user, f"Для гостя {data['guest_name']}: {data['food']}, {drink}. Мы постараемся учесть это 🥂\n"
                             f"Добавить ещё гостя — снова «{BTN_PREFS}».")

    # ================= каналы для гостей: ведущий, организатор =================

    def enter_guest_channel(self, user: Dict, channel_id: str):
        cfg = self.channels[channel_id]
        self._set_state(user, 'in_channel', channel=f'guest:{channel_id}')
        self.send(user, f"{cfg['intro']}\n\nОтвет придёт сюда же. Вернуться — «{BTN_MENU}».", [[BTN_MENU]])

    def _broadcast_channel(self, user: Dict) -> Optional[str]:
        for cid, cfg in self.channels.items():
            if user['role'] in cfg.get('broadcast_roles', []):
                return cid
        return None

    def enter_guest_broadcast(self, user: Dict):
        cid = self._broadcast_channel(user)
        count = len(self.db.guests())
        self._set_state(user, 'in_channel', channel=f'guest:{cid}')
        self.send(user, f'📢 Сообщение отсюда уйдёт всем гостям ({count}), перед отправкой я попрошу подтвердить.\n'
                        f'Можно текст, фото или файл.\n\n'
                        f'Ответить одному гостю — ответьте (reply) на его вопрос или напишите «ответ N текст».',
                  [[BTN_MENU]])

    def _on_channel_message(self, user: Dict, inc: Incoming, text: str):
        kind, cid = self.db.state_data(user)['channel'].split(':', 1)
        if kind == 'room':
            return self._room_message(user, inc, cid)
        cfg = self.channels[cid]
        if not self.guest_view(user) and user['role'] in cfg.get('broadcast_roles', []):
            return self._ask_confirm(user, inc, kind='guest_channel', channel=cid)
        self._guest_channel_message(user, inc, cid)

    def _guest_channel_message(self, user: Dict, inc: Incoming, cid: str):
        cfg = self.channels[cid]
        channel = f'guest:{cid}'
        self.db.log_message(channel, user['id'], self._log_text(inc))
        recipients = [r for r in self.db.users_by_roles(cfg['recipients']) if r['id'] != user['id']]
        header = (f"{cfg['button'].split()[0]} {cfg['title']} · сообщение от гостя №{user['id']}\n"
                  f"👤 {user['full_name']} ({self.platform_title(user['platform'])})")
        footer = f'↩️ Ответьте на это сообщение или напишите: ответ {user["id"]} текст'
        for r in recipients:
            for mid in self._deliver(r, header, inc, footer):
                self.db.save_relay(r['platform'], r['chat_id'], mid, channel, r['id'], target_user_id=user['id'])
        if not recipients:
            self.send(user, f"Сообщение сохранено. {cfg['title']} пока не подключился к боту — "
                            f"организатор увидит сообщение в выгрузке.")
        elif not self.db.state_data(user).get('acked'):
            self.send(user, '✅ Передано. Ответ придёт сюда.')
            if user['state'] == 'in_channel':
                self._set_state(user, 'in_channel', channel=channel, acked=True)

    def _answer_guest(self, staff: Dict, guest_id: int, channel: str, inc: Incoming):
        guest = self.db.get_user(guest_id)
        if guest is None:
            return self.send(staff, f'Гость №{guest_id} не найден.')
        cid = channel.split(':', 1)[1]
        cfg = self.channels.get(cid, {'title': self.role_title(staff['role'])})
        header = f"💬 {cfg['title']} · {staff['full_name']}:"
        for mid in self._deliver(guest, header, inc, '↩️ Чтобы ответить, ответьте на это сообщение.'):
            self.db.save_relay(guest['platform'], guest['chat_id'], mid, channel, guest['id'])
        self.db.log_message(channel, staff['id'], self._log_text(inc), target_user_id=guest['id'])
        self.send(staff, f"✅ Ответ отправлен гостю {guest['full_name']}.")

    def _answer_by_number(self, staff: Dict, guest_id: int, text: str, inc: Incoming):
        channel = self.db.last_channel_with_guest(staff['id'], guest_id)
        if channel is None:
            own = [cid for cid, c in self.channels.items() if staff['role'] in c['recipients']]
            if not own:
                return self.send(staff, 'Отвечать гостям могут только те, кому они пишут.')
            channel = f'guest:{own[0]}'
        self._answer_guest(staff, guest_id, channel, replace(inc, text=text, attachments=[], native=None))

    def _route_reply(self, user: Dict, inc: Incoming, relay: Dict) -> bool:
        """Ответ (reply) на пересланное ботом сообщение уходит туда, откуда оно пришло"""
        kind, cid = relay['channel'].split(':', 1)
        if kind == 'guest' and cid in self.channels:
            if relay['target_user_id'] and self.is_staff(user) and relay['recipient_user_id'] == user['id']:
                self._answer_guest(user, relay['target_user_id'], relay['channel'], inc)
                return True
            if self.guest_view(user):
                self._guest_channel_message(user, inc, cid)
                return True
        if kind == 'room' and cid in self.rooms and user['role'] in self.rooms[cid]['roles']:
            self._room_message(user, inc, cid)
            return True
        return False

    # ================= рабочие чаты команды =================

    def user_rooms(self, user: Dict) -> List[Dict]:
        return [r for r in self.rooms.values() if user['role'] in r['roles']]

    def choose_room(self, user: Dict):
        rooms = self.user_rooms(user)
        if not rooms:
            return self.send(user, 'Для вашей роли пока нет рабочих чатов.')
        self._set_state(user, 'room_select')
        self.send(user, 'Выберите чат:', [[r['title'][:40]] for r in rooms] + [[BTN_MENU]])

    def _on_room_select(self, user: Dict, inc: Incoming, text: str):
        room = next((r for r in self.user_rooms(user) if r['title'][:40] == text), None)
        if room is None:
            return self.send(user, 'Выберите чат кнопкой ниже 👇')
        self._set_state(user, 'in_channel', channel=f"room:{room['id']}")
        members = self.db.users_by_roles(room['roles'])
        people = '\n'.join(f"• {m['full_name']} — {self.role_title(m['role'])} ({self.platform_title(m['platform'])})"
                           for m in members) or '• пока никого'
        history = self.db.messages(f"room:{room['id']}", limit=5)
        recent = ''
        if history:
            recent = '\n\nПоследние сообщения:\n' + '\n'.join(
                f"{h['sender_name']}: {(h['text'] or '')[:200]}" for h in history)
        missing = [self.role_title(r) for r in room['roles'] if not any(m['role'] == r for m in members)]
        not_joined = f"\n\nЕщё не подключились: {', '.join(missing)}." if missing else ''
        self.send(user, f"💬 Чат «{room['title']}». Участники:\n{people}{not_joined}{recent}\n\n"
                        f"Ваши сообщения получат все участники. Выйти — «{BTN_MENU}».", [[BTN_MENU]])

    def _room_message(self, user: Dict, inc: Incoming, room_id: str):
        room = self.rooms[room_id]
        if user['role'] not in room['roles']:
            return self.send(user, 'У вас нет доступа к этому чату.')
        channel = f'room:{room_id}'
        self.db.log_message(channel, user['id'], self._log_text(inc))
        header = f"💬 {room['title']} · {user['full_name']} ({self.role_title(user['role'])})"
        members = [m for m in self.db.users_by_roles(room['roles']) if m['id'] != user['id']]
        for m in members:
            for mid in self._deliver(m, header, inc):
                self.db.save_relay(m['platform'], m['chat_id'], mid, channel, m['id'])
        if not members:
            self.send(user, 'Пока в чате только вы — сообщение сохранено в истории, '
                            'остальные увидят его, когда откроют чат.')

    # ================= рассылки =================

    def start_broadcast(self, user: Dict):
        counts = {'Всем гостям': len(self.db.guests()), 'Кто придёт': len(self.db.guests('yes')),
                  'Кто не ответил': len(self.db.guests(None))}
        labels = [f'{k}' for k in AUDIENCES]
        info = ', '.join(f'{k.lower()} — {v}' for k, v in counts.items())
        self._set_state(user, 'broadcast_audience')
        self.send(user, f'Кому отправить? ({info})', [labels[:2], labels[2:4], labels[4:], [BTN_MENU]])

    def _on_broadcast_audience(self, user: Dict, inc: Incoming, text: str):
        if text not in AUDIENCES:
            return self.send(user, 'Выберите получателей кнопкой ниже 👇')
        self._set_state(user, 'broadcast_text', audience=text)
        self.send(user, f'Напишите сообщение для «{text}». Можно с фото или файлом.', [[BTN_MENU]])

    def _on_broadcast_text(self, user: Dict, inc: Incoming, text: str):
        self._ask_confirm(user, inc, kind='broadcast', audience=self.db.state_data(user)['audience'])

    def _audience_users(self, audience: str) -> List[Dict]:
        kind, rsvp = AUDIENCES[audience]
        if kind == 'guests':
            return self.db.guests(rsvp)
        if kind == 'team':
            return self.db.users_by_roles([r for r in self.roles])
        users = []
        for platform, ids in self.test_users.items():
            users += self.db.users_by_platform_ids(platform, ids)
        return users

    def _ask_confirm(self, user: Dict, inc: Incoming, **params):
        if params['kind'] == 'broadcast':
            count, whom = len(self._audience_users(params['audience'])), f"«{params['audience']}»"
        else:
            count, whom = len(self.db.guests()), 'всем гостям'
        if not inc.text and not inc.attachments:
            return self.send(user, 'Пустое сообщение — напишите текст или прикрепите файл.')
        with self.pending_lock:
            self.pending[user['id']] = (inc, params)
        previous = self.db.state_data(user)
        self._set_state(user, 'confirm_send', back_state=user['state'], back_data=previous)
        self.send(user, f'Отправить это сообщение {whom} ({count} чел.)?', [[BTN_CONFIRM, BTN_CANCEL]])

    def _on_confirm_send(self, user: Dict, inc: Incoming, text: str):
        if text not in (BTN_CONFIRM, BTN_CANCEL):
            return self.send(user, 'Нажмите «Отправить» или «Отмена» 👇')
        data = self.db.state_data(user)
        with self.pending_lock:
            item = self.pending.pop(user['id'], None)
        # Возвращаемся туда, откуда пришли: в канал ведущего или в меню
        back = data.get('back_data') or {}
        if data.get('back_state') == 'in_channel':
            self.db.set_state(user['id'], 'in_channel', back)
            keyboard = [[BTN_MENU]]
        else:
            self._set_idle(user)
            keyboard = self.menu_keyboard(user)
        if text == BTN_CANCEL:
            return self.send(user, 'Отменено.', keyboard)
        if item is None:
            return self.send(user, 'Сообщение устарело — отправьте его ещё раз.', keyboard)
        pending_inc, params = item
        if params['kind'] == 'broadcast':
            recipients = self._audience_users(params['audience'])
            header, channel = f"📨 {self.event['event'].get('title', 'Сообщение')}", 'guest:feedback'
        else:
            cfg = self.channels[params['channel']]
            recipients = self.db.guests()
            header, channel = f"📢 {cfg['title']} · {user['full_name']}", f"guest:{params['channel']}"
        self.db.log_message(f"broadcast:{params.get('audience') or params.get('channel')}",
                            user['id'], self._log_text(pending_inc))
        sent = 0
        for r in recipients:
            if r['id'] == user['id']:
                continue
            ids = self._deliver(r, header, pending_inc)
            if ids:
                sent += 1
                # ответ гостя на рассылку попадёт в канал обратной связи
                if r['role'] == 'guest' and channel.split(':', 1)[1] in self.channels:
                    for mid in ids:
                        self.db.save_relay(r['platform'], r['chat_id'], mid, channel, r['id'])
            time.sleep(SEND_DELAY)
        failed = len([r for r in recipients if r['id'] != user['id']]) - sent
        self.send(user, f'✅ Отправлено: {sent}' + (f', не доставлено: {failed} (заблокировали бота)' if failed else ''),
                  keyboard)

    # ================= выгрузка и сводка =================

    def send_export(self, user: Dict):
        data = build_excel(self.db, self)
        self.messengers[user['platform']].send_document(user['chat_id'], data, 'guests.xlsx', '📋 Выгрузка гостей')

    def send_summary(self, user: Dict):
        guests = self.db.guests()
        by = {k: [g for g in guests if g['rsvp'] == k] for k in ('yes', 'no', 'maybe', None)}
        people = sum(g['party_size'] or 1 for g in by['yes'])
        prefs = self.db.preferences()
        food, drink = {}, {}
        for p in prefs:
            food[p['food']] = food.get(p['food'], 0) + 1
            drink[p['drink']] = drink.get(p['drink'], 0) + 1
        fmt = lambda d: '\n'.join(f'   {k}: {v}' for k, v in sorted(d.items(), key=lambda x: -x[1])) or '   —'
        team = self.db.users_by_roles([r for r in self.roles])
        missing = [self.role_title(r) for r in self.roles if r != 'organizer' and not any(t['role'] == r for t in team)]
        text = (f"📊 Сводка\n\nЗарегистрировано гостей: {len(guests)}\n"
                f"✅ Придут: {len(by['yes'])} (всего человек: {people})\n"
                f"❌ Не придут: {len(by['no'])}\n🤔 Не знают: {len(by['maybe'])}\n"
                f"⏳ Не ответили: {len(by[None])}\n\n"
                f"🍽 Пожелания заполнены на {len(prefs)} чел.\nЕда:\n{fmt(food)}\nНапитки:\n{fmt(drink)}\n\n"
                f"👥 Команда: {len(team)}" + (f"\nЕщё не подключились: {', '.join(missing)}" if missing else ''))
        self.send(user, text)

    # ================= доставка =================

    def _deliver(self, recipient: Dict, header: str, inc: Incoming, footer: str = '') -> List[str]:
        """Переслать сообщение получателю в его мессенджере. Возвращает id отправленных сообщений."""
        m = self.messengers.get(recipient['platform'])
        if m is None:
            return []
        chat = recipient['chat_id']
        ids = []
        try:
            if inc.attachments and inc.native is not None and inc.platform == recipient['platform']:
                ids.append(m.send(chat, header + (f'\n\n{footer}' if footer else '')))
                copied = m.copy_native(inc, chat)
                if copied is not None:
                    return [i for i in ids + [copied] if i is not None]
                if inc.text:  # копирование не удалось — отправим подпись и вложения по отдельности
                    ids.append(m.send(chat, inc.text))
            else:
                body = header + (f'\n{inc.text}' if inc.text else '') + (f'\n\n{footer}' if footer else '')
                ids.append(m.send(chat, body))
            for a in inc.attachments:
                if a.kind == 'photo':
                    ids.append(m.send_photo(chat, a.data()))
                elif a.kind == 'document':
                    ids.append(m.send_document(chat, a.data(), a.filename))
                else:
                    ids.append(m.send(chat, f'[{a.description or "вложение"} — этот тип нельзя переслать в {m.title}, '
                                            f'попросите отправить фото или файлом]'))
        except Exception as e:
            logger.error(f"Delivery to {recipient['platform']}:{chat} failed: {e}")
        return [i for i in ids if i is not None]

    @staticmethod
    def _log_text(inc: Incoming) -> str:
        extra = ', '.join(a.description or a.kind for a in inc.attachments)
        return (inc.text or '') + (f' [{extra}]' if extra else '')

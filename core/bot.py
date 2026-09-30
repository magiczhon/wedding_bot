"""Логика бота, общая для всех мессенджеров и всех мероприятий.

Один бот обслуживает много свадеб. Человек попадает в мероприятие по ссылке или коду:
код гостя открывает приглашение, код роли добавляет в команду. У каждого человека есть
«текущее мероприятие», а если он участвует в нескольких (например, ведущий на трёх свадьбах),
то переключается между ними кнопкой «Мои мероприятия».

Перед тем как сохранить имя и ответы, бот просит согласие на обработку персональных данных;
отозвать его и удалить данные можно командой /delete или через «Мои данные».
"""
import re
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

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
BTN_EVENTS = '🔄 Мои мероприятия'
BTN_PRIVACY = '🔒 Мои данные'
BTN_DELETE_ME = '🗑 Удалить мои данные'
BTN_DELETE_YES = '🗑 Да, удалить всё'
BTN_AGREE = '✅ Даю согласие'
BTN_DISAGREE = '❌ Отказаться'

RSVP_YES, RSVP_NO, RSVP_MAYBE = '✅ Приду', '❌ Не смогу', '🤔 Пока не знаю'
RSVP_LABELS = {'yes': 'придёт', 'no': 'не придёт', 'maybe': 'пока не знает', None: 'не ответил'}
BTN_CONFIRM, BTN_CANCEL = '✅ Отправить', '❌ Отмена'
BTN_SKIP = 'Пропустить'
FOR_ME_PREFIX = 'Для меня: '

RESERVED_BUTTONS = {v for k, v in globals().items() if k.startswith(('BTN_', 'RSVP_Y', 'RSVP_N', 'RSVP_M'))}

# Получатели рассылки: подпись → (кого, фильтр по ответу)
AUDIENCES = {
    'Всем гостям': ('guests', 'any'),
    'Кто придёт': ('guests', 'yes'),
    'Кто не ответил': ('guests', None),
    'Всей команде': ('team', None),
    'Себе (проверка)': ('self', None),
}
WEB_AUDIENCES = {
    'Всем гостям': ('guests', 'any'),
    'Кто придёт': ('guests', 'yes'),
    'Кто не ответил': ('guests', None),
    'Всей команде': ('team', None),
    'Организаторам (проверка)': ('organizers', None),
}

START_WORDS = {'/start', 'начать', 'start'}
ANSWER_RE = re.compile(r'^(?:/reply|/ответ|ответ)\s+(\d+)\s+(.+)$', re.S | re.I)
CODE_RE = re.compile(r'^(?:/join|/code|код|code)[\s_:]*([A-Za-z0-9]{4,12})$', re.I)
SEND_DELAY = 0.05  # пауза между сообщениями рассылки, чтобы не упереться в лимиты мессенджеров

PRIVACY_DEFAULTS = {
    'version': '1',
    'operator': 'владелец сервиса (укажите privacy.operator в config.json)',
    'contacts': '',
    'retention': 'до окончания мероприятия и 3 месяца после него',
    'policy_url': '',
}


class WeddingBot:
    def __init__(self, db: Database, privacy: Optional[Dict] = None, public_url: str = ''):
        self.db = db
        self.public_url = public_url.rstrip('/')
        self.privacy = {**PRIVACY_DEFAULTS, **{k: v for k, v in (privacy or {}).items() if v}}
        self.privacy['version'] = str(self.privacy['version'])
        if not self.privacy['policy_url'] and self.public_url:
            self.privacy['policy_url'] = f'{self.public_url}/privacy'
        self.messengers: Dict[str, Messenger] = {}
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
        user = self.db.get_or_create_user(inc.platform, inc.user_id, inc.chat_id, inc.username, inc.display_name)
        text = (inc.text or '').strip()
        low = text.lower()
        u = self._ctx(user, user['current_event_id'])

        # Сценарии, которые не зависят от мероприятия
        if user['state'] == 'consent':
            return self._on_consent(u, text)
        if user['state'] == 'delete_confirm':
            return self._on_delete_confirm(u, text)
        if user['state'] == 'event_select':
            return self._on_event_select(u, text)
        if low in ('/delete', '/forget', 'удалить мои данные') or text == BTN_DELETE_ME:
            return self.ask_delete(u)
        code = self._extract_code(inc, text)
        if code:
            return self._enter_code(u, code)
        if text == BTN_EVENTS:
            return self.choose_event(u)

        if u['event'] is None:
            return self._no_event(u)
        if u['event']['status'] != 'active':
            return self._archived(u, text)
        if not self._has_consent(u):
            return self.ask_consent(u)
        if text == BTN_PRIVACY or low in ('/privacy', '/mydata'):
            return self.show_my_data(u)

        if low in START_WORDS:
            if u['full_name']:
                self._set_idle(u)
                return self.show_menu(u, f"Рады вас снова видеть! 🥂\n{u['event']['title']}")
            return self._ask_name(u, self._welcome(u))
        if not u['full_name']:
            if user['state'] == 'ask_name':
                return self._on_name(u, inc, text)
            return self._ask_name(u, self._welcome(u))

        if text in (BTN_MENU, '/menu') or low == 'меню':
            self._drop_pending(u)
            self._set_idle(u)
            return self.show_menu(u, 'Главное меню')

        if inc.reply_to_id:
            relay = self.db.find_relay(inc.platform, inc.chat_id, inc.reply_to_id)
            if relay and self._route_reply(user, inc, relay):
                return

        answer = ANSWER_RE.match(text)
        if answer and self.is_staff(u):
            return self._answer_by_number(user, u, int(answer.group(1)), answer.group(2), inc)

        handler = self.states.get(user['state'] or '')
        if handler:
            return handler(u, inc, text)

        action = self._menu_actions(u).get(text)
        if action:
            return action(u)
        self.show_menu(u, 'Не понял 🙂 Выберите пункт меню.')

    def _ctx(self, user: Dict, event_id: Optional[int]) -> Dict:
        """Пользователь + его участие в мероприятии event_id (роль, ответ) + само мероприятие"""
        u = dict(user)
        event = self.db.get_event(event_id) if event_id else None
        member = self.db.get_member(event_id, user['id']) if event else None
        if member is None:
            event = None
        u.update(event=event, event_id=event['id'] if event else None,
                 role=member['role'] if member else None,
                 rsvp=member['rsvp'] if member else None,
                 party_size=member['party_size'] if member else None)
        return u

    def _reload(self, u: Dict) -> Dict:
        return self._ctx(self.db.get_user(u['id']), u['event_id'])

    # ================= настройки мероприятия =================

    @staticmethod
    def cfg(u: Dict) -> Dict:
        return u['event']['config']

    def roles(self, u: Dict) -> Dict[str, str]:
        return self.cfg(u).get('roles', {})

    def channels(self, u: Dict) -> Dict[str, Dict]:
        return self.cfg(u).get('guest_channels') or {}

    def rooms(self, u: Dict) -> Dict[str, Dict]:
        return {r['id']: r for r in self.cfg(u).get('rooms') or []}

    def _welcome(self, u: Dict) -> str:
        ev = self.cfg(u).get('event', {})
        return ev.get('welcome') or f"Добро пожаловать: {u['event']['title']}! 🎉"

    # ================= вспомогательное =================

    def is_staff(self, u: Dict) -> bool:
        return u.get('role') not in (None, 'guest')

    def guest_view(self, u: Dict) -> bool:
        return u['role'] == 'guest' or self.db.state_data(u).get('guest_view', False)

    def role_title(self, u: Dict, role: str) -> str:
        return 'Гость' if role == 'guest' else self.roles(u).get(role, role)

    def platform_title(self, platform: str) -> str:
        m = self.messengers.get(platform)
        return m.title if m else platform

    def send(self, user: Dict, text: str, keyboard: Optional[Keyboard] = None) -> Optional[str]:
        m = self.messengers.get(user['platform'])
        if m is None:
            logger.warning(f"No messenger for platform {user['platform']}")
            return None
        return m.send(user['chat_id'], text, keyboard)

    def _set_idle(self, u: Dict):
        """Сброс сценария с сохранением выбранного вида меню (гостя или команды)"""
        data = {'guest_view': True} if self.db.state_data(u).get('guest_view') else None
        self.db.set_state(u['id'], None, data)
        u['state'], u['state_data'] = None, ('{"guest_view": true}' if data else None)

    def _set_state(self, u: Dict, state: str, **data):
        if self.db.state_data(u).get('guest_view'):
            data['guest_view'] = True
        self.db.set_state(u['id'], state, data)

    def _drop_pending(self, u: Dict):
        with self.pending_lock:
            self.pending.pop(u['id'], None)

    def _tag(self, recipient: Dict, event: Dict) -> str:
        """Подпись мероприятия для тех, кто участвует сразу в нескольких"""
        return f"[{event['title']}] " if len(self.db.user_events(recipient['id'])) > 1 else ''

    @staticmethod
    def _extract_code(inc: Incoming, text: str) -> Optional[str]:
        if inc.start_payload:
            payload = inc.start_payload.strip()
            return re.sub(r'^(ev|join)_', '', payload, flags=re.I)
        m = CODE_RE.match(text)
        return m.group(1) if m else None

    # ================= мероприятия: вход по коду, выбор, архив =================

    def _enter_code(self, u: Dict, code: str):
        found = self.db.resolve_code(code)
        if found is None:
            return self.send(u, 'Код не найден 🤔 Проверьте его в приглашении или спросите у организатора.')
        kind, event, role = found
        if event['status'] != 'active':
            return self.send(u, f"Мероприятие «{event['title']}» уже завершено. Спасибо! 💐")
        role = role or 'guest'
        member = self.db.get_member(event['id'], u['id'])
        if member is None and role == 'guest':
            limit = event['guest_limit']
            if limit is not None and self.db.count_guests(event['id']) >= limit:
                logger.warning(f"Guest limit {limit} reached for event {event['id']}")
                return self.send(u, f"Регистрация на «{event['title']}» закрыта: достигнут лимит гостей. "
                                    f"Пожалуйста, свяжитесь с организатором.")
        new_role = role != 'guest' and (member is None or member['role'] not in (role, 'organizer'))
        if member is None or role != 'guest':
            self.db.add_member(event['id'], u['id'], role)
        self.db.update_user(u['id'], current_event_id=event['id'])
        self.db.set_state(u['id'], None)
        u = self._ctx(self.db.get_user(u['id']), event['id'])
        if role != 'guest':
            intro = f"Вы присоединяетесь к команде мероприятия «{event['title']}»: {self.role_title(u, u['role'])}."
        else:
            intro = self._welcome(u)
        self._open_event(u, intro, notify_join=new_role)

    def _open_event(self, u: Dict, intro: str, notify_join: bool = False):
        """Показать мероприятие: согласие → имя → (гостю) вопрос о присутствии → меню"""
        if not self._has_consent(u):
            return self.ask_consent(u, intro=intro, notify_join=notify_join)
        if not u['full_name']:
            return self._ask_name(u, intro, notify_join=notify_join)
        if notify_join:
            self._notify_join(u)
        if self.is_staff(u):
            return self.show_menu(u, f"{intro}\nРабочие чаты — в меню «{BTN_ROOMS}».")
        if u['rsvp'] is None:
            self.send(u, intro)
            return self.ask_rsvp(u)
        self.show_menu(u, intro)

    def _no_event(self, u: Dict):
        events = [e for e in self.db.user_events(u['id']) if e['status'] == 'active']
        if len(events) == 1:
            self.db.update_user(u['id'], current_event_id=events[0]['id'])
            u = self._ctx(self.db.get_user(u['id']), events[0]['id'])
            return self._open_event(u, events[0]['title'])
        if len(events) > 1:
            return self.choose_event(u)
        self.send(u, 'Здравствуйте! 👋 Чтобы открыть приглашение, перейдите по ссылке из него '
                     'или пришлите код мероприятия, например: код ABC123', [])

    def choose_event(self, u: Dict):
        events = [e for e in self.db.user_events(u['id']) if e['status'] == 'active']
        if not events:
            return self._no_event(u)
        labels, seen = {}, set()
        for e in events:
            label = e['title'][:40]
            if label in seen:
                label = f"{e['title'][:30]} · {e['code']}"
            seen.add(label)
            labels[label] = e['id']
        self.db.set_state(u['id'], 'event_select', {'labels': labels})
        self.send(u, 'Выберите мероприятие:', [[label] for label in labels])

    def _on_event_select(self, u: Dict, text: str):
        event_id = self.db.state_data(u).get('labels', {}).get(text)
        if event_id is None:
            return self.send(u, 'Выберите мероприятие кнопкой ниже 👇')
        self.db.update_user(u['id'], current_event_id=event_id)
        self.db.set_state(u['id'], None)
        u = self._ctx(self.db.get_user(u['id']), event_id)
        if u['event'] is None:
            return self._no_event(u)
        self._open_event(u, f"Мероприятие «{u['event']['title']}»")

    def _archived(self, u: Dict, text: str):
        if text == BTN_PRIVACY:
            return self.show_my_data(u)
        others = [e for e in self.db.user_events(u['id']) if e['status'] == 'active']
        keyboard = ([[BTN_EVENTS]] if others else []) + [[BTN_PRIVACY]]
        self.send(u, f"Мероприятие «{u['event']['title']}» завершено. Спасибо, что были с нами! 💐", keyboard)

    # ================= согласие на обработку персональных данных =================

    def _has_consent(self, u: Dict) -> bool:
        return u.get('consent_version') == self.privacy['version']

    def consent_text(self, u: Dict) -> str:
        p = self.privacy
        messenger = self.platform_title(u['platform'])
        title = u['event']['title'] if u.get('event') else 'мероприятия'
        lines = [
            'Прежде чем продолжить, нам нужно ваше согласие на обработку персональных данных.',
            '',
            f'Что сохраняем: имя и фамилию, ваш аккаунт в {messenger}, ответ на приглашение, '
            f'пожелания по меню и сообщения, которые вы отправляете через бота.',
            f'Зачем: чтобы организаторы мероприятия «{title}» учли ваши ответы и могли связаться с вами.',
            f"Кто обрабатывает: {p['operator']}." + (f" Контакты: {p['contacts']}." if p['contacts'] else ''),
            f"Срок хранения: {p['retention']}.",
        ]
        if p['policy_url']:
            lines.append(f"Политика обработки данных: {p['policy_url']}")
        lines += ['', 'Отозвать согласие и удалить данные можно в любой момент командой /delete '
                      f'или кнопкой «{BTN_PRIVACY}».']
        return '\n'.join(lines)

    def ask_consent(self, u: Dict, intro: str = '', notify_join: bool = False):
        self.db.set_state(u['id'], 'consent', {'intro': intro, 'notify_join': notify_join})
        text = (intro + '\n\n' if intro else '') + self.consent_text(u)
        self.send(u, text, [[BTN_AGREE], [BTN_DISAGREE]])

    def _on_consent(self, u: Dict, text: str):
        data = self.db.state_data(u)
        if text == BTN_AGREE:
            self.db.update_user(u['id'], consent_version=self.privacy['version'], consent_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            self.db.log_consent(u, 'given', self.privacy['version'], u['event_id'])
            self.db.set_state(u['id'], None)
            u = self._reload(u)
            if u['event'] is None:
                return self._no_event(u)
            return self._open_event(u, data.get('intro') or u['event']['title'], data.get('notify_join', False))
        if text == BTN_DISAGREE:
            self.db.log_consent(u, 'declined', self.privacy['version'], u['event_id'])
            self.db.delete_user_data(u['id'])
            return self.send(u, 'Понимаем. Без согласия бот не может сохранять ваши ответы, поэтому ничего не сохранено. '
                                'Если передумаете — снова откройте ссылку из приглашения. '
                                'Связаться с организаторами можно и напрямую.', [])
        self.send(u, 'Пожалуйста, выберите вариант кнопкой ниже 👇', [[BTN_AGREE], [BTN_DISAGREE]])

    def show_my_data(self, u: Dict):
        events = self.db.user_events(u['id'])
        lines = ['🔒 Что о вас хранится:', f"• Имя: {u['full_name'] or 'не указано'}",
                 f"• Аккаунт: {self.platform_title(u['platform'])}" + (f" (@{u['username']})" if u['username'] else '')]
        for e in events:
            prefs = len(self.db.preferences(e['id'], u['id']))
            role = 'гость' if e['role'] == 'guest' else 'команда'
            lines.append(f"• «{e['title']}» — {role}" + (f", пожеланий по меню: {prefs}" if prefs else ''))
        if u.get('consent_at'):
            lines.append(f"• Согласие дано: {str(u['consent_at'])[:16]}")
        if self.privacy['policy_url']:
            lines.append(f"\nПолитика обработки данных: {self.privacy['policy_url']}")
        lines.append('\nЧтобы отозвать согласие и удалить всё, нажмите кнопку ниже.')
        self.send(u, '\n'.join(lines), [[BTN_DELETE_ME], [BTN_MENU]])

    def ask_delete(self, u: Dict):
        self.db.set_state(u['id'], 'delete_confirm', {'back': u['state']})
        self.send(u, 'Удалить все ваши данные — имя, ответы, пожелания и сообщения — во всех мероприятиях? '
                     'Отменить это будет нельзя.', [[BTN_DELETE_YES], [BTN_CANCEL]])

    def _on_delete_confirm(self, u: Dict, text: str):
        if text == BTN_DELETE_YES:
            self.db.log_consent(u, 'withdrawn', self.privacy['version'], u['event_id'])
            self.db.delete_user_data(u['id'])
            self._drop_pending(u)
            return self.send(u, 'Готово: ваши данные удалены, согласие отозвано. '
                                'Чтобы снова участвовать, откройте ссылку из приглашения.', [])
        self.db.set_state(u['id'], None)
        u = self._reload(u)
        if u['event'] is None or not u['full_name']:
            return self.send(u, 'Хорошо, ничего не удаляем.', [])
        self.show_menu(u, 'Хорошо, ничего не удаляем.')

    # ================= меню =================

    def menu_keyboard(self, u: Dict) -> Keyboard:
        multi = len([e for e in self.db.user_events(u['id']) if e['status'] == 'active']) > 1
        last = [BTN_PRIVACY] + ([BTN_EVENTS] if multi else [])
        if self.guest_view(u):
            rows = [[BTN_RSVP], [BTN_PREFS, BTN_DRESS]]
            rows.append([BTN_PLACE, BTN_PROGRAM] if self.cfg(u).get('event', {}).get('program') else [BTN_PLACE])
            buttons = [c['button'] for c in self.channels(u).values()]
            rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            if self.is_staff(u):
                last.append(BTN_STAFF_VIEW)
            return rows + [last]
        rows = [[BTN_ROOMS]]
        if self._broadcast_channel(u):
            rows[0].append(BTN_WRITE_GUESTS)
        if u['role'] in self.cfg(u).get('export_roles', ['organizer']):
            rows.append([BTN_EXPORT, BTN_SUMMARY])
        if u['role'] == 'organizer':
            rows.append([BTN_BROADCAST, BTN_INVITES])
        rows.append([BTN_PLACE, BTN_GUEST_VIEW])
        return rows + [last]

    def show_menu(self, u: Dict, text: str):
        self.send(u, text, self.menu_keyboard(u))

    def _menu_actions(self, u: Dict) -> Dict[str, Callable]:
        actions = {BTN_PLACE: self.show_place}
        if self.guest_view(u):
            actions.update({BTN_RSVP: self.ask_rsvp, BTN_PREFS: self.start_preferences,
                            BTN_DRESS: self.show_dresscode, BTN_PROGRAM: self.show_program})
            for cid, c in self.channels(u).items():
                actions[c['button']] = lambda x, cid=cid: self.enter_guest_channel(x, cid)
            if self.is_staff(u):
                actions[BTN_STAFF_VIEW] = self.switch_to_staff_view
        else:
            actions.update({BTN_ROOMS: self.choose_room, BTN_GUEST_VIEW: self.switch_to_guest_view})
            if self._broadcast_channel(u):
                actions[BTN_WRITE_GUESTS] = self.enter_guest_broadcast
            if u['role'] in self.cfg(u).get('export_roles', ['organizer']):
                actions.update({BTN_EXPORT: self.send_export, BTN_SUMMARY: self.send_summary})
            if u['role'] == 'organizer':
                actions.update({BTN_BROADCAST: self.start_broadcast, BTN_INVITES: self.show_invites})
        return actions

    def switch_to_guest_view(self, u: Dict):
        self.db.set_state(u['id'], None, {'guest_view': True})
        u['state_data'] = '{"guest_view": true}'
        self.show_menu(u, 'Так бот выглядит для гостей. Вернуться — «Меню команды».')

    def switch_to_staff_view(self, u: Dict):
        self.db.set_state(u['id'], None)
        u['state_data'] = None
        self.show_menu(u, 'Меню команды')

    # ================= регистрация и команда =================

    def _ask_name(self, u: Dict, intro: str, notify_join: bool = False):
        self._set_state(u, 'ask_name', notify_join=notify_join)
        self.send(u, f'{intro}\n\nПожалуйста, напишите ваше имя и фамилию:', [])

    def _on_name(self, u: Dict, inc: Incoming, text: str):
        if len(text.split()) < 2 or len(text) > 100:
            return self.send(u, 'Пожалуйста, напишите имя И фамилию (можно и отчество):')
        notify_join = self.db.state_data(u).get('notify_join', False)
        self.db.update_user(u['id'], full_name=text)
        u['full_name'] = text
        self._set_idle(u)
        logger.info(f"Registered {u['platform']}:{u['platform_user_id']} in event {u['event_id']}, role={u['role']}")
        if self.is_staff(u):
            if notify_join:
                self._notify_join(u)
            return self.show_menu(u, f"Спасибо, {text}! Вы в команде «{u['event']['title']}»: "
                                     f"{self.role_title(u, u['role'])}.\nРабочие чаты — в меню «{BTN_ROOMS}».")
        self.send(u, f'Спасибо, {text}! Вы зарегистрированы 🎉')
        if u['rsvp'] is None:
            return self.ask_rsvp(u)
        self.show_menu(u, 'Главное меню')

    def _notify_join(self, u: Dict):
        for org in self.db.members(u['event_id'], roles=['organizer']):
            if org['id'] != u['id']:
                self.send(org, f"{self._tag(org, u['event'])}👋 {u['full_name']} присоединился к команде: "
                               f"{self.role_title(u, u['role'])} ({self.platform_title(u['platform'])})")

    def invite_links(self, event: Dict) -> List[Tuple[str, str, str, List[str]]]:
        """[(роль, название роли, код, [как присоединиться в каждом мессенджере])]"""
        roles = event['config'].get('roles', {})
        codes = self.db.invite_codes(event['id'], roles)
        return [(role, title, codes[role], [f'{m.title}: {m.invite_hint(codes[role])}' for m in self.messengers.values()])
                for role, title in roles.items()]

    def guest_links(self, event: Dict) -> List[str]:
        """Как гостям открыть приглашение в каждом мессенджере"""
        return [f"{m.title}: {m.guest_hint(event['code'])}" for m in self.messengers.values()]

    def show_invites(self, u: Dict):
        lines = [f"🔑 Приглашения на «{u['event']['title']}»\n", 'Гостям:']
        lines += [f'   • {hint}' for hint in self.guest_links(u['event'])]
        lines.append('\nКоманде — отправьте человеку код его роли:')
        for role, title, code, hints in self.invite_links(u['event']):
            lines.append(f'\n{title} — код {code}')
            lines += [f'   • {hint}' for hint in hints]
        self.send(u, '\n'.join(lines))

    # ================= информация =================

    def show_place(self, u: Dict):
        ev = self.cfg(u).get('event', {})
        place = ev.get('place') or {}
        text = f"📍 {place.get('name', '')}\n{place.get('address', '')}\n🕓 {ev.get('date_time', '')}"
        if place.get('how_to_get'):
            text += f"\n\n{place['how_to_get']}"
        self.send(u, text.strip())
        if place.get('lat') is not None and place.get('lon') is not None:
            self.messengers[u['platform']].send_location(u['chat_id'], place['lat'], place['lon'])

    def show_dresscode(self, u: Dict):
        self.send(u, self.cfg(u).get('event', {}).get('dresscode') or 'Дресс-код не указан')

    def show_program(self, u: Dict):
        self.send(u, '📅 Программа\n\n' + (self.cfg(u).get('event', {}).get('program') or 'Программа появится позже'))

    # ================= подтверждение присутствия =================

    def ask_rsvp(self, u: Dict):
        cfg, ev = self.cfg(u).get('rsvp', {}), self.cfg(u).get('event', {})
        current = f"\nСейчас отмечено: {RSVP_LABELS[u.get('rsvp')]}." if u.get('rsvp') else ''
        deadline = f"\nПросим ответить {ev['rsvp_deadline']}." if ev.get('rsvp_deadline') else ''
        self._set_state(u, 'rsvp')
        self.send(u, f"{cfg.get('question', 'Сможете ли вы прийти?')}{current}{deadline}",
                  [[RSVP_YES, RSVP_NO], [RSVP_MAYBE], [BTN_MENU]])

    def _on_rsvp(self, u: Dict, inc: Incoming, text: str):
        answer = {RSVP_YES: 'yes', RSVP_NO: 'no', RSVP_MAYBE: 'maybe'}.get(text)
        if answer is None:
            return self.send(u, 'Выберите вариант кнопкой ниже 👇')
        if answer == 'yes':
            self.db.update_member(u['event_id'], u['id'], rsvp='yes')
            max_size = int(self.cfg(u).get('rsvp', {}).get('max_party_size', 5))
            numbers = [str(i) for i in range(1, max_size + 1)]
            self._set_state(u, 'party_size')
            return self.send(u, 'Ура! 🎉 Сколько вас будет, включая вас?',
                             [numbers[i:i + 5] for i in range(0, len(numbers), 5)] + [[BTN_MENU]])
        self.db.update_member(u['event_id'], u['id'], rsvp=answer, party_size=0 if answer == 'no' else None)
        u['rsvp'] = answer
        self._set_idle(u)
        self.show_menu(u, 'Очень жаль, что не получится 😔 Спасибо, что предупредили!' if answer == 'no'
                       else 'Хорошо! Ответить можно в любой момент кнопкой «Подтвердить присутствие».')

    def _on_party_size(self, u: Dict, inc: Incoming, text: str):
        max_size = int(self.cfg(u).get('rsvp', {}).get('max_party_size', 5))
        if not text.isdigit() or not 1 <= int(text) <= max_size:
            return self.send(u, f'Выберите число от 1 до {max_size} 👇')
        self.db.update_member(u['event_id'], u['id'], party_size=int(text))
        self._set_idle(u)
        self.show_menu(u, f'Спасибо! Ждём вас ({text} чел.) 🥂\n'
                          f'Не забудьте указать пожелания по меню — кнопка «{BTN_PREFS}».')

    # ================= пожелания по меню =================

    def start_preferences(self, u: Dict):
        existing = self.db.preferences(u['event_id'], u['id'])
        text = 'Для кого заполняем пожелания? Напишите имя и фамилию гостя или выберите себя.'
        if existing:
            text = ('Уже указано:\n' + '\n'.join(f"• {p['guest_name']}: {p['food']}, {p['drink']}" for p in existing)
                    + '\n\nЧтобы изменить — выберите того же гостя ещё раз.\n\n' + text)
        me = (FOR_ME_PREFIX + u['full_name'])[:40]
        others = [[p['guest_name'][:40]] for p in existing if p['guest_name'] != u['full_name']][:5]
        self._set_state(u, 'pref_name')
        self.send(u, text, [[me]] + others + [[BTN_MENU]])

    def _on_pref_name(self, u: Dict, inc: Incoming, text: str):
        name = u['full_name'] if text.startswith(FOR_ME_PREFIX) else text
        if len(name) < 2 or len(name) > 100:
            return self.send(u, 'Напишите имя и фамилию гостя:')
        self._ask_option(u, 'pref_food', 'food', guest_name=name)

    def _ask_option(self, u: Dict, state: str, key: str, **data):
        cfg = self.cfg(u)['menu'][key]
        options = list(cfg['options'])
        self._set_state(u, state, **data)
        self.send(u, cfg.get('question', 'Выберите вариант'),
                  [options[i:i + 2] for i in range(0, len(options), 2)] + [[BTN_SKIP]])

    def _option_value(self, u: Dict, key: str, text: str) -> Optional[str]:
        if text == BTN_SKIP:
            return 'не указано'
        return text if text in self.cfg(u)['menu'][key]['options'] else None

    def _on_pref_food(self, u: Dict, inc: Incoming, text: str):
        food = self._option_value(u, 'food', text)
        if food is None:
            return self.send(u, 'Выберите вариант кнопкой ниже 👇')
        data = self.db.state_data(u)
        self._ask_option(u, 'pref_drink', 'drink', guest_name=data['guest_name'], food=food)

    def _on_pref_drink(self, u: Dict, inc: Incoming, text: str):
        drink = self._option_value(u, 'drink', text)
        if drink is None:
            return self.send(u, 'Выберите вариант кнопкой ниже 👇')
        data = self.db.state_data(u)
        self.db.upsert_preference(u['event_id'], u['id'], data['guest_name'], data['food'], drink)
        self._set_idle(u)
        self.show_menu(u, f"Для гостя {data['guest_name']}: {data['food']}, {drink}. Мы постараемся учесть это 🥂\n"
                          f"Добавить ещё гостя — снова «{BTN_PREFS}».")

    # ================= каналы для гостей: ведущий, организатор =================

    def enter_guest_channel(self, u: Dict, channel_id: str):
        cfg = self.channels(u)[channel_id]
        self._set_state(u, 'in_channel', channel=f'guest:{channel_id}')
        self.send(u, f"{cfg['intro']}\n\nОтвет придёт сюда же. Вернуться — «{BTN_MENU}».", [[BTN_MENU]])

    def _broadcast_channel(self, u: Dict) -> Optional[str]:
        for cid, cfg in self.channels(u).items():
            if u['role'] in cfg.get('broadcast_roles', []):
                return cid
        return None

    def _feedback_channel(self, u: Dict) -> Optional[str]:
        """Канал, в который попадают ответы гостей на рассылку организатора"""
        channels = self.channels(u)
        if 'feedback' in channels:
            return 'feedback'
        return next((cid for cid, c in channels.items() if 'organizer' in c.get('recipients', [])), None)

    def enter_guest_broadcast(self, u: Dict):
        cid = self._broadcast_channel(u)
        count = len(self.db.guests(u['event_id']))
        self._set_state(u, 'in_channel', channel=f'guest:{cid}')
        self.send(u, f'📢 Сообщение отсюда уйдёт всем гостям ({count}), перед отправкой я попрошу подтвердить.\n'
                     f'Можно текст, фото или файл.\n\n'
                     f'Ответить одному гостю — ответьте (reply) на его вопрос или напишите «ответ N текст».',
                  [[BTN_MENU]])

    def _on_channel_message(self, u: Dict, inc: Incoming, text: str):
        kind, cid = self.db.state_data(u)['channel'].split(':', 1)
        if kind == 'room':
            if cid not in self.rooms(u):
                self._set_idle(u)
                return self.show_menu(u, 'Этого чата больше нет.')
            return self._room_message(u, inc, cid)
        if cid not in self.channels(u):
            self._set_idle(u)
            return self.show_menu(u, 'Этот раздел больше не доступен.')
        if not self.guest_view(u) and u['role'] in self.channels(u)[cid].get('broadcast_roles', []):
            return self._ask_confirm(u, inc, kind='guest_channel', channel=cid)
        self._guest_channel_message(u, inc, cid)

    def _guest_channel_message(self, u: Dict, inc: Incoming, cid: str):
        cfg = self.channels(u)[cid]
        channel, event = f'guest:{cid}', u['event']
        self.db.log_message(event['id'], channel, self._log_text(inc), sender_user_id=u['id'])
        recipients = [r for r in self.db.members(event['id'], roles=cfg['recipients']) if r['id'] != u['id']]
        header = (f"{cfg['button'].split()[0]} {cfg['title']} · сообщение от гостя №{u['id']}\n"
                  f"👤 {u['full_name']} ({self.platform_title(u['platform'])})")
        footer = f'↩️ Ответьте на это сообщение или напишите: ответ {u["id"]} текст'
        for r in recipients:
            for mid in self._deliver(r, self._tag(r, event) + header, inc, footer):
                self.db.save_relay(event['id'], r['platform'], r['chat_id'], mid, channel, r['id'],
                                   target_user_id=u['id'])
        if not recipients:
            self.send(u, f"Сообщение сохранено. {cfg['title']} пока не подключился к боту — "
                         f"организатор увидит сообщение в журнале.")
        elif not self.db.state_data(u).get('acked'):
            self.send(u, '✅ Передано. Ответ придёт сюда.')
            if u['state'] == 'in_channel':
                self._set_state(u, 'in_channel', channel=channel, acked=True)

    def _answer_guest(self, staff: Dict, guest_id: int, channel: str, inc: Incoming):
        """staff — контекст сотрудника в том мероприятии, где идёт переписка"""
        event = staff['event']
        guest = self.db.get_user(guest_id)
        if guest is None or self.db.get_member(event['id'], guest_id) is None:
            return self.send(staff, f'Гость №{guest_id} не найден в «{event["title"]}».')
        cid = channel.split(':', 1)[1]
        cfg = self.channels(staff).get(cid, {'title': self.role_title(staff, staff['role'])})
        header = f"{self._tag(guest, event)}💬 {cfg['title']} · {staff['full_name']}:"
        for mid in self._deliver(guest, header, inc, '↩️ Чтобы ответить, ответьте на это сообщение.'):
            self.db.save_relay(event['id'], guest['platform'], guest['chat_id'], mid, channel, guest['id'])
        self.db.log_message(event['id'], channel, self._log_text(inc), sender_user_id=staff['id'],
                            target_user_id=guest['id'])
        self.send(staff, f"✅ Ответ отправлен гостю {guest['full_name']}.")

    def _answer_by_number(self, user: Dict, u: Dict, guest_id: int, text: str, inc: Incoming):
        inc = replace(inc, text=text, attachments=[], native=None)
        relay = self.db.last_relay_with_guest(user['id'], guest_id)
        if relay is not None:
            staff = self._ctx(user, relay['event_id'])
            if staff['event'] is not None and self.is_staff(staff):
                return self._answer_guest(staff, guest_id, relay['channel'], inc)
        own = [cid for cid, c in self.channels(u).items() if u['role'] in c.get('recipients', [])]
        if not own:
            return self.send(u, 'Отвечать гостям могут только те, кому они пишут.')
        self._answer_guest(u, guest_id, f'guest:{own[0]}', inc)

    def _route_reply(self, user: Dict, inc: Incoming, relay: Dict) -> bool:
        """Ответ (reply) на пересланное ботом сообщение уходит туда, откуда оно пришло,
        даже если сейчас у человека открыто другое мероприятие"""
        u = self._ctx(user, relay['event_id'])
        if u['event'] is None or u['event']['status'] != 'active':
            return False
        kind, cid = relay['channel'].split(':', 1)
        if kind == 'guest' and cid in self.channels(u):
            if relay['target_user_id'] and self.is_staff(u) and relay['recipient_user_id'] == u['id']:
                self._answer_guest(u, relay['target_user_id'], relay['channel'], inc)
                return True
            if u['role'] == 'guest' or self.guest_view(u):
                self._guest_channel_message(u, inc, cid)
                return True
        if kind == 'room' and cid in self.rooms(u) and u['role'] in self.rooms(u)[cid]['roles']:
            self._room_message(u, inc, cid)
            return True
        return False

    # ================= рабочие чаты команды =================

    def user_rooms(self, u: Dict) -> List[Dict]:
        return [r for r in self.rooms(u).values() if u['role'] in r['roles']]

    def choose_room(self, u: Dict):
        rooms = self.user_rooms(u)
        if not rooms:
            return self.send(u, 'Для вашей роли пока нет рабочих чатов.')
        self._set_state(u, 'room_select')
        self.send(u, 'Выберите чат:', [[r['title'][:40]] for r in rooms] + [[BTN_MENU]])

    def _on_room_select(self, u: Dict, inc: Incoming, text: str):
        room = next((r for r in self.user_rooms(u) if r['title'][:40] == text), None)
        if room is None:
            return self.send(u, 'Выберите чат кнопкой ниже 👇')
        self._set_state(u, 'in_channel', channel=f"room:{room['id']}")
        members = self.db.members(u['event_id'], roles=room['roles'])
        people = '\n'.join(f"• {m['full_name']} — {self.role_title(u, m['role'])} ({self.platform_title(m['platform'])})"
                           for m in members) or '• пока никого'
        history = self.db.messages(u['event_id'], f"room:{room['id']}", limit=5)
        recent = ''
        if history:
            recent = '\n\nПоследние сообщения:\n' + '\n'.join(
                f"{h['sender_name']}: {(h['text'] or '')[:200]}" for h in history)
        missing = [self.role_title(u, r) for r in room['roles'] if not any(m['role'] == r for m in members)]
        not_joined = f"\n\nЕщё не подключились: {', '.join(missing)}." if missing else ''
        self.send(u, f"💬 Чат «{room['title']}». Участники:\n{people}{not_joined}{recent}\n\n"
                     f"Ваши сообщения получат все участники. Выйти — «{BTN_MENU}».", [[BTN_MENU]])

    def _room_message(self, u: Dict, inc: Incoming, room_id: str):
        room, event = self.rooms(u)[room_id], u['event']
        if u['role'] not in room['roles']:
            return self.send(u, 'У вас нет доступа к этому чату.')
        channel = f'room:{room_id}'
        self.db.log_message(event['id'], channel, self._log_text(inc), sender_user_id=u['id'])
        header = f"💬 {room['title']} · {u['full_name']} ({self.role_title(u, u['role'])})"
        members = [m for m in self.db.members(event['id'], roles=room['roles']) if m['id'] != u['id']]
        for m in members:
            for mid in self._deliver(m, self._tag(m, event) + header, inc):
                self.db.save_relay(event['id'], m['platform'], m['chat_id'], mid, channel, m['id'])
        if not members:
            self.send(u, 'Пока в чате только вы — сообщение сохранено в истории, '
                         'остальные увидят его, когда откроют чат.')

    # ================= рассылки из бота =================

    def start_broadcast(self, u: Dict):
        eid = u['event_id']
        counts = {'Всем гостям': len(self.db.guests(eid)), 'Кто придёт': len(self.db.guests(eid, 'yes')),
                  'Кто не ответил': len(self.db.guests(eid, None))}
        labels = list(AUDIENCES)
        info = ', '.join(f'{k.lower()} — {v}' for k, v in counts.items())
        self._set_state(u, 'broadcast_audience')
        self.send(u, f'Кому отправить? ({info})', [labels[:2], labels[2:4], labels[4:], [BTN_MENU]])

    def _on_broadcast_audience(self, u: Dict, inc: Incoming, text: str):
        if text not in AUDIENCES:
            return self.send(u, 'Выберите получателей кнопкой ниже 👇')
        self._set_state(u, 'broadcast_text', audience=text)
        self.send(u, f'Напишите сообщение для «{text}». Можно с фото или файлом.', [[BTN_MENU]])

    def _on_broadcast_text(self, u: Dict, inc: Incoming, text: str):
        self._ask_confirm(u, inc, kind='broadcast', audience=self.db.state_data(u)['audience'])

    def audience_users(self, event_id: int, audience: str, sender: Optional[Dict] = None) -> List[Dict]:
        kind, rsvp = {**AUDIENCES, **WEB_AUDIENCES}[audience]
        if kind == 'guests':
            return self.db.guests(event_id, rsvp)
        if kind == 'team':
            return self.db.members(event_id, exclude_guests=True)
        if kind == 'organizers':
            return self.db.members(event_id, roles=['organizer'])
        return [sender] if sender else []

    def _ask_confirm(self, u: Dict, inc: Incoming, **params):
        if params['kind'] == 'broadcast':
            count = len(self.audience_users(u['event_id'], params['audience'], u))
            whom = f"«{params['audience']}»"
        else:
            count, whom = len(self.db.guests(u['event_id'])), 'всем гостям'
        if not inc.text and not inc.attachments:
            return self.send(u, 'Пустое сообщение — напишите текст или прикрепите файл.')
        params['event_id'] = u['event_id']
        with self.pending_lock:
            self.pending[u['id']] = (inc, params)
        self._set_state(u, 'confirm_send', back_state=u['state'], back_data=self.db.state_data(u))
        self.send(u, f'Отправить это сообщение {whom} ({count} чел.)?', [[BTN_CONFIRM, BTN_CANCEL]])

    def _on_confirm_send(self, u: Dict, inc: Incoming, text: str):
        if text not in (BTN_CONFIRM, BTN_CANCEL):
            return self.send(u, 'Нажмите «Отправить» или «Отмена» 👇')
        data = self.db.state_data(u)
        with self.pending_lock:
            item = self.pending.pop(u['id'], None)
        # возвращаемся туда, откуда пришли: в режим «написать гостям» или в меню
        if data.get('back_state') == 'in_channel':
            self.db.set_state(u['id'], 'in_channel', data.get('back_data') or {})
            keyboard = [[BTN_MENU]]
        else:
            self._set_idle(u)
            keyboard = self.menu_keyboard(u)
        if text == BTN_CANCEL:
            return self.send(u, 'Отменено.', keyboard)
        if item is None or item[1].get('event_id') != u['event_id']:
            return self.send(u, 'Сообщение устарело — отправьте его ещё раз.', keyboard)
        pending_inc, params = item
        event = u['event']
        if params['kind'] == 'broadcast':
            recipients = self.audience_users(event['id'], params['audience'], u)
            header, reply_channel = f"📨 {event['title']}", self._feedback_channel(u)
            log_channel = f"broadcast:{params['audience']}"
        else:
            cfg = self.channels(u)[params['channel']]
            recipients = self.db.guests(event['id'])
            header, reply_channel = f"📢 {cfg['title']} · {u['full_name']}", params['channel']
            log_channel = f"broadcast:{params['channel']}"
        self.db.log_message(event['id'], log_channel, self._log_text(pending_inc), sender_user_id=u['id'])
        sent, failed = self._send_many(event, recipients, header, pending_inc, reply_channel, skip_user_id=u['id']
                                       if params.get('audience') != 'Себе (проверка)' else None)
        self.send(u, f'✅ Отправлено: {sent}' + (f', не доставлено: {failed} (заблокировали бота)' if failed else ''),
                  keyboard)

    def _send_many(self, event: Dict, recipients: List[Dict], header: str, inc: Incoming,
                   reply_channel: Optional[str], skip_user_id: Optional[int] = None) -> Tuple[int, int]:
        sent = failed = 0
        for r in recipients:
            if r['id'] == skip_user_id:
                continue
            ids = self._deliver(r, self._tag(r, event) + header, inc)
            if not ids:
                failed += 1
                continue
            sent += 1
            # ответ гостя на рассылку попадёт в канал обратной связи
            if r.get('role') == 'guest' and reply_channel:
                for mid in ids:
                    self.db.save_relay(event['id'], r['platform'], r['chat_id'], mid, f'guest:{reply_channel}', r['id'])
            time.sleep(SEND_DELAY)
        return sent, failed

    # ================= действия из веб-интерфейса =================

    def web_broadcast(self, event_id: int, audience: str, text: str, account: Dict) -> Tuple[int, int]:
        """Рассылка текста из веб-интерфейса. Возвращает (доставлено, не доставлено)."""
        event = self.db.get_event(event_id)
        channels = event['config'].get('guest_channels') or {}
        reply_channel = 'feedback' if 'feedback' in channels else next(
            (cid for cid, c in channels.items() if 'organizer' in c.get('recipients', [])), None)
        recipients = self.audience_users(event_id, audience)
        self.db.log_message(event_id, f'broadcast:{audience}', text, sender_account_id=account['id'])
        inc = Incoming(platform='web', user_id='', chat_id='', message_id=None, text=text)
        return self._send_many(event, recipients, f"📨 {event['title']}", inc, reply_channel)

    # ================= выгрузка и сводка =================

    def send_export(self, u: Dict):
        data = build_excel(self.db, self, u['event'])
        self.messengers[u['platform']].send_document(u['chat_id'], data, 'guests.xlsx',
                                                     f"📋 Выгрузка: {u['event']['title']}")

    def send_summary(self, u: Dict):
        s = self.db.event_stats(u['event_id'])
        fmt = lambda pairs: '\n'.join(f'   {k}: {v}' for k, v in pairs) or '   —'
        missing = [self.role_title(u, r) for r in self.roles(u) if r != 'organizer' and r not in s['team_roles']]
        text = (f"📊 {u['event']['title']}\n\nЗарегистрировано гостей: {s['guests']}"
                + (f" из {u['event']['guest_limit']}" if u['event']['guest_limit'] else '') +
                f"\n✅ Придут: {s['yes']} (всего человек: {s['people']})\n❌ Не придут: {s['no']}\n"
                f"🤔 Не знают: {s['maybe']}\n⏳ Не ответили: {s['no_answer']}\n\n"
                f"🍽 Пожелания заполнены на {s['preferences']} чел.\nЕда:\n{fmt(s['food'])}\n"
                f"Напитки:\n{fmt(s['drink'])}\n\n👥 Команда: {s['team']}"
                + (f"\nЕщё не подключились: {', '.join(missing)}" if missing else ''))
        self.send(u, text)

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

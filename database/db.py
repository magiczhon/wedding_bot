"""Хранилище сервиса: SQLite, одна база на все мероприятия и мессенджеры.

- users — человек в мессенджере: пара (platform, platform_user_id), имя, согласие на обработку ПДн;
- events — мероприятие (свадьба) со своими настройками, кодом для гостей и лимитом гостей;
- members — участие человека в мероприятии: роль (гость, ведущий, декоратор…), ответ на приглашение.
  Один подрядчик может работать на нескольких свадьбах, у него будет несколько записей members;
- accounts — учётные записи веб-интерфейса (владелец сервиса и организаторы).
"""
import json
import secrets
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from utils.logger import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT,
    is_superadmin INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,            -- код для гостей: t.me/<бот>?start=ev_<код> или «код <код>»
    title TEXT NOT NULL,
    config TEXT NOT NULL,                 -- JSON с текстами, меню, ролями и чатами
    owner_account_id INTEGER REFERENCES accounts(id),
    status TEXT NOT NULL DEFAULT 'active',  -- active / archived
    guest_limit INTEGER,                  -- NULL — без ограничений
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    username TEXT,
    display_name TEXT,
    full_name TEXT,
    consent_version TEXT,
    consent_at TIMESTAMP,
    current_event_id INTEGER,
    state TEXT,
    state_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, platform_user_id)
);

CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL DEFAULT 'guest',
    rsvp TEXT,                 -- yes / no / maybe / NULL (не ответил)
    party_size INTEGER,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    guest_name TEXT NOT NULL,
    food TEXT,
    drink TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, user_id, guest_name)
);

CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    role TEXT NOT NULL,
    UNIQUE (event_id, role)
);

-- Какое отправленное ботом сообщение к какому чату относится:
-- по нему ответ (reply) на пересланное сообщение уходит адресату.
CREATE TABLE IF NOT EXISTS relays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    target_user_id INTEGER,     -- гость, которому уйдёт ответ сотрудника
    recipient_user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS relays_lookup ON relays (platform, chat_id, message_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    sender_user_id INTEGER,         -- NULL, если отправлено из веб-интерфейса
    sender_account_id INTEGER,
    target_user_id INTEGER,
    text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS messages_event ON messages (event_id, channel);

-- Журнал согласий хранится и после удаления данных: это подтверждение, что согласие было и когда отозвано
CREATE TABLE IF NOT EXISTS consent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    event_id INTEGER,
    action TEXT NOT NULL,       -- given / declined / withdrawn / deleted_by_organizer
    version TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # без похожих O/0, I/1
LEGACY_TABLES = ('users', 'preferences', 'invites', 'relays', 'messages')
MEMBER_FIELDS = ('role', 'rsvp', 'party_size')


def now():
    return datetime.now().replace(microsecond=0)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._rename_previous_schema()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = OFF')
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _query(self, sql: str, params=()) -> List[Dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def _one(self, sql: str, params=()) -> Optional[Dict]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ================= коды =================

    def _new_code(self, conn, length: int = 6) -> str:
        while True:
            code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(length))
            taken = conn.execute('SELECT 1 FROM events WHERE code = ? UNION SELECT 1 FROM invites WHERE code = ?',
                                 (code, code)).fetchone()
            if not taken:
                return code

    def resolve_code(self, code: str) -> Optional[Tuple[str, Dict, Optional[str]]]:
        """Код гостя → ('event', мероприятие, None); код команды → ('invite', мероприятие, роль)"""
        code = code.strip().upper()
        event = self._one('SELECT * FROM events WHERE code = ?', (code,))
        if event:
            return 'event', event, None
        invite = self._one('SELECT * FROM invites WHERE code = ?', (code,))
        if invite:
            return 'invite', self.get_event(invite['event_id']), invite['role']
        return None

    # ================= мероприятия =================

    def create_event(self, title: str, config: Dict, owner_account_id: Optional[int] = None,
                     guest_limit: Optional[int] = None) -> Dict:
        config = dict(config)
        config.setdefault('event', {})['title'] = title
        with self.connect() as conn:
            code = self._new_code(conn)
            cur = conn.execute('INSERT INTO events (code, title, config, owner_account_id, guest_limit) '
                               'VALUES (?, ?, ?, ?, ?)',
                               (code, title, json.dumps(config, ensure_ascii=False), owner_account_id, guest_limit))
            event_id = cur.lastrowid
        logger.info(f'Created event {event_id} «{title}» code={code}')
        return self.get_event(event_id)

    def get_event(self, event_id: int) -> Optional[Dict]:
        event = self._one('SELECT * FROM events WHERE id = ?', (event_id,))
        if event:
            event['config'] = json.loads(event['config'])
        return event

    def update_event(self, event_id: int, **fields):
        if 'config' in fields:
            config = dict(fields['config'])
            if 'title' in fields:
                config.setdefault('event', {})['title'] = fields['title']
            fields['config'] = json.dumps(config, ensure_ascii=False)
        elif 'title' in fields:
            event = self.get_event(event_id)
            event['config'].setdefault('event', {})['title'] = fields['title']
            fields['config'] = json.dumps(event['config'], ensure_ascii=False)
        fields['updated_at'] = now()
        cols = ', '.join(f'{k} = ?' for k in fields)
        with self.connect() as conn:
            conn.execute(f'UPDATE events SET {cols} WHERE id = ?', (*fields.values(), event_id))

    def list_events(self, owner_account_id: Optional[int] = None) -> List[Dict]:
        """Мероприятия со сводными цифрами для таблиц веб-интерфейса"""
        guest = "m.event_id = e.id AND m.role = 'guest' AND u.full_name IS NOT NULL"
        sql = f'''
            SELECT e.id, e.code, e.title, e.status, e.guest_limit, e.created_at, e.owner_account_id,
                   a.name AS owner_name, a.login AS owner_login,
                   (SELECT COUNT(*) FROM members m JOIN users u ON u.id = m.user_id WHERE {guest}) AS guests,
                   (SELECT COUNT(*) FROM members m JOIN users u ON u.id = m.user_id
                        WHERE {guest} AND m.rsvp IS NOT NULL) AS answered,
                   (SELECT COUNT(*) FROM members m JOIN users u ON u.id = m.user_id
                        WHERE {guest} AND m.rsvp = 'yes') AS attending,
                   (SELECT COALESCE(SUM(COALESCE(m.party_size, 1)), 0) FROM members m JOIN users u ON u.id = m.user_id
                        WHERE {guest} AND m.rsvp = 'yes') AS people,
                   (SELECT COUNT(*) FROM members m JOIN users u ON u.id = m.user_id
                        WHERE m.event_id = e.id AND m.role != 'guest' AND u.full_name IS NOT NULL) AS team
            FROM events e LEFT JOIN accounts a ON a.id = e.owner_account_id'''
        params = ()
        if owner_account_id is not None:
            sql += ' WHERE e.owner_account_id = ?'
            params = (owner_account_id,)
        return self._query(sql + " ORDER BY e.status = 'archived', e.created_at DESC, e.id DESC", params)

    def delete_event(self, event_id: int):
        """Полное удаление мероприятия и всех связанных данных"""
        with self.connect() as conn:
            user_ids = [r[0] for r in conn.execute('SELECT user_id FROM members WHERE event_id = ?', (event_id,))]
            for table in ('preferences', 'invites', 'relays', 'messages', 'members'):
                conn.execute(f'DELETE FROM {table} WHERE event_id = ?', (event_id,))
            conn.execute('UPDATE users SET current_event_id = NULL, state = NULL, state_data = NULL '
                         'WHERE current_event_id = ?', (event_id,))
            conn.execute('DELETE FROM events WHERE id = ?', (event_id,))
            self._purge_orphans(conn, user_ids)
        logger.info(f'Deleted event {event_id}')

    @staticmethod
    def _purge_orphans(conn, user_ids: Iterable[int]):
        """Людей, которые больше ни в одном мероприятии не участвуют, удаляем вместе с именем"""
        for uid in set(user_ids):
            if not conn.execute('SELECT 1 FROM members WHERE user_id = ?', (uid,)).fetchone():
                conn.execute('DELETE FROM users WHERE id = ?', (uid,))

    # ================= пользователи =================

    def get_or_create_user(self, platform: str, platform_user_id: str, chat_id: str,
                           username: Optional[str] = None, display_name: Optional[str] = None) -> Dict:
        key = (platform, str(platform_user_id))
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM users WHERE platform = ? AND platform_user_id = ?', key).fetchone()
            if row is None:
                conn.execute('INSERT INTO users (platform, platform_user_id, chat_id, username, display_name) '
                             'VALUES (?, ?, ?, ?, ?)', (*key, str(chat_id), username, display_name))
            elif (row['chat_id'], row['username'], row['display_name']) != (str(chat_id), username, display_name):
                conn.execute('UPDATE users SET chat_id = ?, username = ?, display_name = ? WHERE id = ?',
                             (str(chat_id), username, display_name, row['id']))
            return dict(conn.execute('SELECT * FROM users WHERE platform = ? AND platform_user_id = ?',
                                     key).fetchone())

    def get_user(self, user_id: int) -> Optional[Dict]:
        return self._one('SELECT * FROM users WHERE id = ?', (user_id,))

    def update_user(self, user_id: int, **fields):
        fields['updated_at'] = now()
        cols = ', '.join(f'{k} = ?' for k in fields)
        with self.connect() as conn:
            conn.execute(f'UPDATE users SET {cols} WHERE id = ?', (*fields.values(), user_id))

    def set_state(self, user_id: int, state: Optional[str], data: Optional[Dict] = None):
        with self.connect() as conn:
            conn.execute('UPDATE users SET state = ?, state_data = ? WHERE id = ?',
                         (state, json.dumps(data or {}, ensure_ascii=False), user_id))

    @staticmethod
    def state_data(user: Dict) -> Dict:
        return json.loads(user.get('state_data') or '{}')

    def users_by_platform_ids(self, platform: str, ids: Iterable) -> List[Dict]:
        ids = [str(i) for i in ids]
        if not ids:
            return []
        marks = ', '.join('?' for _ in ids)
        return self._query(f'SELECT * FROM users WHERE platform = ? AND platform_user_id IN ({marks})',
                           (platform, *ids))

    # ================= участники мероприятий =================

    def get_member(self, event_id: int, user_id: int) -> Optional[Dict]:
        return self._one('SELECT * FROM members WHERE event_id = ? AND user_id = ?', (event_id, user_id))

    def add_member(self, event_id: int, user_id: int, role: str = 'guest') -> Dict:
        """Добавить человека в мероприятие или сменить ему роль (организатор роль не теряет)"""
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM members WHERE event_id = ? AND user_id = ?',
                               (event_id, user_id)).fetchone()
            if row is None:
                conn.execute('INSERT INTO members (event_id, user_id, role) VALUES (?, ?, ?)',
                             (event_id, user_id, role))
            elif row['role'] != role and row['role'] != 'organizer':
                conn.execute('UPDATE members SET role = ?, updated_at = ? WHERE id = ?', (role, now(), row['id']))
        return self.get_member(event_id, user_id)

    def update_member(self, event_id: int, user_id: int, **fields):
        fields['updated_at'] = now()
        cols = ', '.join(f'{k} = ?' for k in fields)
        with self.connect() as conn:
            conn.execute(f'UPDATE members SET {cols} WHERE event_id = ? AND user_id = ?',
                         (*fields.values(), event_id, user_id))

    def members(self, event_id: int, roles: Optional[Iterable[str]] = None, exclude_guests: bool = False,
                registered_only: bool = True) -> List[Dict]:
        """Участники мероприятия: поля пользователя + role/rsvp/party_size/joined_at, id = id пользователя"""
        sql = '''SELECT u.*, m.role, m.rsvp, m.party_size, m.joined_at, m.event_id
                 FROM members m JOIN users u ON u.id = m.user_id WHERE m.event_id = ?'''
        params: list = [event_id]
        if roles is not None:
            roles = list(roles)
            if not roles:
                return []
            sql += f" AND m.role IN ({', '.join('?' for _ in roles)})"
            params += roles
        if exclude_guests:
            sql += " AND m.role != 'guest'"
        if registered_only:
            sql += ' AND u.full_name IS NOT NULL'
        return self._query(sql + ' ORDER BY m.id', params)

    def guests(self, event_id: int, rsvp: Optional[str] = 'any') -> List[Dict]:
        """rsvp='any' — все гости; None — не ответившие; 'yes'/'no'/'maybe' — по ответу"""
        rows = self.members(event_id, roles=['guest'])
        if rsvp != 'any':
            rows = [r for r in rows if r['rsvp'] == rsvp]
        return sorted(rows, key=lambda r: r['full_name'].lower())

    def count_guests(self, event_id: int) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM members WHERE event_id = ? AND role = 'guest'", (event_id,))
        return row['n']

    def user_events(self, user_id: int) -> List[Dict]:
        """Мероприятия человека с его ролью в каждом"""
        return self._query('''SELECT e.id, e.code, e.title, e.status, m.role FROM members m
                              JOIN events e ON e.id = m.event_id WHERE m.user_id = ? ORDER BY m.id''', (user_id,))

    # ================= пожелания =================

    def upsert_preference(self, event_id: int, user_id: int, guest_name: str, food: str, drink: str):
        """Повторная анкета на того же гостя обновляет запись, а не создаёт дубль"""
        with self.connect() as conn:
            conn.execute('''
                INSERT INTO preferences (event_id, user_id, guest_name, food, drink, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (event_id, user_id, guest_name)
                DO UPDATE SET food = excluded.food, drink = excluded.drink, updated_at = excluded.updated_at
            ''', (event_id, user_id, guest_name.strip(), food, drink, now()))

    def preferences(self, event_id: int, user_id: Optional[int] = None) -> List[Dict]:
        sql = '''SELECT p.*, u.full_name AS owner_name, u.platform, u.username, u.platform_user_id
                 FROM preferences p JOIN users u ON u.id = p.user_id WHERE p.event_id = ?'''
        params: list = [event_id]
        if user_id is not None:
            sql += ' AND p.user_id = ?'
            params.append(user_id)
        return self._query(sql + ' ORDER BY u.full_name, p.guest_name', params)

    # ================= приглашения команды =================

    def invite_codes(self, event_id: int, roles: Iterable[str]) -> Dict[str, str]:
        """Код приглашения на каждую роль команды; создаётся один раз"""
        codes = {}
        with self.connect() as conn:
            for role in roles:
                row = conn.execute('SELECT code FROM invites WHERE event_id = ? AND role = ?',
                                   (event_id, role)).fetchone()
                if row is None:
                    code = self._new_code(conn)
                    conn.execute('INSERT INTO invites (code, event_id, role) VALUES (?, ?, ?)', (code, event_id, role))
                    codes[role] = code
                else:
                    codes[role] = row['code']
        return codes

    def regenerate_invite(self, event_id: int, role: str) -> str:
        """Новый код для роли, если старый попал не в те руки"""
        with self.connect() as conn:
            conn.execute('DELETE FROM invites WHERE event_id = ? AND role = ?', (event_id, role))
            code = self._new_code(conn)
            conn.execute('INSERT INTO invites (code, event_id, role) VALUES (?, ?, ?)', (code, event_id, role))
        return code

    # ================= пересылки и журнал =================

    def save_relay(self, event_id: int, platform: str, chat_id: str, message_id, channel: str,
                   recipient_user_id: int, target_user_id: Optional[int] = None):
        if message_id is None:
            return
        with self.connect() as conn:
            conn.execute('INSERT INTO relays (event_id, platform, chat_id, message_id, channel, target_user_id, '
                         'recipient_user_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (event_id, platform, str(chat_id), str(message_id), channel, target_user_id,
                          recipient_user_id))

    def find_relay(self, platform: str, chat_id: str, message_id) -> Optional[Dict]:
        return self._one('SELECT * FROM relays WHERE platform = ? AND chat_id = ? AND message_id = ? '
                         'ORDER BY id DESC LIMIT 1', (platform, str(chat_id), str(message_id)))

    def last_relay_with_guest(self, staff_user_id: int, guest_user_id: int) -> Optional[Dict]:
        """Последнее сообщение гостя этому сотруднику: из него берём канал и мероприятие для «ответ N»"""
        return self._one('SELECT * FROM relays WHERE recipient_user_id = ? AND target_user_id = ? '
                         'ORDER BY id DESC LIMIT 1', (staff_user_id, guest_user_id))

    def log_message(self, event_id: int, channel: str, text: str, sender_user_id: Optional[int] = None,
                    target_user_id: Optional[int] = None, sender_account_id: Optional[int] = None):
        with self.connect() as conn:
            conn.execute('INSERT INTO messages (event_id, channel, sender_user_id, sender_account_id, target_user_id, '
                         'text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (event_id, channel, sender_user_id, sender_account_id, target_user_id, text, now()))

    def messages(self, event_id: int, channel: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        sql = '''SELECT m.*, COALESCE(s.full_name, a.name, a.login) AS sender_name,
                        sm.role AS sender_role, t.full_name AS target_name
                 FROM messages m
                 LEFT JOIN users s ON s.id = m.sender_user_id
                 LEFT JOIN members sm ON sm.user_id = m.sender_user_id AND sm.event_id = m.event_id
                 LEFT JOIN accounts a ON a.id = m.sender_account_id
                 LEFT JOIN users t ON t.id = m.target_user_id
                 WHERE m.event_id = ?'''
        params: list = [event_id]
        if channel is not None:
            sql += ' AND m.channel = ?'
            params.append(channel)
        sql += ' ORDER BY m.id DESC'
        if limit:
            sql += ' LIMIT ?'
            params.append(limit)
        return list(reversed(self._query(sql, params)))

    # ================= согласие и удаление данных =================

    def log_consent(self, user: Dict, action: str, version: Optional[str] = None, event_id: Optional[int] = None):
        with self.connect() as conn:
            conn.execute('INSERT INTO consent_log (platform, platform_user_id, event_id, action, version, created_at) '
                         'VALUES (?, ?, ?, ?, ?, ?)',
                         (user['platform'], user['platform_user_id'], event_id, action, version, now()))

    def consent_log(self, event_id: Optional[int] = None, limit: int = 200) -> List[Dict]:
        if event_id is None:
            return self._query('SELECT * FROM consent_log ORDER BY id DESC LIMIT ?', (limit,))
        return self._query('SELECT * FROM consent_log WHERE event_id = ? ORDER BY id DESC LIMIT ?', (event_id, limit))

    def delete_user_data(self, user_id: int):
        """Удалить всё о человеке во всех мероприятиях (отзыв согласия)"""
        with self.connect() as conn:
            conn.execute('DELETE FROM preferences WHERE user_id = ?', (user_id,))
            conn.execute('DELETE FROM members WHERE user_id = ?', (user_id,))
            conn.execute('DELETE FROM relays WHERE recipient_user_id = ? OR target_user_id = ?', (user_id, user_id))
            conn.execute('DELETE FROM messages WHERE sender_user_id = ? OR target_user_id = ?', (user_id, user_id))
            conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
        logger.info(f'Deleted all data of user {user_id}')

    def delete_member_data(self, event_id: int, user_id: int):
        """Удалить данные человека в одном мероприятии (например, по просьбе гостя организатору)"""
        with self.connect() as conn:
            params = (event_id, user_id)
            conn.execute('DELETE FROM preferences WHERE event_id = ? AND user_id = ?', params)
            conn.execute('DELETE FROM members WHERE event_id = ? AND user_id = ?', params)
            conn.execute('DELETE FROM relays WHERE event_id = ? AND (recipient_user_id = ? OR target_user_id = ?)',
                         (event_id, user_id, user_id))
            conn.execute('DELETE FROM messages WHERE event_id = ? AND (sender_user_id = ? OR target_user_id = ?)',
                         (event_id, user_id, user_id))
            conn.execute('UPDATE users SET current_event_id = NULL, state = NULL, state_data = NULL '
                         'WHERE id = ? AND current_event_id = ?', (user_id, event_id))
            self._purge_orphans(conn, [user_id])
        logger.info(f'Deleted data of user {user_id} in event {event_id}')

    # ================= статистика =================

    def event_stats(self, event_id: int) -> Dict:
        guests = self.guests(event_id)
        by_rsvp = Counter(g['rsvp'] for g in guests)
        prefs = self.preferences(event_id)
        team = self.members(event_id, exclude_guests=True)
        channels = Counter(r['channel'].split(':', 1)[0] for r in
                           self._query('SELECT channel FROM messages WHERE event_id = ?', (event_id,)))
        return {
            'guests': len(guests),
            'yes': by_rsvp.get('yes', 0), 'no': by_rsvp.get('no', 0), 'maybe': by_rsvp.get('maybe', 0),
            'no_answer': by_rsvp.get(None, 0),
            'people': sum(g['party_size'] or 1 for g in guests if g['rsvp'] == 'yes'),
            'answered_pct': round(100 * (len(guests) - by_rsvp.get(None, 0)) / len(guests)) if guests else 0,
            'food': Counter(p['food'] for p in prefs).most_common(),
            'drink': Counter(p['drink'] for p in prefs).most_common(),
            'preferences': len(prefs),
            'platforms': Counter(g['platform'] for g in guests).most_common(),
            'team': len(team),
            'team_roles': sorted({t['role'] for t in team}),
            'messages': {'guest': channels.get('guest', 0), 'room': channels.get('room', 0),
                         'broadcast': channels.get('broadcast', 0)},
            'registrations': self.registrations_by_day(event_id),
        }

    def registrations_by_day(self, event_id: Optional[int] = None, days: int = 30) -> List[Tuple[str, int]]:
        """Сколько гостей присоединилось в каждый из последних days дней"""
        start = (now() - timedelta(days=days - 1)).date()
        sql = '''SELECT date(m.joined_at) AS day, COUNT(*) AS n FROM members m JOIN users u ON u.id = m.user_id
                 WHERE m.role = 'guest' AND u.full_name IS NOT NULL AND date(m.joined_at) >= ?'''
        params: list = [start.isoformat()]
        if event_id is not None:
            sql += ' AND m.event_id = ?'
            params.append(event_id)
        counts = {r['day']: r['n'] for r in self._query(sql + ' GROUP BY day', params)}
        return [((start + timedelta(days=i)).isoformat(), counts.get((start + timedelta(days=i)).isoformat(), 0))
                for i in range(days)]

    def service_stats(self) -> Dict:
        events = self.list_events()
        guests = self._query('''SELECT u.platform, m.rsvp FROM members m JOIN users u ON u.id = m.user_id
                                WHERE m.role = 'guest' AND u.full_name IS NOT NULL''')
        month_ago = (now() - timedelta(days=30)).isoformat(sep=' ')
        return {
            'events': len(events),
            'active': sum(e['status'] == 'active' for e in events),
            'guests': len(guests),
            'answered_pct': round(100 * sum(g['rsvp'] is not None for g in guests) / len(guests)) if guests else 0,
            'people': sum(e['people'] for e in events),
            'team': sum(e['team'] for e in events),
            'messages_30d': self._one('SELECT COUNT(*) AS n FROM messages WHERE created_at >= ?', (month_ago,))['n'],
            'platforms': Counter(g['platform'] for g in guests).most_common(),
            'registrations': self.registrations_by_day(None),
            'accounts': self._one('SELECT COUNT(*) AS n FROM accounts')['n'],
        }

    # ================= аккаунты веб-интерфейса =================

    def create_account(self, login: str, password_hash: str, name: str = '', is_superadmin: bool = False) -> Dict:
        with self.connect() as conn:
            cur = conn.execute('INSERT INTO accounts (login, password_hash, name, is_superadmin) VALUES (?, ?, ?, ?)',
                               (login.strip().lower(), password_hash, name or login, int(is_superadmin)))
            return dict(conn.execute('SELECT * FROM accounts WHERE id = ?', (cur.lastrowid,)).fetchone())

    def get_account(self, account_id: int) -> Optional[Dict]:
        return self._one('SELECT * FROM accounts WHERE id = ?', (account_id,))

    def account_by_login(self, login: str) -> Optional[Dict]:
        return self._one('SELECT * FROM accounts WHERE login = ?', (login.strip().lower(),))

    def list_accounts(self) -> List[Dict]:
        return self._query('''SELECT a.*, (SELECT COUNT(*) FROM events e WHERE e.owner_account_id = a.id) AS events
                              FROM accounts a ORDER BY a.is_superadmin DESC, a.login''')

    def update_account(self, account_id: int, **fields):
        cols = ', '.join(f'{k} = ?' for k in fields)
        with self.connect() as conn:
            conn.execute(f'UPDATE accounts SET {cols} WHERE id = ?', (*fields.values(), account_id))

    def delete_account(self, account_id: int):
        with self.connect() as conn:
            conn.execute('UPDATE events SET owner_account_id = NULL WHERE owner_account_id = ?', (account_id,))
            conn.execute('DELETE FROM accounts WHERE id = ?', (account_id,))

    # ================= перенос данных из однособытийных версий =================

    def _rename_previous_schema(self):
        """Версия с одной свадьбой хранила роль и ответ прямо в users — переименовываем её таблицы в v1_*"""
        with self.connect() as conn:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
            if cols and 'current_event_id' not in cols:
                logger.info('Found single-event schema, renaming its tables to v1_*')
                existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                conn.execute('DROP INDEX IF EXISTS relays_lookup')
                for table in LEGACY_TABLES:
                    if table in existing:
                        conn.execute(f'ALTER TABLE {table} RENAME TO v1_{table}')

    def has_legacy_data(self) -> bool:
        with self.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return 'guests' in tables or 'v1_users' in tables

    def import_legacy(self, config: Dict, admins: Optional[Dict[str, List]] = None,
                      owner_account_id: Optional[int] = None) -> Optional[Dict]:
        """Перенести данные однособытийной версии (любой из двух) в новое мероприятие.
        Люди, уже отвечавшие боту, считаются давшими согласие по старой версии (consent_version='legacy'),
        при следующем обращении бот попросит согласие по актуальной версии."""
        if not self.has_legacy_data():
            return None
        title = (config.get('event') or {}).get('title') or 'Перенесённое мероприятие'
        event = self.create_event(title, config, owner_account_id)
        eid = event['id']
        with self.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

            def user_id_for(platform, pid, chat_id, username, display_name, full_name):
                conn.execute('INSERT OR IGNORE INTO users (platform, platform_user_id, chat_id, username, display_name, '
                             "full_name, consent_version) VALUES (?, ?, ?, ?, ?, ?, 'legacy')",
                             (platform, str(pid), str(chat_id or pid), username, display_name, full_name))
                return conn.execute('SELECT id FROM users WHERE platform = ? AND platform_user_id = ?',
                                    (platform, str(pid))).fetchone()[0]

            def add_member(uid, role='guest', rsvp=None, party_size=None):
                conn.execute('INSERT OR IGNORE INTO members (event_id, user_id, role, rsvp, party_size) '
                             'VALUES (?, ?, ?, ?, ?)', (eid, uid, role, rsvp, party_size))

            def add_pref(uid, name, food, drink, updated):
                conn.execute('''INSERT INTO preferences (event_id, user_id, guest_name, food, drink, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (event_id, user_id, guest_name) DO UPDATE SET
                                food = excluded.food, drink = excluded.drink, updated_at = excluded.updated_at''',
                             (eid, uid, (name or '').strip() or 'Не указано', food, drink, updated or now()))

            if 'guests' in tables:  # самая первая версия: guests + guest_preferences, только Telegram
                id_map = {}
                for g in conn.execute('SELECT * FROM guests').fetchall():
                    uid = user_id_for('telegram', g['user_id'], g['chat_id'], g['username'], g['tg_name'],
                                      g['full_name'])
                    add_member(uid)
                    id_map[g['id']] = uid
                if 'guest_preferences' in tables:
                    for p in conn.execute('SELECT * FROM guest_preferences ORDER BY id').fetchall():
                        if p['guest_id'] in id_map:  # у незарегистрированных guest_id был «битым»
                            add_pref(id_map[p['guest_id']], p['guest_name'], p['food'], p['drink'], p['updated_at'])
                    conn.execute('ALTER TABLE guest_preferences RENAME TO legacy_guest_preferences')
                conn.execute('ALTER TABLE guests RENAME TO legacy_guests')

            if 'v1_users' in tables:  # версия с Telegram + ВКонтакте для одной свадьбы
                id_map = {}
                for u in conn.execute('SELECT * FROM v1_users').fetchall():
                    uid = user_id_for(u['platform'], u['platform_user_id'], u['chat_id'], u['username'],
                                      u['display_name'], u['full_name'])
                    add_member(uid, u['role'], u['rsvp'], u['party_size'])
                    id_map[u['id']] = uid
                if 'v1_preferences' in tables:
                    for p in conn.execute('SELECT * FROM v1_preferences ORDER BY id').fetchall():
                        if p['user_id'] in id_map:
                            add_pref(id_map[p['user_id']], p['guest_name'], p['food'], p['drink'], p['updated_at'])
                if 'v1_invites' in tables:
                    for i in conn.execute('SELECT * FROM v1_invites').fetchall():
                        conn.execute('INSERT OR IGNORE INTO invites (code, event_id, role) VALUES (?, ?, ?)',
                                     (i['code'], eid, i['role']))
                if 'v1_messages' in tables:
                    for m in conn.execute('SELECT * FROM v1_messages ORDER BY id').fetchall():
                        conn.execute('INSERT INTO messages (event_id, channel, sender_user_id, target_user_id, text, '
                                     'created_at) VALUES (?, ?, ?, ?, ?, ?)',
                                     (eid, m['channel'], id_map.get(m['sender_user_id']),
                                      id_map.get(m['target_user_id']), m['text'], m['created_at']))
                for table in LEGACY_TABLES:
                    if f'v1_{table}' in tables:
                        conn.execute(f'ALTER TABLE v1_{table} RENAME TO legacy_v1_{table}')

            # организаторы из старого config.json
            for platform, ids in (admins or {}).items():
                for pid in ids:
                    row = conn.execute('SELECT id FROM users WHERE platform = ? AND platform_user_id = ?',
                                       (platform, str(pid))).fetchone()
                    if row:
                        conn.execute("INSERT INTO members (event_id, user_id, role) VALUES (?, ?, 'organizer') "
                                     "ON CONFLICT (event_id, user_id) DO UPDATE SET role = 'organizer'", (eid, row[0]))
            conn.execute('UPDATE users SET current_event_id = ? WHERE id IN '
                         '(SELECT user_id FROM members WHERE event_id = ?)', (eid, eid))
        logger.info(f'Imported legacy data into event {eid}')
        return self.get_event(eid)

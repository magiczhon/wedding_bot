"""Хранилище бота: SQLite, одна база на все мессенджеры.

Пользователь идентифицируется парой (platform, platform_user_id), поэтому гости
из Telegram и ВКонтакте лежат в одной таблице и попадают в одну выгрузку.
"""
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from utils.logger import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    username TEXT,
    display_name TEXT,
    full_name TEXT,
    role TEXT NOT NULL DEFAULT 'guest',
    rsvp TEXT,                 -- yes / no / maybe / NULL (не ответил)
    party_size INTEGER,
    state TEXT,
    state_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (platform, platform_user_id)
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    guest_name TEXT NOT NULL,
    food TEXT,
    drink TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, guest_name)
);

CREATE TABLE IF NOT EXISTS invites (
    role TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL
);

-- Какое отправленное ботом сообщение к какому чату относится:
-- по нему ответ (reply) на пересланное сообщение уходит адресату.
CREATE TABLE IF NOT EXISTS relays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    channel TEXT NOT NULL,
    sender_user_id INTEGER NOT NULL,
    target_user_id INTEGER,
    text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INVITE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # без похожих O/0, I/1


class Database:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        self._migrate_legacy()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- пользователи ----------

    def get_or_create_user(self, platform: str, platform_user_id: str, chat_id: str,
                           username: Optional[str] = None, display_name: Optional[str] = None,
                           role: str = 'guest') -> Dict:
        with self.connect() as conn:
            key = (platform, str(platform_user_id))
            row = conn.execute('SELECT * FROM users WHERE platform = ? AND platform_user_id = ?', key).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO users (platform, platform_user_id, chat_id, username, display_name, role) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (*key, str(chat_id), username, display_name, role))
                logger.info(f'New user {platform}:{platform_user_id} role={role}')
            elif (row['chat_id'], row['username'], row['display_name']) != (str(chat_id), username, display_name):
                conn.execute('UPDATE users SET chat_id = ?, username = ?, display_name = ? WHERE id = ?',
                             (str(chat_id), username, display_name, row['id']))
            row = conn.execute('SELECT * FROM users WHERE platform = ? AND platform_user_id = ?', key).fetchone()
            return dict(row)

    def get_user(self, user_id: int) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
            return dict(row) if row else None

    def update_user(self, user_id: int, **fields):
        if not fields:
            return
        fields['updated_at'] = datetime.now()
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

    def users_by_roles(self, roles: Iterable[str]) -> List[Dict]:
        """Зарегистрированные (с указанным именем) пользователи с одной из ролей"""
        roles = list(roles)
        if not roles:
            return []
        marks = ', '.join('?' for _ in roles)
        with self.connect() as conn:
            rows = conn.execute(f'SELECT * FROM users WHERE role IN ({marks}) AND full_name IS NOT NULL '
                                f'ORDER BY id', roles)
            return [dict(r) for r in rows]

    def guests(self, rsvp: Optional[str] = 'any') -> List[Dict]:
        """rsvp='any' — все гости; None — не ответившие; 'yes'/'no'/'maybe' — по ответу"""
        query = "SELECT * FROM users WHERE role = 'guest' AND full_name IS NOT NULL"
        params = ()
        if rsvp is None:
            query += ' AND rsvp IS NULL'
        elif rsvp != 'any':
            query += ' AND rsvp = ?'
            params = (rsvp,)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query + ' ORDER BY full_name', params)]

    def users_by_platform_ids(self, platform: str, ids: Iterable) -> List[Dict]:
        ids = [str(i) for i in ids]
        if not ids:
            return []
        marks = ', '.join('?' for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(f'SELECT * FROM users WHERE platform = ? AND platform_user_id IN ({marks})',
                                (platform, *ids))
            return [dict(r) for r in rows]

    # ---------- пожелания ----------

    def upsert_preference(self, user_id: int, guest_name: str, food: str, drink: str):
        """Повторная анкета на того же гостя обновляет запись, а не создаёт дубль"""
        with self.connect() as conn:
            conn.execute('''
                INSERT INTO preferences (user_id, guest_name, food, drink, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (user_id, guest_name)
                DO UPDATE SET food = excluded.food, drink = excluded.drink, updated_at = excluded.updated_at
            ''', (user_id, guest_name.strip(), food, drink, datetime.now()))

    def preferences(self, user_id: Optional[int] = None) -> List[Dict]:
        query = '''SELECT p.*, u.full_name AS owner_name, u.platform, u.username, u.platform_user_id
                   FROM preferences p JOIN users u ON u.id = p.user_id'''
        params = ()
        if user_id is not None:
            query += ' WHERE p.user_id = ?'
            params = (user_id,)
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query + ' ORDER BY u.full_name, p.guest_name', params)]

    # ---------- приглашения команды ----------

    def invite_codes(self, roles: Iterable[str]) -> Dict[str, str]:
        """Код приглашения на каждую роль команды; создаётся один раз и дальше не меняется"""
        codes = {}
        with self.connect() as conn:
            for role in roles:
                row = conn.execute('SELECT code FROM invites WHERE role = ?', (role,)).fetchone()
                if row is None:
                    code = ''.join(secrets.choice(INVITE_ALPHABET) for _ in range(6))
                    conn.execute('INSERT INTO invites (role, code) VALUES (?, ?)', (role, code))
                    codes[role] = code
                else:
                    codes[role] = row['code']
        return codes

    def role_by_code(self, code: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute('SELECT role FROM invites WHERE code = ?', (code.strip().upper(),)).fetchone()
            return row['role'] if row else None

    # ---------- пересылки и журнал ----------

    def save_relay(self, platform: str, chat_id: str, message_id, channel: str,
                   recipient_user_id: int, target_user_id: Optional[int] = None):
        if message_id is None:
            return
        with self.connect() as conn:
            conn.execute('INSERT INTO relays (platform, chat_id, message_id, channel, target_user_id, '
                         'recipient_user_id) VALUES (?, ?, ?, ?, ?, ?)',
                         (platform, str(chat_id), str(message_id), channel, target_user_id, recipient_user_id))

    def find_relay(self, platform: str, chat_id: str, message_id) -> Optional[Dict]:
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM relays WHERE platform = ? AND chat_id = ? AND message_id = ? '
                               'ORDER BY id DESC LIMIT 1', (platform, str(chat_id), str(message_id))).fetchone()
            return dict(row) if row else None

    def last_channel_with_guest(self, staff_user_id: int, guest_user_id: int) -> Optional[str]:
        """Через какой канал гость последним писал этому сотруднику (для команды «ответ N»)"""
        with self.connect() as conn:
            row = conn.execute('SELECT channel FROM relays WHERE recipient_user_id = ? AND target_user_id = ? '
                               'ORDER BY id DESC LIMIT 1', (staff_user_id, guest_user_id)).fetchone()
            return row['channel'] if row else None

    def log_message(self, channel: str, sender_user_id: int, text: str, target_user_id: Optional[int] = None):
        with self.connect() as conn:
            conn.execute('INSERT INTO messages (channel, sender_user_id, target_user_id, text) VALUES (?, ?, ?, ?)',
                         (channel, sender_user_id, target_user_id, text))

    def messages(self, channel: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        query = '''SELECT m.*, s.full_name AS sender_name, s.role AS sender_role, t.full_name AS target_name
                   FROM messages m JOIN users s ON s.id = m.sender_user_id
                   LEFT JOIN users t ON t.id = m.target_user_id'''
        params = []
        if channel is not None:
            query += ' WHERE m.channel = ?'
            params.append(channel)
        query += ' ORDER BY m.id DESC'
        if limit:
            query += ' LIMIT ?'
            params.append(limit)
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(query, params)]
        return list(reversed(rows))

    # ---------- перенос данных из версии с таблицами guests / guest_preferences ----------

    def _migrate_legacy(self):
        with self.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if 'guests' not in tables:
                return
            logger.info('Migrating legacy tables guests/guest_preferences')
            id_map = {}
            for g in conn.execute('SELECT * FROM guests').fetchall():
                conn.execute('INSERT OR IGNORE INTO users (platform, platform_user_id, chat_id, username, '
                             'display_name, full_name) VALUES (?, ?, ?, ?, ?, ?)',
                             ('telegram', str(g['user_id']), str(g['chat_id'] or g['user_id']),
                              g['username'], g['tg_name'], g['full_name']))
                new = conn.execute("SELECT id FROM users WHERE platform = 'telegram' AND platform_user_id = ?",
                                   (str(g['user_id']),)).fetchone()
                id_map[g['id']] = new['id']
            if 'guest_preferences' in tables:
                for p in conn.execute('SELECT * FROM guest_preferences ORDER BY id').fetchall():
                    user_id = id_map.get(p['guest_id'])
                    if user_id is None:
                        continue  # в старой версии у незарегистрированных guest_id был «битым»
                    conn.execute('''INSERT INTO preferences (user_id, guest_name, food, drink, updated_at)
                                    VALUES (?, ?, ?, ?, ?)
                                    ON CONFLICT (user_id, guest_name) DO UPDATE SET
                                    food = excluded.food, drink = excluded.drink, updated_at = excluded.updated_at''',
                                 (user_id, (p['guest_name'] or '').strip() or 'Не указано',
                                  p['food'], p['drink'], p['updated_at']))
                conn.execute('ALTER TABLE guest_preferences RENAME TO legacy_guest_preferences')
            conn.execute('ALTER TABLE guests RENAME TO legacy_guests')

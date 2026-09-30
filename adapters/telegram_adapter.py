"""Адаптер Telegram на pyTelegramBotAPI"""
from io import BytesIO
from typing import Optional

import telebot
from telebot import types

from core.messenger import Attachment, Incoming, Keyboard, Messenger
from utils.logger import logger

MAX_TEXT = 4000  # лимит Telegram — 4096 символов на сообщение

# Типы вложений, которые не переслать в другой мессенджер (внутри Telegram копируются целиком)
OTHER_KINDS = {'voice': 'голосовое сообщение', 'video': 'видео', 'video_note': 'видеосообщение',
               'audio': 'аудио', 'sticker': 'стикер', 'animation': 'GIF', 'location': 'геолокация',
               'contact': 'контакт'}


def split_text(text: str, limit: int = MAX_TEXT):
    parts = []
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip('\n')
    return parts + [text]


class TelegramMessenger(Messenger):
    name = 'telegram'
    title = 'Telegram'

    def __init__(self, token: str, on_message):
        self.bot = telebot.TeleBot(token)
        self.on_message = on_message
        self._me = None

        @self.bot.message_handler(content_types=['text', 'photo', 'document', *OTHER_KINDS])
        def handle(message: types.Message):
            if message.chat.type != 'private':
                return  # бот работает только в личных сообщениях
            self.on_message(self.to_incoming(message))

    @property
    def me(self):
        if self._me is None:
            self._me = self.bot.get_me()
        return self._me

    def to_incoming(self, message: types.Message) -> Incoming:
        user = message.from_user
        text = message.text or message.caption or ''
        start_payload = None
        if text.startswith('/start'):
            parts = text.split(maxsplit=1)
            text, start_payload = '/start', (parts[1] if len(parts) > 1 else None)

        reply_to = None
        if message.reply_to_message is not None and message.reply_to_message.from_user \
                and message.reply_to_message.from_user.is_bot:
            reply_to = str(message.reply_to_message.message_id)

        attachments = []
        if message.photo:
            file_id = message.photo[-1].file_id
            attachments.append(Attachment('photo', load=lambda f=file_id: self._download(f), filename='photo.jpg'))
        if message.document:
            doc = message.document
            attachments.append(Attachment('document', load=lambda f=doc.file_id: self._download(f),
                                          filename=doc.file_name or 'file'))
        for kind, description in OTHER_KINDS.items():
            if getattr(message, kind, None):
                attachments.append(Attachment('other', load=lambda: b'', description=description))

        name = ' '.join(filter(None, [user.first_name, user.last_name])) or None
        return Incoming(platform=self.name, user_id=str(user.id), chat_id=str(message.chat.id),
                        message_id=str(message.message_id), text=text, username=user.username,
                        display_name=name, reply_to_id=reply_to, attachments=attachments,
                        start_payload=start_payload, native=message)

    def _download(self, file_id: str) -> bytes:
        info = self.bot.get_file(file_id)
        return self.bot.download_file(info.file_path)

    @staticmethod
    def _markup(keyboard: Optional[Keyboard]):
        if keyboard is None:
            return None
        if not keyboard:
            return types.ReplyKeyboardRemove()
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for row in keyboard:
            markup.row(*[types.KeyboardButton(label) for label in row])
        return markup

    def send(self, chat_id, text, keyboard=None):
        message = None
        for part in split_text(text):
            message = self.bot.send_message(chat_id, part, reply_markup=self._markup(keyboard),
                                            disable_web_page_preview=True)
        return str(message.message_id)

    def send_location(self, chat_id, lat, lon):
        return str(self.bot.send_location(chat_id, lat, lon).message_id)

    def send_photo(self, chat_id, data, caption=''):
        return str(self.bot.send_photo(chat_id, BytesIO(data), caption=caption or None).message_id)

    def send_document(self, chat_id, data, filename, caption=''):
        file = BytesIO(data)
        file.name = filename
        return str(self.bot.send_document(chat_id, file, caption=caption or None,
                                          visible_file_name=filename).message_id)

    def copy_native(self, incoming, chat_id):
        try:
            return str(self.bot.copy_message(chat_id, incoming.chat_id, int(incoming.message_id)).message_id)
        except Exception as e:
            logger.warning(f'copy_message failed: {e}')
            return None

    def profile_link(self, user):
        if user.get('username'):
            return f"https://t.me/{user['username']}"
        return f"tg://user?id={user['platform_user_id']}"

    def invite_hint(self, code):
        return f'ссылка https://t.me/{self.me.username}?start=join_{code}'

    def guest_hint(self, code):
        return f'ссылка {self.guest_link(code)}'

    def guest_link(self, code):
        return f'https://t.me/{self.me.username}?start=ev_{code}'

    def run(self):
        logger.info(f'Telegram bot @{self.me.username} started')
        self.bot.infinity_polling(skip_pending=False)

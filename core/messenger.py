"""Общий интерфейс мессенджера.

Ядро бота (core/bot.py) ничего не знает о Telegram или ВКонтакте: оно получает
Incoming и отвечает через методы Messenger. Чтобы добавить новый мессенджер
(например, MAX), достаточно написать ещё один адаптер с этими методами.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

Keyboard = List[List[str]]  # ряды кнопок с текстом; нажатие кнопки приходит как обычный текст


@dataclass
class Attachment:
    kind: str                      # photo / document / other
    load: Callable[[], bytes]      # скачивание содержимого по требованию
    filename: str = 'file'
    description: str = ''          # для kind=other: что это было (голосовое, видео, стикер)
    _data: Optional[bytes] = field(default=None, repr=False)

    def data(self) -> bytes:
        if self._data is None:
            self._data = self.load()
        return self._data


@dataclass
class Incoming:
    platform: str
    user_id: str
    chat_id: str
    message_id: Optional[str]
    text: str = ''
    username: Optional[str] = None
    display_name: Optional[str] = None
    reply_to_id: Optional[str] = None      # id сообщения бота, на которое ответил пользователь
    attachments: List[Attachment] = field(default_factory=list)
    start_payload: Optional[str] = None    # параметр глубокой ссылки t.me/bot?start=...
    native: Any = None                     # исходный объект платформы (для копирования внутри неё)


class Messenger:
    name = 'base'
    title = 'Мессенджер'

    def send(self, chat_id: str, text: str, keyboard: Optional[Keyboard] = None) -> Optional[str]:
        """Отправить текст. keyboard=None — не менять клавиатуру. Возвращает id сообщения."""
        raise NotImplementedError

    def send_location(self, chat_id: str, lat: float, lon: float) -> Optional[str]:
        raise NotImplementedError

    def send_photo(self, chat_id: str, data: bytes, caption: str = '') -> Optional[str]:
        raise NotImplementedError

    def send_document(self, chat_id: str, data: bytes, filename: str, caption: str = '') -> Optional[str]:
        raise NotImplementedError

    def copy_native(self, incoming: Incoming, chat_id: str) -> Optional[str]:
        """Скопировать сообщение внутри той же платформы со всеми вложениями
        (голосовые, видео, стикеры). None — не умеем, ядро перешлёт по частям."""
        return None

    def profile_link(self, user: dict) -> str:
        return ''

    def invite_hint(self, code: str) -> str:
        """Как присоединиться к команде по коду в этом мессенджере"""
        return f'напишите боту: код {code}'

    def guest_hint(self, code: str) -> str:
        """Как гостю открыть приглашение по коду мероприятия"""
        return f'напишите боту: код {code}'

    def guest_link(self, code: str) -> Optional[str]:
        """Прямая ссылка на приглашение (для рассылки гостям и QR-кода), если мессенджер её поддерживает"""
        return None

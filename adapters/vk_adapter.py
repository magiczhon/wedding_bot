"""Адаптер ВКонтакте: бот в сообщениях сообщества (Bots Long Poll API) на vk_api"""
import json
import time
from io import BytesIO
from typing import Dict, Optional

import requests
import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard
from vk_api.upload import VkUpload
from vk_api.utils import get_random_id

from core.messenger import Attachment, Incoming, Keyboard, Messenger
from utils.logger import logger

API_VERSION = '5.199'
MAX_TEXT = 4000
MAX_LABEL = 40  # ВКонтакте обрезает подписи кнопок длиннее 40 символов
OTHER_KINDS = {'audio_message': 'голосовое сообщение', 'video': 'видео', 'audio': 'аудио',
               'sticker': 'стикер', 'wall': 'запись со стены', 'link': 'ссылка', 'graffiti': 'граффити'}


def split_text(text: str, limit: int = MAX_TEXT):
    parts = []
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip('\n')
    return parts + [text]


class VkMessenger(Messenger):
    name = 'vk'
    title = 'ВКонтакте'

    def __init__(self, token: str, group_id: int, on_message):
        self.session = vk_api.VkApi(token=token, api_version=API_VERSION)
        self.api = self.session.get_api()
        self.upload = VkUpload(self.session)
        self.group_id = int(group_id)
        self.on_message = on_message
        self._names: Dict[int, tuple] = {}
        self._screen_name = None

    # ---------- входящие ----------

    def to_incoming(self, msg: dict) -> Incoming:
        from_id = msg['from_id']
        username, display_name = self._user_names(from_id)
        reply = msg.get('reply_message') or {}
        reply_to = None
        # в ответ считаем только reply на сообщения сообщества (from_id < 0)
        if reply and reply.get('from_id', 0) < 0 and reply.get('conversation_message_id'):
            reply_to = str(reply['conversation_message_id'])

        attachments = []
        for a in msg.get('attachments', []):
            kind = a.get('type')
            if kind == 'photo':
                sizes = a['photo'].get('sizes', [])
                best = max(sizes, key=lambda s: s.get('width', 0) * s.get('height', 0), default=None)
                if best:
                    attachments.append(Attachment('photo', load=lambda u=best['url']: self._download(u),
                                                  filename='photo.jpg'))
            elif kind == 'doc':
                doc = a['doc']
                filename = doc.get('title') or f"file.{doc.get('ext', 'bin')}"
                attachments.append(Attachment('document', load=lambda u=doc['url']: self._download(u),
                                              filename=filename))
            else:
                attachments.append(Attachment('other', load=lambda: b'',
                                              description=OTHER_KINDS.get(kind, kind or 'вложение')))

        return Incoming(platform=self.name, user_id=str(from_id), chat_id=str(msg['peer_id']),
                        message_id=str(msg.get('conversation_message_id')), text=msg.get('text', ''),
                        username=username, display_name=display_name, reply_to_id=reply_to,
                        attachments=attachments, native=msg)

    def _user_names(self, user_id: int):
        if user_id not in self._names:
            try:
                info = self.api.users.get(user_ids=user_id, fields='screen_name')[0]
                self._names[user_id] = (info.get('screen_name'),
                                        f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or None)
            except Exception as e:
                logger.warning(f'users.get failed for {user_id}: {e}')
                return None, None
        return self._names[user_id]

    @staticmethod
    def _download(url: str) -> bytes:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        return response.content

    # ---------- исходящие ----------

    @staticmethod
    def _keyboard(keyboard: Optional[Keyboard]) -> Optional[str]:
        if keyboard is None:
            return None
        if not keyboard:
            return VkKeyboard.get_empty_keyboard()
        kb = VkKeyboard(one_time=False)
        for i, row in enumerate(keyboard[:10]):  # не больше 10 рядов
            if i:
                kb.add_line()
            for label in row[:5]:                # и 5 кнопок в ряду
                kb.add_button(label[:MAX_LABEL])
        return kb.get_keyboard()

    def _send(self, peer_id, **params) -> Optional[str]:
        # peer_ids вместо peer_id: так ответ содержит conversation_message_id,
        # по которому потом распознаётся reply пользователя
        result = self.api.messages.send(peer_ids=str(peer_id), random_id=get_random_id(), **params)
        item = result[0] if isinstance(result, list) and result else {}
        if item.get('error'):
            raise RuntimeError(f"VK send error: {item['error']}")
        cmid = item.get('conversation_message_id')
        return str(cmid) if cmid else None

    def send(self, chat_id, text, keyboard=None):
        markup = self._keyboard(keyboard)
        mid = None
        for part in split_text(text or ' '):
            params = {'message': part}
            if markup is not None:
                params['keyboard'] = markup
            mid = self._send(chat_id, **params)
        return mid

    def send_location(self, chat_id, lat, lon):
        return self._send(chat_id, message='📍 На карте', lat=lat, long=lon)

    def send_photo(self, chat_id, data, caption=''):
        file = BytesIO(data)
        file.name = 'photo.jpg'
        photo = self.upload.photo_messages(file, peer_id=int(chat_id))[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        if photo.get('access_key'):
            attachment += f"_{photo['access_key']}"
        return self._send(chat_id, message=caption, attachment=attachment)

    def send_document(self, chat_id, data, filename, caption=''):
        file = BytesIO(data)
        file.name = filename
        doc = self.upload.document_message(file, title=filename, peer_id=int(chat_id))['doc']
        return self._send(chat_id, message=caption, attachment=f"doc{doc['owner_id']}_{doc['id']}")

    def copy_native(self, incoming, chat_id):
        """Внутри ВКонтакте пересылаем исходное сообщение целиком (голосовые, видео и т.д.)"""
        try:
            forward = json.dumps({'peer_id': int(incoming.chat_id),
                                  'conversation_message_ids': [int(incoming.message_id)]})
            return self._send(chat_id, forward=forward)
        except Exception as e:
            logger.warning(f'VK forward failed: {e}')
            return None

    def profile_link(self, user):
        return f"https://vk.com/id{user['platform_user_id']}"

    @property
    def screen_name(self):
        if self._screen_name is None:
            try:
                info = self.api.groups.getById(group_id=self.group_id)
                groups = info.get('groups', []) if isinstance(info, dict) else info
                self._screen_name = groups[0].get('screen_name') or f'club{self.group_id}'
            except Exception:
                self._screen_name = f'club{self.group_id}'
        return self._screen_name

    def invite_hint(self, code):
        return f'написать сообществу vk.me/{self.screen_name} сообщение «код {code}»'

    # ---------- цикл получения сообщений ----------

    def run(self):
        logger.info(f'VK bot for group {self.group_id} started')
        while True:
            try:
                longpoll = VkBotLongPoll(self.session, self.group_id)
                for event in longpoll.listen():
                    if event.type != VkBotEventType.MESSAGE_NEW:
                        continue
                    msg = event.object.get('message', event.object)
                    if msg.get('peer_id', 0) >= 2_000_000_000:
                        continue  # беседы не обслуживаем, только личные сообщения сообществу
                    if msg.get('from_id', 0) <= 0:
                        continue
                    self.on_message(self.to_incoming(msg))
            except Exception:
                logger.exception('VK long poll error, reconnecting in 5 s')
                time.sleep(5)

"""Настройки мероприятия: шаблон по умолчанию и проверка перед сохранением"""
import copy
import re
from pathlib import Path
from typing import Dict, Iterable, List

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / 'event.example.yaml'
MAX_BUTTON = 40  # ВКонтакте обрезает подписи кнопок длиннее 40 символов
ID_RE = re.compile(r'^[a-z][a-z0-9_]{0,30}$')

_template_cache = None


def default_config() -> Dict:
    """Настройки нового мероприятия — из event.example.yaml"""
    global _template_cache
    if _template_cache is None:
        _template_cache = yaml.safe_load(TEMPLATE_PATH.read_text(encoding='utf-8'))
    return copy.deepcopy(_template_cache)


def dump_yaml(config: Dict) -> str:
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=120)


def validate(config, reserved_buttons: Iterable[str] = ()) -> List[str]:
    """Список понятных ошибок; пустой список — настройки корректны"""
    if not isinstance(config, dict):
        return ['Настройки должны быть словарём (ключ: значение)']
    errors = []
    reserved = set(reserved_buttons)

    event = config.get('event')
    if not isinstance(event, dict):
        errors.append('Нет раздела event')
        event = {}
    place = event.get('place') or {}
    if not isinstance(place, dict):
        errors.append('event.place должен быть разделом с name, address, lat, lon')
        place = {}
    for key in ('lat', 'lon'):
        value = place.get(key)
        if value is not None and not isinstance(value, (int, float)):
            errors.append(f'event.place.{key} должен быть числом, например 55.7558')

    rsvp = config.get('rsvp') or {}
    size = rsvp.get('max_party_size', 5)
    if not isinstance(size, int) or not 1 <= size <= 20:
        errors.append('rsvp.max_party_size — целое число от 1 до 20')

    menu = config.get('menu') or {}
    for key, title in (('food', 'еды'), ('drink', 'напитков')):
        options = (menu.get(key) or {}).get('options')
        if not isinstance(options, list) or not options or not all(isinstance(o, str) and o.strip() for o in options):
            errors.append(f'Нужен хотя бы один вариант {title} (menu.{key}.options)')
            continue
        for option in options:
            if len(option) > MAX_BUTTON:
                errors.append(f'Вариант «{option}» длиннее {MAX_BUTTON} символов')
        if len(set(options)) != len(options):
            errors.append(f'Варианты {title} повторяются')

    roles = config.get('roles')
    if not isinstance(roles, dict) or 'organizer' not in roles:
        errors.append('В roles должна быть роль organizer')
        roles = roles if isinstance(roles, dict) else {}
    for role, title in roles.items():
        if role == 'guest' or not ID_RE.match(str(role)):
            errors.append(f'Недопустимый id роли «{role}»: латиница в нижнем регистре, цифры и _, кроме guest')
        if not isinstance(title, str) or not title.strip():
            errors.append(f'У роли {role} нет названия')

    def check_roles(where, values, allow_empty=False):
        if not isinstance(values, list) or (not values and not allow_empty):
            errors.append(f'{where}: нужен список ролей')
            return
        for value in values:
            if value not in roles:
                errors.append(f'{where}: роли «{value}» нет в roles')

    buttons = []
    channels = config.get('guest_channels') or {}
    if not isinstance(channels, dict):
        errors.append('guest_channels должен быть разделом')
        channels = {}
    for cid, channel in channels.items():
        if not ID_RE.match(str(cid)):
            errors.append(f'Недопустимый id канала «{cid}»')
        if not isinstance(channel, dict):
            errors.append(f'Канал {cid} должен быть разделом')
            continue
        for key in ('button', 'title', 'intro'):
            if not isinstance(channel.get(key), str) or not channel[key].strip():
                errors.append(f'guest_channels.{cid}.{key} не заполнен')
        buttons.append(channel.get('button') or '')
        check_roles(f'guest_channels.{cid}.recipients', channel.get('recipients'))
        check_roles(f'guest_channels.{cid}.broadcast_roles', channel.get('broadcast_roles', []), allow_empty=True)

    room_ids = set()
    rooms = config.get('rooms') or []
    if not isinstance(rooms, list):
        errors.append('rooms должен быть списком')
        rooms = []
    for room in rooms:
        if not isinstance(room, dict):
            errors.append('Каждый чат в rooms — раздел с id, title, roles')
            continue
        rid = room.get('id')
        if not ID_RE.match(str(rid)):
            errors.append(f'Недопустимый id чата «{rid}»')
        if rid in room_ids:
            errors.append(f'id чата «{rid}» повторяется')
        room_ids.add(rid)
        if not isinstance(room.get('title'), str) or not room['title'].strip():
            errors.append(f'У чата {rid} нет названия')
        else:
            buttons.append(room['title'])
        check_roles(f'rooms.{rid}.roles', room.get('roles'))

    check_roles('export_roles', config.get('export_roles', ['organizer']), allow_empty=True)

    for button in buttons:
        if len(button) > MAX_BUTTON:
            errors.append(f'Кнопка «{button}» длиннее {MAX_BUTTON} символов')
        if button in reserved:
            errors.append(f'Кнопка «{button}» совпадает со встроенной кнопкой бота')
    if len(set(buttons)) != len(buttons):
        errors.append('Названия кнопок каналов и чатов повторяются')
    return errors

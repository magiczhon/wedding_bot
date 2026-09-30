"""Запуск сервиса: бот в Telegram и ВКонтакте + веб-панель администрирования.

    python main.py                       # config.json из текущей папки
    python main.py --config my.json
    python main.py --no-web              # только бот

Первый аккаунт веб-панели создаётся командой: python manage.py create-admin <логин>
"""
import argparse
import json
import threading
from pathlib import Path

import yaml

from core.bot import WeddingBot
from core.event_config import default_config
from database.db import Database
from utils.logger import logger

DEFAULT_DB = 'guests.db'


def load_config(path: str) -> dict:
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    # Совместимость со старым форматом: token_id + admin_ids (только Telegram)
    if 'token_id' in config and 'telegram' not in config:
        config['telegram'] = {'token': config.pop('token_id')}
    if isinstance(config.get('admin_ids'), list):
        config.setdefault('admins', {})['telegram'] = config.pop('admin_ids')
    if isinstance(config.get('test_users_id'), list):
        config.setdefault('test_users', {})['telegram'] = config.pop('test_users_id')
    return config


def import_legacy_if_needed(db: Database, config: dict, event_path: str):
    """База версии для одной свадьбы переносится в первое мероприятие при первом запуске"""
    if not db.has_legacy_data():
        return
    path = Path(event_path)
    event_config = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else default_config()
    event = db.import_legacy(event_config, admins=config.get('admins'))
    logger.info(f"Старые данные перенесены в мероприятие «{event['title']}» (код {event['code']}). "
                f"Назначьте ему организатора в веб-панели.")


def build(config: dict):
    db = Database(config.get('db_name', DEFAULT_DB))
    web = config.get('web') or {}
    bot = WeddingBot(db, privacy=config.get('privacy'), public_url=web.get('public_url', ''))
    runners = []
    if config.get('telegram', {}).get('token'):
        from adapters.telegram_adapter import TelegramMessenger
        tg = TelegramMessenger(config['telegram']['token'], bot.handle,
                               api_url=config['telegram'].get('api_url'), proxy=config['telegram'].get('proxy'))
        bot.register(tg)
        runners.append(tg)
    if config.get('vk', {}).get('token'):
        from adapters.vk_adapter import VkMessenger
        vk = VkMessenger(config['vk']['token'], config['vk']['group_id'], bot.handle)
        bot.register(vk)
        runners.append(vk)
    return db, bot, runners


def main():
    parser = argparse.ArgumentParser(description='Бот-приглашение на мероприятия')
    parser.add_argument('--config', default='config.json', help='токены, веб-панель, оператор ПДн')
    parser.add_argument('--event', default='event.yaml', help='настройки для переноса базы старой версии')
    parser.add_argument('--no-web', action='store_true', help='не запускать веб-панель')
    args = parser.parse_args()

    config = load_config(args.config)
    db, bot, runners = build(config)
    import_legacy_if_needed(db, config, args.event)
    if not runners:
        raise SystemExit('В config.json не указан ни один мессенджер (telegram.token или vk.token)')

    threads = [threading.Thread(target=r.run, name=r.name, daemon=True) for r in runners]
    web = config.get('web') or {}
    if not args.no_web and web.get('enabled', True):
        from werkzeug.serving import make_server
        from web.app import create_app
        app = create_app(db, bot, {**web, 'default_guest_limit': config.get('default_guest_limit')})
        host, port = web.get('host', '127.0.0.1'), int(web.get('port', 8080))
        server = make_server(host, port, app, threaded=True)
        threads.append(threading.Thread(target=server.serve_forever, name='web', daemon=True))
        logger.info(f'Веб-панель: http://{host}:{port}')
        if not db.list_accounts():
            logger.warning('В веб-панели ещё нет аккаунтов: python manage.py create-admin <логин>')

    logger.info(f"Starting: {', '.join(r.title for r in runners)}")
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == '__main__':
    main()

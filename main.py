"""Запуск бота-приглашения сразу в нескольких мессенджерах.

    python main.py                         # config.json и event.yaml из текущей папки
    python main.py --config my.json --event wedding.yaml
"""
import argparse
import json
import threading
from pathlib import Path

import yaml

from core.bot import WeddingBot
from database.db import Database
from utils.logger import logger


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


def main():
    parser = argparse.ArgumentParser(description='Бот-приглашение на мероприятие')
    parser.add_argument('--config', default='config.json', help='секреты: токены, админы (config.json)')
    parser.add_argument('--event', default='event.yaml', help='тексты и настройки мероприятия (event.yaml)')
    args = parser.parse_args()

    config = load_config(args.config)
    event = yaml.safe_load(Path(args.event).read_text(encoding='utf-8'))
    db = Database(config.get('db_name', 'guests.db'))
    bot = WeddingBot(db, event, admins=config.get('admins', {}), test_users=config.get('test_users', {}))

    runners = []
    if config.get('telegram', {}).get('token'):
        from adapters.telegram_adapter import TelegramMessenger
        tg = TelegramMessenger(config['telegram']['token'], bot.handle)
        bot.register(tg)
        runners.append(tg)
    if config.get('vk', {}).get('token'):
        from adapters.vk_adapter import VkMessenger
        vk = VkMessenger(config['vk']['token'], config['vk']['group_id'], bot.handle)
        bot.register(vk)
        runners.append(vk)
    if not runners:
        raise SystemExit('В config.json не указан ни один мессенджер (telegram.token или vk.token)')

    logger.info(f"Starting: {', '.join(r.title for r in runners)}")
    threads = [threading.Thread(target=r.run, name=r.name, daemon=True) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == '__main__':
    main()

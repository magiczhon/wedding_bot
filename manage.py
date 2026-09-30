"""Служебные команды.

    python manage.py create-admin LOGIN [--name "Имя"]     # аккаунт владельца сервиса (спросит пароль)
    python manage.py create-organizer LOGIN [--name "..."]  # аккаунт организатора
    python manage.py set-password LOGIN
    python manage.py import-legacy [--event event.yaml]     # перенести базу старой версии в мероприятие
"""
import argparse
import getpass
import sys
from pathlib import Path

import yaml
from werkzeug.security import generate_password_hash

from core.event_config import default_config
from database.db import Database
from main import DEFAULT_DB, load_config


def ask_password() -> str:
    while True:
        password = getpass.getpass('Пароль (не короче 8 символов): ')
        if len(password) < 8:
            print('Слишком короткий')
            continue
        if password != getpass.getpass('Повторите пароль: '):
            print('Пароли не совпадают')
            continue
        return password


def main():
    parser = argparse.ArgumentParser(description='Служебные команды бота')
    parser.add_argument('--config', default='config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('create-admin', 'create-organizer', 'set-password'):
        p = sub.add_parser(name)
        p.add_argument('login')
        if name != 'set-password':
            p.add_argument('--name', default='')
    p = sub.add_parser('import-legacy')
    p.add_argument('--event', default='event.yaml')
    args = parser.parse_args()

    config = load_config(args.config) if Path(args.config).exists() else {}
    db = Database(config.get('db_name', DEFAULT_DB))

    if args.command in ('create-admin', 'create-organizer'):
        if db.account_by_login(args.login):
            sys.exit(f'Логин {args.login} уже занят')
        account = db.create_account(args.login, generate_password_hash(ask_password()), args.name,
                                    is_superadmin=args.command == 'create-admin')
        print(f"Готово: {account['login']} ({'владелец сервиса' if account['is_superadmin'] else 'организатор'})")
    elif args.command == 'set-password':
        account = db.account_by_login(args.login)
        if account is None:
            sys.exit(f'Аккаунт {args.login} не найден')
        db.update_account(account['id'], password_hash=generate_password_hash(ask_password()))
        print('Пароль изменён')
    elif args.command == 'import-legacy':
        event_config = (yaml.safe_load(Path(args.event).read_text(encoding='utf-8'))
                        if Path(args.event).exists() else default_config())
        event = db.import_legacy(event_config, admins=config.get('admins'))
        print(f"Перенесено в мероприятие «{event['title']}», код {event['code']}" if event
              else 'Данных старой версии не найдено')


if __name__ == '__main__':
    main()

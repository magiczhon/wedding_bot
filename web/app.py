"""Веб-интерфейс: администрирование мероприятий и статистика.

Два вида учётных записей:
- владелец сервиса (superadmin) видит все мероприятия, статистику сервиса, управляет аккаунтами и лимитами;
- организатор видит и настраивает только свои мероприятия.

Запускается вместе с ботом из main.py (раздел web в config.json).
"""
import secrets
import time
from functools import wraps
from typing import Dict, List

import yaml
from flask import (Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from io import BytesIO

from core.bot import RESERVED_BUTTONS, RSVP_LABELS, WEB_AUDIENCES, WeddingBot
from core.event_config import default_config, dump_yaml, validate
from core.export import build_excel
from database.db import Database
from utils.logger import logger

ADVANCED_KEYS = ('roles', 'guest_channels', 'rooms', 'export_roles')
RSVP_FILTERS = {'yes': 'Придут', 'maybe': 'Думают', 'no': 'Не придут', 'none': 'Не ответили'}
MIN_PASSWORD = 8


def create_app(db: Database, bot: WeddingBot, settings: Dict = None) -> Flask:
    settings = settings or {}
    app = Flask(__name__)
    secret = settings.get('secret_key')
    if not secret:
        logger.warning('web.secret_key не задан в config.json — после перезапуска придётся войти заново')
        secret = secrets.token_hex(32)
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=bool(settings.get('https', False)),
                      MAX_CONTENT_LENGTH=2 * 1024 * 1024)
    default_limit = settings.get('default_guest_limit')

    # ================= сессия, права, CSRF =================

    @app.before_request
    def load_account():
        g.account = None
        if session.get('account_id'):
            g.account = db.get_account(session['account_id'])
            if g.account is None:
                session.clear()
        if request.method == 'POST':
            token = session.get('csrf')
            if not token or request.form.get('csrf') != token:
                abort(400, 'Форма устарела — обновите страницу и попробуйте ещё раз')

    @app.context_processor
    def inject():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return {'account': g.get('account'), 'csrf': session['csrf'], 'RSVP_LABELS': RSVP_LABELS,
                'platform_title': bot.platform_title}

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.account is None:
                return redirect(url_for('login', next=request.path))
            return view(*args, **kwargs)
        return wrapper

    def superadmin_required(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if not g.account['is_superadmin']:
                abort(403)
            return view(*args, **kwargs)
        return wrapper

    def load_event(event_id: int) -> Dict:
        """Мероприятие, если у текущего аккаунта есть к нему доступ; иначе 404 (не раскрываем, что оно есть)"""
        event = db.get_event(event_id)
        if event is None or not (g.account['is_superadmin'] or event['owner_account_id'] == g.account['id']):
            abort(404)
        return event

    # ================= вход =================

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            account = db.account_by_login(request.form.get('login', ''))
            if account and check_password_hash(account['password_hash'], request.form.get('password', '')):
                session.clear()
                session['account_id'] = account['id']
                session['csrf'] = secrets.token_urlsafe(32)
                logger.info(f"Web login: {account['login']}")
                target = request.args.get('next', '')
                return redirect(target if target.startswith('/') and not target.startswith('//') else url_for('index'))
            time.sleep(0.5)  # замедляем подбор пароля
            flash('Неверный логин или пароль', 'error')
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.route('/account', methods=['GET', 'POST'])
    @login_required
    def my_account():
        if request.method == 'POST':
            if not check_password_hash(g.account['password_hash'], request.form.get('current', '')):
                flash('Текущий пароль неверен', 'error')
            elif len(request.form.get('password', '')) < MIN_PASSWORD:
                flash(f'Новый пароль — не короче {MIN_PASSWORD} символов', 'error')
            else:
                db.update_account(g.account['id'], password_hash=generate_password_hash(request.form['password']))
                flash('Пароль изменён', 'ok')
                return redirect(url_for('my_account'))
        return render_template('account.html')

    # ================= главная: мероприятия и статистика =================

    @app.route('/')
    @login_required
    def index():
        if g.account['is_superadmin']:
            events = db.list_events()
            stats = db.service_stats()
            stats['platforms_titled'] = [(bot.platform_title(p), n) for p, n in stats['platforms']]
            return render_template('dashboard.html', events=events, stats=stats)
        events = db.list_events(g.account['id'])
        totals = {
            'events': len(events), 'active': sum(e['status'] == 'active' for e in events),
            'guests': sum(e['guests'] for e in events), 'people': sum(e['people'] for e in events),
            'answered_pct': round(100 * sum(e['answered'] for e in events) / max(1, sum(e['guests'] for e in events))),
            'team': sum(e['team'] for e in events),
        }
        return render_template('dashboard.html', events=events, stats=None, totals=totals)

    @app.route('/events/new', methods=['GET', 'POST'])
    @login_required
    def event_new():
        accounts = db.list_accounts() if g.account['is_superadmin'] else []
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            if not title:
                flash('Укажите название мероприятия', 'error')
                return render_template('event_new.html', accounts=accounts, form=request.form)
            config = default_config()
            ev = config['event']
            ev['date_time'] = request.form.get('date_time', '').strip() or ev.get('date_time', '')
            ev['place']['name'] = request.form.get('place_name', '').strip() or ev['place']['name']
            ev['place']['address'] = request.form.get('place_address', '').strip() or ev['place']['address']
            owner, limit = g.account['id'], default_limit
            if g.account['is_superadmin']:
                owner = int(request.form.get('owner') or g.account['id'])
                limit = _int_or_none(request.form.get('guest_limit'))
            event = db.create_event(title, config, owner_account_id=owner, guest_limit=limit)
            flash('Мероприятие создано. Проверьте тексты в «Настройках» и отправьте ссылку гостям.', 'ok')
            return redirect(url_for('event_overview', event_id=event['id']))
        return render_template('event_new.html', accounts=accounts, form={})

    # ================= мероприятие =================

    def links_for(event) -> Dict:
        guest, errors = [], []
        for m in bot.messengers.values():
            try:
                guest.append({'title': m.title, 'link': m.guest_link(event['code']), 'hint': m.guest_hint(event['code'])})
            except Exception as e:  # мессенджер недоступен — показываем код без ссылки
                errors.append(f'{m.title}: {e}')
        try:
            invites = bot.invite_links(event)
        except Exception as e:
            codes = db.invite_codes(event['id'], event['config'].get('roles', {}))
            invites = [(r, t, codes[r], []) for r, t in event['config'].get('roles', {}).items()]
            errors.append(str(e))
        return {'guest': guest, 'invites': invites, 'errors': errors}

    @app.route('/events/<int:event_id>')
    @login_required
    def event_overview(event_id):
        event = load_event(event_id)
        stats = db.event_stats(event_id)
        stats['platforms_titled'] = [(bot.platform_title(p), n) for p, n in stats['platforms']]
        return render_template('event_overview.html', event=event, tab='overview', stats=stats, links=links_for(event))

    @app.route('/events/<int:event_id>/guests')
    @login_required
    def event_guests(event_id):
        event = load_event(event_id)
        rsvp_filter = request.args.get('rsvp', '')
        query = request.args.get('q', '').strip().lower()
        guests = db.guests(event_id)
        if rsvp_filter in RSVP_FILTERS:
            wanted = None if rsvp_filter == 'none' else rsvp_filter
            guests = [x for x in guests if x['rsvp'] == wanted]
        if query:
            guests = [x for x in guests if query in x['full_name'].lower()]
        prefs: Dict[int, List] = {}
        for p in db.preferences(event_id):
            prefs.setdefault(p['user_id'], []).append(p)
        return render_template('event_guests.html', event=event, tab='guests', guests=guests, prefs=prefs,
                               filters=RSVP_FILTERS, rsvp_filter=rsvp_filter, q=request.args.get('q', ''),
                               total=len(db.guests(event_id)))

    @app.post('/events/<int:event_id>/members/<int:user_id>/delete')
    @login_required
    def member_delete(event_id, user_id):
        event = load_event(event_id)
        user = db.get_user(user_id)
        if user is None or db.get_member(event_id, user_id) is None:
            abort(404)
        db.log_consent(user, 'deleted_by_organizer', event_id=event_id)
        db.delete_member_data(event_id, user_id)
        logger.info(f"Web: {g.account['login']} deleted user {user_id} from event {event_id}")
        flash(f"Данные «{user['full_name'] or 'без имени'}» в мероприятии «{event['title']}» удалены", 'ok')
        return redirect(request.form.get('back') or url_for('event_guests', event_id=event_id))

    @app.route('/events/<int:event_id>/team')
    @login_required
    def event_team(event_id):
        event = load_event(event_id)
        return render_template('event_team.html', event=event, tab='team', links=links_for(event),
                               team=db.members(event_id, exclude_guests=True),
                               roles=event['config'].get('roles', {}))

    @app.post('/events/<int:event_id>/invites/<role>/regenerate')
    @login_required
    def invite_regenerate(event_id, role):
        event = load_event(event_id)
        if role not in event['config'].get('roles', {}):
            abort(404)
        code = db.regenerate_invite(event_id, role)
        flash(f'Новый код для роли «{event["config"]["roles"][role]}»: {code}. Старый больше не работает.', 'ok')
        return redirect(url_for('event_team', event_id=event_id))

    @app.route('/events/<int:event_id>/messages')
    @login_required
    def event_messages(event_id):
        event = load_event(event_id)
        config = event['config']
        channels = {f'guest:{cid}': f"{c['title']} (гости)" for cid, c in (config.get('guest_channels') or {}).items()}
        channels.update({f"room:{r['id']}": f"Чат: {r['title']}" for r in config.get('rooms') or []})
        channel = request.args.get('channel') or None
        messages = db.messages(event_id, channel if channel != 'broadcast' else None)
        if channel == 'broadcast':
            messages = [m for m in messages if m['channel'].startswith('broadcast:')]
        channels['broadcast'] = 'Рассылки'

        def title(ch):
            return channels.get(ch) or ('Рассылка: ' + ch.split(':', 1)[1] if ch.startswith('broadcast:') else ch)
        roles = config.get('roles', {})
        return render_template('event_messages.html', event=event, tab='messages', messages=messages,
                               channels=channels, channel=channel or '', channel_title=title,
                               role_title=lambda r: 'Гость' if r == 'guest' else roles.get(r, r or ''))

    @app.route('/events/<int:event_id>/broadcast', methods=['GET', 'POST'])
    @login_required
    def event_broadcast(event_id):
        event = load_event(event_id)
        counts = {a: len(bot.audience_users(event_id, a)) for a in WEB_AUDIENCES}
        if request.method == 'POST':
            audience, text = request.form.get('audience'), request.form.get('text', '').strip()
            if audience not in WEB_AUDIENCES or not text:
                flash('Выберите получателей и напишите сообщение', 'error')
            elif event['status'] != 'active':
                flash('Мероприятие в архиве — рассылка недоступна', 'error')
            elif len(text) > 3500:
                flash('Сообщение слишком длинное (до 3500 символов)', 'error')
            else:
                sent, failed = bot.web_broadcast(event_id, audience, text, g.account)
                logger.info(f"Web broadcast by {g.account['login']} to «{audience}» in event {event_id}: {sent}/{failed}")
                flash(f'Отправлено: {sent}' + (f', не доставлено: {failed}' if failed else ''), 'ok')
                return redirect(url_for('event_broadcast', event_id=event_id))
        history = [m for m in db.messages(event_id) if m['channel'].startswith('broadcast:')][-20:][::-1]
        return render_template('event_broadcast.html', event=event, tab='broadcast', counts=counts, history=history,
                               form=request.form)

    @app.route('/events/<int:event_id>/export.xlsx')
    @login_required
    def event_export(event_id):
        event = load_event(event_id)
        data = build_excel(db, bot, event)
        return send_file(BytesIO(data), as_attachment=True, download_name=f"guests-{event['code']}.xlsx",
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    # ================= настройки мероприятия =================

    @app.route('/events/<int:event_id>/settings', methods=['GET', 'POST'])
    @login_required
    def event_settings(event_id):
        event = load_event(event_id)
        accounts = db.list_accounts() if g.account['is_superadmin'] else []
        config = event['config']
        advanced = dump_yaml({k: config[k] for k in ADVANCED_KEYS if k in config})
        if request.method == 'POST':
            form = request.form
            new = _config_from_form(config, form)
            title = form.get('title', '').strip()
            errors = [] if title else ['Укажите название мероприятия']
            advanced = form.get('advanced', '')
            if new is None:
                errors.append('Широта и долгота должны быть числами, число гостей — целым')
            else:
                try:
                    extra = yaml.safe_load(advanced) or {}
                    if not isinstance(extra, dict):
                        raise ValueError('ожидается раздел с ключами roles, guest_channels, rooms')
                    for key in ADVANCED_KEYS:
                        if key in extra:
                            new[key] = extra[key]
                    errors += validate(new, RESERVED_BUTTONS)
                except (yaml.YAMLError, ValueError) as e:
                    errors.append(f'Ошибка в расширенных настройках: {e}')
            if errors:
                for e in errors:
                    flash(e, 'error')
                return render_template('event_settings.html', event=event, tab='settings', config=config,
                                       advanced=advanced, accounts=accounts, form=form)
            fields = {'title': title, 'config': new, 'status': 'archived' if form.get('archived') else 'active'}
            if g.account['is_superadmin']:
                fields['guest_limit'] = _int_or_none(form.get('guest_limit'))
                fields['owner_account_id'] = _int_or_none(form.get('owner'))
            db.update_event(event_id, **fields)
            flash('Настройки сохранены — бот уже отвечает по-новому', 'ok')
            return redirect(url_for('event_settings', event_id=event_id))
        return render_template('event_settings.html', event=event, tab='settings', config=config, advanced=advanced,
                               accounts=accounts, form=None)

    @app.post('/events/<int:event_id>/delete')
    @login_required
    def event_delete(event_id):
        event = load_event(event_id)
        if request.form.get('confirm', '').strip().upper() != event['code']:
            flash('Для удаления введите код мероприятия', 'error')
            return redirect(url_for('event_settings', event_id=event_id))
        db.delete_event(event_id)
        logger.info(f"Web: {g.account['login']} deleted event {event_id}")
        flash(f"Мероприятие «{event['title']}» и все его данные удалены", 'ok')
        return redirect(url_for('index'))

    # ================= аккаунты (только владелец сервиса) =================

    @app.route('/accounts', methods=['GET', 'POST'])
    @superadmin_required
    def accounts():
        if request.method == 'POST':
            login_ = request.form.get('login', '').strip().lower()
            password = request.form.get('password', '')
            if not login_ or db.account_by_login(login_):
                flash('Логин пустой или уже занят', 'error')
            elif len(password) < MIN_PASSWORD:
                flash(f'Пароль — не короче {MIN_PASSWORD} символов', 'error')
            else:
                db.create_account(login_, generate_password_hash(password), request.form.get('name', '').strip(),
                                  is_superadmin=bool(request.form.get('is_superadmin')))
                flash(f'Аккаунт {login_} создан. Передайте логин и пароль организатору.', 'ok')
                return redirect(url_for('accounts'))
        return render_template('accounts.html', accounts=db.list_accounts())

    @app.post('/accounts/<int:account_id>/password')
    @superadmin_required
    def account_password(account_id):
        if db.get_account(account_id) is None:
            abort(404)
        password = request.form.get('password', '')
        if len(password) < MIN_PASSWORD:
            flash(f'Пароль — не короче {MIN_PASSWORD} символов', 'error')
        else:
            db.update_account(account_id, password_hash=generate_password_hash(password))
            flash('Пароль изменён', 'ok')
        return redirect(url_for('accounts'))

    @app.post('/accounts/<int:account_id>/delete')
    @superadmin_required
    def account_delete(account_id):
        if account_id == g.account['id']:
            flash('Нельзя удалить свой аккаунт', 'error')
        else:
            db.delete_account(account_id)
            flash('Аккаунт удалён, его мероприятия остались без владельца — назначьте нового в настройках', 'ok')
        return redirect(url_for('accounts'))

    # ================= публичные страницы =================

    @app.route('/privacy')
    def privacy():
        return render_template('privacy.html', p=bot.privacy)

    @app.route('/healthz')
    def healthz():
        return {'ok': True, 'messengers': list(bot.messengers)}

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def error_page(e):
        return render_template('error.html', error=e), e.code

    return app


def _int_or_none(value):
    value = (value or '').strip()
    return int(value) if value.isdigit() else None


def _float_or_none(value):
    value = (value or '').strip().replace(',', '.')
    if not value:
        return None
    return float(value)


def _config_from_form(config: Dict, form) -> Dict:
    """Основные поля формы → копия настроек. None, если числа введены неверно."""
    import copy
    new = copy.deepcopy(config)
    ev = new.setdefault('event', {})
    for key in ('welcome', 'date_time', 'dresscode', 'program', 'rsvp_deadline'):
        ev[key] = form.get(key, '').strip()
    place = ev.setdefault('place', {})
    for key in ('name', 'address', 'how_to_get'):
        place[key] = form.get(f'place_{key}', '').strip()
    try:
        place['lat'] = _float_or_none(form.get('place_lat'))
        place['lon'] = _float_or_none(form.get('place_lon'))
        size = form.get('max_party_size', '').strip()
        new.setdefault('rsvp', {})['max_party_size'] = int(size) if size else 5
    except ValueError:
        return None
    new['rsvp']['question'] = form.get('rsvp_question', '').strip() or 'Сможете ли вы прийти?'
    menu = new.setdefault('menu', {})
    for key in ('food', 'drink'):
        section = menu.setdefault(key, {})
        section['question'] = form.get(f'{key}_question', '').strip() or section.get('question', '')
        section['options'] = [line.strip() for line in form.get(f'{key}_options', '').splitlines() if line.strip()]
    return new

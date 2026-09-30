"""Тесты веб-панели на тестовом клиенте Flask"""
from io import BytesIO

import openpyxl
import pytest
from werkzeug.security import generate_password_hash

from core import bot as b
from web.app import create_app

PASSWORD = 'secret-pass-1'


@pytest.fixture
def web(env):
    app = create_app(env.db, env.bot, {'secret_key': 'test', 'default_guest_limit': 30})
    app.config['TESTING'] = True
    env.admin = env.db.create_account('owner', generate_password_hash(PASSWORD), 'Владелец', is_superadmin=True)
    env.org = env.db.create_account('agency', generate_password_hash(PASSWORD), 'Агентство «Праздник»')
    env.other = env.db.create_account('other', generate_password_hash(PASSWORD), 'Другой организатор')
    return app


def client_for(app, login, password=PASSWORD):
    client = app.test_client()
    client.get('/login')
    client.post('/login', data={'login': login, 'password': password, 'csrf': csrf(client)})
    return client


def csrf(client):
    with client.session_transaction() as s:
        return s['csrf']


def post(client, url, **data):
    data['csrf'] = csrf(client)
    return client.post(url, data=data, follow_redirects=True)


def page(response):
    return response.get_data(as_text=True)


def test_login_required_wrong_password_and_csrf(web, env):
    client = web.test_client()
    assert client.get('/').status_code == 302
    client.get('/login')
    r = client.post('/login', data={'login': 'agency', 'password': 'wrong', 'csrf': csrf(client)})
    assert 'Неверный логин или пароль' in page(r)
    r = client.post('/login', data={'login': 'agency', 'password': PASSWORD})  # без CSRF-токена
    assert r.status_code == 400
    client = client_for(web, 'agency')
    assert 'Мои мероприятия' in page(client.get('/'))


def test_organizer_creates_event_with_default_limit_and_sees_only_own(web, env):
    org = client_for(web, 'agency')
    r = post(org, '/events/new', title='Свадьба Анны и Ивана', date_time='12 июня', place_name='Лебедь')
    assert 'Мероприятие создано' in page(r)
    event = env.db.list_events(env.org['id'])[0]
    assert event['guest_limit'] == 30 and event['owner_account_id'] == env.org['id']
    assert env.db.get_event(event['id'])['config']['event']['place']['name'] == 'Лебедь'
    assert f"https://telegram.example/{event['code']}" in page(r)  # ссылка для гостей на обзоре

    foreign = env.event('Чужая свадьба', owner_account_id=env.other['id'])
    assert org.get(f"/events/{foreign['id']}").status_code == 404
    assert org.get(f"/events/{foreign['id']}/export.xlsx").status_code == 404
    assert 'Чужая свадьба' not in page(org.get('/'))
    assert org.get('/accounts').status_code == 403

    admin = client_for(web, 'owner')
    home = page(admin.get('/'))
    assert 'Сервис целиком' in home and 'Чужая свадьба' in home and 'Свадьба Анны и Ивана' in home


def test_overview_guests_filter_and_delete_on_request(web, env):
    ev = env.event(owner_account_id=env.org['id'])
    env.team(ev)
    env.guest('telegram', 100, 'Петр Иванов', ev, rsvp='yes')
    env.guest('vk', 101, 'Ирина Смирнова', ev)
    env.say('telegram', 100, b.BTN_PREFS)
    env.say('telegram', 100, b.FOR_ME_PREFIX + 'Петр Иванов')
    env.say('telegram', 100, '🐟 Рыба')
    env.say('telegram', 100, '🥂 Вино белое')
    org = client_for(web, 'agency')

    overview = page(org.get(f"/events/{ev['id']}"))
    assert 'Придут: 1' in overview and '🐟 Рыба' in overview and 'Нет ответа' in overview

    guests = page(org.get(f"/events/{ev['id']}/guests?rsvp=yes"))
    assert 'Петр Иванов' in guests and 'Ирина Смирнова' not in guests and 'Рыба' in guests
    assert 'Ирина' in page(org.get(f"/events/{ev['id']}/guests?q=ирина"))

    team = page(org.get(f"/events/{ev['id']}/team"))
    assert 'Олег Ведущий' in team and 'Фотограф' in team  # фотограф ещё не подключился

    irina = [g for g in env.db.guests(ev['id']) if g['full_name'] == 'Ирина Смирнова'][0]
    r = post(org, f"/events/{ev['id']}/members/{irina['id']}/delete")
    assert 'удалены' in page(r)
    assert [g['full_name'] for g in env.db.guests(ev['id'])] == ['Петр Иванов']
    assert env.db.consent_log()[0]['action'] == 'deleted_by_organizer'


def test_settings_change_bot_texts_validate_and_archive(web, env):
    ev = env.event(owner_account_id=env.org['id'])
    env.guest('telegram', 100, 'Петр Иванов', ev)
    org = client_for(web, 'agency')
    settings = page(org.get(f"/events/{ev['id']}/settings"))
    assert 'Дресс-код' in settings and 'guest_channels' in settings

    config = env.db.get_event(ev['id'])['config']
    from core.event_config import dump_yaml
    advanced = dump_yaml({k: config[k] for k in ('roles', 'guest_channels', 'rooms', 'export_roles')})
    form = dict(title='Свадьба мечты', welcome='Привет!', date_time='1 июля', dresscode='Всё белое',
                program='', rsvp_deadline='', place_name='Сад', place_address='ул. Садовая, 1', place_lat='55,75',
                place_lon='37.61', place_how_to_get='', rsvp_question='Придёте?', max_party_size='4',
                food_question='Блюдо?', food_options='Паста\nСалат', drink_question='Напиток?',
                drink_options='Сок\nВино', advanced=advanced)

    r = post(org, f"/events/{ev['id']}/settings", **{**form, 'advanced': 'roles: [broken'})
    assert 'Ошибка в расширенных настройках' in page(r)
    r = post(org, f"/events/{ev['id']}/settings",
             **{**form, 'advanced': advanced.replace('- organizer\n  - couple\n  - decorator',
                                                      '- organizer\n  - couple\n  - florist')})
    assert 'роли «florist» нет в roles' in page(r)
    assert env.db.get_event(ev['id'])['title'] == ev['title']  # ничего не сохранилось

    r = post(org, f"/events/{ev['id']}/settings", **form)
    assert 'Настройки сохранены' in page(r)
    saved = env.db.get_event(ev['id'])
    assert saved['title'] == 'Свадьба мечты' and saved['config']['event']['place']['lat'] == 55.75
    assert saved['config']['menu']['food']['options'] == ['Паста', 'Салат']
    assert saved['guest_limit'] is None  # организатор не может менять лимит

    env.say('telegram', 100, b.BTN_DRESS)
    assert env.tg.texts(100)[-1] == 'Всё белое'
    env.say('telegram', 100, b.BTN_MENU)
    assert [b.BTN_PLACE] in env.tg.last(100)[3]  # программа пустая — в меню осталось только «Место и время»

    post(org, f"/events/{ev['id']}/settings", **{**form, 'archived': '1'})
    env.say('telegram', 100, b.BTN_DRESS)
    assert 'завершено' in env.tg.texts(100)[-1]


def test_superadmin_sets_limit_and_owner(web, env):
    ev = env.event(owner_account_id=env.org['id'])
    admin = client_for(web, 'owner')
    config = env.db.get_event(ev['id'])['config']
    from core.event_config import dump_yaml
    form = dict(title=ev['title'], welcome='', date_time='', dresscode='', program='', rsvp_deadline='',
                place_name='', place_address='', place_lat='', place_lon='', place_how_to_get='',
                rsvp_question='', max_party_size='5', food_question='', food_options='Мясо',
                drink_question='', drink_options='Вода',
                advanced=dump_yaml({k: config[k] for k in ('roles', 'guest_channels', 'rooms', 'export_roles')}),
                guest_limit='150', owner=str(env.other['id']))
    assert 'Настройки сохранены' in page(post(admin, f"/events/{ev['id']}/settings", **form))
    saved = env.db.get_event(ev['id'])
    assert saved['guest_limit'] == 150 and saved['owner_account_id'] == env.other['id']


def test_web_broadcast_export_invites_and_messages(web, env):
    ev = env.event(owner_account_id=env.org['id'])
    env.team(ev)
    env.guest('telegram', 100, 'Петр Иванов', ev, rsvp='yes')
    env.guest('vk', 101, 'Ирина Смирнова', ev)
    org = client_for(web, 'agency')

    assert 'Кто не ответил — 1 чел.' in page(org.get(f"/events/{ev['id']}/broadcast"))
    assert 'Выберите получателей' in page(post(org, f"/events/{ev['id']}/broadcast", audience='Кто не ответил', text=''))
    r = post(org, f"/events/{ev['id']}/broadcast", audience='Кто не ответил', text='Ответьте до пятницы, пожалуйста')
    assert 'Отправлено: 1' in page(r) and 'Ответьте до пятницы' in page(r)
    assert 'Ответьте до пятницы' in env.vk.texts(101)[-1]
    assert not any('пятницы' in t for t in env.tg.texts(100))
    env.say('vk', 101, 'Приду!', reply_to=env.vk.last(101)[4])
    assert 'Приду!' in env.tg.texts(1)[-1]

    messages = page(org.get(f"/events/{ev['id']}/messages"))
    assert 'Приду!' in messages and 'Агентство «Праздник»' in messages

    xlsx = org.get(f"/events/{ev['id']}/export.xlsx")
    wb = openpyxl.load_workbook(BytesIO(xlsx.data))
    assert sorted(r[1] for r in wb['Гости'].iter_rows(min_row=2, values_only=True) if r[0]) == \
        ['Ирина Смирнова', 'Петр Иванов']

    old = env.db.invite_codes(ev['id'], ['host'])['host']
    post(org, f"/events/{ev['id']}/invites/host/regenerate")
    new = env.db.invite_codes(ev['id'], ['host'])['host']
    assert new != old and env.db.resolve_code(old) is None and env.db.resolve_code(new)[2] == 'host'


def test_delete_event_requires_code_and_accounts_management(web, env):
    ev = env.event(owner_account_id=env.org['id'])
    env.guest('telegram', 100, 'Петр Иванов', ev)
    org = client_for(web, 'agency')
    assert 'введите код' in page(post(org, f"/events/{ev['id']}/delete", confirm='нет'))
    assert env.db.get_event(ev['id']) is not None
    post(org, f"/events/{ev['id']}/delete", confirm=ev['code'].lower())
    assert env.db.get_event(ev['id']) is None
    assert env.db.users_by_platform_ids('telegram', ['100']) == []  # гость больше нигде не участвует

    admin = client_for(web, 'owner')
    r = post(admin, '/accounts', login='NewOrg', name='Новое агентство', password='short')
    assert 'не короче' in page(r)
    r = post(admin, '/accounts', login='NewOrg', name='Новое агентство', password='long-enough-1')
    assert 'создан' in page(r) and env.db.account_by_login('neworg') is not None
    assert 'Мои мероприятия' in page(client_for(web, 'neworg', 'long-enough-1').get('/'))
    post(admin, f"/accounts/{env.db.account_by_login('neworg')['id']}/delete")
    assert env.db.account_by_login('neworg') is None


def test_privacy_page_is_public_and_password_change(web, env):
    r = web.test_client().get('/privacy')
    assert r.status_code == 200 and 'ИП Тестов Т.Т.' in page(r) and '/delete' in page(r)
    org = client_for(web, 'agency')
    assert 'неверен' in page(post(org, '/account', current='nope', password='new-password-1'))
    assert 'Пароль изменён' in page(post(org, '/account', current=PASSWORD, password='new-password-1'))

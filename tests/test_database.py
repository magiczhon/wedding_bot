import pytest
import os
import sqlite3
from unittest.mock import patch, mock_open
from database.db import save_user_info, save_preferences, is_user_registered, get_all_preferences, get_all_chat_ids

# Фикстура для настройки тестовой базы данных
@pytest.fixture(scope="module")
def setup_test_db():
    # Временная база данных для тестов
    test_db = "test_guests.db"
    
    # Подменяем config["db_name"] в модуле db
    with patch("builtins.open", new_callable=mock_open, read_data=f"{{\"db_name\": \"{test_db}\"}}"):
        from importlib import reload
        from database import db
        reload(db)  # Перезагружаем модуль db с подмененным config
        db.init_db()
    
    yield test_db
    
    # Удаляем временную базу данных после тестов
    if os.path.exists(test_db):
        os.remove(test_db)

# Фикстура для очистки таблиц перед каждым тестом
@pytest.fixture(autouse=True)
def clear_tables(setup_test_db):
    db_path = setup_test_db
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM guests")
    cursor.execute("DELETE FROM guest_preferences")
    conn.commit()
    conn.close()


def test_init_db(setup_test_db):
    # Проверяем, что таблицы созданы
    conn = sqlite3.connect(setup_test_db)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(guests)")
    guests_columns = cursor.fetchall()
    cursor.execute("PRAGMA table_info(guest_preferences)")
    preferences_columns = cursor.fetchall()
    conn.close()
    
    # Проверяем наличие основных столбцов в таблице guests
    assert len(guests_columns) > 0
    guest_column_names = [col[1] for col in guests_columns]
    expected_guest_columns = ['id', 'user_id', 'chat_id', 'username', 'full_name', 'updated_at']
    for col in expected_guest_columns:
        assert col in guest_column_names
        
    # Проверяем наличие основных столбцов в таблице guest_preferences
    assert len(preferences_columns) > 0
    pref_column_names = [col[1] for col in preferences_columns]
    expected_pref_columns = ['id', 'guest_id', 'guest_name', 'food', 'drink', 'updated_at']
    for col in expected_pref_columns:
        assert col in pref_column_names

def test_save_user_info(setup_test_db):
    save_user_info(123, 456,  "johndoe", "JD", "John Doe")
    
    conn = sqlite3.connect(setup_test_db)
    cursor = conn.cursor()
    cursor.execute("SELECT username, full_name FROM guests WHERE user_id = ?", (123,))
    result = cursor.fetchone()
    conn.close()
    
    # Проверяем, что результат не None
    assert result is not None
    
    # Проверяем полное совпадение с ожидаемыми значениями
    expected_username, expected_full_name = "johndoe", "John Doe"
    assert result[0] == expected_username, f"Ожидалось {expected_username}, получено {result[0]}"
    assert result[1] == expected_full_name, f"Ожидалось {expected_full_name}, получено {result[1]}"

def test_save_preferences(setup_test_db):
    # Сначала сохраняем пользователя
    save_user_info(123, 456,  "johndoe", "JD", "John Doe")
    
    # Затем сохраняем предпочтения
    save_preferences(123, "Mame Rame", "Pizza", "Cola")
    save_preferences(123, "Kuko Jambo", "Burger", "Juce")   
    conn = sqlite3.connect(setup_test_db)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT g.full_name, gp.guest_name, gp.food, gp.drink
        FROM guests g
        JOIN guest_preferences gp ON g.id = gp.guest_id
        WHERE g.user_id = ?
    """, (123,))
    results = cursor.fetchall()
    conn.close()
    
    # Проверяем, что результат не None
    assert results is not None
    
    # Проверяем полное совпадение с ожидаемыми значениями
    expected_full_name, expected_guest_name, expected_food, expected_drink = "John Doe", "Mame Rame", "Pizza", "Cola"
    result = results[0]
    assert result[0] == expected_full_name, f"Ожидалось {expected_full_name}, получено {result[0]}"
    assert result[1] == expected_guest_name, f"Ожидалось {expected_guest_name}, получено {result[1]}"
    assert result[2] == expected_food, f"Ожидалось {expected_food}, получено {result[2]}"
    assert result[3] == expected_drink, f"Ожидалось {expected_drink}, получено {result[3]}"

    expected_full_name, expected_guest_name, expected_food, expected_drink = "John Doe", "Kuko Jambo", "Burger", "Juce"
    result = results[1]
    assert result[0] == expected_full_name, f"Ожидалось {expected_full_name}, получено {result[0]}"
    assert result[1] == expected_guest_name, f"Ожидалось {expected_guest_name}, получено {result[1]}"
    assert result[2] == expected_food, f"Ожидалось {expected_food}, получено {result[2]}"
    assert result[3] == expected_drink, f"Ожидалось {expected_drink}, получено {result[3]}"


def test_is_user_registered(setup_test_db):
    # Пользователь не зарегистрирован
    assert not is_user_registered(999)
    
    # Регистрируем пользователя
    save_user_info(999, 111,  "janedoe", "JD", "Jane Doe")
    
    # Проверяем, что пользователь зарегистрирован
    assert is_user_registered(999)

def test_get_all_preferences(setup_test_db):
    save_user_info(123, 456,  "johndoe", "JD", "John Doe")
    save_preferences(123, "John Doe", "Pizza", "Cola")
    
    save_user_info(124, 457,  "janedoe", "JD", "Jane Doe")
    save_preferences(124, "Jane Doe", "Sushi", "Water")
    
    preferences = get_all_preferences()
    
    assert len(preferences) == 2
    # Проверяем наличие данных
    full_names = [p[0] for p in preferences]
    assert "John Doe" in full_names
    assert "Jane Doe" in full_names

def test_get_all_chat_ids(setup_test_db):
    save_user_info(123, 456,  "johndoe", "JD", "John Doe")
    save_user_info(124, 457,  "janedoe", "JD", "Jane Doe")
    
    chat_ids = get_all_chat_ids()
    
    assert len(chat_ids) == 2
    assert 456 in chat_ids
    assert 457 in chat_ids


import sqlite3
import json

from utils.logger import logger
from datetime import datetime

# Загружаем конфигурацию
with open("config.json", "r") as config_file:
    config = json.load(config_file)

DB_NAME = config["db_name"]

def init_db():
    logger.info(f'Initialization database {DB_NAME}')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Создаем таблицу guests
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            chat_id INTEGER,
            username TEXT,
            tg_name TEXT,
            full_name TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Создаем таблицу guest_preferences
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guest_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guest_id INTEGER,
            guest_name TEXT,
            food TEXT,
            drink TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

def save_user_info(user_id, chat_id, username, tg_name, full_name):
    logger.info(f'Save user info: {user_id = }, {username = }, {full_name = }, {chat_id = }')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO guests (user_id, chat_id, username, tg_name, full_name, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, chat_id, username, tg_name, full_name, datetime.now()))
    conn.commit()
    conn.close()

def save_preferences(user_id: int, guest_name: str, food: str, drink: str):
    logger.info(f'Save info about guest: {guest_name = }, {food = }, {drink = }')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Получаем ID существующего гостя
    cursor.execute("SELECT id FROM guests WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    guest_id = result[0] if result else user_id
    
    # Вставляем предпочтения по еде и напиткам в отдельной таблице
    cursor.execute('''
        INSERT INTO guest_preferences (guest_id, guest_name, food, drink, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (guest_id, guest_name, food, drink, datetime.now()))
    
    conn.commit()
    conn.close()

def is_user_registered(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM guests WHERE user_id = ?", (user_id,))
    result = cursor.fetchone() is not None
    conn.close()
    return result

def get_all_preferences():
    logger.info('Select all guests info')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT g.full_name, g.username, gp.guest_name, gp.food, gp.drink, gp.updated_at 
        FROM guests g 
        JOIN guest_preferences gp ON g.id = gp.guest_id
    ''')
    results = cursor.fetchall()
    conn.close()
    return results

def get_all_chat_ids():
    logger.info('Select all chat ids')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM guests WHERE chat_id IS NOT NULL")
    results = cursor.fetchall()
    conn.close()
    return [r[0] for r in results]

def get_chat_ids(user_ids: list):
    logger.info(f'Select chat ids of users: {user_ids = }')
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    results = []
    for user_id in user_ids:
        cursor.execute("SELECT chat_id FROM guests WHERE user_id = ?", (user_id,))
        results.extend(cursor.fetchall())
    conn.close()
    return results

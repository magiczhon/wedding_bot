import sqlite3
from datetime import datetime

DB_NAME = "guests.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            full_name TEXT,
            food TEXT,
            drink TEXT,
            food_drink_name TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_user_info(user_id: int, full_name: str, username: str = None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO guests (user_id, username, full_name, updated_at)
        VALUES (?, ?, ?, ?)
    ''', (user_id, username, full_name, datetime.now()))
    conn.commit()
    conn.close()

def save_preferences(user_id: int, username: str, name: str, food: str, drink: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO guests (user_id, username, full_name, food, drink, food_drink_name, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, username, name, food, drink, name, datetime.now()))
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
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM guests")
    results = cursor.fetchall()
    conn.close()
    return results

def update_guest_info(guest_id: int, full_name: str = None, food: str = None, drink: str = None) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Проверяем, существует ли гость с таким ID
    cursor.execute("SELECT 1 FROM guests WHERE id = ?", (guest_id,))
    if cursor.fetchone() is None:
        conn.close()
        return False
    
    # Обновляем данные
    cursor.execute('''
        UPDATE guests 
        SET full_name = COALESCE(?, full_name),
            food = COALESCE(?, food),
            drink = COALESCE(?, drink),
            food_drink_name = COALESCE(?, food_drink_name),
            updated_at = ?
        WHERE id = ?
    ''', (full_name, food, drink, full_name, datetime.now(), guest_id))
    
    conn.commit()
    conn.close()
    return True
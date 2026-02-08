import telebot
from telebot import types
import json
from database.db import get_all_preferences
import openpyxl
from openpyxl.styles import Font, Alignment
import os
import re


def register_handlers(bot: telebot.TeleBot):
    def get_admin_ids():
        with open('config.json', 'r') as f:
            config = json.load(f)
            return config.get("admin_ids", [])

    @bot.message_handler(func=lambda message: message.text == '📋 Просмотреть гостей')
    def view_guests(message):
        if message.from_user.id not in get_admin_ids():
            bot.send_message(message.chat.id, "У вас нет прав для выполнения этой операции.")
            return
            
        guests = get_all_preferences()
        
        if not guests:
            bot.send_message(message.chat.id, "База данных пуста.")
            return
        
        # Создаем и отправляем Excel-файл
        filename = create_excel_file(guests)
        with open(filename, 'rb') as file:
            bot.send_document(message.chat.id, file)
        
        # Удаляем временный файл
        os.remove(filename)

    def create_excel_file(guests):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Гости"
        
        # Заголовки
        headers = ["ID", "Telegram ID", "Username", "ФИО (регистрация)", "Еда", "Напиток", "ФИО (еда/напитки)", "Дата обновления"]
        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.value = header
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        
        # Данные
        for row_idx, guest in enumerate(guests, 2):
            ws.cell(row=row_idx, column=1, value=guest[0])
            ws.cell(row=row_idx, column=2, value=guest[1])
            ws.cell(row=row_idx, column=3, value=f"@{guest[2]}" if guest[2] else "не указан")
            ws.cell(row=row_idx, column=4, value=guest[3])
            ws.cell(row=row_idx, column=5, value=guest[4] if guest[4] else "не указано")
            ws.cell(row=row_idx, column=6, value=guest[5] if guest[5] else "не указано")
            ws.cell(row=row_idx, column=7, value=guest[6] if guest[6] else "не указано")
            ws.cell(row=row_idx, column=8, value=str(guest[7]))
        
        # Автоширина столбцов
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width
        
        filename = "guests.xlsx"
        wb.save(filename)
        return filename

    @bot.message_handler(func=lambda message: message.text == '✏️ Изменить данные')
    def request_guest_id(message):
        if message.from_user.id not in get_admin_ids():
            bot.send_message(message.chat.id, "У вас нет прав для выполнения этой операции.")
            return
            
        bot.send_message(message.chat.id, "Введите ID гостя, данные которого вы хотите изменить:")
        bot.register_next_step_handler(message, process_guest_id_step)

    
    def process_guest_id_step(message):
        try:
            guest_id = int(message.text)
            bot.send_message(message.chat.id, f"Введите новые данные для гостя {guest_id}.\nФормат: Имя Фамилия;Еда;Напиток\nПример: Иван Петров;Мясо;Водка")
            bot.register_next_step_handler(message, lambda msg: process_update_data_step(msg, guest_id))
        except ValueError:
            bot.send_message(message.chat.id, "Неверный формат ID. Попробуйте снова.")
            
    def process_update_data_step(message, guest_id):
        try:
            parts = message.text.split(';')
            if len(parts) != 3:
                bot.send_message(message.chat.id, "Неверный формат данных. Используйте: ФИО;Еда;Напиток")
                return
                
            full_name, food, drink = [part.strip() for part in parts]
            from database.db import update_guest_info
            success = update_guest_info(guest_id, full_name, food, drink)
            
            if success:
                bot.send_message(message.chat.id, f"Данные гостя {guest_id} успешно обновлены!")
            else:
                bot.send_message(message.chat.id, f"Гость с ID {guest_id} не найден.")
                
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка при обновлении данных: {str(e)}")

        # Возвращаем админское меню
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn4 = types.KeyboardButton('📋 Просмотреть гостей')
        btn5 = types.KeyboardButton('✏️ Изменить данные')
        markup.add(btn4, btn5)
        bot.send_message(message.chat.id, "Возврат в меню администратора:", reply_markup=markup)

    @bot.message_handler(func=lambda message: message.text == '⬅️ Назад')
    def go_back_to_main(message):
        main_menu = types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn1 = types.KeyboardButton('👗 Дресс-код')
        btn2 = types.KeyboardButton('📍 Место проведения')
        btn3 = types.KeyboardButton('🍽 Пожелания по еде �� напиткам')
        main_menu.add(btn1, btn2, btn3)
        
        bot.send_message(message.chat.id, "Возврат в основное меню:", reply_markup=main_menu)
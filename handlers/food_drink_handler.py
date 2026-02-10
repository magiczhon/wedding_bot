import telebot
from telebot import types
from utils.logger import logger

# Словарь для хранения состояния пользователей
user_states = {}

# Константы состояний
STATE_WAITING_NAME = 'waiting_name'
STATE_WAITING_FOOD = 'waiting_food'
STATE_WAITING_DRINK = 'waiting_drink'

def register_handlers(bot: telebot.TeleBot):
    @bot.message_handler(func=lambda message: message.text == '🍽 Добавить пожелания по еде и напиткам гостя')
    def food_drink_start(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        user_states[user_id] = {'state': STATE_WAITING_NAME, 'chat_id': chat_id}
        bot.send_message(chat_id, "Укажите  имя и фамилию гостя:")

    @bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('state') == STATE_WAITING_NAME)
    def name_received(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        user_states[user_id]['name'] = message.text
        user_states[user_id]['state'] = STATE_WAITING_FOOD
        
        # Клавиатура для выбора еды
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        btn1 = types.KeyboardButton('🥩 Мясо')
        btn2 = types.KeyboardButton('🐟 Рыба')
        markup.add(btn1, btn2)
        
        bot.send_message(chat_id, "Отлично! Теперь выберите, что вы бы хотели из основного:", reply_markup=markup)

    @bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('state') == STATE_WAITING_FOOD)
    def food_received(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        food_choice = message.text
        
        # Сохраняем выбор
        if '🥩 Мясо' in food_choice:
            user_states[user_id]['food'] = 'Мясо'
        elif '🐟 Рыба' in food_choice:
            user_states[user_id]['food'] = 'Рыба'
        else:
            user_states[user_id]['food'] = 'не указано'
            
        user_states[user_id]['state'] = STATE_WAITING_DRINK
        
        # Клавиатура для выбора напитков
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        btn1 = types.KeyboardButton('🍸 Водка')
        btn2 = types.KeyboardButton('🥃 Коньяк')
        btn3 = types.KeyboardButton('🥂 Вино белое')
        btn4 = types.KeyboardButton('🍷 Вино красное')
        markup.add(btn1, btn2, btn3, btn4)
        
        bot.send_message(chat_id, "Теперь выберите, что вы бы хотели из напитков:", reply_markup=markup)

    @bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('state') == STATE_WAITING_DRINK)
    def drink_received(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        drink_choice = message.text
        
        # Сохраняем выбор напитка
        if '🍸 Водка' in drink_choice:
            drink = 'Водка'
        elif '🥃 Коньяк' in drink_choice:
            drink = 'Коньяк'
        elif '🥂 Вино белое' in drink_choice:
            drink = 'Вино белое'
        elif '🍷 Вино красное' in drink_choice:
            drink = 'Вино красное'
        else:
            drink = 'не указано'
            
        user_data = user_states.get(user_id, {})
        guest_name = user_data.get('name', 'Не указано')
        food = user_data.get('food', 'не указано')

        # Сохранение в БД
        from database.db import save_preferences
        save_preferences(user_id, guest_name, food, drink)

        # Возвращаем клавиатуру к основному меню
        main_menu = types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn1 = types.KeyboardButton('👗 Дресс-код')
        btn2 = types.KeyboardButton('📍 Место проведения')
        btn3 = types.KeyboardButton('🍽 Добавить пожелания по еде и напиткам гостя')
        main_menu.add(btn1, btn2, btn3)
        
        bot.send_message(chat_id, f"Для гостя {guest_name} Вы выбрали: {food} и {drink}. Мы постараемся учесть это 🥂", reply_markup=main_menu)

        # Очистка состояния
        if user_id in user_states:
            del user_states[user_id]
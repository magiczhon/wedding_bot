import telebot
from telebot import types
from database.db import get_all_preferences, update_guest_info

# Словарь для хранения состояния пользователей
user_states = {}

# Константы состояний
STATE_WAITING_NAME = 'waiting_name'
STATE_WAITING_FOOD = 'waiting_food'
STATE_WAITING_DRINK = 'waiting_drink'

def register_handlers(bot: telebot.TeleBot):
    @bot.message_handler(func=lambda message: message.text == '🍽 Пожелания по еде и напиткам')
    def food_drink_start(message):
        user_states[message.from_user.id] = {'state': STATE_WAITING_NAME}
        bot.send_message(message.chat.id, "Для начала, пожалуйста, укажите ваше имя и фамилию:")

    @bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('state') == STATE_WAITING_NAME)
    def name_received(message):
        user_id = message.from_user.id
        user_states[user_id]['name'] = message.text
        user_states[user_id]['state'] = STATE_WAITING_FOOD
        
        # Клавиатура для выбора еды
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        btn1 = types.KeyboardButton('🥩 Мясо')
        btn2 = types.KeyboardButton('🐟 Рыба')
        markup.add(btn1, btn2)
        
        bot.send_message(message.chat.id, "Отлично! Теперь выберите, что вы бы хотели из основного:", reply_markup=markup)

    @bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('state') == STATE_WAITING_FOOD)
    def food_received(message):
        user_id = message.from_user.id
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
        markup.add(btn1, btn2)
        
        bot.send_message(message.chat.id, "Отлично! А какой алкоголь вы предпочитаете?", reply_markup=markup)

    @bot.message_handler(func=lambda message: user_states.get(message.from_user.id, {}).get('state') == STATE_WAITING_DRINK)
    def drink_received(message):
        user_id = message.from_user.id
        drink_choice = message.text
        
        # Сохраняем выбор напитка
        if '🍸 Водка' in drink_choice:
            drink = 'Водка'
        elif '🥃 Коньяк' in drink_choice:
            drink = 'Коньяк'
        else:
            drink = 'не указано'
            
        user_data = user_states.get(user_id, {})
        name = user_data.get('name', 'Не указано')
        food = user_data.get('food', 'не указано')
        username = message.from_user.username or "unknown"

        # Сохранение в БД
        from database.db import save_preferences
        save_preferences(user_id, username, name, food, drink)

        # Возвращаем клавиатуру к основному меню
        main_menu = types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn1 = types.KeyboardButton('👗 Дресс-код')
        btn2 = types.KeyboardButton('📍 Место проведения')
        btn3 = types.KeyboardButton('🍽 Пожелания по еде и напиткам')
        main_menu.add(btn1, btn2, btn3)
        
        bot.send_message(message.chat.id, f"Спасибо, {name}! Вы выбрали: {food} и {drink}. Мы постараемся учесть это 🥂", reply_markup=main_menu)

        # Очистка состояния
        if user_id in user_states:
            del user_states[user_id]
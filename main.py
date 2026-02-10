import json
import telebot

from utils.logger import logger
from telebot import types
from handlers import dreskcode_handler, location_handler, food_drink_handler, admin_handler
from database.db import init_db, is_user_registered

logger.info('Start app')
# Инициализация бота
config = json.load(open('config.json', 'r'))
API_TOKEN = config["token_id"]
ADMIN_IDS = config["admin_ids"]

bot = telebot.TeleBot(API_TOKEN)

# Инициализация базы данных
init_db()

# Основное меню
main_menu = types.ReplyKeyboardMarkup(resize_keyboard=True)
btn1 = types.KeyboardButton('👗 Дресс-код')
btn2 = types.KeyboardButton('📍 Место проведения')
btn3 = types.KeyboardButton('🍽 Добавить пожелания по еде и напиткам гостя')
main_menu.add(btn1, btn2, btn3)

# Меню администратора
admin_menu = types.ReplyKeyboardMarkup(resize_keyboard=True,)
btn4 = types.KeyboardButton('📋 Выгрузка списка гостей')
btn5 = types.KeyboardButton('📨 Отправить сообщение всем пользователям бота')
btn6 = types.KeyboardButton('Отправить сообщение ТЕСТОВЫМ пользователям')
admin_menu.add(btn4, btn5, btn6)


@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    logger.info(f'Попытка авторизации пользователя {user_id = }')
    # Проверяем, является ли пользователь администратором
    if user_id in ADMIN_IDS:
        bot.send_message(user_id, "Добро пожаловать, администратор! 🎯\nВы можете управлять данными гостей.\nДля переключения в режим пользователя используйте команду /user", reply_markup=admin_menu)
        return
    
    # Обычный пользователь
    if not is_user_registered(user_id):
        bot.send_message(user_id, "Добро пожаловать на свадебное приглашение! 🎉\nДля продолжения, пожалуйста, введите ваше полное имя (ФИО):")
        bot.register_next_step_handler(message, process_name_step)
    else:
        logger.info(f'Пользователь {user_id = } зашел в бота')
        bot.reply_to(message, "Рады вас снова видеть! 🥂\nВыберите один из пунктов ниже:", reply_markup=main_menu)


def process_name_step(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    full_name = message.text.strip()
    username = message.from_user.username
    tg_name = message.from_user.first_name
    
    if not full_name or len(full_name.split()) < 2:
        bot.send_message(message.chat.id, "Пожалуйста, имя И фамилию (и отчество, если хотите):")
        bot.register_next_step_handler(message, process_name_step)
        return
    
    # Сохраняем ФИО и user_id в базу
    logger.info(f'Register user {user_id = }, {username = }, {full_name = }, {chat_id = }')
    from database.db import save_user_info
    save_user_info(user_id, chat_id, username, tg_name, full_name)
     
    bot.send_message(message.chat.id, f"Спасибо, {full_name}! Вы успешно зарегистрированы.\nТеперь вы можете воспользоваться функциями бота.", reply_markup=main_menu)
    

@bot.message_handler(commands=['admin'])
def switch_to_admin(message):
    user_id = message.from_user.id
    if user_id in ADMIN_IDS:
        logger.info(f'Switch to admin ({user_id = })')
        bot.send_message(user_id, "Вы переключились в режим администратора. 🎯\nДля переключения обратно используйте команду /user", reply_markup=admin_menu)
    else:
        logger.info(f'Unsuccessful try switch to admin ({user_id = })')
        bot.send_message(message.chat.id, "У вас нет прав администратора.")


@bot.message_handler(commands=['user'])
def switch_to_user(message):
    user_id = message.from_user.id
    # Все пользователи могут переключиться в режим обычного пользователя
    if is_user_registered(user_id):
        bot.send_message(user_id, "Вы переключились в режим пользователя.", reply_markup=main_menu)
    else:
        bot.send_message(user_id, "Для начала, пожалуйста, введите ваше полное имя (ФИО):")
        bot.register_next_step_handler(message, process_name_step)


# Регистрация обработчиков
dreskcode_handler.register_handlers(bot)
location_handler.register_handlers(bot)
food_drink_handler.register_handlers(bot)
admin_handler.register_handlers(bot)

if __name__ == '__main__':
    bot.polling(none_stop=True)

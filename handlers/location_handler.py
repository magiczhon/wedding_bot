import telebot
from telebot import types

def register_handlers(bot: telebot.TeleBot):
    @bot.message_handler(func=lambda message: message.text == '📍 Место проведения')
    def location(message):
        bot.send_message(message.chat.id, "Место проведения: Ресторан \"Белоснежный Лебедь\" \nАдрес: г. Москва, ул. Цветочная, д. 15\nНачало: 16:00")
        bot.send_location(message.chat.id, 55.7558, 37.6176)  # Пример координат (Москва)
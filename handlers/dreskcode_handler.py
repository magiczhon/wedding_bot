import telebot
from telebot import types

def register_handlers(bot: telebot.TeleBot):
    @bot.message_handler(func=lambda message: message.text == '👗 Дресс-код')
    def dreskcode(message):
        bot.send_message(message.chat.id, "Дресс-код: формальный стиль 🕴\nЦвета: пастельные тона или классический чёрный/белый.\nНикаких спортивных костюмов, пожалуйста!")
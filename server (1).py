# main.py
import asyncio
import logging
import sqlite3
import re
import json
import random
from datetime import datetime, timedelta
from telethon import TelegramClient, events, functions, types
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import MessageEntityMention, MessageEntityTextUrl
import time
from threading import Lock
import requests

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== КОНФИГУРАЦИЯ ==========
API_ID = 35800959
API_HASH = "708e7d0bc3572355bcaf68562cc068f1"
BOT_TOKEN = '8606197799:AAGi3sOfrKUgWU9-hkwdAim9B3v0cyT3dXM'  # Замените на ваш токен бота
OWNER_ID = 8480939483  # ID владельца для получения уведомлений
TARGET_BOT = '@jaarskgocbot'  # Бот для поиска
DB_NAME = 'contest_bot.db'  # Название базы данных
CRYPTOBOT_TOKEN = '584253:AAtmtq8kSHDZ1mFRwa5rky9PLKRyrckYNLu'  # Токен CryptoBot
DAILY_BONUS_AMOUNT = 0.3  # Ежедневный бонус в рублях
REFERRAL_BONUS = 0.3  # Бонус за реферала
BOT_LINK = "https://t.me/pozzity_infobot"  # Ссылка на вашего бота
PRICE_PER_SEARCH = 6.5  # Стоимость одного поиска (13% комиссия = 1.3р)
TG_STARS_PRICE = 1.3  # Цена звезды Telegram в рублях

# Блокировка для БД
db_lock = Lock()

# ========== ИНИЦИАЛИЗАЦИЯ БД ==========
def get_db():
    return sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)

def init_db():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        
        # Таблица аккаунтов для поиска
        c.execute('''CREATE TABLE IF NOT EXISTS search_accounts (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     phone TEXT UNIQUE,
                     username TEXT,
                     first_name TEXT,
                     session_string TEXT,
                     is_active INTEGER DEFAULT 1,
                     last_check TEXT,
                     requests_used INTEGER DEFAULT 0,
                     daily_bonus_claimed TEXT
                     )''')
        
        # Таблица пользователей бота
        c.execute('''CREATE TABLE IF NOT EXISTS bot_users (
                     user_id INTEGER PRIMARY KEY,
                     username TEXT,
                     first_name TEXT,
                     balance REAL DEFAULT 0,
                     total_requests INTEGER DEFAULT 0,
                     referral_count INTEGER DEFAULT 0,
                     referral_earned REAL DEFAULT 0,
                     referred_by INTEGER,
                     join_date TEXT,
                     last_bonus_date TEXT,
                     is_banned INTEGER DEFAULT 0
                     )''')
        
        # Таблица рефералов
        c.execute('''CREATE TABLE IF NOT EXISTS referrals (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     referrer_id INTEGER,
                     referred_id INTEGER,
                     date TEXT,
                     bonus_earned REAL DEFAULT 0,
                     FOREIGN KEY (referrer_id) REFERENCES bot_users(user_id),
                     FOREIGN KEY (referred_id) REFERENCES bot_users(user_id)
                     )''')
        
        # Таблица запросов
        c.execute('''CREATE TABLE IF NOT EXISTS search_history (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     query TEXT,
                     result TEXT,
                     account_used INTEGER,
                     search_date TEXT,
                     cost REAL DEFAULT 0,
                     FOREIGN KEY (user_id) REFERENCES bot_users(user_id)
                     )''')
        
        # Таблица для кодов Telegram
        c.execute('''CREATE TABLE IF NOT EXISTS tg_codes (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     account_id INTEGER,
                     code TEXT,
                     received_at TEXT,
                     processed INTEGER DEFAULT 0,
                     FOREIGN KEY (account_id) REFERENCES search_accounts(id)
                     )''')
        
        # Таблица для зеркал
        c.execute('''CREATE TABLE IF NOT EXISTS mirrors (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     bot_token TEXT,
                     bot_username TEXT,
                     markup REAL DEFAULT 0,
                     is_private INTEGER DEFAULT 0,
                     created_at TEXT,
                     is_active INTEGER DEFAULT 1,
                     FOREIGN KEY (user_id) REFERENCES bot_users(user_id)
                     )''')
        
        # Таблица для администраторов
        c.execute('''CREATE TABLE IF NOT EXISTS admins (
                     user_id INTEGER PRIMARY KEY,
                     added_by INTEGER,
                     added_at TEXT,
                     permissions TEXT
                     )''')
        
        # Таблица для банов
        c.execute('''CREATE TABLE IF NOT EXISTS bans (
                     user_id INTEGER PRIMARY KEY,
                     banned_by INTEGER,
                     reason TEXT,
                     banned_at TEXT
                     )''')
        
        # Таблица для звезд Telegram
        c.execute('''CREATE TABLE IF NOT EXISTS stars_payments (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     user_id INTEGER,
                     stars_amount INTEGER,
                     rub_amount REAL,
                     payment_date TEXT,
                     status TEXT DEFAULT 'pending',
                     FOREIGN KEY (user_id) REFERENCES bot_users(user_id)
                     )''')
        
        # Миграции
        try:
            c.execute("ALTER TABLE bot_users ADD COLUMN is_banned INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        
        try:
            c.execute("ALTER TABLE search_accounts ADD COLUMN daily_bonus_claimed TEXT")
        except sqlite3.OperationalError:
            pass
        
        conn.commit()
        conn.close()

init_db()

# ========== ФУНКЦИИ ДЛЯ CRYPTOBOT ==========
class CryptoBot:
    def __init__(self, token):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
    
    def create_invoice(self, amount, currency="RUB", description="Пополнение баланса"):
        """Создает счет для оплаты через CryptoBot"""
        url = f"{self.base_url}/createInvoice"
        headers = {"Crypto-Pay-API-Token": self.token}
        data = {
            "asset": "RUB",
            "amount": amount,
            "description": description,
            "paid_btn_name": "openBot",
            "paid_btn_url": BOT_LINK
        }
        try:
            response = requests.post(url, headers=headers, json=data)
            if response.status_code == 200:
                result = response.json()
                if result.get('ok'):
                    return result['result']['invoice_id'], result['result']['pay_url']
            return None, None
        except Exception as e:
            logger.error(f"Ошибка создания счета: {e}")
            return None, None
    
    def check_payment(self, invoice_id):
        """Проверяет статус оплаты"""
        url = f"{self.base_url}/getInvoices"
        headers = {"Crypto-Pay-API-Token": self.token}
        params = {"invoice_ids": invoice_id}
        try:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 200:
                result = response.json()
                if result.get('ok') and result['result']['items']:
                    invoice = result['result']['items'][0]
                    if invoice['status'] == 'paid':
                        return True, invoice['amount']
            return False, 0
        except Exception as e:
            logger.error(f"Ошибка проверки оплаты: {e}")
            return False, 0

# ========== ФУНКЦИИ БД ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ==========
def get_user(user_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM bot_users WHERE user_id=?", (user_id,))
        user = c.fetchone()
        conn.close()
        return user

def create_user(user_id, username, first_name, referred_by=None):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        
        # Проверяем, существует ли пользователь
        c.execute("SELECT user_id FROM bot_users WHERE user_id=?", (user_id,))
        if c.fetchone():
            conn.close()
            return 0
        
        # Проверка реферера
        referrer_bonus = 0
        if referred_by and referred_by != user_id:
            c.execute("SELECT user_id FROM bot_users WHERE user_id=? AND is_banned=0", (referred_by,))
            if c.fetchone():
                # Начисляем бонус рефереру
                c.execute("UPDATE bot_users SET balance = balance + ?, referral_count = referral_count + 1, referral_earned = referral_earned + ? WHERE user_id=?",
                         (REFERRAL_BONUS, REFERRAL_BONUS, referred_by))
                referrer_bonus = REFERRAL_BONUS
                
                # Сохраняем реферала
                c.execute("INSERT INTO referrals (referrer_id, referred_id, date, bonus_earned) VALUES (?, ?, ?, ?)",
                         (referred_by, user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), REFERRAL_BONUS))
        
        # Создаем пользователя
        c.execute("""INSERT INTO bot_users 
                     (user_id, username, first_name, balance, join_date, referred_by) 
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (user_id, username, first_name, 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), referred_by))
        
        conn.commit()
        conn.close()
        return referrer_bonus

def update_balance(user_id, amount):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE bot_users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        conn.commit()
        conn.close()

def get_balance(user_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT balance FROM bot_users WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else 0

def add_search_history(user_id, query, result, account_used, cost):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO search_history 
                     (user_id, query, result, account_used, search_date, cost) 
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (user_id, query[:500], result[:1000] if result else "Ничего не найдено", account_used, 
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cost))
        c.execute("UPDATE bot_users SET total_requests = total_requests + 1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

def get_search_history(user_id, limit=10):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT query, result, search_date FROM search_history 
                     WHERE user_id=? ORDER BY id DESC LIMIT ?""", (user_id, limit))
        history = c.fetchall()
        conn.close()
        return history

def get_all_users():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id, username, first_name, balance, total_requests, referral_count, join_date FROM bot_users WHERE is_banned=0")
        users = c.fetchall()
        conn.close()
        return users

def get_user_stats():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM bot_users WHERE is_banned=0")
        total_users = c.fetchone()[0]
        c.execute("SELECT SUM(balance) FROM bot_users WHERE is_banned=0")
        total_balance = c.fetchone()[0] or 0
        c.execute("SELECT SUM(total_requests) FROM bot_users WHERE is_banned=0")
        total_requests = c.fetchone()[0] or 0
        conn.close()
        return total_users, total_balance, total_requests

def is_user_banned(user_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM bans WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result is not None

def ban_user(user_id, banned_by, reason):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO bans (user_id, banned_by, reason, banned_at) VALUES (?, ?, ?, ?)",
                  (user_id, banned_by, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.execute("UPDATE bot_users SET is_banned=1 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

def unban_user(user_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
        c.execute("UPDATE bot_users SET is_banned=0 WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

def get_banned_users():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT b.user_id, u.username, u.first_name, b.reason, b.banned_at 
                     FROM bans b JOIN bot_users u ON b.user_id = u.user_id""")
        users = c.fetchall()
        conn.close()
        return users

# ========== ФУНКЦИИ БД ДЛЯ АДМИНОВ ==========
def is_admin(user_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM admins WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result is not None

def add_admin(user_id, added_by, permissions="all"):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO admins (user_id, added_by, added_at, permissions) VALUES (?, ?, ?, ?)",
                  (user_id, added_by, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), permissions))
        conn.commit()
        conn.close()

def remove_admin(user_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        conn.commit()
        conn.close()

def get_admins():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT a.user_id, u.username, u.first_name, a.added_at, a.permissions 
                     FROM admins a JOIN bot_users u ON a.user_id = u.user_id""")
        admins = c.fetchall()
        conn.close()
        return admins

# ========== ФУНКЦИИ БД ДЛЯ АККАУНТОВ ==========
def get_all_search_accounts(only_active=True):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        if only_active:
            c.execute("SELECT id, phone, username, first_name, session_string FROM search_accounts WHERE is_active=1")
        else:
            c.execute("SELECT id, phone, username, first_name, session_string FROM search_accounts")
        accounts = c.fetchall()
        conn.close()
    return accounts

def add_search_account(phone, username, first_name, session_string):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO search_accounts (phone, username, first_name, session_string, last_check) 
                     VALUES (?, ?, ?, ?, ?) 
                     ON CONFLICT(phone) DO UPDATE SET 
                     session_string=?, username=?, first_name=?, is_active=1, last_check=?""",
                  (phone, username, first_name, session_string, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   session_string, username, first_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()

def update_account_requests(account_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE search_accounts SET requests_used = requests_used + 1, last_check=? WHERE id=?", 
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), account_id))
        conn.commit()
        conn.close()

def toggle_account_active(account_id, is_active):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE search_accounts SET is_active=?, last_check=? WHERE id=?", 
                  (is_active, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), account_id))
        conn.commit()
        conn.close()

def delete_search_account(account_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM search_accounts WHERE id=?", (account_id,))
        c.execute("DELETE FROM tg_codes WHERE account_id=?", (account_id,))
        conn.commit()
        conn.close()

def save_tg_code(account_id, code):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO tg_codes (account_id, code, received_at) VALUES (?, ?, ?)",
                  (account_id, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()

def get_tg_codes(account_id=None, limit=10):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        if account_id:
            c.execute("""SELECT c.id, a.phone, a.username, c.code, c.received_at, c.processed 
                         FROM tg_codes c JOIN search_accounts a ON c.account_id = a.id 
                         WHERE c.account_id=? ORDER BY c.id DESC LIMIT ?""", (account_id, limit))
        else:
            c.execute("""SELECT c.id, a.phone, a.username, c.code, c.received_at, c.processed 
                         FROM tg_codes c JOIN search_accounts a ON c.account_id = a.id 
                         ORDER BY c.id DESC LIMIT ?""", (limit,))
        codes = c.fetchall()
        conn.close()
        return codes

def mark_code_processed(code_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE tg_codes SET processed=1 WHERE id=?", (code_id,))
        conn.commit()
        conn.close()

def update_daily_bonus(account_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("UPDATE search_accounts SET daily_bonus_claimed=? WHERE id=?", (today, account_id))
        conn.commit()
        conn.close()

def get_account_daily_bonus(account_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT daily_bonus_claimed FROM search_accounts WHERE id=?", (account_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None

def get_all_codes_unprocessed():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""SELECT c.id, a.phone, a.username, c.code, c.received_at 
                     FROM tg_codes c JOIN search_accounts a ON c.account_id = a.id 
                     WHERE c.processed=0 ORDER BY c.id DESC""")
        codes = c.fetchall()
        conn.close()
        return codes

# ========== ФУНКЦИИ БД ДЛЯ ЗВЕЗД ==========
def add_stars_payment(user_id, stars_amount, rub_amount):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO stars_payments (user_id, stars_amount, rub_amount, payment_date) 
                     VALUES (?, ?, ?, ?)""",
                  (user_id, stars_amount, rub_amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        payment_id = c.lastrowid
        conn.commit()
        conn.close()
        return payment_id

def confirm_stars_payment(payment_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE stars_payments SET status='confirmed' WHERE id=?", (payment_id,))
        conn.commit()
        conn.close()

# ========== КЛАСС ДЛЯ УПРАВЛЕНИЯ АККАУНТАМИ ==========
class AccountManager:
    def __init__(self):
        self.accounts = []
        self.current_index = 0
        self.clients = {}
        self.lock = asyncio.Lock()
        self.load_accounts()
    
    def load_accounts(self):
        self.accounts = get_all_search_accounts(only_active=True)
        self.current_index = 0
        logger.info(f"Загружено {len(self.accounts)} аккаунтов")
    
    def get_accounts_count(self):
        return len(self.accounts)
    
    async def get_next_account(self):
        if not self.accounts:
            return None
        
        async with self.lock:
            # Ищем активный аккаунт
            for _ in range(len(self.accounts)):
                account = self.accounts[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.accounts)
                
                # Проверяем, активен ли аккаунт
                if account[4]:  # session_string есть
                    return account
            
            return None
    
    async def get_client(self, account):
        account_id, phone, username, first_name, session_string = account
        if account_id not in self.clients:
            try:
                client = TelegramClient(session_string, API_ID, API_HASH)
                await client.start()
                self.clients[account_id] = client
                logger.info(f"Подключен аккаунт {phone}")
            except Exception as e:
                logger.error(f"Ошибка подключения аккаунта {phone}: {e}")
                return None
        return self.clients[account_id]
    
    async def claim_daily_bonus(self, account):
        """Забирает ежедневный бонус на аккаунте"""
        account_id, phone, username, first_name, session_string = account
        
        # Проверяем, забирали ли уже сегодня
        last_claimed = get_account_daily_bonus(account_id)
        today = datetime.now().strftime("%Y-%m-%d")
        if last_claimed == today:
            logger.info(f"Бонус уже забран на аккаунте {phone} сегодня")
            return True
        
        try:
            client = await self.get_client(account)
            if not client:
                return False
            
            # Находим бота
            bot = await client.get_entity(TARGET_BOT)
            
            # Отправляем /start
            await client.send_message(bot, "/start")
            await asyncio.sleep(1)
            
            # Ищем инлайн кнопку "🎁Забрать ежедневный бонус"
            async for message in client.iter_messages(bot, limit=5):
                if message.buttons:
                    for row in message.buttons:
                        for button in row:
                            if "🎁Забрать ежедневный бонус" in button.text or "ежедневный бонус" in button.text:
                                await button.click()
                                await asyncio.sleep(1)
                                update_daily_bonus(account_id)
                                logger.info(f"Бонус забран на аккаунте {phone}")
                                return True
            
            return False
        except Exception as e:
            logger.error(f"Ошибка при взятии бонуса на аккаунте {phone}: {e}")
            return False
    
    async def search_query(self, account, query):
        """Выполняет поиск через бот с указанного аккаунта"""
        account_id, phone, username, first_name, session_string = account
        
        try:
            client = await self.get_client(account)
            if not client:
                return "error", "Не удалось подключиться к аккаунту"
            
            bot = await client.get_entity(TARGET_BOT)
            
            # Отправляем запрос
            await client.send_message(bot, query)
            await asyncio.sleep(3)
            
            # Получаем ответ
            responses = []
            async for message in client.iter_messages(bot, limit=10):
                if message.text and message.text.strip():
                    responses.append(message.text)
            
            if responses:
                # Ищем последний ответ (самый свежий)
                full_response = responses[0] if responses else ""
                
                # Проверяем на ошибки
                if "Недостаточно запросов" in full_response or "⛔️ Недостаточно запросов" in full_response:
                    return "limit", "Недостаточно запросов на аккаунте"
                elif "Не удалось распознать" in full_response or "❔ Не удалось распознать" in full_response:
                    return "error", "Не удалось распознать запрос"
                elif "Ничего не найдено" in full_response:
                    return "not_found", "Ничего не найдено"
                else:
                    # Очищаем от спама ссылок
                    lines = full_response.split('\n')
                    cleaned_lines = []
                    skip = False
                    for line in lines:
                        if "💾 Сохраните ссылку" in line:
                            skip = True
                            continue
                        if "void.help" in line or "https://void.help" in line:
                            skip = True
                            continue
                        if "Сохраните ссылку на актуальное зеркало" in line:
                            skip = True
                            continue
                        if skip and "Сохраните ссылку" not in line and "void.help" not in line:
                            skip = False
                        if not skip and line.strip():
                            cleaned_lines.append(line.strip())
                    
                    result = '\n'.join(cleaned_lines) if cleaned_lines else full_response
                    
                    # Обновляем счетчик запросов
                    update_account_requests(account_id)
                    return "success", result
            
            return "error", "Нет ответа от бота"
            
        except FloodWaitError as e:
            logger.warning(f"Flood wait на аккаунте {phone}: {e}")
            return "flood", f"Ожидание {e.seconds} секунд"
        except Exception as e:
            logger.error(f"Ошибка поиска на аккаунте {phone}: {e}")
            return "error", f"Ошибка: {str(e)}"

# ========== ОСНОВНОЙ БОТ ==========
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from aiogram.utils import executor
from aiogram.dispatcher import filters

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

account_manager = AccountManager()
crypto_bot = CryptoBot(CRYPTOBOT_TOKEN)

# Хранилище для временных данных
temp_data = {}

# ========== КЛАВИАТУРЫ ==========
def get_main_menu(user_id):
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔍 Поиск", callback_data="search"),
        InlineKeyboardButton("💰 Баланс", callback_data="balance"),
    )
    keyboard.add(
        InlineKeyboardButton("👤 Рефералы", callback_data="referrals"),
        InlineKeyboardButton("📊 История", callback_data="history"),
    )
    if is_admin(user_id):
        keyboard.add(InlineKeyboardButton("⚙️ Админ панель", callback_data="admin_panel"))
    return keyboard

def get_admin_menu():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
    )
    keyboard.add(
        InlineKeyboardButton("👑 Управление админами", callback_data="admin_admins"),
        InlineKeyboardButton("💳 Управление балансом", callback_data="admin_balance"),
    )
    keyboard.add(
        InlineKeyboardButton("📢 Рассылка", callback_data="admin_mailing"),
        InlineKeyboardButton("📁 База аккаунтов", callback_data="admin_accounts"),
    )
    keyboard.add(
        InlineKeyboardButton("🚫 Управление банами", callback_data="admin_bans"),
        InlineKeyboardButton("📝 История запросов", callback_data="admin_requests_history"),
    )
    keyboard.add(
        InlineKeyboardButton("🔑 Коды Telegram", callback_data="admin_codes"),
        InlineKeyboardButton("🔄 Создать зеркало", callback_data="admin_create_mirror"),
    )
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="back_to_main"))
    return keyboard

def get_accounts_menu(accounts):
    keyboard = InlineKeyboardMarkup(row_width=1)
    for acc in accounts:
        status = "✅" if acc[4] else "❌"
        keyboard.add(InlineKeyboardButton(f"{status} {acc[1]} (@{acc[2]})", callback_data=f"acc_{acc[0]}"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_panel"))
    return keyboard

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message_handler(commands=['start'])
async def start_command(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "без username"
    first_name = message.from_user.first_name or "пользователь"
    
    # Проверка на бан
    if is_user_banned(user_id):
        await message.answer("🚫 Вы забанены в этом боте.")
        return
    
    # Проверка реферальной ссылки
    referred_by = None
    if len(message.text.split()) > 1:
        try:
            referred_by = int(message.text.split()[1])
        except:
            pass
    
    # Создаем или получаем пользователя
    bonus = create_user(user_id, username, first_name, referred_by)
    
    # Отправляем приветствие
    welcome_text = f"""
👋 Привет, {first_name}!

🤖 Добро пожаловать в OSINT бот для поиска данных по никнеймам, номерам и другим данным.

💰 Баланс: {get_balance(user_id)} руб.

🔍 Просто отправь мне любой никнейм, номер телефона или другие данные для поиска.

📌 Стоимость одного поиска: {PRICE_PER_SEARCH} руб.

📌 Ежедневный бонус на аккаунтах поиска автоматически активируется.
"""
    
    if bonus > 0:
        welcome_text += f"\n🎉 Вы получили бонус {bonus} руб. за приглашение!"
    
    await message.answer_photo(
        photo="https://your_image_url.jpg",  # Замените на URL вашей картинки
        caption=welcome_text,
        reply_markup=get_main_menu(user_id)
    )

@dp.message_handler(filters.Text(contains="@") | filters.Text(contains="+") | filters.Text(contains="."))
async def search_query(message: types.Message):
    user_id = message.from_user.id
    query = message.text.strip()
    
    # Проверка на бан
    if is_user_banned(user_id):
        await message.answer("🚫 Вы забанены в этом боте.")
        return
    
    # Проверка баланса
    balance = get_balance(user_id)
    if balance < PRICE_PER_SEARCH:
        await message.answer(f"""
❌ Недостаточно средств!

💰 Ваш баланс: {balance} руб.
💳 Стоимость поиска: {PRICE_PER_SEARCH} руб.

Пополните баланс через кнопку "💰 Баланс"
""")
        return
    
    # Поиск
    await message.answer("🔍 Ищу информацию...")
    
    # Получаем аккаунт и пробуем выполнить поиск
    account = await account_manager.get_next_account()
    if not account:
        await message.answer("❌ Нет доступных аккаунтов для поиска. Попробуйте позже.")
        return
    
    # Забираем бонус перед поиском
    await account_manager.claim_daily_bonus(account)
    
    # Выполняем поиск
    status, result = await account_manager.search_query(account, query)
    
    if status == "success":
        # Списываем средства
        update_balance(user_id, -PRICE_PER_SEARCH)
        add_search_history(user_id, query, result, account[0], PRICE_PER_SEARCH)
        
        # Отправляем результат
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            InlineKeyboardButton("📝 Запросить больше информации", callback_data=f"more_info_{account[0]}")
        )
        
        await message.answer(f"""
✅ Поиск выполнен!

📋 Результат:
{result}

💰 Баланс: {get_balance(user_id)} руб.
""", reply_markup=keyboard)
        
    elif status == "not_found":
        # Не списываем баланс
        add_search_history(user_id, query, "Ничего не найдено", account[0], 0)
        await message.answer("❌ Ничего не найдено. Баланс не был списан.")
        
    elif status == "limit":
        # Отключаем аккаунт и пробуем на следующем
        toggle_account_active(account[0], 0)
        account_manager.load_accounts()
        
        # Пробуем на следующем аккаунте
        next_account = await account_manager.get_next_account()
        if next_account:
            await message.answer("🔄 Аккаунт закончился, пробуем на следующем...")
            # Рекурсивно пробуем снова
            await search_query(message)
        else:
            await message.answer("❌ Нет доступных аккаунтов. Попробуйте позже.")
            
    elif status == "error" or status == "flood":
        await message.answer(f"❌ {result}")
    
    else:
        await message.answer(f"❌ Произошла ошибка. Попробуйте позже.")

# ========== ОБРАБОТЧИКИ КОЛБЭКОВ ==========
@dp.callback_query_handler(lambda c: c.data == "search")
async def search_callback(callback_query: types.CallbackQuery):
    await callback_query.message.answer("🔍 Просто отправьте мне любой никнейм, номер телефона или другие данные для поиска.")
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "balance")
async def balance_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    balance = get_balance(user_id)
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💳 Пополнить через CryptoBot", callback_data="deposit_crypto"),
        InlineKeyboardButton("⭐ Пополнить через Telegram Stars", callback_data="deposit_stars"),
    )
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="back_to_main"))
    
    await callback_query.message.edit_text(f"""
💰 Ваш баланс: {balance} руб.

💳 Способы пополнения:
- Через CryptoBot (RUB)
- Через Telegram Stars (⭐)

Стоимость одного поиска: {PRICE_PER_SEARCH} руб.
""", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "deposit_crypto")
async def deposit_crypto_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    keyboard = InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        InlineKeyboardButton("100 руб", callback_data="deposit_100"),
        InlineKeyboardButton("200 руб", callback_data="deposit_200"),
        InlineKeyboardButton("500 руб", callback_data="deposit_500"),
    )
    keyboard.add(
        InlineKeyboardButton("1000 руб", callback_data="deposit_1000"),
        InlineKeyboardButton("2000 руб", callback_data="deposit_2000"),
        InlineKeyboardButton("5000 руб", callback_data="deposit_5000"),
    )
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="balance"))
    
    await callback_query.message.edit_text("""
💳 Выберите сумму пополнения через CryptoBot:

После оплаты баланс будет начислен автоматически.
""", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("deposit_"))
async def process_deposit(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    amount = int(callback_query.data.split("_")[1])
    
    # Создаем счет
    invoice_id, pay_url = crypto_bot.create_invoice(amount)
    
    if invoice_id and pay_url:
        temp_data[f"invoice_{invoice_id}"] = {"user_id": user_id, "amount": amount}
        
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            InlineKeyboardButton("💳 Оплатить", url=pay_url),
            InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_payment_{invoice_id}"),
        )
        keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="balance"))
        
        await callback_query.message.edit_text(f"""
💳 Счет создан!

Сумма: {amount} руб.
Счет: {invoice_id}

Нажмите "Оплатить" для перехода к оплате.
После оплаты нажмите "Проверить оплату" для начисления баланса.
""", reply_markup=keyboard)
    else:
        await callback_query.message.answer("❌ Ошибка создания счета. Попробуйте позже.")
    
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("check_payment_"))
async def check_payment(callback_query: types.CallbackQuery):
    invoice_id = int(callback_query.data.split("_")[2])
    user_id = callback_query.from_user.id
    
    # Проверяем оплату
    paid, amount = crypto_bot.check_payment(invoice_id)
    
    if paid:
        # Начисляем баланс
        update_balance(user_id, amount)
        
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="balance"))
        
        await callback_query.message.edit_text(f"""
✅ Оплата подтверждена!

💰 Начислено: {amount} руб.
💰 Новый баланс: {get_balance(user_id)} руб.
""", reply_markup=keyboard)
    else:
        await callback_query.answer("❌ Оплата не найдена. Попробуйте позже или оплатите снова.", show_alert=True)

@dp.callback_query_handler(lambda c: c.data == "deposit_stars")
async def deposit_stars_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    # Отправляем запрос на звезды
    bot_username = (await bot.get_me()).username
    stars_amount = 10  # Минимальное количество звезд
    
    # Создаем счет для звезд
    rub_amount = stars_amount * TG_STARS_PRICE
    payment_id = add_stars_payment(user_id, stars_amount, rub_amount)
    
    await callback_query.message.answer(f"""
⭐ Оплата через Telegram Stars

Отправьте {stars_amount} ⭐ на аккаунт @DEAMORGAN (ID: {OWNER_ID})
Сумма в рублях: {rub_amount} руб.

После отправки нажмите кнопку ниже для проверки.

📌 Бот постоянно проверяет, скинули ли подарок, и автоматически начисляет баланс.
""")
    
    # Добавляем кнопку для проверки
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("✅ Проверить звезды", callback_data=f"check_stars_{payment_id}"),
        InlineKeyboardButton("◀️ Назад", callback_data="balance"),
    )
    
    await callback_query.message.edit_text(f"""
⭐ Пополнение через Telegram Stars

1. Отправьте звезды на аккаунт @DEAMORGAN (ID: {OWNER_ID})
2. Нажмите кнопку проверки

Сумма: {stars_amount} ⭐ = {rub_amount} руб.
""", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("check_stars_"))
async def check_stars(callback_query: types.CallbackQuery):
    payment_id = int(callback_query.data.split("_")[2])
    user_id = callback_query.from_user.id
    
    # Проверяем, был ли отправлен подарок
    # Здесь нужно реализовать проверку через аккаунт @DEAMORGAN
    # Это сложная часть, требует доступа к аккаунту
    
    # Временная заглушка
    await callback_query.answer("⏳ Проверка звезд... Ожидайте.", show_alert=True)
    
    # TODO: Реализовать проверку через аккаунт @DEAMORGAN

@dp.callback_query_handler(lambda c: c.data == "referrals")
async def referrals_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    # Получаем данные о рефералах
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT referral_count, referral_earned FROM bot_users WHERE user_id=?", (user_id,))
        result = c.fetchone()
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,))
        total = c.fetchone()[0]
        conn.close()
    
    if result:
        count, earned = result
    else:
        count, earned = 0, 0
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("📋 Список рефералов", callback_data="referral_list"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="back_to_main"))
    
    await callback_query.message.edit_text(f"""
👤 Реферальная система

Ваша реферальная ссылка:
`{BOT_LINK}?start={user_id}`

📊 Статистика:
- Всего рефералов: {total}
- Получено бонусов: {earned} руб.

💰 Бонус за приглашение: {REFERRAL_BONUS} руб.
""", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "history")
async def history_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    history = get_search_history(user_id, 10)
    
    if not history:
        text = "📊 История запросов пуста."
    else:
        text = "📊 Последние запросы:\n\n"
        for query, result, date in history:
            text += f"🔍 {query}\n📅 {date}\n📋 {result[:100]}...\n\n"
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="back_to_main"))
    
    await callback_query.message.edit_text(text[:4000], reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "back_to_main")
async def back_to_main(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    await callback_query.message.edit_text("🏠 Главное меню", reply_markup=get_main_menu(user_id))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_panel")
async def admin_panel(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    if not is_admin(user_id):
        await callback_query.answer("⛔ У вас нет доступа к админ панели!", show_alert=True)
        return
    
    await callback_query.message.edit_text("⚙️ Админ панель", reply_markup=get_admin_menu())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_stats")
async def admin_stats(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    total_users, total_balance, total_requests = get_user_stats()
    total_accounts = account_manager.get_accounts_count()
    
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM search_accounts")
        all_accounts = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM search_accounts WHERE is_active=1")
        active_accounts = c.fetchone()[0]
        conn.close()
    
    await callback_query.message.edit_text(f"""
📊 Статистика бота

👥 Пользователи: {total_users}
💰 Общий баланс: {total_balance} руб.
🔍 Всего запросов: {total_requests}

📱 Аккаунты для поиска:
- Всего: {all_accounts}
- Активных: {active_accounts}
- В очереди: {total_accounts}

📅 Дата: {datetime.now().strftime("%d.%m.%Y %H:%M")}
""", reply_markup=get_admin_menu())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_users")
async def admin_users(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    users = get_all_users()
    text = "👥 Список пользователей:\n\n"
    for user_id, username, first_name, balance, total_requests, referrals, join_date in users[:20]:
        text += f"🆔 {user_id} | @{username} | {first_name}\n"
        text += f"💰 {balance} руб. | 🔍 {total_requests} запр. | 👤 {referrals} реф.\n"
        text += f"📅 {join_date}\n\n"
    
    if len(users) > 20:
        text += f"... и еще {len(users) - 20} пользователей"
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_panel"))
    
    await callback_query.message.edit_text(text[:4000], reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_admins")
async def admin_admins(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    admins = get_admins()
    text = "👑 Список администраторов:\n\n"
    for admin_id, username, first_name, added_at, permissions in admins:
        text += f"🆔 {admin_id} | @{username} | {first_name}\n"
        text += f"📅 {added_at} | Права: {permissions}\n\n"
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add_admin"))
    keyboard.add(InlineKeyboardButton("➖ Удалить админа", callback_data="admin_remove_admin"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_panel"))
    
    await callback_query.message.edit_text(text or "Администраторов нет.", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_balance")
async def admin_balance(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("➕ Увеличить баланс", callback_data="admin_add_balance"))
    keyboard.add(InlineKeyboardButton("➖ Уменьшить баланс", callback_data="admin_sub_balance"))
    keyboard.add(InlineKeyboardButton("📊 Проверить баланс", callback_data="admin_check_balance"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_panel"))
    
    await callback_query.message.edit_text("💳 Управление балансом пользователей", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_mailing")
async def admin_mailing(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    await callback_query.message.answer("📢 Введите текст для рассылки (или отправьте фото с подписью):")
    await callback_query.answer()
    
    # Устанавливаем состояние ожидания
    temp_data[callback_query.from_user.id] = {"state": "mailing"}

@dp.message_handler(content_types=['text', 'photo'])
async def handle_mailing(message: types.Message):
    user_id = message.from_user.id
    
    if temp_data.get(user_id, {}).get("state") == "mailing":
        # Получаем всех пользователей
        users = get_all_users()
        success = 0
        
        await message.answer(f"📢 Начинаю рассылку для {len(users)} пользователей...")
        
        for user in users:
            try:
                if message.text:
                    await bot.send_message(user[0], message.text)
                elif message.photo:
                    caption = message.caption or ""
                    await bot.send_photo(user[0], message.photo[-1].file_id, caption=caption)
                success += 1
                await asyncio.sleep(0.05)  # Чтобы не превысить лимиты
            except:
                pass
        
        await message.answer(f"✅ Рассылка завершена! Отправлено: {success}/{len(users)}")
        temp_data.pop(user_id, None)

@dp.callback_query_handler(lambda c: c.data == "admin_accounts")
async def admin_accounts(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    accounts = get_all_search_accounts(only_active=False)
    await callback_query.message.edit_text("📁 Управление аккаунтами для поиска", reply_markup=get_accounts_menu(accounts))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_bans")
async def admin_bans(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    banned = get_banned_users()
    text = "🚫 Забаненные пользователи:\n\n"
    for user_id, username, first_name, reason, banned_at in banned:
        text += f"🆔 {user_id} | @{username} | {first_name}\n"
        text += f"📅 {banned_at} | Причина: {reason}\n\n"
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("➕ Забанить", callback_data="admin_ban_user"))
    keyboard.add(InlineKeyboardButton("➖ Разбанить", callback_data="admin_unban_user"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_panel"))
    
    await callback_query.message.edit_text(text or "Забаненных пользователей нет.", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_codes")
async def admin_codes(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    codes = get_all_codes_unprocessed()
    text = "🔑 Необработанные коды Telegram:\n\n"
    for code_id, phone, username, code, received_at in codes:
        text += f"📱 {phone} (@{username})\n"
        text += f"🔢 Код: {code}\n"
        text += f"📅 {received_at}\n\n"
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("✅ Отметить все как обработанные", callback_data="admin_codes_process"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_panel"))
    
    await callback_query.message.edit_text(text or "Новых кодов нет.", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_codes_process")
async def admin_codes_process(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    codes = get_all_codes_unprocessed()
    for code_id, _, _, _, _ in codes:
        mark_code_processed(code_id)
    
    await callback_query.message.edit_text("✅ Все коды отмечены как обработанные")
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "admin_create_mirror")
async def admin_create_mirror(callback_query: types.CallbackQuery):
    if not is_admin(callback_query.from_user.id):
        await callback_query.answer("⛔ Нет доступа!", show_alert=True)
        return
    
    # Здесь логика создания зеркала
    await callback_query.message.answer("""
🔄 Создание зеркала бота

Для создания зеркала вам понадобится:
1. Токен вашего бота (@BotFather)
2. Выбрать наценку
3. Выбрать приватный/публичный режим

Введите токен бота для создания зеркала:
""")
    await callback_query.answer()

# ========== ОБРАБОТЧИК ЗАПРОСА БОЛЬШЕ ИНФОРМАЦИИ ==========
@dp.callback_query_handler(lambda c: c.data.startswith("more_info_"))
async def more_info(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    account_id = int(callback_query.data.split("_")[2])
    
    # Получаем информацию об аккаунте
    accounts = get_all_search_accounts(only_active=False)
    account_info = None
    for acc in accounts:
        if acc[0] == account_id:
            account_info = acc
            break
    
    # Отправляем запрос владельцу
    if account_info:
        await bot.send_message(OWNER_ID, f"""
📝 Запрос на дополнительную информацию!

👤 Пользователь: {callback_query.from_user.first_name} (@{callback_query.from_user.username})
🆔 ID: {user_id}
📱 Аккаунт: {account_info[1]} (@{account_info[2]})

📋 Ответ пользователя:
{callback_query.message.text if callback_query.message.text else "См. сообщение выше"}

💡 Для ответа используйте /reply_{user_id} текст
""")
    
    await callback_query.message.answer("✅ Запрос отправлен! Ожидайте ответа.")
    await callback_query.answer()

# ========== ЗАПУСК БОТА ==========
if __name__ == '__main__':
    print("🤖 Бот запущен!")
    print(f"📱 Используется {account_manager.get_accounts_count()} аккаунтов для поиска")
    executor.start_polling(dp, skip_updates=True)
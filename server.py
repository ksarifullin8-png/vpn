import asyncio
import re
import sqlite3
import logging
import os
import json
import random
from threading import Lock
from datetime import datetime, timedelta
from io import BytesIO
from telethon import TelegramClient, functions, types, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
import aiohttp

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = "8729005607:AAGFxfC7TmM0XfexLV_BVce6SMpwau7VNT0"
OWNER_ID = 8480939483
API_ID = 35800959
API_HASH = "708e7d0bc3572355bcaf68562cc068f1"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

user_state = {}
temp_data = {}
db_lock = Lock()
win_monitors = {}          # {account_id: asyncio.Task}
active_accounts_cache = {} # кеш данных аккаунтов для мониторинга
bonus_tasks = {}           # {account_id: asyncio.Task}

# ========== БАЗА ДАННЫХ ==========
def get_db():
    return sqlite3.connect('contest_bot.db', timeout=30, check_same_thread=False)

def init_db():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS accounts
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      phone TEXT UNIQUE,
                      username TEXT,
                      first_name TEXT,
                      session_string TEXT)''')
        # Миграции для старых таблиц
        migrations = [
            ("last_name", "TEXT"),
            ("user_id", "INTEGER"),
            ("is_active", "INTEGER DEFAULT 1"),
            ("last_check", "TEXT"),
            ("is_personal", "INTEGER DEFAULT 0")
        ]
        for col_name, col_type in migrations:
            try:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        c.execute('''CREATE TABLE IF NOT EXISTS telegram_codes
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      account_id INTEGER,
                      code TEXT,
                      received_at TEXT,
                      FOREIGN KEY (account_id) REFERENCES accounts(id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS wins_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      account_id INTEGER,
                      chat_name TEXT,
                      chat_username TEXT,
                      post_text TEXT,
                      post_link TEXT,
                      detected_at TEXT,
                      FOREIGN KEY (account_id) REFERENCES accounts(id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS leave_history
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      account_id INTEGER,
                      chat_name TEXT,
                      chat_type TEXT,
                      left_at TEXT,
                      reason TEXT,
                      FOREIGN KEY (account_id) REFERENCES accounts(id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS cleanup_log
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      account_id INTEGER,
                      action TEXT,
                      details TEXT,
                      created_at TEXT,
                      FOREIGN KEY (account_id) REFERENCES accounts(id))''')
        conn.commit()
        conn.close()

init_db()

# ========== ФУНКЦИИ БД ==========
def get_all_accounts(only_active=False):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        if only_active:
            c.execute("SELECT id, phone, username, first_name, last_name, user_id, session_string FROM accounts WHERE is_active=1")
        else:
            c.execute("SELECT id, phone, username, first_name, last_name, user_id, session_string FROM accounts")
        accounts = c.fetchall()
        conn.close()
    return accounts

def save_account(phone, username, first_name, last_name, user_id, session_string, is_personal=0):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO accounts (phone, username, first_name, last_name, user_id, session_string, is_personal) 
                     VALUES (?, ?, ?, ?, ?, ?, ?) 
                     ON CONFLICT(phone) DO UPDATE SET 
                     session_string=?, username=?, first_name=?, last_name=?, user_id=?, is_active=1, is_personal=?""",
                  (phone, username, first_name, last_name, user_id, session_string, is_personal,
                   session_string, username, first_name, last_name, user_id, is_personal))
        conn.commit()
        conn.close()

def update_account_status(account_id, is_active):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE accounts SET is_active=?, last_check=? WHERE id=?", 
                  (is_active, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), account_id))
        conn.commit()
        conn.close()

def delete_account(account_id):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        c.execute("DELETE FROM telegram_codes WHERE account_id=?", (account_id,))
        conn.commit()
        conn.close()

def save_telegram_code(account_id, code):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO telegram_codes (account_id, code, received_at) VALUES (?, ?, ?)",
                  (account_id, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.execute("""DELETE FROM telegram_codes WHERE id NOT IN (
                        SELECT id FROM telegram_codes WHERE account_id=? ORDER BY id DESC LIMIT 5
                     ) AND account_id=?""", (account_id, account_id))
        conn.commit()
        conn.close()

def get_last_codes(account_id, limit=5):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT code, received_at FROM telegram_codes WHERE account_id=? ORDER BY id DESC LIMIT ?",
                  (account_id, limit))
        codes = c.fetchall()
        conn.close()
    return codes

def save_win(account_id, chat_name, chat_username, post_text, post_link):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO wins_history (account_id, chat_name, chat_username, post_text, post_link, detected_at) 
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (account_id, chat_name, chat_username, post_text[:1000], post_link, 
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()

def get_wins_history(limit=20):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute("""SELECT w.id, a.username, a.first_name, a.phone, w.chat_name, w.post_text, w.post_link, w.detected_at 
                         FROM wins_history w 
                         JOIN accounts a ON w.account_id = a.id 
                         ORDER BY w.detected_at DESC LIMIT ?""", (limit,))
        except:
            return []
        wins = c.fetchall()
        conn.close()
    return wins

def save_leave_history(account_id, chat_name, chat_type, reason=""):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute("INSERT INTO leave_history (account_id, chat_name, chat_type, left_at, reason) VALUES (?, ?, ?, ?, ?)",
                      (account_id, chat_name, chat_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason))
        except:
            c.execute("INSERT INTO leave_history (account_id, chat_name, chat_type, left_at) VALUES (?, ?, ?, ?)",
                      (account_id, chat_name, chat_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()

def get_leave_stats(limit=20):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute("""SELECT a.username, a.first_name, l.chat_name, l.chat_type, l.left_at, l.reason 
                         FROM leave_history l 
                         JOIN accounts a ON l.account_id = a.id 
                         ORDER BY l.left_at DESC LIMIT ?""", (limit,))
        except:
            c.execute("""SELECT a.username, a.first_name, l.chat_name, l.chat_type, l.left_at, '' as reason 
                         FROM leave_history l 
                         JOIN accounts a ON l.account_id = a.id 
                         ORDER BY l.left_at DESC LIMIT ?""", (limit,))
        stats = c.fetchall()
        conn.close()
    return stats

def save_cleanup_log(account_id, action, details):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO cleanup_log (account_id, action, details, created_at) VALUES (?, ?, ?, ?)",
                  (account_id, action, details, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def extract_channels_from_text(text):
    channels = set()
    channels.update(re.findall(r'@([a-zA-Z0-9_]+)', text))
    channels.update(re.findall(r't\.me/([a-zA-Z0-9_]+)', text))
    channels.update(re.findall(r't\.me/\+([a-zA-Z0-9_-]+)', text))
    return list(channels)

# ========== АВТОУДАЛЕНИЕ НЕРАБОЧИХ СЕССИЙ ==========
async def auto_cleanup_inactive_sessions(owner_bot):
    accounts = get_all_accounts()
    deleted_count = 0
    for acc in accounts:
        acc_id, phone, username, first_name, last_name, user_id, session = acc
        name = f"@{username or first_name} ({phone})"
        try:
            client = TelegramClient(StringSession(session), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                logger.info(f"[{name}] 🗑 Сессия невалидна, удаляю аккаунт...")
                delete_account(acc_id)
                if acc_id in win_monitors:
                    try: win_monitors[acc_id].cancel()
                    except: pass
                    del win_monitors[acc_id]
                deleted_count += 1
                await owner_bot.send_message(OWNER_ID, f"🗑 *Автоудаление:* {name} - сессия невалидна", parse_mode="Markdown")
            await client.disconnect()
        except Exception as e:
            logger.error(f"[{name}] ❌ Ошибка проверки: {e}")
            try:
                delete_account(acc_id)
                if acc_id in win_monitors:
                    try: win_monitors[acc_id].cancel()
                    except: pass
                    del win_monitors[acc_id]
                deleted_count += 1
            except: pass
    if deleted_count > 0:
        await owner_bot.send_message(OWNER_ID, f"🧹 Автоочистка завершена. Удалено аккаунтов: {deleted_count}", parse_mode="Markdown")
    return deleted_count

# ========== МОНИТОРИНГ УПОМИНАНИЙ ==========
async def start_win_monitor(session_string, account_id, account_name, owner_bot):
    if account_id in win_monitors:
        try: win_monitors[account_id].cancel()
        except: pass
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    async def monitor_loop():
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.error(f"[{account_name}] ❌ Сессия невалидна для мониторинга")
                update_account_status(account_id, 0)
                return
            me = await client.get_me()
            search_patterns = []
            if me.username:
                search_patterns.append(f"@{me.username}")
            if me.first_name:
                search_patterns.append(me.first_name)
            if me.last_name:
                search_patterns.append(f"{me.first_name} {me.last_name}")
                search_patterns.append(me.last_name)
            if me.id:
                search_patterns.append(str(me.id))
            active_accounts_cache[account_id] = {
                'name': account_name,
                'patterns': search_patterns,
                'user_id': me.id,
                'username': me.username
            }
            logger.info(f"[{account_name}] 👁 Мониторинг запущен. Паттерны: {search_patterns}")
            @client.on(events.NewMessage())
            async def handler(event):
                try:
                    if not event.message: return
                    text = event.message.text or ""
                    if not text: return
                    chat = await event.get_chat()
                    chat_name = getattr(chat, 'title', None) or getattr(chat, 'first_name', 'Неизвестный чат')
                    chat_username = getattr(chat, 'username', None)
                    text_lower = text.lower()
                    is_mentioned = False
                    matched_pattern = None
                    for pattern in search_patterns:
                        if pattern.lower() in text_lower:
                            is_mentioned = True
                            matched_pattern = pattern
                            break
                    if is_mentioned:
                        logger.info(f"[{account_name}] 🎯 Упоминание в {chat_name}: {matched_pattern}")
                        msg_link = ""
                        if chat_username and event.message.id:
                            msg_link = f"https://t.me/{chat_username}/{event.message.id}"
                        save_win(account_id, chat_name, chat_username or "", text, msg_link)
                        notification = (
                            f"🔔 *УПОМИНАНИЕ АККАУНТА*\n\n"
                            f"👤 *Аккаунт:* {account_name}\n"
                            f"📱 *Паттерн:* `{matched_pattern}`\n"
                            f"💬 *Чат:* {chat_name}\n"
                        )
                        if msg_link:
                            notification += f"🔗 [Ссылка на сообщение]({msg_link})\n"
                        notification += f"\n📝 *Текст:*\n{text[:500]}"
                        try:
                            await client.forward_messages(OWNER_ID, event.message)
                        except:
                            try:
                                await owner_bot.send_message(OWNER_ID, notification, parse_mode="Markdown")
                            except: pass
                except Exception as e:
                    logger.error(f"[{account_name}] ❌ Ошибка в мониторинге: {e}")
            while True:
                await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"[{account_name}] ❌ Мониторинг упал: {e}")
            update_account_status(account_id, 0)
        finally:
            await client.disconnect()
            if account_id in win_monitors:
                del win_monitors[account_id]
    task = asyncio.create_task(monitor_loop())
    win_monitors[account_id] = task
    return task

async def check_all_sessions(owner_bot):
    accounts = get_all_accounts()
    results = []
    for acc in accounts:
        acc_id, phone, username, first_name, last_name, user_id, session = acc
        name = f"@{username or first_name} ({phone})"
        try:
            client = TelegramClient(StringSession(session), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                update_account_status(acc_id, 1)
                results.append(f"✅ {name} - активен (@{me.username or 'нет username'})")
                if acc_id not in win_monitors:
                    await start_win_monitor(session, acc_id, name, owner_bot)
            else:
                update_account_status(acc_id, 0)
                results.append(f"❌ {name} - невалиден")
            await client.disconnect()
        except Exception as e:
            update_account_status(acc_id, 0)
            results.append(f"❌ {name} - ошибка: {str(e)[:50]}")
    deleted = await auto_cleanup_inactive_sessions(owner_bot)
    if deleted > 0:
        results.append(f"\n🗑 Автоудалено: {deleted} аккаунтов")
    return results

# ========== ПОЛУЧЕНИЕ КОДОВ TELEGRAM ==========
async def fetch_recent_codes_from_account(session_string, account_name, account_id, owner_bot):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await owner_bot.send_message(OWNER_ID, f"❌ *{account_name}* - сессия невалидна", parse_mode="Markdown")
            return []
        messages = await client.get_messages(777000, limit=20)
        codes_found = []
        for msg in messages:
            text = msg.text or ""
            found = re.findall(r'\b(\d{5,7})\b', text)
            codes_found.extend(found)
        unique_codes = list(dict.fromkeys(codes_found))[:5]
        for code in unique_codes:
            save_telegram_code(account_id, code)
        await owner_bot.send_message(OWNER_ID, f"✅ *{account_name}* - найдено кодов: {len(unique_codes)}", parse_mode="Markdown")
        return unique_codes
    except Exception as e:
        logger.error(f"[{account_name}] ❌ {e}")
        return []
    finally:
        await client.disconnect()

# ========== ВЫХОД ИЗ КАНАЛОВ ==========
async def leave_all_channels(session_string, account_name, account_id, owner_bot):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    left_count = 0; error_count = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{account_name}] ❌ Невалидная сессия")
            return 0, 0
        dialogs = await client.get_dialogs()
        for dialog in dialogs:
            try:
                if dialog.is_channel or dialog.is_group:
                    chat_name = dialog.name or dialog.title or "Неизвестный"
                    try:
                        if dialog.is_channel:
                            await client(functions.channels.LeaveChannelRequest(dialog.id))
                        else:
                            await client.delete_dialog(dialog.entity)
                        left_count += 1
                        save_leave_history(account_id, chat_name, "Канал" if dialog.is_channel else "Группа", "Ручной выход")
                        await asyncio.sleep(0.05)
                    except Exception as e:
                        error_count += 1
            except: error_count += 1; continue
    except Exception as e:
        logger.error(f"[{account_name}] ❌ {e}")
    finally:
        await client.disconnect()
    return left_count, error_count

async def leave_specific_channels(session_string, account_name, account_id, owner_bot, target_list):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    left_count = 0; error_count = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{account_name}] ❌ Невалидная сессия")
            return 0, 0
        for target in target_list:
            target = target.strip()
            if not target: continue
            clean = target.replace('@', '').replace('https://t.me/', '').split('?')[0].split('/')[0]
            try:
                entity = await client.get_entity(clean)
                if hasattr(entity, 'id'):
                    try:
                        if getattr(entity, 'broadcast', False) or getattr(entity, 'megagroup', False):
                            await client(functions.channels.LeaveChannelRequest(entity.id))
                        else:
                            await client.delete_dialog(entity)
                        left_count += 1
                        save_leave_history(account_id, getattr(entity, 'title', clean), "Канал/Группа", "Выход по запросу")
                    except: error_count += 1
                else: error_count += 1
            except: error_count += 1; continue
    except Exception as e:
        logger.error(f"[{account_name}] ❌ {e}")
    finally:
        await client.disconnect()
    return left_count, error_count

async def leave_all_chats(session_string, account_name, account_id, owner_bot):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    left_count = 0; deleted_count = 0; error_count = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{account_name}] ❌ Невалидная сессия")
            return 0,0,0
        dialogs = await client.get_dialogs()
        for dialog in dialogs:
            try:
                if dialog.is_channel:
                    try:
                        await client(functions.channels.LeaveChannelRequest(dialog.id))
                        left_count += 1
                    except: error_count += 1
                else:
                    try:
                        await client.delete_dialog(dialog.entity)
                        deleted_count += 1
                    except: error_count += 1
                await asyncio.sleep(0.03)
            except: error_count += 1; continue
        archived = await client.get_dialogs(archived=True)
        for dialog in archived:
            try:
                if dialog.is_channel:
                    try:
                        await client(functions.channels.LeaveChannelRequest(dialog.id))
                        left_count += 1
                    except: error_count += 1
                else:
                    try:
                        await client.delete_dialog(dialog.entity)
                        deleted_count += 1
                    except: error_count += 1
                await asyncio.sleep(0.03)
            except: error_count += 1; continue
    except Exception as e:
        logger.error(f"[{account_name}] ❌ {e}")
    finally:
        await client.disconnect()
    return left_count, deleted_count, error_count

async def leave_archived_chats(session_string, account_name, account_id, owner_bot):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    left_count = 0; error_count = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{account_name}] ❌ Невалидная сессия")
            return 0,0
        dialogs = await client.get_dialogs(archived=True)
        for dialog in dialogs:
            try:
                if dialog.is_channel or dialog.is_group:
                    chat_name = dialog.name or dialog.title or "Неизвестный"
                    try:
                        if dialog.is_channel:
                            await client(functions.channels.LeaveChannelRequest(dialog.id))
                        else:
                            await client.delete_dialog(dialog.entity)
                        left_count += 1
                        save_leave_history(account_id, chat_name, "Канал" if dialog.is_channel else "Группа", "Выход из архива")
                        await asyncio.sleep(0.05)
                    except: error_count += 1
            except: error_count += 1; continue
    except Exception as e:
        logger.error(f"[{account_name}] ❌ {e}")
    finally:
        await client.disconnect()
    return left_count, error_count

# ========== ОЧИСТКА НЕАКТИВНЫХ КАНАЛОВ ==========
async def cleanup_inactive_chats(session_string, account_name, account_id, owner_bot, months_threshold=1):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    left_count = 0; skipped_count = 0; error_count = 0
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{account_name}] ❌ Невалидная сессия")
            return 0,0,0
        threshold_date = datetime.now() - timedelta(days=30 * months_threshold)
        dialogs = await client.get_dialogs()
        for dialog in dialogs:
            try:
                if not (dialog.is_channel or dialog.is_group): continue
                chat_name = dialog.name or dialog.title or "Без названия"
                last_msg = dialog.message
                should_leave = False
                if last_msg and last_msg.date:
                    if last_msg.date.replace(tzinfo=None) < threshold_date:
                        should_leave = True
                else:
                    should_leave = True
                if should_leave:
                    try:
                        if dialog.is_channel:
                            await client(functions.channels.LeaveChannelRequest(dialog.id))
                        else:
                            await client.delete_dialog(dialog.entity)
                        left_count += 1
                        save_leave_history(account_id, chat_name, "Канал" if dialog.is_channel else "Группа", f"Неактивен {months_threshold}+ мес")
                        save_cleanup_log(account_id, "LEAVE", f"{chat_name}")
                        await asyncio.sleep(0.05)
                    except: error_count += 1
                else:
                    skipped_count += 1
            except: error_count += 1; continue
    except Exception as e:
        logger.error(f"[{account_name}] ❌ {e}")
    finally:
        await client.disconnect()
    return left_count, skipped_count, error_count

# ========== РЕАКЦИИ НА ПОСТЫ ==========
async def react_to_post(session_string, account_name, post_link, reaction_emoji, owner_bot):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"[{account_name}] ❌ Невалидная сессия")
            return False
        match = re.search(r'(?:t\.me|telegram\.me)/([^/]+)/(\d+)', post_link)
        if not match: return False
        chat_username, message_id = match.group(1), int(match.group(2))
        try:
            entity = await client.get_entity(chat_username)
        except:
            try:
                entity = await client.get_entity(f"@{chat_username}")
            except: return False
        emoji = reaction_emoji.strip()
        # Используем современный метод send_reaction (Telethon 1.30+)
        await client.send_reaction(entity, message_id, emoji)
        logger.info(f"[{account_name}] ✅ Реакция {emoji}")
        return True
    except Exception as e:
        logger.error(f"[{account_name}] ❌ Ошибка реакции: {e}")
        return False
    finally:
        await client.disconnect()

# ========== КОММЕНТИРОВАНИЕ ==========
async def comment_on_post(session_string, account_name, post_link, comment_text, owner_bot, delay=0):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False
        match = re.search(r'(?:t\.me|telegram\.me)/([^/]+)/(\d+)', post_link)
        if not match: return False
        chat_username, message_id = match.group(1), int(match.group(2))
        if delay > 0:
            await asyncio.sleep(delay * 60)
        try:
            entity = await client.get_entity(chat_username)
        except:
            try:
                entity = await client.get_entity(f"@{chat_username}")
            except: return False
        try:
            await client.send_message(entity, comment_text, comment_to=message_id)
            return True
        except:
            try:
                await client.send_message(entity, comment_text, reply_to=message_id)
                return True
            except: return False
    except: return False
    finally:
        await client.disconnect()

# ========== АВТОБОНУС TAKIWORK ==========
async def auto_bonus_loop(session_string, account_name, account_id, owner_bot):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    async def worker():
        while True:
            try:
                if not client.is_connected():
                    await client.connect()
                    if not await client.is_user_authorized():
                        logger.error(f"[{account_name}] ❌ Сессия невалидна для бонуса")
                        if account_id in bonus_tasks:
                            del bonus_tasks[account_id]
                        return
                await client.send_message('takiwork_bot', '💸 Бонус')
                logger.info(f"[{account_name}] 💸 Бонус отправлен")
                # Ждём 1 час (3600 секунд)
                await asyncio.sleep(3600)
            except Exception as e:
                logger.error(f"[{account_name}] ❌ Ошибка бонуса: {e}")
                await asyncio.sleep(60)
    task = asyncio.create_task(worker())
    bonus_tasks[account_id] = task
    return task

# ========== РЕШЕНИЕ КАПЧИ ==========
async def solve_captcha(client, event, account_name):
    try:
        if event.message.photo:
            photo_bytes = await client.download_media(event.message, file=BytesIO())
            photo_bytes.seek(0)
            captcha_text = await ocr_space(photo_bytes.getvalue())
            if captcha_text:
                return await try_captcha_answer(client, event, captcha_text)
            if event.message.buttons:
                digit_buttons = [btn for row in event.message.buttons for btn in row if btn.text and btn.text.strip().isdigit()]
                if digit_buttons:
                    await random.choice(digit_buttons).click()
                    return True
    except: pass
    return False

async def ocr_space(image_data):
    try:
        url = "https://api.ocr.space/parse/image"
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field('file', image_data, filename='captcha.png')
            form.add_field('apikey', 'helloworld')
            form.add_field('language', 'eng')
            form.add_field('isTable', 'true')
            form.add_field('OCREngine', '2')
            async with session.post(url, data=form, timeout=15) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get('ParsedResults'):
                        text = result['ParsedResults'][0].get('ParsedText', '').strip()
                        text = re.sub(r'[^0-9a-zA-Z]', '', text)
                        if len(text) >= 2:
                            return text
    except: return None

async def try_captcha_answer(client, event, captcha_text):
    try:
        captcha_text = captcha_text.strip()
        if captcha_text.isdigit():
            await event.message.respond(captcha_text)
            return True
        if event.message.buttons:
            for row in event.message.buttons:
                for btn in row:
                    if btn.text and captcha_text.lower() in btn.text.lower():
                        await btn.click()
                        return True
            # fallback
            first_btn = next((btn for row in event.message.buttons for btn in row if btn.text), None)
            if first_btn:
                await first_btn.click()
                return True
    except: pass
    return False

# ========== УЧАСТИЕ В КОНКУРСЕ ==========
async def participate_one_account(session_string, account_name, channels_input, ref_link, owner_bot, account_db_id=None):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False
        channel_list = [ch.strip() for ch in channels_input.split(',') if ch.strip()]
        if channel_list:
            await asyncio.gather(*[join_channel(client, ch) for ch in channel_list], return_exceptions=True)
            await asyncio.sleep(0.3)
        match = re.search(r'(?:t\.me|telegram\.me)/([^/?]+)(?:\?start=([\w.-]+))?', ref_link)
        if not match: return False
        bot_username, start_param = match.group(1), match.group(2)
        msg = f"/start {start_param}" if start_param else "/start"
        await client.send_message(bot_username, msg)
        success = False
        captcha_attempts = 0
        @client.on(events.NewMessage(from_user=bot_username))
        async def handler(event):
            nonlocal success, captcha_attempts
            text = event.message.text or ""
            text_lower = text.lower()
            win_triggers = ['вы участник', 'участник конкурса', 'поздравляем', 'успешно','вы в игре','участвуете','теперь вы участник','вы участвуете','вы зарегистрированы','вы приняты','участие подтверждено','вы уже участвуете','вы в списке','ваше участие','принято','registered','success','congratulations','вы подписаны']
            if any(p in text_lower for p in win_triggers):
                success = True
                return
            try:
                if event.message.photo and captcha_attempts < 3:
                    captcha_attempts += 1
                    await solve_captcha(client, event, account_name)
                    return
                if event.message.buttons:
                    buttons = [btn for row in event.message.buttons for btn in row if btn.text]
                    for btn in buttons:
                        if any(w in btn.text.lower() for w in ['участвовать','принять','join','play','начать','start','продолжить','готово','проверить','подписался','check']):
                            await btn.click()
                            return
                    await buttons[-1].click()
            except: pass
        for _ in range(15):
            if success: break
            await asyncio.sleep(2)
        await client.disconnect()
        if account_db_id and account_db_id not in win_monitors:
            asyncio.create_task(start_win_monitor(session_string, account_db_id, account_name, owner_bot))
        return success
    except:
        try: await client.disconnect()
        except: pass
        return False

async def join_channel(client, channel_input):
    if not channel_input: return False
    channel_input = channel_input.strip()
    try:
        if channel_input.startswith('+') or '/+' in channel_input:
            hash_match = re.search(r'\+([a-zA-Z0-9_-]+)', channel_input)
            if hash_match:
                await client(functions.messages.ImportChatInviteRequest(hash_match.group(1)))
                return True
        else:
            username = channel_input.replace('@', '').replace('https://t.me/', '')
            if '?' not in username:
                await client(functions.channels.JoinChannelRequest(username))
                return True
    except: pass
    return False

# ========== ОБРАБОТЧИКИ БОТА ==========
async def start_cmd(update: Update, context):
    if update.effective_user.id != OWNER_ID: return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton("📋 Аккаунты", callback_data="list_acc")],
        [InlineKeyboardButton("🎁 НОВЫЙ КОНКУРС", callback_data="new_contest")],
        [InlineKeyboardButton("📨 Коды Telegram", callback_data="get_codes")],
        [InlineKeyboardButton("🔄 Обновить коды со всех", callback_data="fetch_all_codes")],
        [InlineKeyboardButton("🔄 Проверить сессии", callback_data="check_sessions")],
        [InlineKeyboardButton("👁 Мониторинг упоминаний", callback_data="monitor_menu")],
        [InlineKeyboardButton("🚪 Управление каналами", callback_data="leave_menu")],
        [InlineKeyboardButton("🧹 Очистка неактивных каналов", callback_data="cleanup_menu")],
        [InlineKeyboardButton("💬 Комментировать пост", callback_data="comment_menu")],
        [InlineKeyboardButton("👍 Реакции на пост", callback_data="react_menu")],
        [InlineKeyboardButton("💰 Автобонус TakiWork", callback_data="bonus_menu")],
        [InlineKeyboardButton("🗑 Автоудаление невалидных", callback_data="auto_cleanup")],
    ])
    await update.message.reply_text("🎯 *Главное меню*", reply_markup=keyboard, parse_mode="Markdown")

async def callback_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id != OWNER_ID: return
    data = query.data

    # ===== ОБЩИЕ =====
    if data == "add_acc":
        user_state[user_id] = "waiting_phone"
        await query.edit_message_text("📱 Введите номер:\n+79123456789", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))
    elif data == "list_acc":
        accounts = get_all_accounts()
        if not accounts:
            text = "📭 Нет аккаунтов"
        else:
            text = f"📋 *Аккаунтов: {len(accounts)}*\n\n"
            for acc in accounts:
                name = acc[3] or acc[2] or 'Без имени'
                status = "✅" if acc[6] else "❌"
                codes = get_last_codes(acc[0])
                text += f"{status} • {name} — {acc[1]}"
                if codes: text += f" | 📨 {len(codes)} кодов"
                text += "\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Проверить все сессии", callback_data="check_sessions")],
            [InlineKeyboardButton("🗑 Автоудаление невалидных", callback_data="auto_cleanup")],
            [InlineKeyboardButton("◀️ Назад", callback_data="start_menu")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    elif data == "check_sessions":
        await query.edit_message_text("🔄 Проверяю сессии...")
        results = await check_all_sessions(context.bot)
        text = "📊 *Результаты:*\n\n" + "\n".join(results)
        await query.edit_message_text(text, parse_mode="Markdown")
    elif data == "auto_cleanup":
        await query.edit_message_text("🗑 Проверка и удаление невалидных...")
        deleted = await auto_cleanup_inactive_sessions(context.bot)
        await query.edit_message_text(f"✅ Удалено: {deleted}")
    elif data == "get_codes":
        accounts = get_all_accounts()
        if not accounts:
            await query.edit_message_text("📭 Нет аккаунтов"); return
        keyboard = []
        for acc in accounts:
            name = acc[3] or acc[2] or acc[1]
            codes = get_last_codes(acc[0])
            keyboard.append([InlineKeyboardButton(f"📱 {name} ({len(codes)})", callback_data=f"codes_{acc[0]}")])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="start_menu")])
        await query.edit_message_text("📨 Выбери аккаунт:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("codes_"):
        account_id = int(data.split("_")[1])
        codes = get_last_codes(account_id)
        account = next((acc for acc in get_all_accounts() if acc[0] == account_id), None)
        if not account: await query.edit_message_text("❌ Не найден"); return
        name = account[3] or account[2] or account[1]
        if not codes:
            text = f"📱 {name}\n📭 Нет кодов"
        else:
            text = f"📱 {name}\n📨 Последние коды:\n" + "\n".join([f"• `{c}` _{d}_" for c,d in codes])
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить коды", callback_data=f"fetch_{account_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="get_codes")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    elif data.startswith("fetch_"):
        account_id = int(data.split("_")[1])
        account = next((acc for acc in get_all_accounts() if acc[0] == account_id), None)
        if not account: return
        name = account[3] or account[2] or account[1]
        await query.edit_message_text(f"🔄 Собираю коды для {name}...")
        codes = await fetch_recent_codes_from_account(account[6], name, account_id, context.bot)
        text = f"📱 {name}\n📨 Кодов: {len(codes)}\n" + "\n".join([f"• `{c}`" for c in codes]) if codes else f"📭 Нет кодов"
        await query.edit_message_text(text, parse_mode="Markdown")
    elif data == "fetch_all_codes":
        accounts = get_all_accounts()
        await query.edit_message_text(f"🔄 Собираю коды со всех ({len(accounts)})...")
        total = 0
        for acc in accounts:
            total += len(await fetch_recent_codes_from_account(acc[6], acc[3] or acc[2], acc[0], context.bot))
        await query.edit_message_text(f"✅ Собрано кодов: {total}")
    elif data == "start_menu":
        await start_cmd(update, context)
    elif data == "cancel":
        user_state.pop(user_id, None)
        temp_data.pop(user_id, None)
        await query.edit_message_text("❌ Отменено")

    # ===== МОНИТОРИНГ =====
    elif data == "monitor_menu":
        accounts = get_all_accounts(only_active=True)
        monitored = len(win_monitors)
        text = f"👁 Мониторинг: активно {monitored}/{len(get_all_accounts())}\n"
        if monitored:
            text += "Активные:\n"
            for aid in win_monitors:
                if aid in active_accounts_cache:
                    text += f"• {active_accounts_cache[aid]['name']}\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Запустить все", callback_data="start_all_monitors")],
            [InlineKeyboardButton("⏹ Остановить все", callback_data="stop_all_monitors")],
            [InlineKeyboardButton("🎉 История", callback_data="wins_history")],
            [InlineKeyboardButton("◀️ Назад", callback_data="start_menu")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    elif data == "start_all_monitors":
        accounts = get_all_accounts(only_active=True)
        for acc in accounts:
            name = acc[3] or acc[2] or acc[1]
            if acc[0] not in win_monitors:
                await start_win_monitor(acc[6], acc[0], name, context.bot)
        await query.edit_message_text(f"✅ Запущено {len(win_monitors)}")
    elif data == "stop_all_monitors":
        for t in list(win_monitors.values()): t.cancel()
        win_monitors.clear(); active_accounts_cache.clear()
        await query.edit_message_text("✅ Остановлены")
    elif data == "wins_history":
        wins = get_wins_history(10)
        if not wins: text = "📭 Пусто"
        else:
            text = "🎉 История:\n\n" + "\n\n".join([f"👤 {w[1] or w[2]} ({w[3]})\n💬 {w[4]}\n📅 {w[7]}" for w in wins])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="monitor_menu")]]))

    # ===== УПРАВЛЕНИЕ КАНАЛАМИ =====
    elif data == "leave_menu":
        accounts = get_all_accounts(only_active=True)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 ПОЛНАЯ очистка (все)", callback_data="clear_all_chats")],
            [InlineKeyboardButton("🗑 ПОЛНАЯ очистка (один)", callback_data="clear_one_chat")],
            [InlineKeyboardButton("🚪 Выйти из ВСЕХ каналов", callback_data="leave_all_accounts")],
            [InlineKeyboardButton("📝 Выйти из указанных (все)", callback_data="leave_specific_all")],
            [InlineKeyboardButton("📝 Выйти из указанных (один)", callback_data="leave_specific")],
            [InlineKeyboardButton("📦 Архив (все)", callback_data="leave_archive_all")],
            [InlineKeyboardButton("📦 Архив (один)", callback_data="leave_archive_one")],
            [InlineKeyboardButton("📊 Статистика", callback_data="leave_stats")],
            [InlineKeyboardButton("◀️ Назад", callback_data="start_menu")],
        ])
        await query.edit_message_text("🚪 Управление каналами", reply_markup=keyboard)
    elif data == "clear_all_chats":
        accounts = get_all_accounts(only_active=True)
        await query.edit_message_text(f"🗑 Полная очистка для {len(accounts)} аккаунтов...")
        total_left = total_deleted = 0
        for acc in accounts:
            l,d,_ = await leave_all_chats(acc[6], acc[3] or acc[2], acc[0], context.bot)
            total_left += l; total_deleted += d
        await context.bot.send_message(OWNER_ID, f"✅ Каналов покинуто: {total_left}\nЧатов удалено: {total_deleted}", parse_mode="Markdown")
    elif data == "clear_one_chat":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"{acc[3] or acc[2]}", callback_data=f"clearall_{acc[0]}")] for acc in get_all_accounts(only_active=True)] + [[InlineKeyboardButton("◀️ Назад", callback_data="leave_menu")]])
        await query.edit_message_text("Выберите аккаунт:", reply_markup=keyboard)
    elif data.startswith("clearall_"):
        acc_id = int(data.split("_")[1])
        acc = next((a for a in get_all_accounts() if a[0]==acc_id), None)
        if not acc: return
        await query.edit_message_text(f"🗑 Очистка {acc[3] or acc[2]}...")
        l,d,_ = await leave_all_chats(acc[6], acc[3] or acc[2], acc_id, context.bot)
        await query.edit_message_text(f"✅ {acc[3] or acc[2]}\nКаналов: {l}, чатов: {d}", parse_mode="Markdown")
    elif data == "leave_all_accounts":
        accounts = get_all_accounts(only_active=True)
        total_left = 0
        for acc in accounts:
            l,_ = await leave_all_channels(acc[6], acc[3] or acc[2], acc[0], context.bot)
            total_left += l
        await context.bot.send_message(OWNER_ID, f"✅ Всего покинуто каналов: {total_left}", parse_mode="Markdown")
    elif data == "leave_specific_all":
        user_state[user_id] = "waiting_leave_list_all"
        temp_data[user_id] = {"leave_accounts": get_all_accounts(only_active=True)}
        await query.edit_message_text("📝 Отправьте список каналов", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))
    elif data == "leave_specific":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"{acc[3] or acc[2]}", callback_data=f"leavespec_{acc[0]}")] for acc in get_all_accounts(only_active=True)] + [[InlineKeyboardButton("◀️ Назад", callback_data="leave_menu")]])
        await query.edit_message_text("Выберите аккаунт:", reply_markup=keyboard)
    elif data.startswith("leavespec_"):
        acc_id = int(data.split("_")[1])
        acc = next((a for a in get_all_accounts() if a[0]==acc_id), None)
        if not acc: return
        user_state[user_id] = "waiting_leave_list"
        temp_data[user_id] = {"leave_account_id": acc_id, "leave_account": acc}
        await query.edit_message_text(f"📝 Список каналов для {acc[3] or acc[2]}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))
    elif data == "leave_archive_all":
        accounts = get_all_accounts(only_active=True)
        total = 0
        for acc in accounts:
            l,_ = await leave_archived_chats(acc[6], acc[3] or acc[2], acc[0], context.bot)
            total += l
        await context.bot.send_message(OWNER_ID, f"✅ Архив очищен, покинуто: {total}", parse_mode="Markdown")
    elif data == "leave_archive_one":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"{acc[3] or acc[2]}", callback_data=f"archive_{acc[0]}")] for acc in get_all_accounts(only_active=True)] + [[InlineKeyboardButton("◀️ Назад", callback_data="leave_menu")]])
        await query.edit_message_text("Выберите аккаунт:", reply_markup=keyboard)
    elif data.startswith("archive_"):
        acc_id = int(data.split("_")[1])
        acc = next((a for a in get_all_accounts() if a[0]==acc_id), None)
        if not acc: return
        l,_ = await leave_archived_chats(acc[6], acc[3] or acc[2], acc_id, context.bot)
        await query.edit_message_text(f"✅ Архив: покинуто {l}", parse_mode="Markdown")
    elif data == "leave_stats":
        stats = get_leave_stats(20)
        text = "📊 Статистика:\n\n" + "\n".join([f"👤 {s[0] or s[1]} — {s[2]} ({s[3]}) {s[4]}" for s in stats]) if stats else "Нет данных"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="leave_menu")]]))

    # ===== ОЧИСТКА НЕАКТИВНЫХ =====
    elif data == "cleanup_menu":
        accounts = get_all_accounts(only_active=True)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🧹 ВСЕ неактивные", callback_data="cleanup_all")]] +
            [[InlineKeyboardButton(f"{acc[3] or acc[2]}", callback_data=f"cleanup_{acc[0]}")] for acc in accounts] +
            [[InlineKeyboardButton("◀️ Назад", callback_data="start_menu")]]
        )
        await query.edit_message_text("🧹 Очистка неактивных (>1 мес)", reply_markup=keyboard)
    elif data == "cleanup_all":
        accounts = get_all_accounts(only_active=True)
        total = 0
        for acc in accounts:
            l,_,_ = await cleanup_inactive_chats(acc[6], acc[3] or acc[2], acc[0], context.bot)
            total += l
        await context.bot.send_message(OWNER_ID, f"✅ Покинуто неактивных: {total}", parse_mode="Markdown")
    elif data.startswith("cleanup_"):
        acc_id = int(data.split("_")[1])
        acc = next((a for a in get_all_accounts() if a[0]==acc_id), None)
        if not acc: return
        await query.edit_message_text(f"🧹 Очистка {acc[3] or acc[2]}...")
        l,s,e = await cleanup_inactive_chats(acc[6], acc[3] or acc[2], acc_id, context.bot)
        await query.edit_message_text(f"✅ Покинуто: {l}, пропущено: {s}, ошибок: {e}", parse_mode="Markdown")

    # ===== КОММЕНТАРИИ =====
    elif data == "comment_menu":
        accounts = get_all_accounts(only_active=True)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Все аккаунты", callback_data="comment_all")],
            [InlineKeyboardButton("💬 Выбрать один", callback_data="comment_one")],
            [InlineKeyboardButton("◀️ Назад", callback_data="start_menu")],
        ])
        await query.edit_message_text(f"💬 Комментирование\nАккаунтов: {len(accounts)}", reply_markup=keyboard)
    elif data == "comment_all":
        user_state[user_id] = "waiting_comment_all"
        temp_data[user_id] = {"comment_accounts": get_all_accounts(only_active=True)}
        await query.edit_message_text("📝 Отправьте:\n1 строка - ссылка\n2 строка - текст\n3 строка - интервал (мин)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))
    elif data == "comment_one":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"{acc[3] or acc[2]}", callback_data=f"commentacc_{acc[0]}")] for acc in get_all_accounts(only_active=True)] + [[InlineKeyboardButton("◀️ Назад", callback_data="comment_menu")]])
        await query.edit_message_text("Выберите аккаунт:", reply_markup=keyboard)
    elif data.startswith("commentacc_"):
        acc_id = int(data.split("_")[1])
        acc = next((a for a in get_all_accounts() if a[0]==acc_id), None)
        if not acc: return
        user_state[user_id] = "waiting_comment"
        temp_data[user_id] = {"comment_account": acc}
        await query.edit_message_text(f"📝 Для {acc[3] or acc[2]}:\n1 - ссылка\n2 - текст\n3 - задержка", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))

    # ===== РЕАКЦИИ =====
    elif data == "react_menu":
        accounts = get_all_accounts(only_active=True)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👍 Все аккаунты", callback_data="react_all")],
            [InlineKeyboardButton("👍 Выбрать один", callback_data="react_one")],
            [InlineKeyboardButton("◀️ Назад", callback_data="start_menu")],
        ])
        await query.edit_message_text(f"👍 Реакции\nАккаунтов: {len(accounts)}", reply_markup=keyboard)
    elif data == "react_all":
        user_state[user_id] = "waiting_react_all"
        temp_data[user_id] = {"react_accounts": get_all_accounts(only_active=True)}
        await query.edit_message_text("📝 Отправьте:\n1 строка - ссылка\n2 строка - эмодзи", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))
    elif data == "react_one":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"{acc[3] or acc[2]}", callback_data=f"reactacc_{acc[0]}")] for acc in get_all_accounts(only_active=True)] + [[InlineKeyboardButton("◀️ Назад", callback_data="react_menu")]])
        await query.edit_message_text("Выберите аккаунт:", reply_markup=keyboard)
    elif data.startswith("reactacc_"):
        acc_id = int(data.split("_")[1])
        acc = next((a for a in get_all_accounts() if a[0]==acc_id), None)
        if not acc: return
        user_state[user_id] = "waiting_react"
        temp_data[user_id] = {"react_account": acc}
        await query.edit_message_text(f"📝 Для {acc[3] or acc[2]}:\n1 - ссылка\n2 - эмодзи", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))

    # ===== АВТОБОНУС TAKIWORK =====
    elif data == "bonus_menu":
        # Показываем меню выбора аккаунтов для запуска бонуса
        accounts = get_all_accounts(only_active=True)
        if not accounts:
            await query.edit_message_text("📭 Нет активных аккаунтов"); return
        # Сохраняем список аккаунтов и состояние выбора (пока все не выбраны)
        temp_data[user_id] = {"bonus_accounts": accounts, "selected": set()}
        await show_bonus_selection(query, user_id, accounts, set())
    elif data.startswith("toggle_bonus_"):
        acc_id = int(data.split("_")[2])
        data_dict = temp_data.get(user_id)
        if not data_dict: return
        selected = data_dict["selected"]
        if acc_id in selected:
            selected.discard(acc_id)
        else:
            selected.add(acc_id)
        await show_bonus_selection(query, user_id, data_dict["bonus_accounts"], selected)
    elif data == "start_bonus_selected":
        data_dict = temp_data.get(user_id)
        if not data_dict: return
        selected = data_dict["selected"]
        accounts = [acc for acc in data_dict["bonus_accounts"] if acc[0] in selected]
        if not accounts:
            await query.answer("❌ Не выбрано ни одного аккаунта", show_alert=True)
            return
        # Запускаем бонус для выбранных
        started = 0
        for acc in accounts:
            if acc[0] not in bonus_tasks:
                name = acc[3] or acc[2] or acc[1]
                await auto_bonus_loop(acc[6], name, acc[0], context.bot)
                started += 1
        await query.edit_message_text(f"✅ Запущен бонус для {started} аккаунтов. Каждый час будет отправляться 💸 Бонус.")
        temp_data.pop(user_id, None)
    elif data == "stop_all_bonus":
        for tid in list(bonus_tasks.keys()):
            bonus_tasks[tid].cancel()
            del bonus_tasks[tid]
        await query.edit_message_text("✅ Бонус остановлен для всех аккаунтов")
        temp_data.pop(user_id, None)
    elif data == "cancel_bonus":
        temp_data.pop(user_id, None)
        await query.edit_message_text("❌ Отменено")

    # ===== КОНКУРС =====
    elif data == "new_contest":
        if not get_all_accounts(only_active=True):
            await query.edit_message_text("❌ Нет активных аккаунтов!"); return
        user_state[user_id] = "waiting_channels"
        await query.edit_message_text("📢 *Этап 1/2*\nВведите каналы через запятую:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]))
    elif data == "start_contest_now":
        if user_id not in temp_data: return
        channels = temp_data[user_id].get('channels')
        ref_link = temp_data[user_id].get('ref_link')
        if not channels or not ref_link: await query.edit_message_text("❌ Нет данных"); return
        accounts = get_all_accounts(only_active=True)
        temp_data.pop(user_id, None)
        total = len(accounts)
        await query.edit_message_text(f"🚀 Запуск {total} аккаунтов (батчами по 5)...")
        success_count = 0
        batch_size = 5
        for i in range(0, total, batch_size):
            batch = accounts[i:i+batch_size]
            tasks = []
            for acc in batch:
                name = acc[3] or acc[2] or acc[1]
                tasks.append(participate_one_account(acc[6], name, channels, ref_link, context.bot, acc[0]))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count += sum(1 for r in results if r is True)
            done = min(i+batch_size, total)
            await query.edit_message_text(f"🚀 Прогресс: {done}/{total}")
            if i + batch_size < total: await asyncio.sleep(0.5)
        await context.bot.send_message(OWNER_ID, f"✅ *ГОТОВО!*\n✅ Успешно: {success_count}\n❌ Неудачно: {total - success_count}", parse_mode="Markdown")

async def show_bonus_selection(query, user_id, accounts, selected):
    keyboard = []
    for acc in accounts:
        name = acc[3] or acc[2] or acc[1]
        prefix = "✅ " if acc[0] in selected else "☑️ "
        keyboard.append([InlineKeyboardButton(f"{prefix}{name}", callback_data=f"toggle_bonus_{acc[0]}")])
    keyboard.append([InlineKeyboardButton("🚀 Запустить бонус для выбранных", callback_data="start_bonus_selected")])
    keyboard.append([InlineKeyboardButton("⏹ Остановить все бонусы", callback_data="stop_all_bonus")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_bonus")])
    await query.edit_message_text("💸 Выберите аккаунты для автобонуса TakiWork:", reply_markup=InlineKeyboardMarkup(keyboard))

async def message_handler(update: Update, context):
    user_id = update.effective_user.id
    if user_id != OWNER_ID: return
    state = user_state.get(user_id)
    text = update.message.text.strip() if update.message.text else ""

    # Обработка состояний ожидания списка каналов и т.д.
    if state == "waiting_leave_list_all":
        data = temp_data.get(user_id)
        if not data: return
        accounts = data["leave_accounts"]
        target_list = extract_channels_from_text(text)
        if update.message.forward_from_chat:
            target_list.extend(extract_channels_from_text(update.message.text or ""))
        target_list = list(set(target_list))
        if not target_list: await update.message.reply_text("❌ Не найдено каналов"); return
        total_left = 0
        for acc in accounts:
            name = acc[3] or acc[2] or acc[1]
            l,_ = await leave_specific_channels(acc[6], name, acc[0], context.bot, target_list)
            total_left += l
        await context.bot.send_message(OWNER_ID, f"✅ Покинуто: {total_left}", parse_mode="Markdown")
        user_state.pop(user_id, None); temp_data.pop(user_id, None)
    elif state == "waiting_leave_list":
        data = temp_data.get(user_id)
        if not data: return
        acc = data["leave_account"]
        target_list = extract_channels_from_text(text)
        if update.message.forward_from_chat:
            target_list.extend(extract_channels_from_text(update.message.text or ""))
        target_list = list(set(target_list))
        if not target_list: await update.message.reply_text("❌ Не найдено каналов"); return
        l,_ = await leave_specific_channels(acc[6], acc[3] or acc[2], acc[0], context.bot, target_list)
        await update.message.reply_text(f"✅ Покинуто {l}", parse_mode="Markdown")
        user_state.pop(user_id, None); temp_data.pop(user_id, None)

    elif state == "waiting_comment_all":
        data = temp_data.get(user_id)
        if not data: return
        lines = text.split('\n', 2)
        post_link = lines[0].strip() if len(lines)>0 else ""
        comment_text = lines[1].strip() if len(lines)>1 else "👍"
        delay = 5
        if len(lines)>2:
            try: delay = int(lines[2].strip().replace('мин',''))
            except: pass
        if 't.me/' not in post_link: await update.message.reply_text("❌ Неверная ссылка"); return
        accounts = data["comment_accounts"]
        success = 0
        for i, acc in enumerate(accounts):
            d = 0 if i==0 else delay
            if await comment_on_post(acc[6], acc[3] or acc[2], post_link, comment_text, context.bot, d):
                success += 1
        await context.bot.send_message(OWNER_ID, f"✅ Комментарии: {success}/{len(accounts)}", parse_mode="Markdown")
        user_state.pop(user_id, None); temp_data.pop(user_id, None)
    elif state == "waiting_comment":
        data = temp_data.get(user_id)
        if not data: return
        lines = text.split('\n', 2)
        post_link = lines[0].strip() if len(lines)>0 else ""
        comment_text = lines[1].strip() if len(lines)>1 else "👍"
        delay = 0
        if len(lines)>2:
            try: delay = int(lines[2].strip().replace('мин',''))
            except: pass
        if 't.me/' not in post_link: await update.message.reply_text("❌ Неверная ссылка"); return
        acc = data["comment_account"]
        res = await comment_on_post(acc[6], acc[3] or acc[2], post_link, comment_text, context.bot, delay)
        await update.message.reply_text(f"{'✅' if res else '❌'}")
        user_state.pop(user_id, None); temp_data.pop(user_id, None)

    elif state == "waiting_react_all":
        data = temp_data.get(user_id)
        if not data: return
        lines = text.split('\n', 1)
        post_link = lines[0].strip()
        emoji = lines[1].strip() if len(lines)>1 else "👍"
        if 't.me/' not in post_link: await update.message.reply_text("❌ Неверная ссылка"); return
        accounts = data["react_accounts"]
        success = 0
        for acc in accounts:
            if await react_to_post(acc[6], acc[3] or acc[2], post_link, emoji, context.bot):
                success += 1
            await asyncio.sleep(0.5)
        await context.bot.send_message(OWNER_ID, f"✅ Реакций: {success}/{len(accounts)}", parse_mode="Markdown")
        user_state.pop(user_id, None); temp_data.pop(user_id, None)
    elif state == "waiting_react":
        data = temp_data.get(user_id)
        if not data: return
        lines = text.split('\n', 1)
        post_link = lines[0].strip()
        emoji = lines[1].strip() if len(lines)>1 else "👍"
        if 't.me/' not in post_link: await update.message.reply_text("❌ Неверная ссылка"); return
        acc = data["react_account"]
        res = await react_to_post(acc[6], acc[3] or acc[2], post_link, emoji, context.bot)
        await update.message.reply_text(f"{'✅' if res else '❌'}")
        user_state.pop(user_id, None); temp_data.pop(user_id, None)

    elif state == "waiting_phone":
        user_state[user_id] = "waiting_code"
        temp_data[user_id] = {"phone": text}
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(text)
            temp_data[user_id]["client"] = client
            temp_data[user_id]["hash"] = sent.phone_code_hash
            await update.message.reply_text("📨 Введите код из Telegram:")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            user_state.pop(user_id, None)
            await client.disconnect()
    elif state == "waiting_code":
        data = temp_data.get(user_id, {})
        client = data.get("client")
        phone = data.get("phone")
        code = text
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=data["hash"])
            me = await client.get_me()
            session = client.session.save()
            save_account(phone, me.username, me.first_name, me.last_name, me.id, session)
            accounts = get_all_accounts()
            for acc in accounts:
                if acc[1] == phone:
                    save_telegram_code(acc[0], code)
                    break
            name = f"@{me.username or me.first_name}"
            await update.message.reply_text(f"✅ Аккаунт {name} добавлен!")
            for acc in get_all_accounts():
                if acc[1] == phone:
                    await start_win_monitor(session, acc[0], name, context.bot)
                    break
            user_state.pop(user_id, None); temp_data.pop(user_id, None)
            await client.disconnect()
        except SessionPasswordNeededError:
            user_state[user_id] = "waiting_password"
            await update.message.reply_text("🔐 Введите пароль 2FA:")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            user_state.pop(user_id, None)
            await client.disconnect()
    elif state == "waiting_password":
        data = temp_data.get(user_id, {})
        client = data.get("client")
        phone = data.get("phone")
        try:
            await client.sign_in(password=text)
            me = await client.get_me()
            session = client.session.save()
            save_account(phone, me.username, me.first_name, me.last_name, me.id, session)
            name = f"@{me.username or me.first_name}"
            await update.message.reply_text(f"✅ Аккаунт {name} добавлен!")
            for acc in get_all_accounts():
                if acc[1] == phone:
                    await start_win_monitor(session, acc[0], name, context.bot)
                    break
            user_state.pop(user_id, None); temp_data.pop(user_id, None)
            await client.disconnect()
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            user_state.pop(user_id, None)
            await client.disconnect()
    elif state == "waiting_channels":
        temp_data[user_id] = {'channels': text}
        user_state[user_id] = "waiting_link"
        await update.message.reply_text("🔗 *Этап 2/2*\nВведите ссылку на конкурс:", parse_mode="Markdown")
    elif state == "waiting_link":
        temp_data[user_id]['ref_link'] = text
        user_state.pop(user_id, None)
        accounts = get_all_accounts(only_active=True)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 НАЧАТЬ УЧАСТИЕ", callback_data="start_contest_now")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ])
        await update.message.reply_text(f"✅ Данные получены\nАктивных аккаунтов: {len(accounts)}", reply_markup=keyboard)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    print("✅ Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
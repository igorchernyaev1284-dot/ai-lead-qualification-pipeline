import asyncio
import logging
import os
import sys
import re
import requests
import urllib3
from telethon import TelegramClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

API_ID = os.getenv('TG_API_ID')
API_HASH = os.getenv('TG_API_HASH')
SESSION_PATH = os.getenv('TG_SESSION_PATH')
CONFIG_URL = os.getenv('PARSER_CONFIG_URL')
LEADS_URL = os.getenv('PARSER_LEADS_URL')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')

if not all([API_ID, API_HASH, SESSION_PATH, CONFIG_URL, LEADS_URL]):
    logging.error("Критическая ошибка: Не все переменные окружения заданы!")
    sys.exit(1)

API_ID = int(API_ID)

CHATS_RAW = []
CHATS_RESOLVED = set()
KEYWORDS = []
STOP_WORDS = []
INTENTS = []
DIALOGS_CACHE = {}
LAST_MSG_IDS = {}

def clean_id(val):
    val_str = str(val).strip().lower()
    if val_str.endswith('.0'):
        val_str = val_str[:-2]
    return val_str

async def build_dialogs_cache():
    global DIALOGS_CACHE
    try:
        dialogs = await client.get_dialogs()
        new_cache = {}
        for d in dialogs:
            new_cache[str(d.id)] = d.id
            new_cache[str(d.id).replace("-100", "")] = d.id
            if getattr(d.entity, 'username', None):
                un = d.entity.username.lower()
                new_cache[un] = d.id
                new_cache[f"@{un}"] = d.id
                new_cache[f"https://t.me/{un}"] = d.id
            title = d.name.lower().strip()
            if title:
                new_cache[title] = d.id
        DIALOGS_CACHE = new_cache
    except Exception as e:
        logging.error(f"Ошибка сборки кеша: {e}")

def resolve_to_id(item):
    item_str = clean_id(item)
    if not item_str: return None
    if item_str in DIALOGS_CACHE: return DIALOGS_CACHE[item_str]
    if item_str.startswith('-') or item_str.isdigit():
        try: return int(item_str)
        except: pass
    return None

async def get_remote_config():
    global CHATS_RAW, CHATS_RESOLVED, KEYWORDS, STOP_WORDS, INTENTS
    try:
        response = await asyncio.to_thread(requests.get, CONFIG_URL, timeout=10, verify=False)
        if response.status_code == 200:
            data = response.json()
            CHATS_RAW = [str(c).strip() for c in data.get('chats', []) if str(c).strip()]
            
            KEYWORDS = [str(k).strip().lower() for k in data.get('keywords', []) if str(k).strip()]
            STOP_WORDS = [str(s).strip().lower() for s in data.get('stop_words', []) if str(s).strip()]
            INTENTS = [str(i).strip().lower() for i in data.get('intents', []) if str(i).strip()]
            
            await build_dialogs_cache()
            
            new_resolved = set()
            for c in CHATS_RAW:
                resolved = resolve_to_id(c)
                if resolved: new_resolved.add(resolved)
            CHATS_RESOLVED = new_resolved
            logging.info(f"Конфиг обновлен. Чатов: {len(CHATS_RESOLVED)}, Ключей: {len(KEYWORDS)}, Интентов: {len(INTENTS)}, Стоп-слов: {len(STOP_WORDS)}")
    except Exception as e:
        logging.error(f"Ошибка конфига: {e}")

async def config_updater():
    while True:
        await asyncio.sleep(900)
        await get_remote_config()

async def active_pull_parser():
    logging.info("=== ЗАПУСК АКТИВНОГО PULL-ПАРСЕРА (С ФИЛЬТРАЦИЕЙ ПО КЛЮЧАМ И ИНТЕНТАМ) ===")
    while True:
        for chat_id in list(CHATS_RESOLVED):
            try:
                if chat_id not in LAST_MSG_IDS:
                    msgs = await client.get_messages(chat_id, limit=1)
                    if msgs:
                        LAST_MSG_IDS[chat_id] = msgs[0].id
                    continue
                
                msgs = await client.get_messages(chat_id, min_id=LAST_MSG_IDS[chat_id], limit=50)
                
                if not msgs:
                    await asyncio.sleep(0.5)
                    continue
                
                LAST_MSG_IDS[chat_id] = max(m.id for m in msgs)
                
                for msg in msgs:
                    text = msg.text
                    if not text: continue
                    
                    if len(text.split()) < 6: continue
                    
                    if re.search(r'(https?://|t\.me|www\.)', text.lower()): continue
                    
                    text_lc = text.lower()
                    
                    skip = False
                    for stop in STOP_WORDS:
                        if stop and stop in text_lc:
                            skip = True
                            break
                    if skip: continue
                    
                    has_keyword = any(word in text_lc for word in KEYWORDS if word)
                    has_intent = any(intent in text_lc for intent in INTENTS if intent)
                    
                    if not (has_keyword and has_intent):
                        continue
                    
                    chat = await msg.get_chat()
                    chat_username = getattr(chat, 'username', None)
                    chat_id_full = str(chat_id)
                    chat_id_short = chat_id_full.replace("-100", "")
                    
                    sender = await msg.get_sender()
                    sender_username = getattr(sender, 'username', None)
                    sender_first = getattr(sender, 'first_name', getattr(sender, 'title', ''))
                    sender_last = getattr(sender, 'last_name', '')
                    sender_full_name = f"{sender_first} {sender_last}".strip() or "Без имени"

                    sender_display = f"{sender_full_name} (https://t.me/{sender_username})" if sender_username else f"{sender_full_name} (tg://user?id={getattr(sender, 'id', 'unknown')})"

                    data = {
                        'source': 'chat_lead_raw',
                        'text': text,
                        'channel': f"@{chat_username}" if chat_username else chat_id_full,
                        'sender': sender_display,
                        'date': msg.date.strftime("%Y-%m-%d %H:%M:%S"),
                        'link': f"https://t.me/{chat_username}/{msg.id}" if chat_username else f"https://t.me/c/{chat_id_short}/{msg.id}"
                    }

                    res = await asyncio.to_thread(requests.post, LEADS_URL, json=data, timeout=5, verify=False)
                    logging.info(f"✓ Отправлено в n8n! Статус: {res.status_code} | Текст: {text[:30]}...")
                    
            except Exception as e:
                logging.debug(f"Ошибка чтения чата {chat_id}: {e}")
            
            await asyncio.sleep(1)
            
        await asyncio.sleep(30)

async def connection_watchdog():
    while True:
        await asyncio.sleep(60)
        try:
            if not client.is_connected():
                await client.connect()
            else:
                await client.get_me()
        except Exception as e:
            try:
                await client.disconnect()
                await asyncio.sleep(5)
                await client.connect()
            except: pass

client = TelegramClient(SESSION_PATH, API_ID, API_HASH, device_model="PC 64bit", system_version="Windows 10", app_version="4.8.1")

async def main():
    await client.start()
    await get_remote_config()
    
    if TG_CHAT_ID:
        try:
            admin_chat = int(TG_CHAT_ID) if TG_CHAT_ID.startswith('-') or TG_CHAT_ID.isdigit() else TG_CHAT_ID
            await client.send_message(admin_chat, "🚀 **Pull-Парсер запущен! Фильтрация по ключам и интентам ВКЛЮЧЕНА.**")
        except: pass
            
    asyncio.create_task(config_updater())
    asyncio.create_task(connection_watchdog())
    
    await active_pull_parser()

if __name__ == '__main__':
    asyncio.run(main())

import logging
from logging.handlers import RotatingFileHandler

import asyncio
import aiosqlite
import re
from aiogram import Bot, types
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from config import DB_NAME, BOT_TOKEN, CHANNEL_ID


CHECK_INTERVAL = 5  # seconds between watcher loops
DELAY_BETWEEN_PRODUCTS = 1.5  # seconds between sending each product
CONCURRENT_LIMIT = 1  # safest for SendMediaGroup-heavy posts
MAX_RETRIES = 3  # retry attempts for flood control


AUTOPOST_INTERVAL = 60 * 60   # 60 минут
AUTOPOST_BATCH_SIZE = 1       # 1 товар за цикл
MIN_STOCK_TO_POST = 2         # “если больше 1”


MANAGER_URL = "https://t.me/tumi_kazakhstan"  # <-- замени на юзернейм менеджера

def build_kb(product_url: str) -> types.InlineKeyboardMarkup:
    rows = []
    if product_url:
        rows.append([types.InlineKeyboardButton(text="КУПИТЬ", url=product_url)])
    rows.append([types.InlineKeyboardButton(text="НАПИСАТЬ МЕНЕДЖЕРУ", url=MANAGER_URL)])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# --- Logging Setup ---

# Formatter
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

# General bot logger — INFO and above
bot_logger = logging.getLogger("bot")
bot_logger.setLevel(logging.INFO)

bot_handler = RotatingFileHandler(
    filename="bot.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8"
)
bot_handler.setFormatter(formatter)
bot_logger.addHandler(bot_handler)

# Error logger — WARNING and above
error_logger = logging.getLogger("errors")
error_logger.setLevel(logging.WARNING)

error_handler = RotatingFileHandler(
    filename="errors.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
error_handler.setFormatter(formatter)
error_logger.addHandler(error_handler)

# ✅ Critical line: prevent error logs from going to bot.log
error_logger.propagate = False

# Console output
console = logging.StreamHandler()
console.setFormatter(formatter)
bot_logger.addHandler(console)
error_logger.addHandler(console)

# --- Test ---
# bot_logger.info("INFO: bot logger working")
# error_logger.error("ERROR: error logger working")



def clean_html(text: str) -> str:
    if not text:
        return ""
    # переносы
    text = re.sub(r'</?p[^>]*>', '\n', text)
    text = re.sub(r'<br\s*/?>', '\n', text)

    # удаляем все теги КРОМЕ <b>...</b>
    text = re.sub(r'</?(?!b\b)[a-zA-Z][^>]*>', '', text)

    return text.strip()


async def send_images(bot: Bot, chat_id: str, image_urls: list[str], caption: str, keyboard: types.InlineKeyboardMarkup):
    # всегда берём только 1-ю картинку
    if image_urls:
        msg = await bot.send_photo(
            chat_id=chat_id,
            photo=image_urls[0],
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return [msg.message_id]

    # если картинок нет — просто текст + кнопки
    msg = await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )
    return [msg.message_id]


async def delete_previous_messages(db: aiosqlite.Connection, bot: Bot, product_id: int):
    async with db.execute("SELECT message_id FROM product_messages WHERE product_id = ?", (product_id,)) as cur:
        rows = await cur.fetchall()

    for (msg_id,) in rows:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id)
            print(f"🗑️ Deleted message {msg_id}")
            bot_logger.info(f"Deleted message {msg_id} for product {product_id}")
        except TelegramBadRequest:
            print(f"⚠️ Message {msg_id} could not be deleted (already removed).")
            error_logger.warning(f"Failed to delete message {msg_id} for product {product_id}")


    await db.execute("DELETE FROM product_messages WHERE product_id = ?", (product_id,))
    await db.commit()

async def delete_out_of_stock(bot: Bot, db: aiosqlite.Connection, product_id: int):
    """ Deletes Telegram messages when stock is gone """
    print(f"🚫 Product {product_id} is OUT OF STOCK — removing posts...")
    bot_logger.info(f"Product {product_id} OUT OF STOCK — deleting messages")

    await delete_previous_messages(db, bot, product_id)

    # Optional: hide it from further processing
    # await db.execute("UPDATE products SET needs_update = 0 WHERE id = ?", (product_id,))
    # await db.commit()


async def save_message_ids(db: aiosqlite.Connection, product_id: int, message_ids: list[int]):
    await db.executemany(
        "INSERT INTO product_messages (product_id, message_id) VALUES (?, ?)",
        [(product_id, mid) for mid in message_ids]
    )
    await db.commit()


# async def get_products_to_update(db: aiosqlite.Connection):
#     async with db.execute("SELECT id FROM products WHERE visible = 1 AND needs_update = 1") as cursor:
#         rows = await cursor.fetchall()
#         return [r[0] for r in rows]

async def get_products_to_update(db: aiosqlite.Connection):
    """
    Возвращает два списка:
      - update_list: товары, которые нужно отправить/обновить (needs_update = 1 и есть остаток)
      - delete_list: товары, чьи посты нужно удалить (stock = 0 и есть сообщения в product_messages)
    """
    # 1. Товары, которые нужно отправить / обновить
    async with db.execute("""
        SELECT id
        FROM products
        WHERE visible = 1
          AND needs_update = 1
          AND stock IS NOT NULL
          AND stock > 0
    """) as cursor:
        rows = await cursor.fetchall()
        update_list = [row[0] for row in rows]

    # 2. Товары, по которым НУЖНО удалить посты (есть сообщения и нет остатков)
    async with db.execute("""
        SELECT DISTINCT p.id
        FROM products p
        JOIN product_messages pm ON pm.product_id = p.id
        WHERE p.visible = 1
          AND (p.stock IS NULL OR p.stock = 0)
    """) as cursor:
        rows = await cursor.fetchall()
        delete_list = [row[0] for row in rows]

    return update_list, delete_list



async def mark_product_sent(db: aiosqlite.Connection, product_id: int):
    await db.execute("UPDATE products SET needs_update = 0 WHERE id = ?", (product_id,))
    await db.commit()


async def send_product(bot: Bot, db: aiosqlite.Connection, product_id: int):
    async with db.execute(
        "SELECT name, description, url FROM products WHERE id = ? AND visible = 1",
        (product_id,)
    ) as cursor:
        product = await cursor.fetchone()
        if not product:
            return
    name, description, url = product
    description = clean_html(description)

    async with db.execute(
        "SELECT image_url FROM product_images WHERE product_id = ?", (product_id,)
    ) as cursor:
        images = await cursor.fetchall()
        image_urls = [img[0] for img in images if img[0]]

    await delete_previous_messages(db, bot, product_id)

    kb = build_kb(url)

    caption = f"🛒 <b>{name}</b>\n\n{description}"
    message_ids = []

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            message_ids = await send_images(bot, CHANNEL_ID, image_urls, caption, kb)
            break
        except TelegramRetryAfter as e:
            wait = getattr(e, "retry_after", 5)
            backoff = wait * attempt
            print(f"⚠️ Flood control hit for product {product_id}. Retry {attempt}/{MAX_RETRIES} after {backoff}s.")
            error_logger.error(f"⚠️ Flood control hit for product {product_id}. Retry {attempt}/{MAX_RETRIES} after {backoff}s.")
            await asyncio.sleep(backoff)
        except Exception as e:
            print(f"⚠️ Unexpected error sending product {product_id}: {e}")
            error_logger.error(f"⚠️ Unexpected error sending product {product_id}: {e}")
            return  # skip this product

    if message_ids:
        await save_message_ids(db, product_id, message_ids)
        await mark_product_sent(db, product_id)
        print(f"✅ Product {product_id} posted.")
        bot_logger.info(f"Product {product_id} posted.")

    await asyncio.sleep(DELAY_BETWEEN_PRODUCTS)


async def watch_products(bot: Bot):
    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    async with aiosqlite.connect(DB_NAME, timeout=30) as db:
        while True:
            try:
                update_list, delete_list = await get_products_to_update(db)
                # ⭐ Delete products that are OUT OF STOCK
                for pid in delete_list:
                    await delete_out_of_stock(bot, db, pid)

                # Process normal updates
                if update_list:
                    async def sem_task(pid):
                        async with semaphore:
                            await send_product(bot, db, pid)
                    await asyncio.gather(*(sem_task(pid) for pid in update_list))
                else:
                    print("⏱️ No updates found.")

            except Exception as e:
                print(f"⚠️ Error in watcher loop: {e}")
                error_logger.error(f"⚠️ Error in watcher loop: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

def get_type_priority(name: str, category: str) -> int:
    n = (name or "").lower()
    c = (category or "").lower()

    if "каталог/рюкзаки" in c or "рюкзак" in n:
        return 1
    if "каталог/плечевые сумки" in c or "сумка" in n:
        return 2
    if "каталог/багаж" in c or "чемодан" in n or "ручная кладь" in c:
        return 3
    return 99



async def get_products_for_autopost(db: aiosqlite.Connection, limit: int):
    async with db.execute("""
        SELECT p.id, p.name, p.category
        FROM products p
        LEFT JOIN product_messages pm ON pm.product_id = p.id
        WHERE p.visible = 1
          AND p.stock IS NOT NULL
          AND p.stock >= ?
          AND pm.product_id IS NULL
    """, (MIN_STOCK_TO_POST,)) as cur:
        rows = await cur.fetchall()

    # ВАЖНО: передаём и name, и category
    rows_sorted = sorted(rows, key=lambda x: get_type_priority(x[1], x[2]))
    return [pid for pid, _, _ in rows_sorted[:limit]]

async def autopost_loop(bot: Bot):
    async with aiosqlite.connect(DB_NAME, timeout=30) as db:
        while True:
            try:
                ids = await get_products_for_autopost(db, AUTOPOST_BATCH_SIZE)
                if ids:
                    bot_logger.info(f"Autopost: posting {len(ids)} products")
                    for pid in ids:
                        await send_product(bot, db, pid)
                        await asyncio.sleep(DELAY_BETWEEN_PRODUCTS)
                else:
                    bot_logger.info("Autopost: nothing to post")
            except Exception as e:
                error_logger.exception(f"Autopost loop error: {e}")

            await asyncio.sleep(AUTOPOST_INTERVAL)


if __name__ == "__main__":

    bot = Bot(token=BOT_TOKEN)
    async def main():
    # ensures session closes even after crash / KeyboardInterrupt
        async with bot:
            while True:
                try:
                    bot_logger.info("Starting watcher loop...")
                    await asyncio.gather(
                        watch_products(bot),   # удаление stock=0 + ручные обновления по needs_update
                        autopost_loop(bot),    # автопостинг раз в 60 минут
                    )
                except KeyboardInterrupt:
                    bot_logger.info("Bot stopped manually.")
                    break
                except Exception as e:
                    error_logger.exception(f"Bot crashed: {e}. Restarting in 10s...")
                    await asyncio.sleep(10)
                else:
                    bot_logger.warning("Watcher exited unexpectedly. Restarting in 5s...")
                    await asyncio.sleep(5)

    asyncio.run(main())

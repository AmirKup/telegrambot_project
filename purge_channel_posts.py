import asyncio
import aiosqlite
from aiogram import Bot
from config import BOT_TOKEN, CHANNEL_ID, DB_NAME  # у тебя это уже есть в проекте

async def main():
    bot = Bot(token=BOT_TOKEN)

    async with aiosqlite.connect(DB_NAME, timeout=30) as db:
        async with db.execute("SELECT product_id, message_id FROM product_messages") as cur:
            rows = await cur.fetchall()

        print(f"Найдено сообщений для удаления: {len(rows)}")

        deleted = 0
        failed = 0

        for product_id, message_id in rows:
            try:
                await bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id)
                deleted += 1
            except Exception as e:
                failed += 1
                print(f"Не удалилось: product_id={product_id}, message_id={message_id}, err={e}")

            await asyncio.sleep(0.05)  # чтобы не упереться в лимиты

        # Сбрасываем “историю публикаций”, чтобы автопостинг пошёл заново
        await db.execute("DELETE FROM product_messages")
        await db.execute("UPDATE products SET needs_update = 0")  # чтобы ничего не перепостилось вручную
        await db.commit()

        print(f"Удалено: {deleted}, ошибок: {failed}")
        print("product_messages очищена, needs_update сброшен.")

    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
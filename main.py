from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Dict
import os
import dotenv
import logging
import asyncio
import aiosqlite
from aiogram import Bot, Dispatcher, BaseMiddleware, Router, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import BotCommand, BotCommandScopeDefault
from aiogram.types import TelegramObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_gigachat.chat_models import GigaChat

dotenv.load_dotenv()


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        state = data.get("state")
        current_state = None
        if state:
            current_state = await state.get_state()
        start_cmd = (
            (data["event_update"].message.text == "/start")
            or (current_state == NameForm.name.state)
            or (current_state == NameForm.last_name.state)
        )
        if not start_cmd:
            user_id = data["event_from_user"].id
            async with aiosqlite.connect("users.db") as db:
                async with db.execute(
                    "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
                ) as cursor:
                    if await cursor.fetchone() is None:
                        await bot.send_message(
                            chat_id=user_id,
                            text="Вы не зарегистрированы! Зарегистрируйтесь, используя команду /start.",
                        )
                        return
        result = await handler(event, data)
        return result


class NameForm(StatesGroup):
    name = State()
    last_name = State()


class DishForm(StatesGroup):
    name = State()
    proteins = State()
    fats = State()
    carbs = State()


class ReminderState(StatesGroup):
    waiting_for_meal = State()
    waiting_for_toggle = State()
    waiting_for_hour = State()
    waiting_for_minute = State()


class RecipeState(StatesGroup):
    message = State()


meals = {"breakfast": "завтрак", "lunch": "обед", "snack": "перекус", "dinner": "ужин"}

logging.basicConfig(
    force=True,
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

token = os.getenv('BOT_TOKEN')
GigaChatKey = os.getenv('API_KEY')

bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
start_router = Router()


llm = GigaChat(
    credentials=GigaChatKey,
    scope="GIGACHAT_API_PERS",
    model="GigaChat",
    verify_ssl_certs=False,
    streaming=False,
)


@start_router.message(Command("recipes"))
async def cmd_recipe(message: Message, state: FSMContext):
    await state.set_state(RecipeState.message)
    await message.answer("Чтобы получить рецепт, введите ингредиенты и ваши пожелания")


@start_router.message(RecipeState.message)
async def process_recipe(message: Message, state: FSMContext):
    user_input = message.text
    messages = [
        SystemMessage(
            content="Ты профессиональный повар. Твоя задача — предложить рецепт блюда, основываясь на следующих ингредиентах, и предоставить пошаговую инструкцию по его приготовлению."
        ),
        HumanMessage(content=user_input),
    ]
    res = llm.invoke(messages)
    await message.answer(res.content)
    await state.clear()


@start_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(NameForm.name)
    await message.answer("Введите Имя")


@start_router.message(NameForm.name)
async def process_name(message: Message, state: FSMContext):
    name = message.text
    if len(name) < 3:
        await message.answer("Имя слишком короткое")
        return
    await state.update_data(name=name)
    await state.set_state(NameForm.last_name)
    await message.answer("Введите Фамилию")


@start_router.message(NameForm.last_name)
async def process_last_name(message: Message, state: FSMContext):
    last_name = message.text
    if len(last_name) < 3:
        await message.answer("Фамилия слишком короткая")
        return

    data = await state.get_data()
    user_id = message.from_user.id
    name = data["name"]

    async with aiosqlite.connect("users.db") as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.commit()

    async with aiosqlite.connect("users.db") as db:
        await db.execute(
            "INSERT INTO users (user_id, name, last_name) VALUES (?, ?, ?)",
            (user_id, name, last_name),
        )
        await db.commit()
    await message.answer("Вы успешно добавлены в базу данных")
    await state.clear()


async def send_reminder(user_id: int, meal: str):
    await bot.send_message(user_id, f"Напоминание: пора на {meals[meal.lower()]}!")


async def load_reminders():
    async with aiosqlite.connect("reminders.db") as db:
        async with db.execute(
            "SELECT user_id, meal, hour, minute FROM reminders"
        ) as cursor:
            async for row in cursor:
                user_id, meal, hour, minute = row
                job_id = f"{user_id}_{meal}"
                scheduler.add_job(
                    send_reminder,
                    CronTrigger(hour=hour, minute=minute),
                    args=[user_id, meal],
                    id=job_id,
                    replace_existing=True,
                )


@start_router.message(Command("setreminder"))
async def set_reminder_command(message: types.Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Завтрак", callback_data="meal_breakfast")],
            [InlineKeyboardButton(text="Обед", callback_data="meal_lunch")],
            [InlineKeyboardButton(text="Перекус", callback_data="meal_snack")],
            [InlineKeyboardButton(text="Ужин", callback_data="meal_dinner")],
        ]
    )
    await message.answer(
        "Выберите прием пищи для настройки напоминания:", reply_markup=keyboard
    )
    await state.set_state(ReminderState.waiting_for_meal)


@start_router.callback_query(F.data.startswith("meal_"))
async def process_meal_choice(callback: types.CallbackQuery, state: FSMContext):
    meal = callback.data.split("_")[1]
    await state.update_data(meal=meal)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вкл", callback_data="toggle_on")],
            [InlineKeyboardButton(text="Выкл", callback_data="toggle_off")],
        ]
    )
    await callback.message.edit_text(
        f"Напоминания на {meals[meal.lower()]}:", reply_markup=keyboard
    )
    await state.set_state(ReminderState.waiting_for_toggle)


@start_router.callback_query(F.data.startswith("toggle_"))
async def process_toggle(callback: types.CallbackQuery, state: FSMContext):
    toggle = callback.data.split("_")[1]
    data = await state.get_data()
    meal = data["meal"]

    if toggle == "off":
        async with aiosqlite.connect("reminders.db") as db:
            await db.execute(
                "DELETE FROM reminders WHERE user_id = ? AND meal = ?",
                (callback.from_user.id, meal),
            )
            await db.commit()
        job_id = f"{callback.from_user.id}_{meal}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        await callback.message.edit_text(
            f"Напоминания на {meals[meal.lower()]} отключены."
        )
        await state.clear()
    else:
        await callback.message.edit_text("Введите часы для напоминания (0-23):")
        await state.set_state(ReminderState.waiting_for_hour)


@start_router.message(ReminderState.waiting_for_hour)
async def process_hour(message: types.Message, state: FSMContext):
    try:
        hour = int(message.text)
        if 0 <= hour <= 23:
            await state.update_data(hour=hour)
            await message.answer("Введите минуты для напоминания (0-59):")
            await state.set_state(ReminderState.waiting_for_minute)
        else:
            await message.answer(
                "Часы должны быть в диапазоне от 0 до 23. Попробуйте снова:"
            )
    except ValueError:
        await message.answer("Введите число для часов. Попробуйте снова:")


@start_router.message(ReminderState.waiting_for_minute)
async def process_minute(message: types.Message, state: FSMContext):
    try:
        minute = int(message.text)
        if 0 <= minute <= 59:
            data = await state.get_data()
            meal = data["meal"]
            hour = data["hour"]

            async with aiosqlite.connect("reminders.db") as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO reminders (user_id, meal, hour, minute)
                    VALUES (?, ?, ?, ?)
                    """,
                    (message.from_user.id, meal, hour, minute),
                )
                await db.commit()

            job_id = f"{message.from_user.id}_{meal}"
            scheduler.add_job(
                send_reminder,
                CronTrigger(hour=hour, minute=minute),
                args=[message.from_user.id, meal],
                id=job_id,
                replace_existing=True,
            )

            await message.answer(
                f"Напоминание на {meals[meal.lower()]} установлено на {hour:02d}:{minute:02d}.\n"
                "Выберите прием пищи для настройки следующего напоминания:",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Завтрак", callback_data="meal_breakfast"
                            )
                        ],
                        [InlineKeyboardButton(text="Обед", callback_data="meal_lunch")],
                        [
                            InlineKeyboardButton(
                                text="Перекус", callback_data="meal_snack"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="Ужин", callback_data="meal_dinner"
                            )
                        ],
                    ]
                ),
            )
            await state.set_state(ReminderState.waiting_for_meal)
        else:
            await message.answer(
                "Минуты должны быть в диапазоне от 0 до 59. Попробуйте снова:"
            )
    except ValueError:
        await message.answer("Введите число для минут. Попробуйте снова:")


@start_router.message(Command("viewlogs"), State(None))
async def cmd_com1(message: Message, state: FSMContext):
    kb_list = [
        [KeyboardButton(text="Сегодня")],
        [KeyboardButton(text="Вчера")],
        [KeyboardButton(text="7 дней")],
        [KeyboardButton(text="30 дней")],
    ]
    keyboard = ReplyKeyboardMarkup(
        keyboard=kb_list,
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Воспользуйтесь меню:",
    )
    await message.answer(
        "Выберите за сколько последних дней показать историю питания",
        reply_markup=keyboard,
    )


@start_router.message(F.text.in_({"Сегодня", "Вчера", "7 дней", "30 дней"}))
async def handle_period_selection(message: Message):
    selected_period = message.text
    user_id = message.from_user.id
    # Определяем диапазон дат
    if selected_period == "Сегодня":
        start_date = date.today().isoformat()
        end_date = (date.today() + timedelta(days=1)).isoformat()
        group_by_day = False
    elif selected_period == "Вчера":
        start_date = (date.today() - timedelta(days=1)).isoformat()
        end_date = date.today().isoformat()
        group_by_day = False
    elif selected_period == "7 дней":
        start_date = (date.today() - timedelta(days=7)).isoformat()
        end_date = (date.today() + timedelta(days=1)).isoformat()
        group_by_day = True
    elif selected_period == "30 дней":
        start_date = (date.today() - timedelta(days=30)).isoformat()
        end_date = (date.today() + timedelta(days=1)).isoformat()
        group_by_day = True
    else:
        await message.answer("Неверный выбор. Попробуйте снова.")
        return

    async with aiosqlite.connect("logs.db") as db:
        if group_by_day:
            # Группируем записи по дате
            query = """
            SELECT 
                timestamp AS date, 
                SUM(proteins) AS total_proteins, 
                SUM(fats) AS total_fats, 
                SUM(carbs) AS total_carbs, 
                SUM(calories) AS total_calories
            FROM logs
            WHERE timestamp >= ? AND timestamp < ? AND user_id = ?
            GROUP BY date
            ORDER BY date ASC
            """
        else:
            # Выводим записи для одного дня
            query = """
            SELECT name, proteins, fats, carbs, calories, timestamp
            FROM logs
            WHERE timestamp >= ? AND timestamp < ? AND user_id = ?
            """

        cursor = await db.execute(query, (start_date, end_date, user_id))
        rows = await cursor.fetchall()

    if rows:
        if group_by_day:
            response = ""
            for row in rows:
                date_str, total_proteins, total_fats, total_carbs, total_calories = row
                response += (
                    f"Дата: {date_str}\n"
                    f"  Белки: {total_proteins}, Жиры: {total_fats}, Углеводы: {total_carbs}, Калории: {total_calories}\n\n"
                )
        else:
            response = ""
            for row in rows:
                name, proteins, fats, carbs, calories, timestamp = row
                response += (
                    f"- {name}\n"
                    f"  Б: {proteins}, Ж: {fats}, У: {carbs}, Ккал: {calories}\n"
                    f"  Дата: {timestamp}\n\n"
                )
    else:
        response = "За указанный период данных нет."

    await message.answer(response)


# Command addmeal
@start_router.message(Command("addmeal"))
async def cmd_addmeal(message: Message, state: FSMContext):
    await state.set_state(DishForm.name)
    await message.answer("Введите название блюда")


@start_router.message(DishForm.name)
async def process_dishname(message: Message, state: FSMContext):
    name = message.text
    if len(name) < 3:
        await message.answer("Название блюда слишком короткое")
        return
    await state.update_data(name=name)
    await state.set_state(DishForm.proteins)
    await message.answer("Введите количество белков")


@start_router.message(DishForm.proteins)
async def process_proteins(message: Message, state: FSMContext):
    proteins = message.text
    if not proteins.isdigit() or int(proteins) < 0:
        await message.answer("Введите корректное количество граммов")
        return
    print(proteins)
    print(DishForm)
    await state.update_data(proteins=int(proteins))
    await state.set_state(DishForm.fats)
    await message.answer("Введите количество жиров")


@start_router.message(DishForm.fats)
async def process_fats(message: Message, state: FSMContext):
    fats = message.text
    if not fats.isdigit() or int(fats) < 0:
        await message.answer("Введите корректное количество граммов")
        return
    await state.update_data(fats=int(fats))
    await state.set_state(DishForm.carbs)
    await message.answer("Введите количество Углеводов")


@start_router.message(DishForm.carbs)
async def process_carbs(message: Message, state: FSMContext):
    carbss = message.text
    if not carbss.isdigit() or int(carbss) < 0:
        await message.answer("Введите корректное количество граммов")
        return
    carbs = int(carbss)
    data = await state.get_data()
    user_id = message.from_user.id
    name = data["name"]
    proteins = data["proteins"]
    fats = data["fats"]
    calories = 4 * proteins + 9 * fats + 4 * carbs
    timestamp = date.today()
    async with aiosqlite.connect("logs.db") as db:
        await db.execute(
            "INSERT INTO logs (user_id, name, calories, proteins, fats, carbs, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, calories, proteins, fats, carbs, timestamp),
        )
        await db.commit()
    await message.answer("Блюдо успешно добавлено в базу данных")
    await state.clear()


@start_router.message()
async def reply_msg(message: Message):
    await message.answer("Для взаимодействия со мной используйте команды")


async def start_bot():
    commands = [
        BotCommand(command="start", description="Старт"),
        BotCommand(command="addmeal", description="Добавить блюдо"),
        BotCommand(command="setreminder", description="Напоминания"),
        BotCommand(command="viewlogs", description="История питания"),
        BotCommand(command="recipes", description="Найти рецепт"),
    ]
    await bot.set_my_commands(commands, BotCommandScopeDefault())


async def start_db():
    async with aiosqlite.connect("users.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER NOT NULL PRIMARY KEY,
                name TEXT,
                last_name TEXT
            )
        """
        )
        await db.commit()

    async with aiosqlite.connect("logs.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                user_id INTEGER NOT NULL,
                name TEXT,
                calories INTEGER,
                proteins INTEGER,
                fats INTEGER,
                carbs INTEGER,
                timestamp TEXT
            )
        """
        )
        await db.commit()

    async with aiosqlite.connect("reminders.db") as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                user_id INTEGER NOT NULL,
                meal TEXT NOT NULL,
                hour INTEGER NOT NULL CHECK (hour >= 0 AND hour <= 23),
                minute INTEGER NOT NULL CHECK (minute >= 0 AND minute <= 59),
                PRIMARY KEY (user_id, meal)
            );
        """
        )
        await db.commit()


async def main():
    scheduler.start()
    start_router.message.outer_middleware(AuthMiddleware())
    dp.include_router(start_router)
    dp.startup.register(start_bot)
    dp.startup.register(start_db)
    try:
        print("Бот запущен...")
        await load_reminders()
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        print("Бот остановлен")


asyncio.run(main())

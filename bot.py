import asyncio
import glob
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List, Set
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaPhoto, Message,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from dotenv import load_dotenv

# ================= НАСТРОЙКИ =================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env файле!")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
NOTIFICATION_CHAT_ID = int(os.getenv("NOTIFICATION_CHAT_ID", ADMIN_ID))

WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "10"))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "20"))
BOOKING_DAYS_AHEAD = int(os.getenv("BOOKING_DAYS_AHEAD", "14"))
MAX_ACTIVE_BOOKINGS = int(os.getenv("MAX_ACTIVE_BOOKINGS", "3"))
PORTFOLIO_DIR = os.getenv("PORTFOLIO_DIR", "portfolio")

# Настройки ценообразования
PRICE_FACTOR_PER_RATING = float(os.getenv("PRICE_FACTOR_PER_RATING", "0.15"))
BASE_PRICE_MULTIPLIER = float(os.getenv("BASE_PRICE_MULTIPLIER", "0.7"))

ORGANIZATION_NAME = os.getenv("ORGANIZATION_NAME", "Салон красоты «Ногти-Люкс»")
ORGANIZATION_INN = os.getenv("ORGANIZATION_INN", "1234567890")
ORGANIZATION_OGRN = os.getenv("ORGANIZATION_OGRN", "1234567890123")
ORGANIZATION_ADDRESS = os.getenv("ORGANIZATION_ADDRESS", "г. Москва, ул. Примерная, д. 15")
ORGANIZATION_PHONE = os.getenv("ORGANIZATION_PHONE", "+7 (999) 123-45-67")
ORGANIZATION_EMAIL = os.getenv("ORGANIZATION_EMAIL", "nails@example.ru")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================= ДАННЫЕ =================
@dataclass
class Service:
    name: str
    description: str
    base_price: int
    duration: int

@dataclass
class Master:
    name: str
    description: str
    tg_id: int
    photo: str = ""

# Базовые цены
BASE_PRICES = [
    Service("💅 Маникюр", "Классический / аппаратный", 1500, 60),
    Service("🎨 Покрытие гель-лак", "Однотонное, дизайн по желанию", 1000, 45),
    Service("✨ Наращивание", "Гель, форма на выбор", 2500, 90),
    Service("🦶 Педикюр", "Аппаратный + покрытие", 2200, 80),
    Service("💎 Дизайн", "Френч, стразы, слайдеры", 200, 30),
    Service("🧴 Снятие + уход", "Снятие покрытия, масло, крем", 300, 20),
]

MASTERS = [
    Master("👩‍🎨 Анна", "Мастер маникюра, 5 лет опыта", 0),
    Master("👩‍🎨 Екатерина", "Мастер педикюра, 4 года опыта", 0),
    Master("👩‍🎨 Мария", "Специалист по дизайну, 3 года опыта", 0),
]

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ================= КНОПКИ ВНИЗУ =================
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Записаться"), KeyboardButton(text="💰 Прайс-лист")],
            [KeyboardButton(text="📸 Примеры работ"), KeyboardButton(text="⭐ Отзывы")],
            [KeyboardButton(text="👩‍🎨 О мастере"), KeyboardButton(text="💬 Оставить отзыв")],
            [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="❌ Отменить запись")],
            [KeyboardButton(text="📜 Согласие на обработку ПД"), KeyboardButton(text="🆘 Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )

def master_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Моя статистика"), KeyboardButton(text="⭐ Мои отзывы")],
            [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="📈 Мой рейтинг")],
            [KeyboardButton(text="⏰ Мой график"), KeyboardButton(text="⬅️ В главное меню")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Личный кабинет мастера..."
    )

def admin_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Все записи"), KeyboardButton(text="⭐ Отзывы на модерации")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 База клиентов")],
            [KeyboardButton(text="👩‍🎨 Управление мастерами"), KeyboardButton(text="⏰ График работы")],
            [KeyboardButton(text="⚙️ Настройки цен"), KeyboardButton(text="⬅️ В главное меню")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Админ-панель..."
    )

def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭️ Пропустить"), KeyboardButton(text="❌ Отменить действие")],
        ],
        resize_keyboard=True
    )

def skip_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭️ Пропустить")],
        ],
        resize_keyboard=True
    )

# ================= БАЗА ДАННЫХ =================
class Database:
    def __init__(self, db_name: str = "bot.db"):
        self.db_name = db_name
        self.init_db()
    
    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Ошибка БД: {e}")
            raise
        finally:
            conn.close()
    
    def init_db(self):
        with self.get_connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tg_username TEXT,
                day TEXT,
                time TEXT,
                first_name TEXT,
                last_name TEXT,
                patronymic TEXT,
                phone TEXT,
                email TEXT,
                master TEXT,
                services TEXT,
                comment TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TEXT,
                updated_at TEXT,
                consent_given INTEGER DEFAULT 0,
                consent_date TEXT,
                master_confirmed INTEGER DEFAULT 0,
                final_price INTEGER DEFAULT 0
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                master_name TEXT,
                rating INTEGER,
                text TEXT,
                is_approved INTEGER DEFAULT 0,
                created_at TEXT
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tg_username TEXT,
                first_name TEXT,
                last_name TEXT,
                patronymic TEXT,
                phone TEXT,
                email TEXT,
                created_at TEXT,
                last_activity TEXT,
                total_bookings INTEGER DEFAULT 0,
                consent_given INTEGER DEFAULT 0,
                consent_date TEXT
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS masters (
                name TEXT PRIMARY KEY,
                description TEXT,
                tg_id INTEGER DEFAULT 0,
                rating REAL DEFAULT 0,
                total_reviews INTEGER DEFAULT 0,
                total_bookings INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                work_start TEXT DEFAULT '10:00',
                work_end TEXT DEFAULT '20:00',
                is_on_vacation INTEGER DEFAULT 0,
                vacation_start TEXT,
                vacation_end TEXT,
                vacation_days INTEGER DEFAULT 0,
                custom_price_factor REAL DEFAULT 1.0
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS consent_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ip_address TEXT,
                consent_date TEXT,
                consent_version TEXT DEFAULT '1.0'
            )""")
            
            conn.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )""")
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_day ON bookings(day)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_master ON bookings(master)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_consent ON users(consent_given)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_master ON feedback(master_name)")
            
            for master in MASTERS:
                conn.execute(
                    """INSERT OR IGNORE INTO masters (name, description, tg_id) 
                       VALUES (?, ?, ?)""",
                    (master.name, master.description, master.tg_id)
                )
            
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('price_factor_per_rating', '0.15')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('base_price_multiplier', '0.7')"
            )
    
    def get_setting(self, key: str, default: str = "0") -> str:
        with self.get_connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row['value'] if row else default
    
    def set_setting(self, key: str, value: str):
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
    
    def get_master_price_factor(self, master_name: str) -> float:
        rating, reviews = self.get_master_rating(master_name)
        if reviews == 0:
            return float(self.get_setting('base_price_multiplier', '0.7'))
        factor_per_rating = float(self.get_setting('price_factor_per_rating', '0.15'))
        base_multiplier = float(self.get_setting('base_price_multiplier', '0.7'))
        factor = base_multiplier + (rating * factor_per_rating)
        return min(factor, 2.0)
    
    def get_master_price_for_service(self, master_name: str, service_name: str) -> int:
        base_price = 0
        for service in BASE_PRICES:
            if service.name == service_name:
                base_price = service.base_price
                break
        if base_price == 0:
            return 0
        factor = self.get_master_price_factor(master_name)
        price = int(base_price * factor)
        price = round(price / 50) * 50
        return price
    
    def get_service_price_display(self, service_name: str, master_name: str) -> str:
        price = self.get_master_price_for_service(master_name, service_name)
        rating, reviews = self.get_master_rating(master_name)
        if price == 0:
            return "по запросу"
        if reviews < 3:
            base_price = 0
            for service in BASE_PRICES:
                if service.name == service_name:
                    base_price = service.base_price
                    break
            if base_price > 0 and price < base_price:
                discount = int((1 - price / base_price) * 100)
                return f"{price} ₽ (-{discount}% для новичка!)"
        return f"{price} ₽"
    
    def get_master_price_info(self, master_name: str) -> str:
        rating, reviews = self.get_master_rating(master_name)
        factor = self.get_master_price_factor(master_name)
        if reviews == 0:
            return "🆕 Новичок! Специальные цены"
        elif factor < 0.9:
            return "💎 Низкие цены! Набирает рейтинг"
        elif factor < 1.2:
            return "⭐ Средние цены"
        elif factor < 1.5:
            return "🌟 Высокий рейтинг, цены выше среднего"
        else:
            return "👑 Топ-мастер, премиум-цены"
    
    def get_master_schedule(self, master_name: str) -> Dict:
        with self.get_connection() as conn:
            row = conn.execute(
                """SELECT work_start, work_end, is_on_vacation, 
                          vacation_start, vacation_end, vacation_days
                   FROM masters WHERE name = ?""",
                (master_name,)
            ).fetchone()
            if row:
                return dict(row)
            return {
                'work_start': '10:00',
                'work_end': '20:00',
                'is_on_vacation': 0,
                'vacation_start': None,
                'vacation_end': None,
                'vacation_days': 0
            }
    
    def update_master_schedule(self, master_name: str, work_start: str = None, work_end: str = None):
        with self.get_connection() as conn:
            if work_start and work_end:
                conn.execute(
                    "UPDATE masters SET work_start = ?, work_end = ? WHERE name = ?",
                    (work_start, work_end, master_name)
                )
    
    def set_master_vacation(self, master_name: str, days: int) -> tuple[bool, str]:
        if days <= 0:
            return False, "❌ Количество дней должно быть больше 0"
        today = date.today()
        start_date = today
        end_date = today + timedelta(days=days)
        with self.get_connection() as conn:
            bookings = conn.execute(
                """SELECT COUNT(*) FROM bookings 
                   WHERE master = ? AND day BETWEEN ? AND ? AND status = 'active'""",
                (master_name, start_date.isoformat(), end_date.isoformat())
            ).fetchone()[0]
            if bookings > 0:
                return False, f"❌ У мастера {bookings} записей на период отпуска."
            conn.execute(
                """UPDATE masters SET 
                    is_on_vacation = 1, 
                    vacation_start = ?, 
                    vacation_end = ?,
                    vacation_days = ?
                   WHERE name = ?""",
                (start_date.isoformat(), end_date.isoformat(), days, master_name)
            )
            return True, f"✅ Мастер отправлен в отпуск на {days} дней"
    
    def end_master_vacation(self, master_name: str) -> tuple[bool, str]:
        with self.get_connection() as conn:
            conn.execute(
                """UPDATE masters SET 
                    is_on_vacation = 0, 
                    vacation_start = NULL, 
                    vacation_end = NULL,
                    vacation_days = 0
                   WHERE name = ?""",
                (master_name,)
            )
            return True, "✅ Отпуск мастера завершен"
    
    def is_master_on_vacation(self, master_name: str) -> bool:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT is_on_vacation FROM masters WHERE name = ?",
                (master_name,)
            ).fetchone()
            return row and row['is_on_vacation'] == 1
    
    def get_vacation_info(self, master_name: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute(
                """SELECT vacation_start, vacation_end, vacation_days 
                   FROM masters WHERE name = ? AND is_on_vacation = 1""",
                (master_name,)
            ).fetchone()
            return dict(row) if row else None
    
    def get_master_by_tg_id(self, tg_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM masters WHERE tg_id = ? AND is_active = 1",
                (tg_id,)
            ).fetchone()
            return dict(row) if row else None
    
    def is_master(self, tg_id: int) -> bool:
        return self.get_master_by_tg_id(tg_id) is not None
    
    def get_master_by_name(self, name: str) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM masters WHERE name = ?",
                (name,)
            ).fetchone()
            return dict(row) if row else None
    
    def get_master_bookings(self, master_name: str) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, day, time, first_name, last_name, patronymic, 
                       phone, email, services, comment, created_at, final_price
                FROM bookings 
                WHERE master = ? AND status = 'active' AND day >= date('now')
                ORDER BY day, time
            """, (master_name,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_master_stats(self, master_name: str) -> Dict:
        with self.get_connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE master = ? AND status = 'active'",
                (master_name,)
            ).fetchone()[0]
            today = date.today().isoformat()
            today_count = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE master = ? AND day = ? AND status = 'active'",
                (master_name, today)
            ).fetchone()[0]
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            tomorrow_count = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE master = ? AND day = ? AND status = 'active'",
                (master_name, tomorrow)
            ).fetchone()[0]
            clients = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM bookings WHERE master = ? AND status = 'active'",
                (master_name,)
            ).fetchone()[0]
            rating, reviews = self.get_master_rating(master_name)
            return {
                'total': total,
                'today': today_count,
                'tomorrow': tomorrow_count,
                'clients': clients,
                'rating': rating,
                'reviews': reviews
            }
    
    def get_master_feedback(self, master_name: str, limit: int = 10) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT username, rating, text, created_at
                FROM feedback 
                WHERE master_name = ? AND is_approved = 1
                ORDER BY created_at DESC
                LIMIT ?
            """, (master_name, limit)).fetchall()
            return [dict(row) for row in rows]
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            return dict(row) if row else None
    
    def has_consent(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        if user:
            return user.get('consent_given', 0) == 1
        return False
    
    def update_user(self, user_id: int, tg_username: str, first_name: str, 
                    last_name: str, patronymic: str, phone: str, email: str,
                    consent_given: bool = False):
        with self.get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            existing = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if existing:
                conn.execute("""
                    UPDATE users SET 
                        tg_username = ?, first_name = ?, last_name = ?, 
                        patronymic = ?, phone = ?, email = ?, last_activity = ?,
                        consent_given = ?, consent_date = ?
                    WHERE user_id = ?
                """, (tg_username, first_name, last_name, patronymic, phone, email, 
                      now, 1 if consent_given else 0, now if consent_given else None, user_id))
            else:
                conn.execute("""
                    INSERT INTO users (user_id, tg_username, first_name, last_name, patronymic, 
                                       phone, email, created_at, last_activity, consent_given, consent_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, tg_username, first_name, last_name, patronymic, 
                      phone, email, now, now, 1 if consent_given else 0, now if consent_given else None))
    
    def log_consent(self, user_id: int, ip_address: str = "unknown"):
        with self.get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO consent_log (user_id, ip_address, consent_date, consent_version)
                VALUES (?, ?, ?, ?)
            """, (user_id, ip_address, now, "1.0"))
    
    def increment_user_bookings(self, user_id: int):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET total_bookings = total_bookings + 1 WHERE user_id = ?",
                (user_id,)
            )
    
    def increment_master_bookings(self, master_name: str):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE masters SET total_bookings = total_bookings + 1 WHERE name = ?",
                (master_name,)
            )
    
    def get_all_users(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT user_id, tg_username, first_name, last_name, patronymic, phone, email, 
                       total_bookings, consent_given, consent_date, created_at, last_activity
                FROM users 
                ORDER BY created_at DESC
            """).fetchall()
            return [dict(row) for row in rows]
    
    def get_master_rating(self, master_name: str) -> tuple[float, int]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT rating, total_reviews FROM masters WHERE name = ?",
                (master_name,)
            ).fetchone()
            if row:
                return row['rating'], row['total_reviews']
            return 0, 0
    
    def update_master_rating(self, master_name: str, rating: int):
        with self.get_connection() as conn:
            current_rating, total = self.get_master_rating(master_name)
            new_total = total + 1
            new_rating = ((current_rating * total) + rating) / new_total
            conn.execute("""
                UPDATE masters SET rating = ?, total_reviews = ? WHERE name = ?
            """, (new_rating, new_total, master_name))
    
    def get_all_masters_with_rating(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT name, description, tg_id, rating, total_reviews, total_bookings, 
                       is_active, work_start, work_end, is_on_vacation, vacation_start, vacation_end
                FROM masters 
                WHERE is_active = 1
                ORDER BY rating DESC, total_reviews DESC
            """).fetchall()
            return [dict(row) for row in rows]
    
    def update_master_tg_id(self, master_name: str, tg_id: int):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE masters SET tg_id = ? WHERE name = ?",
                (tg_id, master_name)
            )
    
    def get_active_booking_count(self, user_id: int) -> int:
        with self.get_connection() as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND status = 'active' AND day >= date('now')",
                (user_id,)
            ).fetchone()
            return result[0] if result else 0
    
    def can_make_booking(self, user_id: int, master_name: str = None) -> tuple[bool, str]:
        if not self.has_consent(user_id):
            return False, "⚠️ Для записи необходимо дать согласие на обработку персональных данных.\nНажмите «📜 Согласие на обработку ПД» в главном меню."
        active_count = self.get_active_booking_count(user_id)
        if active_count >= MAX_ACTIVE_BOOKINGS:
            return False, f"❌ У Вас уже есть {MAX_ACTIVE_BOOKINGS} активных записей.\nОтмените одну, чтобы создать новую."
        if master_name and self.is_master_on_vacation(master_name):
            vacation = self.get_vacation_info(master_name)
            if vacation:
                return False, f"❌ Мастер {master_name} в отпуске до {vacation['vacation_end']}"
        return True, "✅ Можно создать запись"
    
    def save_booking(self, user_id: int, tg_username: str, day: str, time: str,
                     first_name: str, last_name: str, patronymic: str,
                     phone: str, email: str, master: str,
                     services: List[str], comment: str = "") -> tuple[bool, str]:
        can_book, message = self.can_make_booking(user_id, master)
        if not can_book:
            return False, message
        with self.get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM bookings WHERE day = ? AND time = ? AND status = 'active' AND master = ?",
                (day, time, master)
            ).fetchone()
            if existing:
                return False, f"❌ У мастера {master} это время уже занято."
            services_str = ", ".join(services)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            total_price = 0
            for service in services:
                total_price += self.get_master_price_for_service(master, service)
            conn.execute("""INSERT INTO bookings 
                (user_id, tg_username, day, time, first_name, last_name, patronymic, 
                 phone, email, master, services, comment, status, created_at, updated_at,
                 consent_given, consent_date, final_price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, tg_username, day, time, first_name, last_name, patronymic,
                 phone, email, master, services_str, comment, 'active', now, now,
                 1, now, total_price))
            self.update_user(user_id, tg_username, first_name, last_name, patronymic, phone, email, True)
            self.increment_user_bookings(user_id)
            self.increment_master_bookings(master)
            return True, f"✅ Запись создана! Итоговая цена: {total_price} ₽"
    
    def cancel_booking(self, booking_id: int, user_id: int) -> tuple[bool, str]:
        with self.get_connection() as conn:
            booking = conn.execute(
                "SELECT * FROM bookings WHERE id = ? AND user_id = ? AND status = 'active'",
                (booking_id, user_id)
            ).fetchone()
            if not booking:
                return False, "❌ Запись не найдена"
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE bookings SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (now, booking_id)
            )
            return True, "✅ Запись отменена"
    
    def get_user_bookings(self, user_id: int) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, day, time, first_name, last_name, patronymic, 
                       phone, email, master, services, comment, created_at, final_price
                FROM bookings 
                WHERE user_id = ? AND status = 'active' AND day >= date('now')
                ORDER BY day, time
            """, (user_id,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_all_bookings(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, user_id, tg_username, day, time, first_name, last_name, patronymic,
                       phone, email, master, services, comment, created_at, consent_given, final_price
                FROM bookings 
                WHERE status = 'active' AND day >= date('now')
                ORDER BY day, time
            """).fetchall()
            return [dict(row) for row in rows]
    
    def get_booked_slots(self, day: str, master: str) -> set:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT time FROM bookings WHERE day = ? AND master = ? AND status = 'active'",
                (day, master)
            ).fetchall()
            return {row[0] for row in rows}
    
    def save_feedback(self, user_id: int, username: str, master_name: str, rating: int, text: str):
        with self.get_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""INSERT INTO feedback 
                (user_id, username, master_name, rating, text, is_approved, created_at)
                VALUES (?,?,?,?,?,0,?)""",
                (user_id, username, master_name, rating, text, now))
            self.update_master_rating(master_name, rating)
    
    def get_approved_feedback(self, master_name: str = None, limit: int = 10) -> List[Dict]:
        with self.get_connection() as conn:
            if master_name:
                rows = conn.execute("""
                    SELECT username, master_name, rating, text, created_at
                    FROM feedback 
                    WHERE is_approved = 1 AND master_name = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (master_name, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT username, master_name, rating, text, created_at
                    FROM feedback 
                    WHERE is_approved = 1
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,)).fetchall()
            return [dict(row) for row in rows]
    
    def get_unapproved_feedback(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("""
                SELECT id, user_id, username, master_name, rating, text, created_at
                FROM feedback 
                WHERE is_approved = 0
                ORDER BY created_at DESC
            """).fetchall()
            return [dict(row) for row in rows]
    
    def approve_feedback(self, feedback_id: int):
        with self.get_connection() as conn:
            conn.execute("UPDATE feedback SET is_approved = 1 WHERE id = ?", (feedback_id,))

db = Database()
# ================= INLINE КЛАВИАТУРЫ =================
def inline_menu_kb():
    buttons = [
        [InlineKeyboardButton(text="📅 Записаться", callback_data="book")],
        [InlineKeyboardButton(text="💰 Прайс-лист", callback_data="price")],
        [InlineKeyboardButton(text="📸 Примеры работ", callback_data="portfolio")],
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews")],
        [InlineKeyboardButton(text="👩‍🎨 О мастере", callback_data="about")],
        [InlineKeyboardButton(text="💬 Оставить отзыв", callback_data="feedback")],
        [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_bookings")],
        [InlineKeyboardButton(text="📜 Согласие на ПД", callback_data="consent")],
    ]
    if ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def consent_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Даю согласие", callback_data="consent_agree")],
        [InlineKeyboardButton(text="❌ Не даю согласие", callback_data="consent_decline")],
    ])

def back_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]])

def masters_inline_kb():
    buttons = []
    for i, master in enumerate(db.get_all_masters_with_rating()):
        rating = master['rating']
        reviews = master['total_reviews']
        if reviews == 0:
            stars = "🆕 Новичок"
        else:
            stars = "⭐" * int(rating) + "☆" * (5 - int(rating))
        price_info = db.get_master_price_info(master['name'])
        vacation_status = " 🏖️" if master['is_on_vacation'] else ""
        text = f"{master['name']} {stars}{vacation_status}\n   {price_info}"
        buttons.append([InlineKeyboardButton(text=text[:60], callback_data=f"master_{i}")])
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def days_inline_kb():
    buttons = []
    row = []
    today = date.today()
    for i in range(1, BOOKING_DAYS_AHEAD + 1):
        d = today + timedelta(days=i)
        if d == today and datetime.now().hour >= WORK_END_HOUR:
            continue
        label = f"{WEEKDAYS[d.weekday()]} {d.day}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"day_{d.isoformat()}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к мастерам", callback_data="back_to_masters")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def times_inline_kb(day: str, master: str):
    buttons = []
    row = []
    taken = db.get_booked_slots(day, master)
    schedule = db.get_master_schedule(master)
    start_hour = int(schedule['work_start'].split(':')[0])
    end_hour = int(schedule['work_end'].split(':')[0])
    for hour in range(start_hour, end_hour):
        t = f"{hour:02d}:00"
        if t not in taken:
            if day == date.today().isoformat() and hour <= datetime.now().hour:
                continue
            row.append(InlineKeyboardButton(text=t, callback_data=f"time_{day}_{t}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="book")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def services_inline_kb(selected: List[str] = None, master: str = None):
    if selected is None:
        selected = []
    buttons = []
    row = []
    for i, service in enumerate(BASE_PRICES):
        check = "✅ " if service.name in selected else ""
        if master:
            price = db.get_master_price_for_service(master, service.name)
            price_text = f" {price}₽"
        else:
            price_text = ""
        row.append(InlineKeyboardButton(
            text=f"{check}{service.name[:12]}{price_text}",
            callback_data=f"svc_{i}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(text="✅ Готово", callback_data="services_done"),
        InlineKeyboardButton(text="❌ Очистить все", callback_data="services_clear")
    ])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="book")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_feedback_inline_kb(feedback_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"admin_approve_fb_{feedback_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_fb_{feedback_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_feedback")]
    ])

def master_schedule_inline_kb(master_name: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ Изменить рабочее время", callback_data=f"schedule_time_{master_name}")],
        [InlineKeyboardButton(text="🏖️ Отправить в отпуск", callback_data=f"schedule_vacation_{master_name}")],
        [InlineKeyboardButton(text="✅ Завершить отпуск", callback_data=f"schedule_end_vacation_{master_name}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_schedule")]
    ])

def admin_price_settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Показать текущие настройки", callback_data="admin_show_prices")],
        [InlineKeyboardButton(text="📈 Изменить фактор цены", callback_data="admin_change_factor")],
        [InlineKeyboardButton(text="📉 Изменить минимум для новичков", callback_data="admin_change_min")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])

# ================= ТЕКСТ СОГЛАСИЯ =================
CONSENT_TEXT = f"""
📜 **СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ**

Уважаемый клиент!

Нажимая кнопку «✅ Даю согласие», Вы даете свое согласие {ORGANIZATION_NAME} (ИНН: {ORGANIZATION_INN}, ОГРН: {ORGANIZATION_OGRN}) на обработку своих персональных данных в соответствии с Федеральным законом от 27.07.2006 № 152-ФЗ «О персональных данных».

**Какие данные обрабатываются:**
• Фамилия, имя, отчество
• Номер телефона
• Адрес электронной почты
• ID в Telegram
• Username в Telegram
• История записей и посещений

**Цели обработки:**
• Запись на процедуры в салоне
• Напоминание о записи
• Информирование о специальных предложениях
• Ведение статистики и улучшение качества обслуживания
• Обратная связь и работа с отзывами

**Ваши права:**
• Отозвать согласие в любой момент
• Требовать удаления своих данных
• Получить информацию о своих данных

**Срок хранения:** 5 лет с момента последнего обращения

**Контакты:**
📞 {ORGANIZATION_PHONE}
📧 {ORGANIZATION_EMAIL}
📍 {ORGANIZATION_ADDRESS}

Нажимая «✅ Даю согласие», Вы подтверждаете, что ознакомлены с условиями и даете добровольное согласие на обработку Ваших персональных данных.

Если Вы не даете согласие, Вы не сможете пользоваться функциями записи и оставления отзывов.
"""

# ================= FSM =================
class BookingStates(StatesGroup):
    master = State()
    day = State()
    time = State()
    first_name = State()
    last_name = State()
    patronymic = State()
    phone = State()
    email = State()
    services = State()
    comment = State()
    confirm = State()

class FeedbackStates(StatesGroup):
    master = State()
    rating = State()
    text = State()

class ConsentStates(StatesGroup):
    waiting = State()

class AdminMasterStates(StatesGroup):
    select_master = State()
    set_tg_id = State()
    set_work_time = State()
    set_vacation_days = State()
    set_price_factor = State()
    set_price_min = State()

# ================= ДЕКОРАТОРЫ =================
def admin_only(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        for arg in args:
            if isinstance(arg, Message) and arg.from_user.id != ADMIN_ID:
                await arg.answer("⛔ У Вас нет прав администратора")
                return
            if isinstance(arg, CallbackQuery) and arg.from_user.id != ADMIN_ID:
                await arg.answer("⛔ У Вас нет прав администратора", show_alert=True)
                return
        return await func(*args, **kwargs)
    return wrapper

def master_only(func):
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if not db.is_master(message.from_user.id):
            await message.answer(
                "⛔ Эта функция доступна только для мастеров.",
                reply_markup=main_menu_kb()
            )
            return
        return await func(message, *args, **kwargs)
    return wrapper

# ================= ОСНОВНЫЕ КОМАНДЫ =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    has_consent = db.has_consent(message.from_user.id)
    is_master = db.is_master(message.from_user.id)
    bookings = db.get_user_bookings(message.from_user.id)
    booking_status = ""
    if bookings:
        booking_status = f"\n📋 У Вас есть активные записи:"
        for b in bookings:
            d = date.fromisoformat(b['day'])
            date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
            booking_status += f"\n   📅 {date_label} в {b['time']} — {b['master']}"
    consent_status = "✅ Согласие на ПД получено" if has_consent else "❌ Согласие на ПД НЕ получено"
    master_status = "👩‍🎨 Вы зарегистрированы как мастер" if is_master else ""
    await message.answer(
        f"👋 Здравствуйте, {message.from_user.first_name}!\n"
        "💅 Добро пожаловать в салон красоты!\n\n"
        f"📜 {consent_status}\n"
        f"{master_status}\n"
        f"{booking_status}\n\n"
        "Для записи необходимо дать согласие на обработку персональных данных.\n"
        "Нажмите «📜 Согласие на обработку ПД» в меню.",
        reply_markup=main_menu_kb()
    )

@dp.message(F.text == "📜 Согласие на обработку ПД")
async def btn_consent(message: Message, state: FSMContext):
    has_consent = db.has_consent(message.from_user.id)
    if has_consent:
        await message.answer(
            "✅ Вы уже дали согласие на обработку персональных данных.\n\n"
            "Вы можете отозвать согласие в любой момент, написав администратору.",
            reply_markup=main_menu_kb()
        )
        return
    await state.set_state(ConsentStates.waiting)
    await message.answer(
        CONSENT_TEXT,
        reply_markup=consent_inline_kb(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "consent_agree")
async def cb_consent_agree(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    username = call.from_user.username or ""
    db.update_user(user_id, username, "", "", "", "", "", True)
    db.log_consent(user_id, "telegram")
    await state.clear()
    await call.message.edit_text(
        "✅ **СОГЛАСИЕ ПРИНЯТО!**\n\n"
        "Благодарим Вас! Теперь Вы можете:\n"
        "• 📅 Записываться на процедуры\n"
        "• 💬 Оставлять отзывы\n"
        "• 📋 Управлять своими записями\n\n"
        "Ваши данные в безопасности и используются только для записи.",
        reply_markup=back_inline_kb(),
        parse_mode="Markdown"
    )
    if NOTIFICATION_CHAT_ID:
        try:
            await bot.send_message(
                NOTIFICATION_CHAT_ID,
                f"📜 Новое согласие на ПД!\n"
                f"👤 @{username or 'без username'} (ID: {user_id})"
            )
        except:
            pass
    await call.answer()

@dp.callback_query(F.data == "consent_decline")
async def cb_consent_decline(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "❌ **ВЫ НЕ ДАЛИ СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ**\n\n"
        "К сожалению, без согласия мы не можем записать Вас или сохранять Ваши данные.\n\n"
        "Если передумаете, нажмите «📜 Согласие на обработку ПД» в главном меню.",
        reply_markup=back_inline_kb(),
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    user = db.get_user(message.from_user.id)
    has_consent = db.has_consent(message.from_user.id)
    is_master = db.is_master(message.from_user.id)
    text = f"🆔 Ваш ID: {message.from_user.id}\n"
    text += f"👤 Ваш username: @{message.from_user.username or 'не указан'}\n"
    text += f"📜 Согласие на ПД: {'✅ Да' if has_consent else '❌ Нет'}\n"
    text += f"👩‍🎨 Статус мастера: {'✅ Да' if is_master else '❌ Нет'}\n"
    if user:
        text += f"\n📋 Ваши данные в базе:\n"
        text += f"   👤 {user.get('first_name', '')} {user.get('last_name', '')} {user.get('patronymic', '')}\n"
        text += f"   📞 {user.get('phone', 'не указан')}\n"
        text += f"   📧 {user.get('email', 'не указан')}\n"
        text += f"   📊 Всего записей: {user.get('total_bookings', 0)}"
    await message.answer(text, reply_markup=main_menu_kb())

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено ✅", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except:
        pass
    await call.message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())
    await call.answer()

# ================= ОБРАБОТЧИКИ КНОПОК =================
@dp.message(F.text == "📅 Записаться")
async def btn_book(message: Message, state: FSMContext):
    await state.clear()
    can_book, msg = db.can_make_booking(message.from_user.id)
    if not can_book:
        await message.answer(msg, reply_markup=main_menu_kb())
        return
    user = db.get_user(message.from_user.id)
    if user and user.get('first_name') and user.get('consent_given', 0) == 1:
        full_name = f"{user.get('first_name', '')} {user.get('last_name', '')} {user.get('patronymic', '')}".strip()
        if full_name:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, это я", callback_data="use_existing_user")],
                [InlineKeyboardButton(text="❌ Нет, я другой человек", callback_data="new_user")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
            ])
            await message.answer(
                f"👋 Мы Вас узнали!\n\n"
                f"Ваши данные в базе:\n"
                f"👤 {full_name}\n"
                f"📞 {user.get('phone', 'не указан')}\n"
                f"📧 {user.get('email', 'не указан')}\n\n"
                f"Использовать эти данные для записи?",
                reply_markup=kb
            )
            return
    await state.set_state(BookingStates.master)
    await message.answer(
        "👩‍🎨 Выберите мастера:\n\n"
        "🆕 — новичок (специальные цены)\n"
        "⭐ — рейтинг мастера\n"
        "🏖️ — в отпуске (недоступен)",
        reply_markup=masters_inline_kb()
    )

@dp.callback_query(F.data == "use_existing_user")
async def cb_use_existing_user(call: CallbackQuery, state: FSMContext):
    user = db.get_user(call.from_user.id)
    if user:
        await state.update_data(
            first_name=user.get('first_name', ''),
            last_name=user.get('last_name', ''),
            patronymic=user.get('patronymic', ''),
            phone=user.get('phone', ''),
            email=user.get('email', '')
        )
        await state.set_state(BookingStates.master)
        await call.message.edit_text(
            "👩‍🎨 Выберите мастера:",
            reply_markup=masters_inline_kb()
        )
    await call.answer()

@dp.callback_query(F.data == "new_user")
async def cb_new_user(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.first_name)
    await call.message.edit_text(
        "👤 Пожалуйста, укажите Вашу фамилию, имя, отчество:\n"
        "Например: Иванова Анна Сергеевна"
    )
    await call.answer()

@dp.message(F.text == "💰 Прайс-лист")
async def btn_price(message: Message):
    text = "💰 ПРАЙС-ЛИСТ\n\n**Базовые цены:**\n"
    for service in BASE_PRICES:
        text += f"{service.name}\n"
        text += f"   {service.description}\n"
        text += f"   💵 от {service.base_price} ₽\n"
        text += f"   ⏱ {service.duration} мин.\n\n"
    text += "\n💡 **Цены зависят от рейтинга мастера:**\n"
    text += "   • 🆕 Новичок: скидка до 30%\n"
    text += "   • ⭐ Средний рейтинг: стандартные цены\n"
    text += "   • 👑 Топ-мастер: премиум-цены\n"
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

@dp.message(F.text == "📸 Примеры работ")
async def btn_portfolio(message: Message):
    paths = sorted(glob.glob(os.path.join(PORTFOLIO_DIR, "*.jpg")) +
                   glob.glob(os.path.join(PORTFOLIO_DIR, "*.png")))
    if not paths:
        await message.answer("📸 Фото работ пока нет.", reply_markup=main_menu_kb())
        return
    try:
        media = [InputMediaPhoto(media=FSInputFile(p)) for p in paths[:10]]
        media[0].caption = "📸 Примеры работ"
        await message.answer_media_group(media)
        await message.answer("📸 Выберите действие:", reply_markup=main_menu_kb())
    except Exception as e:
        logger.error(f"Ошибка портфолио: {e}")
        await message.answer("❌ Ошибка загрузки фото", reply_markup=main_menu_kb())

@dp.message(F.text == "⭐ Отзывы")
async def btn_reviews(message: Message):
    reviews = db.get_approved_feedback()
    if not reviews:
        text = "⭐ Отзывов пока нет. Будьте первыми!"
    else:
        text = "⭐ ОТЗЫВЫ КЛИЕНТОВ\n\n"
        for r in reviews:
            stars = "⭐" * int(r['rating']) + "☆" * (5 - int(r['rating']))
            text += f"{r['username'] or 'Аноним'} — {r['master_name']}\n"
            text += f"   {stars}\n"
            text += f"   📝 {r['text']}\n"
            text += f"   📅 {r['created_at'][:10]}\n\n"
    await message.answer(text, reply_markup=main_menu_kb())

@dp.message(F.text == "👩‍🎨 О мастере")
async def btn_about(message: Message):
    masters = db.get_all_masters_with_rating()
    about = "👩‍🎨 НАШИ МАСТЕРА\n\n"
    for m in masters:
        rating, reviews = db.get_master_rating(m['name'])
        factor = db.get_master_price_factor(m['name'])
        stars = "⭐" * int(rating) + "☆" * (5 - int(rating)) if reviews > 0 else "🆕 Новичок"
        about += f"{m['name']}\n"
        about += f"   {m['description']}\n"
        about += f"   Рейтинг: {stars} ({reviews} отзывов)\n"
        about += f"   💰 Коэф. цен: {factor:.2f}x\n"
        about += f"   📊 {m['total_bookings']} записей\n\n"
    about += "\n📍 Адрес: ул. Примерная, 15\n"
    about += "⏰ Режим работы: 10:00–20:00"
    await message.answer(about, reply_markup=main_menu_kb())

@dp.message(F.text == "💬 Оставить отзыв")
async def btn_feedback(message: Message, state: FSMContext):
    if not db.has_consent(message.from_user.id):
        await message.answer(
            "⚠️ Для оставления отзыва необходимо дать согласие на обработку персональных данных.",
            reply_markup=main_menu_kb()
        )
        return
    await state.set_state(FeedbackStates.master)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=master['name'], callback_data=f"fb_master_{i}")]
        for i, master in enumerate(db.get_all_masters_with_rating())
    ] + [[InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu")]])
    await message.answer("👩‍🎨 На кого из мастеров хотите оставить отзыв?", reply_markup=kb)

@dp.message(F.text == "📋 Мои записи")
async def btn_my_bookings(message: Message):
    if not db.has_consent(message.from_user.id):
        await message.answer(
            "⚠️ Для просмотра записей необходимо дать согласие на обработку персональных данных.",
            reply_markup=main_menu_kb()
        )
        return
    bookings = db.get_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("📋 У Вас нет активных записей", reply_markup=main_menu_kb())
        return
    text = "📋 ВАШИ ЗАПИСИ\n\n"
    for b in bookings:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']}\n"
        text += f"📅 {date_label} в {b['time']}\n"
        text += f"👤 {b['first_name']} {b.get('last_name', '')}\n"
        text += f"👩‍🎨 {b['master']}\n"
        if b['phone']:
            text += f"📞 {b['phone']}\n"
        if b['email']:
            text += f"📧 {b['email']}\n"
        if b['services']:
            text += f"💅 {b['services']}\n"
        if b['final_price']:
            text += f"💰 {b['final_price']} ₽\n"
        text += "\n"
    await message.answer(text, reply_markup=main_menu_kb())

@dp.message(F.text == "❌ Отменить запись")
async def btn_cancel_booking(message: Message, state: FSMContext):
    if not db.has_consent(message.from_user.id):
        await message.answer(
            "⚠️ Для отмены записи необходимо дать согласие на обработку персональных данных.",
            reply_markup=main_menu_kb()
        )
        return
    bookings = db.get_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("📋 У Вас нет активных записей для отмены", reply_markup=main_menu_kb())
        return
    text = "❌ ВЫБЕРИТЕ ЗАПИСЬ ДЛЯ ОТМЕНЫ:\n\n"
    kb_buttons = []
    for b in bookings:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']} — {date_label} в {b['time']} ({b['master']})"
        if b['final_price']:
            text += f" — {b['final_price']} ₽"
        text += "\n"
        kb_buttons.append([InlineKeyboardButton(
            text=f"❌ Отменить #{b['id']}",
            callback_data=f"cancel_booking_{b['id']}"
        )])
    kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@dp.message(F.text == "🆘 Помощь")
async def btn_help(message: Message):
    help_text = (
        "🆘 ПОМОЩЬ\n\n"
        "📜 Согласие на обработку ПД — ознакомиться и дать согласие\n"
        "📅 Записаться — выбрать мастера, дату, время и услуги\n"
        "💰 Прайс-лист — посмотреть цены на услуги\n"
        "📸 Примеры работ — фото работ мастеров\n"
        "⭐ Отзывы — отзывы клиентов с рейтингом\n"
        "👩‍🎨 О мастере — информация о мастерах\n"
        "💬 Оставить отзыв — оставить отзыв о мастере\n"
        "📋 Мои записи — посмотреть свои записи\n"
        "❌ Отменить запись — отменить запись\n\n"
        "💡 **О ценах:**\n"
        "   • 🆕 Новички — специальные цены (скидка до 30%)\n"
        "   • ⭐ Мастера с рейтингом — цены выше\n"
        "   • 👑 Топ-мастера — премиум-цены\n\n"
        "👩‍🎨 Если Вы мастер, нажмите «👩‍🎨 Личный кабинет мастера»\n\n"
        "⚠️ Без согласия на обработку ПД запись невозможна!"
    )
    await message.answer(help_text, reply_markup=main_menu_kb())
# ================= ЗАПИСЬ (INLINE) =================
@dp.callback_query(F.data == "book")
async def cb_book(call: CallbackQuery, state: FSMContext):
    await state.clear()
    can_book, msg = db.can_make_booking(call.from_user.id)
    if not can_book:
        await call.message.edit_text(msg)
        await call.answer()
        return
    await call.message.edit_text(
        "👩‍🎨 Выберите мастера:\n\n"
        "🆕 — новичок (специальные цены)\n"
        "⭐ — рейтинг мастера\n"
        "🏖️ — в отпуске (недоступен)",
        reply_markup=masters_inline_kb()
    )
    await call.answer()

@dp.callback_query(F.data == "back_to_masters")
async def cb_back_to_masters(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.master)
    await call.message.edit_text(
        "👩‍🎨 Выберите мастера:",
        reply_markup=masters_inline_kb()
    )
    await call.answer()

@dp.callback_query(F.data.startswith("master_"))
async def cb_master(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split("_")[1])
    masters = db.get_all_masters_with_rating()
    master = masters[idx]
    await state.update_data(master=master['name'])
    rating, reviews = db.get_master_rating(master['name'])
    factor = db.get_master_price_factor(master['name'])
    price_info = db.get_master_price_info(master['name'])
    text = f"👩‍🎨 Выбран мастер: {master['name']}\n"
    if reviews == 0:
        text += f"🆕 Новичок! Специальные цены (коэф. {factor:.2f}x)\n"
    else:
        text += f"⭐ Рейтинг: {rating:.1f} ({reviews} отзывов)\n"
        text += f"💰 {price_info} (коэф. {factor:.2f}x)\n"
    text += "\n📅 Выберите день:"
    await call.message.edit_text(text, reply_markup=days_inline_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("day_"))
async def cb_day(call: CallbackQuery, state: FSMContext):
    day = call.data.split("_")[1]
    await state.update_data(day=day)
    data = await state.get_data()
    master = data.get('master')
    d = date.fromisoformat(day)
    label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
    kb = times_inline_kb(day, master)
    if len(kb.inline_keyboard) <= 1:
        await call.message.edit_text(f"😔 У мастера {master} на {label} всё занято", reply_markup=days_inline_kb())
    else:
        await call.message.edit_text(f"🕐 {label} — свободное время у {master}:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("time_"))
async def cb_time(call: CallbackQuery, state: FSMContext):
    _, day, t = call.data.split("_", 2)
    await state.update_data(day=day, time=t)
    user = db.get_user(call.from_user.id)
    if user and user.get('first_name') and user.get('consent_given', 0) == 1:
        await state.update_data(
            first_name=user.get('first_name', ''),
            last_name=user.get('last_name', ''),
            patronymic=user.get('patronymic', ''),
            phone=user.get('phone', ''),
            email=user.get('email', '')
        )
        await state.set_state(BookingStates.services)
        await show_services(call.message, state)
    else:
        await state.set_state(BookingStates.first_name)
        await call.message.edit_text(
            f"✅ Вы выбрали: {day} в {t}\n\n"
            "👤 Пожалуйста, укажите Вашу фамилию, имя, отчество:\n"
            "Например: Иванова Анна Сергеевна"
        )
    await call.answer()

# ================= ЗАПОЛНЕНИЕ ДАННЫХ (FSM) =================
@dp.message(BookingStates.first_name)
async def bk_first_name(message: Message, state: FSMContext):
    full_name = message.text.strip()
    parts = full_name.split()
    if len(parts) == 3:
        await state.update_data(last_name=parts[0], first_name=parts[1], patronymic=parts[2])
        await state.set_state(BookingStates.phone)
        await message.answer("📞 Пожалуйста, укажите Ваш номер телефона:", reply_markup=cancel_kb())
    elif len(parts) == 2:
        await state.update_data(last_name=parts[0], first_name=parts[1], patronymic="")
        await state.set_state(BookingStates.patronymic)
        await message.answer("👤 Пожалуйста, укажите Ваше отчество (или нажмите 'Пропустить'):", reply_markup=skip_kb())
    elif len(parts) == 1:
        await state.update_data(first_name=parts[0], last_name="", patronymic="")
        await state.set_state(BookingStates.last_name)
        await message.answer("👤 Пожалуйста, укажите Вашу фамилию (или нажмите 'Пропустить'):", reply_markup=skip_kb())
    else:
        await message.answer("⚠️ Пожалуйста, введите корректные данные.\nНапример: Иванова Анна Сергеевна")

@dp.message(BookingStates.last_name, Command("skip"))
async def bk_last_name_skip(message: Message, state: FSMContext):
    await state.update_data(last_name="")
    await state.set_state(BookingStates.patronymic)
    await message.answer("👤 Пожалуйста, укажите Ваше отчество (или нажмите 'Пропустить'):", reply_markup=skip_kb())

@dp.message(BookingStates.last_name)
async def bk_last_name(message: Message, state: FSMContext):
    await state.update_data(last_name=message.text.strip())
    await state.set_state(BookingStates.patronymic)
    await message.answer("👤 Пожалуйста, укажите Ваше отчество (или нажмите 'Пропустить'):", reply_markup=skip_kb())

@dp.message(BookingStates.patronymic, Command("skip"))
async def bk_patronymic_skip(message: Message, state: FSMContext):
    await state.update_data(patronymic="")
    await state.set_state(BookingStates.phone)
    await message.answer("📞 Пожалуйста, укажите Ваш номер телефона:", reply_markup=cancel_kb())

@dp.message(BookingStates.patronymic)
async def bk_patronymic(message: Message, state: FSMContext):
    await state.update_data(patronymic=message.text.strip())
    await state.set_state(BookingStates.phone)
    await message.answer("📞 Пожалуйста, укажите Ваш номер телефона:", reply_markup=cancel_kb())

@dp.message(BookingStates.phone)
async def bk_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    await state.set_state(BookingStates.email)
    await message.answer("📧 Пожалуйста, укажите Ваш email (или нажмите 'Пропустить'):", reply_markup=skip_kb())

@dp.message(BookingStates.email, Command("skip"))
async def bk_email_skip(message: Message, state: FSMContext):
    await state.update_data(email="")
    await state.set_state(BookingStates.services)
    await show_services(message, state)

@dp.message(BookingStates.email)
async def bk_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if "@" not in email or "." not in email:
        await message.answer("⚠️ Похоже, это не email. Введите корректный email или нажмите 'Пропустить'")
        return
    await state.update_data(email=email)
    await state.set_state(BookingStates.services)
    await show_services(message, state)

async def show_services(message: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_services", [])
    master = data.get("master", "")
    text = "💅 ВЫБЕРИТЕ УСЛУГИ\n\n"
    if selected:
        text += "✅ Выбрано:\n"
        for s in selected:
            price = db.get_master_price_for_service(master, s)
            text += f"   • {s} — {price} ₽\n"
        text += "\n"
    text += "Нажмите на услугу, чтобы выбрать/отменить.\n"
    text += "Можно выбрать несколько услуг."
    await message.answer(text, reply_markup=services_inline_kb(selected, master))

@dp.callback_query(F.data.startswith("svc_"))
async def cb_service_toggle(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_services", [])
    idx = int(call.data.split("_")[1])
    service_name = BASE_PRICES[idx].name
    if service_name in selected:
        selected.remove(service_name)
    else:
        selected.append(service_name)
    await state.update_data(selected_services=selected)
    master = data.get("master", "")
    text = "💅 ВЫБЕРИТЕ УСЛУГИ\n\n"
    if selected:
        text += "✅ Выбрано:\n"
        for s in selected:
            price = db.get_master_price_for_service(master, s)
            text += f"   • {s} — {price} ₽\n"
        text += "\n"
    text += "Нажмите на услугу, чтобы выбрать/отменить.\n"
    text += "Можно выбрать несколько услуг."
    await call.message.edit_text(text, reply_markup=services_inline_kb(selected, master))
    await call.answer()

@dp.callback_query(F.data == "services_clear")
async def cb_services_clear(call: CallbackQuery, state: FSMContext):
    await state.update_data(selected_services=[])
    master = (await state.get_data()).get("master", "")
    await call.message.edit_text(
        "💅 ВЫБЕРИТЕ УСЛУГИ\n\n"
        "✅ Выбрано: ничего не выбрано\n\n"
        "Нажмите на услугу, чтобы выбрать.\n"
        "Можно выбрать несколько услуг.",
        reply_markup=services_inline_kb([], master)
    )
    await call.answer()

@dp.callback_query(F.data == "services_done")
async def cb_services_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_services", [])
    if not selected:
        await call.answer("⚠️ Выберите хотя бы одну услугу!", show_alert=True)
        return
    await state.update_data(services=selected)
    await state.set_state(BookingStates.comment)
    await call.message.edit_text(
        "📝 Комментарий к записи?\n"
        "(например, особые пожелания, или нажмите 'Пропустить')"
    )
    await call.answer()

@dp.message(BookingStates.comment, Command("skip"))
async def bk_comment_skip(message: Message, state: FSMContext):
    await state.update_data(comment="")
    await show_confirm(message, state)

@dp.message(BookingStates.comment)
async def bk_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text.strip())
    await show_confirm(message, state)

async def show_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    d = date.fromisoformat(data["day"])
    date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]}) {data['time']}"
    master = data.get("master", "")
    services = data.get("services", [])
    services_text = "\n".join([f"   • {s} — {db.get_master_price_for_service(master, s)} ₽" for s in services])
    total_price = 0
    for s in services:
        total_price += db.get_master_price_for_service(master, s)
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="change_data")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="menu")]
    ])
    full_name = f"{data['first_name']} {data.get('last_name', '')} {data.get('patronymic', '')}".strip()
    await message.answer(
        f"📋 ПРОВЕРЬТЕ ЗАПИСЬ:\n\n"
        f"📅 {date_label}\n"
        f"👤 {full_name}\n"
        f"👩‍🎨 {master}\n"
        f"📞 {data.get('phone', '—')}\n"
        f"📧 {data.get('email', '—')}\n"
        f"💅 Услуги:\n{services_text}\n"
        f"💰 Итоговая цена: {total_price} ₽\n"
        f"📝 {data.get('comment', 'Без комментария')}\n\n"
        "Всё верно?",
        reply_markup=confirm_kb
    )

@dp.callback_query(F.data == "change_data")
async def cb_change_data(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Изменить ФИО", callback_data="change_fio")],
        [InlineKeyboardButton(text="📞 Изменить телефон", callback_data="change_phone")],
        [InlineKeyboardButton(text="📧 Изменить email", callback_data="change_email")],
        [InlineKeyboardButton(text="💅 Изменить услуги", callback_data="change_services")],
        [InlineKeyboardButton(text="📝 Изменить комментарий", callback_data="change_comment")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_confirm")]
    ])
    await call.message.edit_text("✏️ Что хотите изменить?", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "back_to_confirm")
async def cb_back_to_confirm(call: CallbackQuery, state: FSMContext):
    await show_confirm(call.message, state)
    await call.answer()

@dp.callback_query(F.data == "change_fio")
async def cb_change_fio(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.first_name)
    await call.message.edit_text("👤 Пожалуйста, введите Ваши ФИО заново:\nНапример: Иванова Анна Сергеевна")
    await call.answer()

@dp.callback_query(F.data == "change_phone")
async def cb_change_phone(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.phone)
    await call.message.edit_text("📞 Введите новый номер телефона:")
    await call.answer()

@dp.callback_query(F.data == "change_email")
async def cb_change_email(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.email)
    await call.message.edit_text("📧 Введите новый email (или /skip):")
    await call.answer()

@dp.callback_query(F.data == "change_services")
async def cb_change_services(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.services)
    data = await state.get_data()
    selected = data.get("selected_services", data.get("services", []))
    await state.update_data(selected_services=selected)
    await call.message.edit_text(
        "💅 Выберите услуги:",
        reply_markup=services_inline_kb(selected, data.get("master", ""))
    )
    await call.answer()

@dp.callback_query(F.data == "change_comment")
async def cb_change_comment(call: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.comment)
    await call.message.edit_text("📝 Введите новый комментарий (или /skip):")
    await call.answer()

# ================= ПОДТВЕРЖДЕНИЕ =================
@dp.callback_query(F.data == "confirm_yes")
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data or not data.get("day") or not data.get("time"):
        await call.message.edit_text("❌ Ошибка: данные не найдены. Попробуйте заново.", reply_markup=main_menu_kb())
        await call.answer()
        return
    success, message = db.save_booking(
        call.from_user.id,
        call.from_user.username or "",
        data["day"],
        data["time"],
        data.get("first_name", "Клиент"),
        data.get("last_name", ""),
        data.get("patronymic", ""),
        data.get("phone", ""),
        data.get("email", ""),
        data.get("master", ""),
        data.get("services", []),
        data.get("comment", "")
    )
    if success:
        d = date.fromisoformat(data["day"])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]}) {data['time']}"
        services = data.get("services", [])
        master = data.get("master", "")
        services_text = "\n".join([f"   • {s} — {db.get_master_price_for_service(master, s)} ₽" for s in services])
        total_price = sum([db.get_master_price_for_service(master, s) for s in services])
        full_name = f"{data['first_name']} {data.get('last_name', '')} {data.get('patronymic', '')}".strip()
        await call.message.edit_text(
            f"✅ ЗАПИСЬ ПОДТВЕРЖДЕНА!\n\n"
            f"📅 {date_label}\n"
            f"👤 {full_name}\n"
            f"👩‍🎨 {master}\n"
            f"📞 {data.get('phone', '—')}\n"
            f"📧 {data.get('email', '—')}\n"
            f"💅 Услуги:\n{services_text}\n"
            f"💰 Итоговая цена: {total_price} ₽\n"
            f"📝 {data.get('comment', 'Без комментария')}\n\n"
            "✨ Ждём Вас!",
            reply_markup=main_menu_kb()
        )
        if NOTIFICATION_CHAT_ID:
            try:
                notification = (
                    f"🆕 НОВАЯ ЗАПИСЬ!\n\n"
                    f"📅 {date_label}\n"
                    f"👤 {full_name}\n"
                    f"👩‍🎨 {master}\n"
                    f"📞 {data.get('phone', '—')}\n"
                    f"📧 {data.get('email', '—')}\n"
                    f"💅 Услуги:\n{services_text}\n"
                    f"💰 Итоговая цена: {total_price} ₽\n"
                    f"🆔 ID: {call.from_user.id}\n"
                    f"👤 TG: @{call.from_user.username or 'не указан'}"
                )
                await bot.send_message(NOTIFICATION_CHAT_ID, notification)
                master_data = db.get_master_by_name(master)
                if master_data and master_data.get('tg_id'):
                    try:
                        await bot.send_message(
                            master_data['tg_id'],
                            f"📅 Новая запись к Вам!\n\n"
                            f"📅 {date_label}\n"
                            f"👤 {full_name}\n"
                            f"📞 {data.get('phone', '—')}\n"
                            f"💅 Услуги:\n{services_text}\n"
                            f"💰 Итоговая цена: {total_price} ₽"
                        )
                    except:
                        pass
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление: {e}")
    else:
        await call.message.edit_text(message, reply_markup=main_menu_kb())
    await state.clear()
    await call.answer()

# ================= ОТЗЫВЫ (FSM) =================
@dp.callback_query(F.data.startswith("fb_master_"))
async def cb_fb_master(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split("_")[2])
    masters = db.get_all_masters_with_rating()
    master = masters[idx]
    await state.update_data(master=master['name'])
    await state.set_state(FeedbackStates.rating)
    rating_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ 1", callback_data="fb_rating_1"),
            InlineKeyboardButton(text="⭐ 2", callback_data="fb_rating_2"),
            InlineKeyboardButton(text="⭐ 3", callback_data="fb_rating_3"),
            InlineKeyboardButton(text="⭐ 4", callback_data="fb_rating_4"),
            InlineKeyboardButton(text="⭐ 5", callback_data="fb_rating_5")
        ],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu")]
    ])
    await call.message.edit_text(
        f"⭐ Оцените работу мастера {master['name']} от 1 до 5:",
        reply_markup=rating_kb
    )
    await call.answer()

@dp.callback_query(F.data.startswith("fb_rating_"))
async def cb_fb_rating(call: CallbackQuery, state: FSMContext):
    rating = int(call.data.split("_")[2])
    await state.update_data(rating=rating)
    await state.set_state(FeedbackStates.text)
    await call.message.edit_text(
        f"⭐ Ваша оценка: {rating}\n\n"
        "📝 Напишите отзыв о мастере:"
    )
    await call.answer()

@dp.message(FeedbackStates.text)
async def fb_text(message: Message, state: FSMContext):
    data = await state.get_data()
    db.save_feedback(
        message.from_user.id,
        message.from_user.username or "",
        data['master'],
        data['rating'],
        message.text
    )
    await state.clear()
    await message.answer(
        "✅ Спасибо за отзыв! ❤️\n"
        "После модерации он появится в разделе отзывов.",
        reply_markup=main_menu_kb()
    )
    if ADMIN_ID:
        try:
            stars = "⭐" * data['rating']
            await bot.send_message(
                ADMIN_ID,
                f"📩 Новый отзыв!\n"
                f"👩‍🎨 Мастер: {data['master']}\n"
                f"Оценка: {stars}\n"
                f"Текст: {message.text}"
            )
        except:
            pass

# ================= ОТМЕНА ЗАПИСИ =================
@dp.callback_query(F.data.startswith("cancel_booking_"))
async def cb_cancel_booking(call: CallbackQuery):
    booking_id = int(call.data.split("_")[2])
    success, message = db.cancel_booking(booking_id, call.from_user.id)
    await call.message.edit_text(message, reply_markup=main_menu_kb())
    await call.answer()

# ================= МАСТЕР: ЛИЧНЫЙ КАБИНЕТ =================
@dp.message(F.text == "👩‍🎨 Личный кабинет мастера")
@master_only
async def btn_master_panel(message: Message):
    master = db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await message.answer("⛔ Вы не зарегистрированы как мастер.", reply_markup=main_menu_kb())
        return
    stats = db.get_master_stats(master['name'])
    rating, reviews = db.get_master_rating(master['name'])
    factor = db.get_master_price_factor(master['name'])
    stars = "⭐" * int(rating) + "☆" * (5 - int(rating)) if reviews > 0 else "🆕 Новичок"
    await message.answer(
        f"👩‍🎨 **ЛИЧНЫЙ КАБИНЕТ МАСТЕРА**\n\n"
        f"👤 {master['name']}\n"
        f"{master['description']}\n\n"
        f"⭐ Рейтинг: {stars} ({reviews} отзывов)\n"
        f"💰 Коэф. цен: {factor:.2f}x\n\n"
        f"📊 **Статистика:**\n"
        f"   • Всего записей: {stats['total']}\n"
        f"   • Сегодня: {stats['today']}\n"
        f"   • Завтра: {stats['tomorrow']}\n"
        f"   • Клиентов: {stats['clients']}\n\n"
        "Выберите действие в меню ниже:",
        reply_markup=master_menu_kb(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "📊 Моя статистика")
@master_only
async def btn_master_stats(message: Message):
    master = db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await message.answer("⛔ Вы не зарегистрированы как мастер.", reply_markup=main_menu_kb())
        return
    stats = db.get_master_stats(master['name'])
    rating, reviews = db.get_master_rating(master['name'])
    factor = db.get_master_price_factor(master['name'])
    text = f"📊 **СТАТИСТИКА МАСТЕРА {master['name']}**\n\n"
    text += f"⭐ Рейтинг: {rating:.1f} ({reviews} отзывов)\n"
    text += f"💰 Коэф. цен: {factor:.2f}x\n\n"
    text += f"📅 **Всего записей:** {stats['total']}\n"
    text += f"📅 **Сегодня:** {stats['today']}\n"
    text += f"📅 **Завтра:** {stats['tomorrow']}\n"
    text += f"👥 **Всего клиентов:** {stats['clients']}\n"
    await message.answer(text, reply_markup=master_menu_kb(), parse_mode="Markdown")

@dp.message(F.text == "📈 Мой рейтинг")
@master_only
async def btn_master_rating(message: Message):
    master = db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await message.answer("⛔ Вы не зарегистрированы как мастер.", reply_markup=main_menu_kb())
        return
    rating, reviews = db.get_master_rating(master['name'])
    factor = db.get_master_price_factor(master['name'])
    stars = "⭐" * int(rating) + "☆" * (5 - int(rating)) if reviews > 0 else "🆕 Новичок"
    text = f"📈 **РЕЙТИНГ МАСТЕРА {master['name']}**\n\n"
    text += f"⭐ {stars}\n"
    text += f"📊 Средний балл: {rating:.1f} из 5\n"
    text += f"📝 Всего отзывов: {reviews}\n"
    text += f"💰 Коэф. цен: {factor:.2f}x\n\n"
    feedbacks = db.get_master_feedback(master['name'], 5)
    if feedbacks:
        text += "📝 **Последние отзывы:**\n\n"
        for fb in feedbacks:
            stars_fb = "⭐" * fb['rating']
            text += f"👤 {fb['username'] or 'Аноним'} {stars_fb}\n"
            text += f"   {fb['text'][:100]}...\n"
            text += f"   📅 {fb['created_at'][:10]}\n\n"
    else:
        text += "📝 Отзывов пока нет.\n"
    await message.answer(text, reply_markup=master_menu_kb(), parse_mode="Markdown")

@dp.message(F.text == "⭐ Мои отзывы")
@master_only
async def btn_master_feedback(message: Message):
    master = db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await message.answer("⛔ Вы не зарегистрированы как мастер.", reply_markup=main_menu_kb())
        return
    feedbacks = db.get_master_feedback(master['name'], 20)
    if not feedbacks:
        await message.answer(
            "📝 У Вас пока нет отзывов.\n\n"
            "Когда клиенты оставят отзывы, они появятся здесь.",
            reply_markup=master_menu_kb()
        )
        return
    text = f"⭐ **ОТЗЫВЫ О МАСТЕРЕ {master['name']}**\n\n"
    for i, fb in enumerate(feedbacks, 1):
        stars = "⭐" * fb['rating']
        text += f"{i}. 👤 {fb['username'] or 'Аноним'} {stars}\n"
        text += f"   📝 {fb['text']}\n"
        text += f"   📅 {fb['created_at'][:10]}\n\n"
    await message.answer(text, reply_markup=master_menu_kb(), parse_mode="Markdown")

@dp.message(F.text == "📋 Мои записи")
@master_only
async def btn_master_bookings(message: Message):
    master = db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await message.answer("⛔ Вы не зарегистрированы как мастер.", reply_markup=main_menu_kb())
        return
    bookings = db.get_master_bookings(master['name'])
    if not bookings:
        await message.answer(
            "📋 У Вас нет активных записей.\n\n"
            "Когда клиенты запишутся, они появятся здесь.",
            reply_markup=master_menu_kb()
        )
        return
    text = f"📋 **ЗАПИСИ К МАСТЕРУ {master['name']}**\n\n"
    for b in bookings[:20]:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        full_name = f"{b['first_name']} {b.get('last_name', '')} {b.get('patronymic', '')}".strip()
        text += f"🆔 #{b['id']} {date_label} в {b['time']}\n"
        text += f"   👤 {full_name}\n"
        if b['phone']:
            text += f"   📞 {b['phone']}\n"
        if b['email']:
            text += f"   📧 {b['email']}\n"
        if b['services']:
            text += f"   💅 {b['services']}\n"
        if b['final_price']:
            text += f"   💰 {b['final_price']} ₽\n"
        text += f"   📅 Записано: {b['created_at'][:10]}\n\n"
    if len(bookings) > 20:
        text += f"\n... и еще {len(bookings) - 20} записей"
    await message.answer(text, reply_markup=master_menu_kb(), parse_mode="Markdown")

@dp.message(F.text == "⏰ Мой график")
@master_only
async def btn_master_schedule(message: Message):
    master = db.get_master_by_tg_id(message.from_user.id)
    if not master:
        await message.answer("⛔ Вы не зарегистрированы как мастер.", reply_markup=main_menu_kb())
        return
    schedule = db.get_master_schedule(master['name'])
    vacation_info = db.get_vacation_info(master['name'])
    rating, reviews = db.get_master_rating(master['name'])
    factor = db.get_master_price_factor(master['name'])
    text = f"⏰ **ГРАФИК РАБОТЫ МАСТЕРА {master['name']}**\n\n"
    text += f"🕐 Рабочее время: {schedule['work_start']} - {schedule['work_end']}\n"
    text += f"⭐ Рейтинг: {rating:.1f} ({reviews} отзывов)\n"
    text += f"💰 Коэф. цен: {factor:.2f}x\n"
    if master['is_on_vacation'] and vacation_info:
        text += f"🏖️ **В ОТПУСКЕ!**\n"
        text += f"   📅 С: {vacation_info['vacation_start']}\n"
        text += f"   📅 По: {vacation_info['vacation_end']}\n"
    else:
        text += "✅ Работает в обычном режиме\n"
    bookings = db.get_master_bookings(master['name'])
    if bookings:
        text += f"\n📋 **Ближайшие записи ({len(bookings)}):**\n"
        for b in bookings[:5]:
            d = date.fromisoformat(b['day'])
            date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
            full_name = f"{b['first_name']} {b.get('last_name', '')}".strip()
            text += f"   • {date_label} в {b['time']} — {full_name}"
            if b['final_price']:
                text += f" ({b['final_price']} ₽)"
            text += "\n"
    await message.answer(text, reply_markup=master_menu_kb(), parse_mode="Markdown")

@dp.message(F.text == "⬅️ В главное меню")
async def btn_back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())

# ================= АДМИН: ВСЕ ЗАПИСИ =================
@dp.message(F.text == "📋 Все записи")
@admin_only
async def btn_admin_bookings(message: Message):
    bookings = db.get_all_bookings()
    if not bookings:
        await message.answer("📋 Нет активных записей", reply_markup=admin_menu_kb())
        return
    text = "📋 ВСЕ ЗАПИСИ\n\n"
    for b in bookings[:20]:
        d = date.fromisoformat(b['day'])
        date_label = f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"
        text += f"🆔 #{b['id']} {date_label} {b['time']}\n"
        text += f"   👤 {b['first_name']} {b.get('last_name', '')}\n"
        text += f"   👩‍🎨 {b['master']}\n"
        if b['phone']:
            text += f"   📞 {b['phone']}\n"
        if b['email']:
            text += f"   📧 {b['email']}\n"
        if b['services']:
            text += f"   💅 {b['services']}\n"
        if b['final_price']:
            text += f"   💰 {b['final_price']} ₽\n"
        text += f"   🆔 {b['user_id']} @{b.get('tg_username', '—')}\n\n"
    await message.answer(text, reply_markup=admin_menu_kb())

@dp.message(F.text == "⭐ Отзывы на модерации")
@admin_only
async def btn_admin_feedback(message: Message):
    feedbacks = db.get_unapproved_feedback()
    if not feedbacks:
        await message.answer("⭐ Нет отзывов на модерации", reply_markup=admin_menu_kb())
        return
    fb = feedbacks[0]
    stars = "⭐" * fb['rating'] + "☆" * (5 - fb['rating'])
    await message.answer(
        f"⭐ ОТЗЫВ #{fb['id']}\n\n"
        f"👤 {fb['username'] or fb['user_id']}\n"
        f"👩‍🎨 Мастер: {fb['master_name']}\n"
        f"Рейтинг: {stars}\n"
        f"📝 {fb['text']}\n"
        f"📅 {fb['created_at']}\n\n"
        f"Осталось {len(feedbacks) - 1} отзывов",
        reply_markup=admin_feedback_inline_kb(fb['id'])
    )

@dp.message(F.text == "📊 Статистика")
@admin_only
async def btn_admin_stats(message: Message):
    bookings = db.get_all_bookings()
    total = len(bookings)
    days = {}
    masters = {}
    for b in bookings:
        day = b['day']
        days[day] = days.get(day, 0) + 1
        master = b['master']
        masters[master] = masters.get(master, 0) + 1
    text = "📊 СТАТИСТИКА\n\n"
    text += f"Всего записей: {total}\n\n"
    text += "👩‍🎨 По мастерам:\n"
    for master, count in sorted(masters.items(), key=lambda x: x[1], reverse=True):
        rating, reviews = db.get_master_rating(master)
        text += f"   {master}: {count} записей (⭐ {rating:.1f})\n"
    text += "\n📅 По дням:\n"
    for day, count in sorted(days.items())[:10]:
        d = date.fromisoformat(day)
        text += f"   {d.day:02d}.{d.month:02d}: {count}\n"
    await message.answer(text, reply_markup=admin_menu_kb())

@dp.message(F.text == "👥 База клиентов")
@admin_only
async def btn_admin_users(message: Message):
    users = db.get_all_users()
    if not users:
        await message.answer("👥 База клиентов пуста", reply_markup=admin_menu_kb())
        return
    text = "👥 БАЗА КЛИЕНТОВ\n\n"
    for u in users[:20]:
        text += f"🆔 {u['user_id']}\n"
        text += f"👤 {u['first_name'] or ''} {u['last_name'] or ''} {u['patronymic'] or ''}\n"
        if u['phone']:
            text += f"📞 {u['phone']}\n"
        if u['email']:
            text += f"📧 {u['email']}\n"
        text += f"📊 Записей: {u['total_bookings']}\n"
        text += f"📜 Согласие на ПД: {'✅' if u['consent_given'] else '❌'}\n"
        text += f"📅 {u['created_at'][:10]}\n\n"
    if len(users) > 20:
        text += f"... и еще {len(users) - 20} клиентов"
    await message.answer(text, reply_markup=admin_menu_kb())

# ================= АДМИН: УПРАВЛЕНИЕ МАСТЕРАМИ =================
@dp.message(F.text == "👩‍🎨 Управление мастерами")
@admin_only
async def btn_admin_masters(message: Message):
    masters = db.get_all_masters_with_rating()
    text = "👩‍🎨 **УПРАВЛЕНИЕ МАСТЕРАМИ**\n\n"
    for m in masters:
        rating, reviews = db.get_master_rating(m['name'])
        factor = db.get_master_price_factor(m['name'])
        tg_status = f"TG ID: {m['tg_id']}" if m['tg_id'] else "⚠️ TG ID не указан"
        text += f"👤 {m['name']}\n"
        text += f"   {m['description']}\n"
        text += f"   {tg_status}\n"
        text += f"   ⭐ {rating:.1f} ({reviews} отзывов)\n"
        text += f"   💰 Коэф. цен: {factor:.2f}x\n"
        text += f"   📊 {m['total_bookings']} записей\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить TG ID мастеру", callback_data="admin_add_master_tg")],
        [InlineKeyboardButton(text="🔄 Обновить данные мастеров", callback_data="admin_refresh_masters")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_add_master_tg")
@admin_only
async def cb_admin_add_master_tg(call: CallbackQuery, state: FSMContext):
    masters = db.get_all_masters_with_rating()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=m['name'], callback_data=f"admin_master_tg_{i}")]
        for i, m in enumerate(masters)
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]])
    await call.message.edit_text(
        "👩‍🎨 Выберите мастера, которому хотите добавить TG ID:",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data.startswith("admin_master_tg_"))
@admin_only
async def cb_admin_master_tg_select(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split("_")[3])
    masters = db.get_all_masters_with_rating()
    master = masters[idx]
    await state.update_data(selected_master=master['name'])
    await state.set_state(AdminMasterStates.set_tg_id)
    await call.message.edit_text(
        f"👩‍🎨 Мастер: {master['name']}\n\n"
        "Введите TG ID этого мастера.\n"
        "Мастер может узнать свой ID через команду /myid",
        reply_markup=back_inline_kb()
    )
    await call.answer()

@dp.message(AdminMasterStates.set_tg_id)
@admin_only
async def admin_set_master_tg_id(message: Message, state: FSMContext):
    data = await state.get_data()
    master_name = data.get('selected_master')
    try:
        tg_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Пожалуйста, введите корректный числовой ID")
        return
    db.update_master_tg_id(master_name, tg_id)
    await state.clear()
    await message.answer(
        f"✅ TG ID для мастера {master_name} успешно обновлен!",
        reply_markup=admin_menu_kb()
    )

@dp.callback_query(F.data == "admin_refresh_masters")
@admin_only
async def cb_admin_refresh_masters(call: CallbackQuery):
    with db.get_connection() as conn:
        for master in MASTERS:
            conn.execute(
                """INSERT OR REPLACE INTO masters (name, description, tg_id) 
                   VALUES (?, ?, ?)""",
                (master.name, master.description, master.tg_id)
            )
    await call.message.edit_text(
        "✅ Данные мастеров обновлены!",
        reply_markup=back_inline_kb()
    )
    await call.answer()

# ================= АДМИН: ГРАФИК РАБОТЫ =================
@dp.message(F.text == "⏰ График работы")
@admin_only
async def btn_admin_schedule(message: Message):
    masters = db.get_all_masters_with_rating()
    text = "⏰ **ГРАФИК РАБОТЫ МАСТЕРОВ**\n\n"
    for m in masters:
        schedule = db.get_master_schedule(m['name'])
        vacation = db.get_vacation_info(m['name'])
        rating, reviews = db.get_master_rating(m['name'])
        factor = db.get_master_price_factor(m['name'])
        text += f"👤 {m['name']}\n"
        text += f"   ⭐ Рейтинг: {rating:.1f} ({reviews} отзывов)\n"
        text += f"   🕐 {schedule['work_start']} - {schedule['work_end']}\n"
        text += f"   💰 Коэф. цен: {factor:.2f}x\n"
        if m['is_on_vacation'] and vacation:
            text += f"   🏖️ **В ОТПУСКЕ** до {vacation['vacation_end']}\n"
        else:
            text += "   ✅ Работает\n"
        text += "\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Настроить график", callback_data="admin_edit_schedule")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_edit_schedule")
@admin_only
async def cb_admin_edit_schedule(call: CallbackQuery):
    masters = db.get_all_masters_with_rating()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=m['name'], callback_data=f"schedule_master_{i}")]
        for i, m in enumerate(masters)
    ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]])
    await call.message.edit_text(
        "👩‍🎨 Выберите мастера для настройки графика:",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data.startswith("schedule_master_"))
@admin_only
async def cb_schedule_master(call: CallbackQuery):
    idx = int(call.data.split("_")[2])
    masters = db.get_all_masters_with_rating()
    master = masters[idx]
    schedule = db.get_master_schedule(master['name'])
    vacation_info = db.get_vacation_info(master['name'])
    rating, reviews = db.get_master_rating(master['name'])
    factor = db.get_master_price_factor(master['name'])
    text = f"👩‍🎨 **{master['name']}**\n\n"
    text += f"⭐ Рейтинг: {rating:.1f} ({reviews} отзывов)\n"
    text += f"💰 Коэф. цен: {factor:.2f}x\n"
    text += f"🕐 Текущее рабочее время: {schedule['work_start']} - {schedule['work_end']}\n"
    if master['is_on_vacation'] and vacation_info:
        text += f"🏖️ **В ОТПУСКЕ!**\n"
        text += f"   📅 С: {vacation_info['vacation_start']}\n"
        text += f"   📅 По: {vacation_info['vacation_end']}\n"
    else:
        text += "✅ Работает\n"
    await call.message.edit_text(
        text,
        reply_markup=master_schedule_inline_kb(master['name']),
        parse_mode="Markdown"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("schedule_time_"))
@admin_only
async def cb_schedule_time(call: CallbackQuery, state: FSMContext):
    master_name = call.data.split("_", 2)[2]
    await state.update_data(master_name=master_name)
    await state.set_state(AdminMasterStates.set_work_time)
    await call.message.edit_text(
        f"⏰ Настройка рабочего времени для {master_name}\n\n"
        "Введите время начала и окончания работы в формате:\n"
        "`10:00 20:00`\n\n"
        "Например: 10:00 20:00",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(AdminMasterStates.set_work_time)
@admin_only
async def admin_set_work_time(message: Message, state: FSMContext):
    data = await state.get_data()
    master_name = data.get('master_name')
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            raise ValueError("Неверный формат")
        start_time = parts[0]
        end_time = parts[1]
        start_hour = int(start_time.split(':')[0])
        end_hour = int(end_time.split(':')[0])
        if start_hour < 0 or start_hour > 23 or end_hour < 0 or end_hour > 23:
            raise ValueError("Часы должны быть от 0 до 23")
        if start_hour >= end_hour:
            raise ValueError("Время начала должно быть меньше времени окончания")
        db.update_master_schedule(master_name, start_time, end_time)
        await state.clear()
        await message.answer(
            f"✅ Рабочее время для {master_name} обновлено: {start_time} - {end_time}",
            reply_markup=admin_menu_kb()
        )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}\n\nВведите время в формате: `10:00 20:00`", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("schedule_vacation_"))
@admin_only
async def cb_schedule_vacation(call: CallbackQuery, state: FSMContext):
    master_name = call.data.split("_", 2)[2]
    await state.update_data(master_name=master_name)
    await state.set_state(AdminMasterStates.set_vacation_days)
    await call.message.edit_text(
        f"🏖️ Отправка в отпуск: {master_name}\n\n"
        "Введите количество дней отпуска (целое число):"
    )
    await call.answer()

@dp.message(AdminMasterStates.set_vacation_days)
@admin_only
async def admin_set_vacation(message: Message, state: FSMContext):
    data = await state.get_data()
    master_name = data.get('master_name')
    try:
        days = int(message.text.strip())
        success, msg = db.set_master_vacation(master_name, days)
        await state.clear()
        if success:
            await message.answer(msg, reply_markup=admin_menu_kb())
            master = db.get_master_by_name(master_name)
            if master and master.get('tg_id'):
                try:
                    await bot.send_message(
                        master['tg_id'],
                        f"🏖️ Вас отправили в отпуск на {days} дней!"
                    )
                except:
                    pass
        else:
            await message.answer(msg, reply_markup=admin_menu_kb())
    except ValueError:
        await message.answer("⚠️ Введите целое число (количество дней)")

@dp.callback_query(F.data.startswith("schedule_end_vacation_"))
@admin_only
async def cb_schedule_end_vacation(call: CallbackQuery):
    master_name = call.data.split("_", 2)[2]
    success, msg = db.end_master_vacation(master_name)
    if success:
        master = db.get_master_by_name(master_name)
        if master and master.get('tg_id'):
            try:
                await bot.send_message(
                    master['tg_id'],
                    f"✅ Ваш отпуск завершен! Вы снова работаете."
                )
            except:
                pass
    await call.message.edit_text(msg, reply_markup=back_inline_kb())
    await call.answer()

# ================= АДМИН: НАСТРОЙКИ ЦЕН =================
@dp.message(F.text == "⚙️ Настройки цен")
@admin_only
async def btn_admin_price_settings(message: Message):
    factor = float(db.get_setting('price_factor_per_rating', '0.15'))
    min_multiplier = float(db.get_setting('base_price_multiplier', '0.7'))
    text = "⚙️ **НАСТРОЙКИ ЦЕНООБРАЗОВАНИЯ**\n\n"
    text += f"📈 Фактор цены за рейтинг: {factor:.2f}\n"
    text += f"   (каждый балл рейтинга увеличивает цену на {factor*100:.0f}%)\n\n"
    text += f"📉 Минимальный коэффициент для новичков: {min_multiplier:.2f}\n"
    text += f"   (минимальная цена = базовая * {min_multiplier:.2f})\n\n"
    text += "💡 **Пример:**\n"
    text += f"   • Новичок (0 отзывов): цена = базовая * {min_multiplier:.2f}\n"
    text += f"   • Рейтинг 3.0: цена = базовая * {1 + (3.0 * factor):.2f}\n"
    text += f"   • Рейтинг 5.0: цена = базовая * {1 + (5.0 * factor):.2f}\n"
    await message.answer(text, reply_markup=admin_price_settings_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_show_prices")
@admin_only
async def cb_admin_show_prices(call: CallbackQuery):
    masters = db.get_all_masters_with_rating()
    text = "📊 **ЦЕНЫ МАСТЕРОВ**\n\n"
    for master in masters:
        rating, reviews = db.get_master_rating(master['name'])
        factor = db.get_master_price_factor(master['name'])
        text += f"👤 {master['name']}\n"
        text += f"   ⭐ Рейтинг: {rating:.1f} ({reviews} отзывов)\n"
        text += f"   💰 Коэффициент: {factor:.2f}x\n"
        text += f"   📋 Цены:\n"
        for service in BASE_PRICES:
            price = db.get_master_price_for_service(master['name'], service.name)
            text += f"      • {service.name}: {price} ₽\n"
        text += "\n"
    await call.message.edit_text(text, reply_markup=admin_price_settings_kb(), parse_mode="Markdown")
    await call.answer()

@dp.callback_query(F.data == "admin_change_factor")
@admin_only
async def cb_admin_change_factor(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminMasterStates.set_price_factor)
    await call.message.edit_text(
        "📈 Введите новый фактор цены за рейтинг (число от 0 до 0.5):\n\n"
        "Примеры:\n"
        "• 0.10 — +10% за каждый балл рейтинга\n"
        "• 0.15 — +15% за каждый балл рейтинга (по умолчанию)\n"
        "• 0.20 — +20% за каждый балл рейтинга",
        reply_markup=back_inline_kb()
    )
    await call.answer()

@dp.message(AdminMasterStates.set_price_factor)
@admin_only
async def admin_set_price_factor(message: Message, state: FSMContext):
    try:
        factor = float(message.text.strip())
        if factor < 0 or factor > 0.5:
            raise ValueError("Фактор должен быть от 0 до 0.5")
        db.set_setting('price_factor_per_rating', str(factor))
        await state.clear()
        await message.answer(
            f"✅ Фактор цены обновлен: {factor:.2f}",
            reply_markup=admin_menu_kb()
        )
    except ValueError:
        await message.answer("⚠️ Введите число от 0 до 0.5")

@dp.callback_query(F.data == "admin_change_min")
@admin_only
async def cb_admin_change_min(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminMasterStates.set_price_min)
    await call.message.edit_text(
        "📉 Введите минимальный коэффициент для новичков (число от 0.3 до 1.0):\n\n"
        "Примеры:\n"
        "• 0.70 — новички получают скидку 30% (по умолчанию)\n"
        "• 0.50 — новички получают скидку 50%\n"
        "• 1.00 — новички платят полную цену",
        reply_markup=back_inline_kb()
    )
    await call.answer()

@dp.message(AdminMasterStates.set_price_min)
@admin_only
async def admin_set_price_min(message: Message, state: FSMContext):
    try:
        min_multiplier = float(message.text.strip())
        if min_multiplier < 0.3 or min_multiplier > 1.0:
            raise ValueError("Коэффициент должен быть от 0.3 до 1.0")
        db.set_setting('base_price_multiplier', str(min_multiplier))
        await state.clear()
        await message.answer(
            f"✅ Минимальный коэффициент обновлен: {min_multiplier:.2f}",
            reply_markup=admin_menu_kb()
        )
    except ValueError:
        await message.answer("⚠️ Введите число от 0.3 до 1.0")

# ================= АДМИН (INLINE) =================
@dp.callback_query(F.data == "admin_panel")
@admin_only
async def cb_admin_panel(call: CallbackQuery):
    await call.message.edit_text("⚙️ АДМИН-ПАНЕЛЬ")
    await call.message.answer("⚙️ АДМИН-ПАНЕЛЬ", reply_markup=admin_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "admin_feedback")
@admin_only
async def cb_admin_feedback(call: CallbackQuery):
    feedbacks = db.get_unapproved_feedback()
    if not feedbacks:
        await call.message.edit_text("⭐ Нет отзывов на модерации", reply_markup=back_inline_kb())
        await call.answer()
        return
    fb = feedbacks[0]
    stars = "⭐" * fb['rating'] + "☆" * (5 - fb['rating'])
    await call.message.edit_text(
        f"⭐ ОТЗЫВ #{fb['id']}\n\n"
        f"👤 {fb['username'] or fb['user_id']}\n"
        f"👩‍🎨 Мастер: {fb['master_name']}\n"
        f"Рейтинг: {stars}\n"
        f"📝 {fb['text']}\n"
        f"📅 {fb['created_at']}\n\n"
        f"Осталось {len(feedbacks) - 1} отзывов",
        reply_markup=admin_feedback_inline_kb(fb['id'])
    )
    await call.answer()

@dp.callback_query(F.data.startswith("admin_approve_fb_"))
@admin_only
async def cb_admin_approve_feedback(call: CallbackQuery):
    fb_id = int(call.data.split("_")[3])
    db.approve_feedback(fb_id)
    await call.message.edit_text("✅ Отзыв одобрен", reply_markup=admin_menu_kb())
    await call.answer()

@dp.callback_query(F.data.startswith("admin_reject_fb_"))
@admin_only
async def cb_admin_reject_feedback(call: CallbackQuery):
    fb_id = int(call.data.split("_")[3])
    with db.get_connection() as conn:
        conn.execute("DELETE FROM feedback WHERE id = ?", (fb_id,))
    await call.message.edit_text("❌ Отзыв отклонен", reply_markup=admin_menu_kb())
    await call.answer()

# ================= ЗАПУСК =================
async def main():
    try:
        db.init_db()
        logger.info("✅ База данных инициализирована")
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Вебхук удален")
        logger.info("🚀 Бот запущен!")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())

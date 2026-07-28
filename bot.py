"""Шуточный Telegram-бот с виртуальными очками.

Проект намеренно не содержит покупки, вывода или обмена очков.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl import types
from telethon.tl.types import Channel, Chat, MessageMediaDice, User

from games import casino, dice, guess_sound
from games.guess_sound.freesound import FreesoundProvider

from games.pets import (
    Pet, SPECS, SPEC_EMOJI, SPEC_NAMES, MAX_SLOTS, SLOT_EMOJIS,
    roll_spec, roll_egg_value, create_pure_pet, breed,
    BASE_INCOME_PER_SEC,
)
from games.blind_bank import (
    BBPlayer, BBRoom, BET_OPTIONS, TRANSFER_PHASE_SECONDS,
    distribute_strength, format_strength_range, resolve_room,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def env_int(name: str, default: int | None = None) -> int:
    """Прочитать целое число из окружения и выдать понятную ошибку."""
    raw = os.getenv(name)
    if raw is None:
        if default is not None:
            return default
        raise RuntimeError(f"В .env не задано обязательное поле {name}")
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} должно быть целым числом") from error


API_ID = env_int("API_ID")
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = env_int("ADMIN_ID")
SESSION_NAME = os.getenv("SESSION_NAME", "casino_bot").strip()
INITIAL_BALANCE = env_int("INITIAL_BALANCE", 1000)
MIN_BET = env_int("MIN_BET", 10)
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY", "").strip()
CHERRY_CODE = 1

if not API_HASH:
    raise RuntimeError("В .env не задан API_HASH")
if not BOT_TOKEN or BOT_TOKEN == "replace_me":
    raise RuntimeError("В .env не задан BOT_TOKEN от BotFather")
if ADMIN_ID <= 0:
    raise RuntimeError("В .env должен быть указан положительный ADMIN_ID")
if INITIAL_BALANCE < 0 or MIN_BET <= 0:
    raise RuntimeError("Проверьте INITIAL_BALANCE и MIN_BET в .env")


DEFAULT_BET_COOLDOWN_SECONDS = 20
GAME_MESSAGE_TTL_SECONDS = 5
HISTORY_LIMIT = 10
WORK_PAYOUT_AMOUNT = 1_000
WORK_PAYOUT_INTERVAL_SECONDS = 30 * 60
WORK_PAYOUT_MESSAGE = (
    "Вы поработали в долбильне и заработали 1000 очков. Время депать!"
)
ASSETS = {
    "малышка": ("girl_available", "👩", 2_000, "Карта девушки"),
    "мать": ("mother_available", "👵", 10_000, "Карта матери"),
    "тачка": ("car_available", "🚗", 25_000, "Машина"),
    "хата": ("home_available", "🏠", 100_000, "Квартира"),
}

HELP_TEXT = casino.help_text(MIN_BET)


class BalanceStore:
    """Небольшое SQLite-хранилище балансов по чату и пользователю."""
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS balances (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                balance INTEGER NOT NULL CHECK (balance >= 0),
                last_bet_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        # Миграция базы, созданной до появления ограничения между ставками.
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(balances)")
        }
        if "last_bet_at" not in columns:
            self.connection.execute(
                "ALTER TABLE balances "
                "ADD COLUMN last_bet_at REAL NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                auto_delete INTEGER NOT NULL DEFAULT 1
                    CHECK (auto_delete IN (0, 1)),
                bet_cooldown_seconds INTEGER NOT NULL DEFAULT 20
                    CHECK (bet_cooldown_seconds >= 0),
                activated INTEGER NOT NULL DEFAULT 0
                    CHECK (activated IN (0, 1))
            )
            """
        )
        chat_setting_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(chat_settings)")
        }
        if "auto_delete" not in chat_setting_columns:
            self.connection.execute(
                "ALTER TABLE chat_settings "
                "ADD COLUMN auto_delete INTEGER NOT NULL DEFAULT 1"
            )
        if "bet_cooldown_seconds" not in chat_setting_columns:
            self.connection.execute(
                "ALTER TABLE chat_settings "
                "ADD COLUMN bet_cooldown_seconds INTEGER NOT NULL DEFAULT 20"
            )
        if "activated" not in chat_setting_columns:
            self.connection.execute(
                "ALTER TABLE chat_settings "
                "ADD COLUMN activated INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS player_assets (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                girl_available INTEGER NOT NULL DEFAULT 1
                    CHECK (girl_available IN (0, 1)),
                mother_available INTEGER NOT NULL DEFAULT 1
                    CHECK (mother_available IN (0, 1)),
                car_available INTEGER NOT NULL DEFAULT 1
                    CHECK (car_available IN (0, 1)),
                home_available INTEGER NOT NULL DEFAULT 1
                    CHECK (home_available IN (0, 1)),
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dice_challenges (
                chat_id INTEGER NOT NULL,
                proposal_message_id INTEGER NOT NULL,
                challenger_id INTEGER NOT NULL,
                challenger_name TEXT NOT NULL,
                opponent_id INTEGER NOT NULL,
                opponent_name TEXT NOT NULL,
                stake INTEGER NOT NULL CHECK (stake > 0),
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                PRIMARY KEY (chat_id, proposal_message_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS game_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                game_type TEXT NOT NULL CHECK (game_type IN ('casino', 'dice')),
                player_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                opponent_id INTEGER,
                opponent_name TEXT,
                stake INTEGER NOT NULL,
                player_payout INTEGER NOT NULL,
                opponent_payout INTEGER,
                player_result INTEGER,
                opponent_result INTEGER,
                created_at REAL NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_game_history_chat_time
            ON game_history(chat_id, created_at DESC)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                operation_type TEXT NOT NULL
                    CHECK (operation_type IN ('transfer', 'grant', 'set')),
                actor_id INTEGER NOT NULL,
                actor_name TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                target_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_balance_history_chat_time
            ON balance_history(chat_id, created_at DESC)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS work_payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                recipients_count INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS work_payout_recipients (
                payout_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                PRIMARY KEY (payout_id, user_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_topics (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                PRIMARY KEY (chat_id, topic_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS casino_topics (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                PRIMARY KEY (chat_id, topic_id)
            )
            """
        )
        # Перенести прежнюю общую настройку в основной раздел один раз.
        self.connection.execute(
            """
            INSERT OR IGNORE INTO casino_topics(chat_id, topic_id, enabled)
            SELECT chat_id, 0, enabled
            FROM chat_settings
            """
        )
                # --- Генетическая Мерзость ---
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS player_specs (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                spec TEXT NOT NULL CHECK (spec IN ('stench', 'ugliness', 'stickiness')),
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pets (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                slot_index INTEGER NOT NULL CHECK (slot_index >= 0 AND slot_index < 4),
                name TEXT NOT NULL DEFAULT 'Мутант',
                stench INTEGER NOT NULL DEFAULT 1 CHECK (stench >= 1 AND stench <= 100),
                ugliness INTEGER NOT NULL DEFAULT 1 CHECK (ugliness >= 1 AND ugliness <= 100),
                stickiness INTEGER NOT NULL DEFAULT 1 CHECK (stickiness >= 1 AND stickiness <= 100),
                generation INTEGER NOT NULL DEFAULT 0,
                is_egg INTEGER NOT NULL DEFAULT 0 CHECK (is_egg IN (0, 1)),
                egg_hatch_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, slot_index)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pet_income (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                accumulated REAL NOT NULL DEFAULT 0,
                last_claim_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )

        # --- Слепой Банк ---
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS blind_bank_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting'
                    CHECK (status IN ('waiting', 'betting', 'strength', 'transfer', 'finished')),
                bank INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                phase_ends_at REAL NOT NULL DEFAULT 0,
                winner_id INTEGER,
                winner_take_all INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS blind_bank_players (
                room_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                bet INTEGER NOT NULL DEFAULT 0,
                strength REAL NOT NULL DEFAULT 0,
                strength_range TEXT,
                transfer_target_id INTEGER,
                ready INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, user_id)
            )
            """
        )
        self.connection.commit()

    async def is_topic_enabled(self, chat_id: int, topic_id: int) -> bool:
        """Проверить работу казино в конкретном разделе активного чата."""
        async with self.lock:
            chat_row = self.connection.execute(
                """
                SELECT activated FROM chat_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            if chat_row is None or not chat_row["activated"]:
                return False
            topic_row = self.connection.execute(
                """
                SELECT enabled FROM casino_topics
                WHERE chat_id = ? AND topic_id = ?
                """,
                (chat_id, topic_id),
            ).fetchone()
            if topic_row is not None:
                return bool(topic_row["enabled"])
            return topic_id == 0

    async def is_chat_activated(self, chat_id: int) -> bool:
        """Проверить, активировал ли администратор бота в чате."""
        async with self.lock:
            row = self.connection.execute(
                "SELECT activated FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return bool(row is not None and row["activated"])

    async def activate_chat(self, chat_id: int) -> None:
        """Активировать чат и разрешить обработку команд."""
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO chat_settings(chat_id, enabled, activated)
                VALUES (?, 1, 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                    enabled = 1,
                    activated = 1
                """,
                (chat_id,),
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO casino_topics(chat_id, topic_id, enabled)
                VALUES (?, 0, 1)
                """,
                (chat_id,),
            )
            self.connection.commit()

    async def set_topic_enabled(
        self, chat_id: int, topic_id: int, enabled: bool
    ) -> None:
        """Сохранить состояние казино для конкретного раздела чата."""
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO casino_topics(chat_id, topic_id, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, topic_id)
                DO UPDATE SET enabled = excluded.enabled
                """,
                (chat_id, topic_id, int(enabled)),
            )
            self.connection.commit()

    async def is_auto_delete_enabled(self, chat_id: int) -> bool:
        """Проверить настройку автоматического удаления в чате."""
        async with self.lock:
            row = self.connection.execute(
                "SELECT auto_delete FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return row is None or bool(row["auto_delete"])

    async def toggle_auto_delete(self, chat_id: int) -> bool:
        """Переключить автоудаление и вернуть новое состояние."""
        async with self.lock:
            row = self.connection.execute(
                "SELECT auto_delete FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            new_state = not (row is None or bool(row["auto_delete"]))
            self.connection.execute(
                """
                INSERT INTO chat_settings(chat_id, auto_delete) VALUES (?, ?)
                ON CONFLICT(chat_id)
                DO UPDATE SET auto_delete = excluded.auto_delete
                """,
                (chat_id, int(new_state)),
            )
            self.connection.commit()
            return new_state

    async def toggle_topic_notifications(
        self, chat_id: int, topic_id: int
    ) -> bool:
        """Переключить периодические уведомления в одном топике."""
        async with self.lock:
            row = self.connection.execute(
                """
                SELECT enabled FROM notification_topics
                WHERE chat_id = ? AND topic_id = ?
                """,
                (chat_id, topic_id),
            ).fetchone()
            # Основной топик (0) включён по умолчанию, остальные выключены.
            current_state = (
                bool(row["enabled"]) if row is not None else topic_id == 0
            )
            new_state = not current_state
            self.connection.execute(
                """
                INSERT INTO notification_topics(chat_id, topic_id, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, topic_id)
                DO UPDATE SET enabled = excluded.enabled
                """,
                (chat_id, topic_id, int(new_state)),
            )
            self.connection.commit()
            return new_state

        # ======= Генетическая Мерзость =======

    async def get_or_create_spec(self, chat_id: int, user_id: int) -> str:
        async with self.lock:
            row = self.connection.execute(
                "SELECT spec FROM player_specs WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            if row:
                return row["spec"]
            spec = random.choice(SPECS)
            self.connection.execute(
                "INSERT INTO player_specs(chat_id, user_id, spec) VALUES (?, ?, ?)",
                (chat_id, user_id, spec),
            )
            self.connection.commit()
            return spec

    async def get_pets(self, chat_id: int, user_id: int) -> list[sqlite3.Row]:
        async with self.lock:
            return self.connection.execute(
                "SELECT * FROM pets WHERE chat_id = ? AND user_id = ? ORDER BY slot_index",
                (chat_id, user_id),
            ).fetchall()

    async def add_pet(self, chat_id: int, user_id: int, pet: Pet) -> bool:
        async with self.lock:
            occupied = {
                r["slot_index"]
                for r in self.connection.execute(
                    "SELECT slot_index FROM pets WHERE chat_id = ? AND user_id = ?",
                    (chat_id, user_id),
                ).fetchall()
            }
            free = [i for i in range(MAX_SLOTS) if i not in occupied]
            if not free:
                return False
            slot = free[0]
            self.connection.execute(
                """
                INSERT INTO pets(
                    chat_id, user_id, slot_index, name, stench, ugliness,
                    stickiness, generation, is_egg, egg_hatch_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id, user_id, slot, pet.name, pet.stench, pet.ugliness,
                    pet.stickiness, pet.generation, int(pet.is_egg),
                    pet.egg_hatch_at, pet.created_at,
                ),
            )
            self.connection.commit()
            return True

    async def remove_pet(self, chat_id: int, user_id: int, slot_index: int) -> bool:
        async with self.lock:
            cur = self.connection.execute(
                "DELETE FROM pets WHERE chat_id = ? AND user_id = ? AND slot_index = ?",
                (chat_id, user_id, slot_index),
            )
            self.connection.commit()
            return cur.rowcount > 0

    async def hatch_egg(self, chat_id: int, user_id: int, slot_index: int) -> bool:
        async with self.lock:
            row = self.connection.execute(
                """
                SELECT * FROM pets WHERE chat_id = ? AND user_id = ? AND slot_index = ?
                AND is_egg = 1
                """,
                (chat_id, user_id, slot_index),
            ).fetchone()
            if not row or row["egg_hatch_at"] > time.time():
                return False
            self.connection.execute(
                """
                UPDATE pets SET is_egg = 0, egg_hatch_at = 0
                WHERE chat_id = ? AND user_id = ? AND slot_index = ?
                """,
                (chat_id, user_id, slot_index),
            )
            self.connection.commit()
            return True

    async def rename_pet(
        self, chat_id: int, user_id: int, slot_index: int, name: str
    ) -> bool:
        async with self.lock:
            cur = self.connection.execute(
                """
                UPDATE pets SET name = ?
                WHERE chat_id = ? AND user_id = ? AND slot_index = ? AND is_egg = 0
                """,
                (name, chat_id, user_id, slot_index),
            )
            self.connection.commit()
            return cur.rowcount > 0

    async def get_pet_income(self, chat_id: int, user_id: int) -> tuple[float, float]:
        async with self.lock:
            row = self.connection.execute(
                "SELECT accumulated, last_claim_at FROM pet_income WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            now = time.time()
            if not row:
                self.connection.execute(
                    """
                    INSERT INTO pet_income(chat_id, user_id, accumulated, last_claim_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (chat_id, user_id, now),
                )
                self.connection.commit()
                return 0.0, now

            pets = self.connection.execute(
                "SELECT * FROM pets WHERE chat_id = ? AND user_id = ? AND is_egg = 0",
                (chat_id, user_id),
            ).fetchall()
            income_per_sec = sum(
                BASE_INCOME_PER_SEC
                * (1 + r["stench"] / 100)
                * (1 + r["ugliness"] / 100)
                * (1 + r["stickiness"] / 100)
                for r in pets
            )
            elapsed = now - row["last_claim_at"]
            accumulated = row["accumulated"] + income_per_sec * elapsed
            return accumulated, now

    async def claim_pet_income(self, chat_id: int, user_id: int) -> tuple[int, float]:
        async with self.lock:
            accumulated, now = await self.get_pet_income(chat_id, user_id)
            amount = int(accumulated)
            if amount > 0:
                self.connection.execute(
                    "UPDATE balances SET balance = balance + ? WHERE chat_id = ? AND user_id = ?",
                    (amount, chat_id, user_id),
                )
            self.connection.execute(
                """
                INSERT INTO pet_income(chat_id, user_id, accumulated, last_claim_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    accumulated = 0,
                    last_claim_at = excluded.last_claim_at
                """,
                (chat_id, user_id, now),
            )
            self.connection.commit()
            return amount, accumulated

    # ======= Слепой Банк =======

    async def create_bb_room(self, chat_id: int, creator_id: int, creator_name: str) -> int:
        async with self.lock:
            cur = self.connection.execute(
                """
                INSERT INTO blind_bank_rooms(chat_id, status, bank, created_at)
                VALUES (?, 'waiting', 0, ?)
                """,
                (chat_id, time.time()),
            )
            room_id = cur.lastrowid
            self.connection.execute(
                """
                INSERT INTO blind_bank_players(room_id, user_id, name, ready)
                VALUES (?, ?, ?, 0)
                """,
                (room_id, creator_id, creator_name),
            )
            self.connection.commit()
            return room_id

    async def join_bb_room(self, room_id: int, user_id: int, name: str) -> str:
        async with self.lock:
            room = self.connection.execute(
                "SELECT * FROM blind_bank_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if not room:
                return "not_found"
            if room["status"] != "waiting":
                return "started"
            cnt = self.connection.execute(
                "SELECT COUNT(*) FROM blind_bank_players WHERE room_id = ?",
                (room_id,),
            ).fetchone()[0]
            if cnt >= 3:
                return "full"
            if self.connection.execute(
                "SELECT 1 FROM blind_bank_players WHERE room_id = ? AND user_id = ?",
                (room_id, user_id),
            ).fetchone():
                return "already"
            self.connection.execute(
                "INSERT INTO blind_bank_players(room_id, user_id, name, ready) VALUES (?, ?, ?, 0)",
                (room_id, user_id, name),
            )
            self.connection.commit()
            return "full_ready" if cnt + 1 == 3 else "ok"

    async def start_bb_betting(self, room_id: int) -> None:
        async with self.lock:
            self.connection.execute(
                "UPDATE blind_bank_rooms SET status = 'betting' WHERE id = ?",
                (room_id,),
            )
            self.connection.commit()

    async def set_bb_bet(self, room_id: int, user_id: int, bet: int) -> str:
        async with self.lock:
            room = self.connection.execute(
                "SELECT * FROM blind_bank_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if not room or room["status"] != "betting":
                return "invalid"
            if bet not in BET_OPTIONS:
                return "bad_bet"
            bal = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (room["chat_id"], user_id),
            ).fetchone()
            if not bal or bal["balance"] < bet:
                return "funds"
            self.connection.execute(
                """
                UPDATE blind_bank_players SET bet = ?, ready = 1
                WHERE room_id = ? AND user_id = ?
                """,
                (bet, room_id, user_id),
            )
            self.connection.execute(
                "UPDATE balances SET balance = balance - ? WHERE chat_id = ? AND user_id = ?",
                (bet, room["chat_id"], user_id),
            )
            # Добавить в банк
            self.connection.execute(
                "UPDATE blind_bank_rooms SET bank = bank + ? WHERE id = ?",
                (bet, room_id),
            )
            self.connection.commit()
            ready_cnt = self.connection.execute(
                "SELECT COUNT(*) FROM blind_bank_players WHERE room_id = ? AND ready = 1",
                (room_id,),
            ).fetchone()[0]
            return "all_ready" if ready_cnt == 3 else "ok"

    async def assign_bb_strength(self, room_id: int) -> list[dict]:
        async with self.lock:
            players = self.connection.execute(
                "SELECT * FROM blind_bank_players WHERE room_id = ?", (room_id,)
            ).fetchall()
            shares = distribute_strength()
            updates = []
            for i, row in enumerate(players):
                uid = row["user_id"]
                strength = shares[i]
                rng = format_strength_range(strength)
                self.connection.execute(
                    """
                    UPDATE blind_bank_players
                    SET strength = ?, strength_range = ?, ready = 0
                    WHERE room_id = ? AND user_id = ?
                    """,
                    (strength, rng, room_id, uid),
                )
                updates.append(
                    {"user_id": uid, "name": row["name"], "range": rng}
                )
            self.connection.execute(
                """
                UPDATE blind_bank_rooms
                SET status = 'transfer', phase_ends_at = ?
                WHERE id = ?
                """,
                (time.time() + TRANSFER_PHASE_SECONDS, room_id),
            )
            self.connection.commit()
            return updates

    async def set_bb_transfer(
        self, room_id: int, user_id: int, target_id: Optional[int]
    ) -> str:
        async with self.lock:
            room = self.connection.execute(
                "SELECT * FROM blind_bank_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if not room or room["status"] != "transfer":
                return "invalid"
            if room["phase_ends_at"] < time.time():
                return "expired"
            self.connection.execute(
                """
                UPDATE blind_bank_players
                SET transfer_target_id = ?, ready = 1
                WHERE room_id = ? AND user_id = ?
                """,
                (target_id, room_id, user_id),
            )
            self.connection.commit()
            ready_cnt = self.connection.execute(
                "SELECT COUNT(*) FROM blind_bank_players WHERE room_id = ? AND ready = 1",
                (room_id,),
            ).fetchone()[0]
            return "all_ready" if ready_cnt == 3 else "ok"

    async def finish_bb_room(self, room_id: int) -> Optional[dict]:
        async with self.lock:
            room = self.connection.execute(
                "SELECT * FROM blind_bank_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if not room or room["status"] == "finished":
                return None
            rows = self.connection.execute(
                "SELECT * FROM blind_bank_players WHERE room_id = ?", (room_id,)
            ).fetchall()
            player_map = {
                r["user_id"]: BBPlayer(
                    user_id=r["user_id"],
                    name=r["name"],
                    bet=r["bet"],
                    strength=r["strength"],
                    strength_range=r["strength_range"],
                    transfer_target_id=r["transfer_target_id"],
                    ready=bool(r["ready"]),
                )
                for r in rows
            }
            room_obj = BBRoom(
                id=room_id,
                chat_id=room["chat_id"],
                status=room["status"],
                bank=room["bank"],
                players=player_map,
                created_at=room["created_at"],
                phase_ends_at=room["phase_ends_at"],
            )
            winner_id, payouts, desc = resolve_room(room_obj)
            for uid, amount in payouts.items():
                if amount > 0:
                    self.connection.execute(
                        """
                        UPDATE balances SET balance = balance + ?
                        WHERE chat_id = ? AND user_id = ?
                        """,
                        (amount, room["chat_id"], uid),
                    )
            self.connection.execute(
                """
                UPDATE blind_bank_rooms
                SET status = 'finished', winner_id = ?, winner_take_all = ?
                WHERE id = ?
                """,
                (winner_id, 1 if winner_id else 0, room_id),
            )
            self.connection.commit()
            return {
                "winner_id": winner_id,
                "payouts": payouts,
                "description": desc,
                "players": player_map,
                "chat_id": room["chat_id"],
                "bank": room["bank"],
            }

    async def get_bb_room(self, room_id: int) -> Optional[sqlite3.Row]:
        async with self.lock:
            return self.connection.execute(
                "SELECT * FROM blind_bank_rooms WHERE id = ?", (room_id,)
            ).fetchone()

    async def get_bb_players(self, room_id: int) -> list[sqlite3.Row]:
        async with self.lock:
            return self.connection.execute(
                "SELECT * FROM blind_bank_players WHERE room_id = ?", (room_id,)
            ).fetchall()
    
    async def notification_topic_ids(self, chat_id: int) -> list[int]:
        """Вернуть топики, в которые нужно отправить периодическое сообщение."""
        async with self.lock:
            rows = self.connection.execute(
                """
                SELECT topic_id, enabled FROM notification_topics
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchall()
            states = {int(row["topic_id"]): bool(row["enabled"]) for row in rows}
            topic_ids = []
            if states.get(0, True):
                topic_ids.append(0)
            topic_ids.extend(
                sorted(
                    topic_id
                    for topic_id, enabled in states.items()
                    if topic_id != 0 and enabled
                )
            )
            return topic_ids

    async def get_bet_cooldown(self, chat_id: int) -> int:
        """Получить кулдаун ставок казино для конкретного чата."""
        async with self.lock:
            row = self.connection.execute(
                """
                SELECT bet_cooldown_seconds FROM chat_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            return (
                DEFAULT_BET_COOLDOWN_SECONDS
                if row is None
                else int(row["bet_cooldown_seconds"])
            )

    async def set_bet_cooldown(self, chat_id: int, seconds: int) -> None:
        """Установить неотрицательный кулдаун ставок для чата."""
        if seconds < 0:
            raise ValueError("Кулдаун не может быть отрицательным")
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO chat_settings(chat_id, bet_cooldown_seconds)
                VALUES (?, ?)
                ON CONFLICT(chat_id)
                DO UPDATE SET bet_cooldown_seconds = excluded.bet_cooldown_seconds
                """,
                (chat_id, seconds),
            )
            self.connection.commit()

    async def get_or_create(
        self, chat_id: int, user_id: int, display_name: str
    ) -> int:
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO balances(chat_id, user_id, display_name, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET display_name = excluded.display_name
                """,
                (chat_id, user_id, display_name, INITIAL_BALANCE),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return int(row["balance"])

    async def get_assets(self, chat_id: int, user_id: int) -> dict[str, bool]:
        """Вернуть ещё не обменянные ресурсы пользователя."""
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO player_assets(chat_id, user_id) VALUES (?, ?)
                ON CONFLICT(chat_id, user_id) DO NOTHING
                """,
                (chat_id, user_id),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM player_assets WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return {
                asset_name: bool(row[column])
                for asset_name, (column, _emoji, _reward, _title) in ASSETS.items()
            }

    async def redeem_asset(
        self,
        chat_id: int,
        user_id: int,
        display_name: str,
        asset_name: str,
    ) -> tuple[bool, int]:
        """Однократно обменять ресурс на очки и вернуть новый баланс."""
        column, _emoji, reward, _title = ASSETS[asset_name]
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO balances(chat_id, user_id, display_name, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET display_name = excluded.display_name
                """,
                (chat_id, user_id, display_name, INITIAL_BALANCE),
            )
            self.connection.execute(
                """
                INSERT INTO player_assets(chat_id, user_id) VALUES (?, ?)
                ON CONFLICT(chat_id, user_id) DO NOTHING
                """,
                (chat_id, user_id),
            )
            # Имя колонки берётся только из константы ASSETS, не из сообщения.
            cursor = self.connection.execute(
                f"""
                UPDATE player_assets SET {column} = 0
                WHERE chat_id = ? AND user_id = ? AND {column} = 1
                """,
                (chat_id, user_id),
            )
            if cursor.rowcount:
                self.connection.execute(
                    """
                    UPDATE balances SET balance = balance + ?
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (reward, chat_id, user_id),
                )
            self.connection.commit()
            balance = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()["balance"]
            return bool(cursor.rowcount), int(balance)

    async def reserve_bet(
        self,
        chat_id: int,
        user_id: int,
        display_name: str,
        bet: int | None,
        ignore_cooldown: bool = False,
    ) -> tuple[str, int, float, int]:
        """Проверить кулдаун и атомарно списать ставку.

        None вместо суммы означает ставку всего текущего баланса.
        Возвращает статус, баланс, время ожидания и фактическую ставку.
        """
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO balances(chat_id, user_id, display_name, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET display_name = excluded.display_name
                """,
                (chat_id, user_id, display_name, INITIAL_BALANCE),
            )
            row = self.connection.execute(
                """
                SELECT balance, last_bet_at FROM balances
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            ).fetchone()
            now = time.time()
            cooldown_row = self.connection.execute(
                """
                SELECT bet_cooldown_seconds FROM chat_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            cooldown_seconds = (
                DEFAULT_BET_COOLDOWN_SECONDS
                if cooldown_row is None
                else int(cooldown_row["bet_cooldown_seconds"])
            )
            remaining = cooldown_seconds - (now - row["last_bet_at"])
            if not ignore_cooldown and remaining > 0:
                self.connection.commit()
                return "cooldown", int(row["balance"]), remaining, 0

            actual_bet = int(row["balance"]) if bet is None else bet
            if actual_bet <= 0 or row["balance"] < actual_bet:
                self.connection.commit()
                return "insufficient", int(row["balance"]), 0, 0

            self.connection.execute(
                """
                UPDATE balances
                SET balance = balance - ?, display_name = ?, last_bet_at = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (actual_bet, display_name, now, chat_id, user_id),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return "ok", int(row["balance"]), 0, actual_bet

    async def add_points(
        self, chat_id: int, user_id: int, amount: int
    ) -> int:
        """Начислить выплату или вернуть ставку после технической ошибки."""
        async with self.lock:
            self.connection.execute(
                """
                UPDATE balances SET balance = balance + ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (amount, chat_id, user_id),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            return int(row["balance"])

    async def transfer(
        self,
        chat_id: int,
        sender_id: int,
        sender_name: str,
        recipient_id: int,
        recipient_name: str,
        amount: int,
    ) -> tuple[bool, int, int]:
        """Атомарно перевести очки между двумя пользователями одного чата."""
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO balances(chat_id, user_id, display_name, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET display_name = excluded.display_name
                """,
                (chat_id, sender_id, sender_name, INITIAL_BALANCE),
            )
            self.connection.execute(
                """
                INSERT INTO balances(chat_id, user_id, display_name, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET display_name = excluded.display_name
                """,
                (chat_id, recipient_id, recipient_name, INITIAL_BALANCE),
            )
            cursor = self.connection.execute(
                """
                UPDATE balances SET balance = balance - ?
                WHERE chat_id = ? AND user_id = ? AND balance >= ?
                """,
                (amount, chat_id, sender_id, amount),
            )
            if cursor.rowcount:
                self.connection.execute(
                    """
                    UPDATE balances SET balance = balance + ?
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (amount, chat_id, recipient_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO balance_history(
                        chat_id, operation_type,
                        actor_id, actor_name,
                        target_id, target_name,
                        amount, created_at
                    )
                    VALUES (?, 'transfer', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        sender_id,
                        sender_name,
                        recipient_id,
                        recipient_name,
                        amount,
                        time.time(),
                    ),
                )
            self.connection.commit()
            sender_balance = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (chat_id, sender_id),
            ).fetchone()["balance"]
            recipient_balance = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (chat_id, recipient_id),
            ).fetchone()["balance"]
            return bool(cursor.rowcount), int(sender_balance), int(recipient_balance)

    async def create_dice_challenge(
        self,
        chat_id: int,
        proposal_message_id: int,
        challenger_id: int,
        challenger_name: str,
        opponent_id: int,
        opponent_name: str,
        stake: int,
    ) -> None:
        """Сохранить предложение игры, на которое должен ответить соперник."""
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO dice_challenges(
                    chat_id, proposal_message_id,
                    challenger_id, challenger_name,
                    opponent_id, opponent_name,
                    stake, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    chat_id,
                    proposal_message_id,
                    challenger_id,
                    challenger_name,
                    opponent_id,
                    opponent_name,
                    stake,
                    time.time(),
                ),
            )
            self.connection.commit()

    async def accept_dice_challenge(
        self,
        chat_id: int,
        proposal_message_id: int,
        accepting_user_id: int,
        expires_before: float,
    ) -> tuple[str, dict | None]:
        """Принять вызов и атомарно списать ставку у обоих игроков."""
        async with self.lock:
            row = self.connection.execute(
                """
                SELECT * FROM dice_challenges
                WHERE chat_id = ? AND proposal_message_id = ?
                  AND status = 'pending'
                """,
                (chat_id, proposal_message_id),
            ).fetchone()
            if row is None:
                return "not_found", None
            challenge = dict(row)
            if challenge["created_at"] <= expires_before:
                self.connection.execute(
                    """
                    UPDATE dice_challenges SET status = 'expired'
                    WHERE chat_id = ? AND proposal_message_id = ?
                      AND status = 'pending'
                    """,
                    (chat_id, proposal_message_id),
                )
                self.connection.commit()
                return "expired", challenge
            if challenge["opponent_id"] != accepting_user_id:
                return "wrong_user", None

            for user_id, user_name in (
                (challenge["challenger_id"], challenge["challenger_name"]),
                (challenge["opponent_id"], challenge["opponent_name"]),
            ):
                self.connection.execute(
                    """
                    INSERT INTO balances(chat_id, user_id, display_name, balance)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id, user_id)
                    DO UPDATE SET display_name = excluded.display_name
                    """,
                    (chat_id, user_id, user_name, INITIAL_BALANCE),
                )

            balances = {
                row["user_id"]: row["balance"]
                for row in self.connection.execute(
                    """
                    SELECT user_id, balance FROM balances
                    WHERE chat_id = ? AND user_id IN (?, ?)
                    """,
                    (
                        chat_id,
                        challenge["challenger_id"],
                        challenge["opponent_id"],
                    ),
                )
            }
            if balances[challenge["challenger_id"]] < challenge["stake"]:
                self.connection.commit()
                return "challenger_funds", challenge
            if balances[challenge["opponent_id"]] < challenge["stake"]:
                self.connection.commit()
                return "opponent_funds", challenge

            self.connection.execute(
                """
                UPDATE balances SET balance = balance - ?
                WHERE chat_id = ? AND user_id IN (?, ?)
                """,
                (
                    challenge["stake"],
                    chat_id,
                    challenge["challenger_id"],
                    challenge["opponent_id"],
                ),
            )
            self.connection.execute(
                """
                UPDATE dice_challenges SET status = 'playing'
                WHERE chat_id = ? AND proposal_message_id = ?
                """,
                (chat_id, proposal_message_id),
            )
            self.connection.commit()
            return "ok", challenge

    async def expire_dice_challenge(
        self, chat_id: int, proposal_message_id: int
    ) -> bool:
        """Пометить непринятый вызов истёкшим."""
        async with self.lock:
            cursor = self.connection.execute(
                """
                UPDATE dice_challenges SET status = 'expired'
                WHERE chat_id = ? AND proposal_message_id = ?
                  AND status = 'pending'
                """,
                (chat_id, proposal_message_id),
            )
            self.connection.commit()
            return bool(cursor.rowcount)

    async def get_pending_dice_challenges(self) -> list[sqlite3.Row]:
        """Вернуть вызовы, таймеры которых нужно восстановить после запуска."""
        async with self.lock:
            return self.connection.execute(
                """
                SELECT chat_id, proposal_message_id, created_at
                FROM dice_challenges WHERE status = 'pending'
                """
            ).fetchall()

    async def finish_dice_challenge(
        self,
        chat_id: int,
        proposal_message_id: int,
        winner_id: int,
    ) -> int:
        """Передать победителю банк и закрыть игру."""
        async with self.lock:
            challenge = self.connection.execute(
                """
                SELECT stake FROM dice_challenges
                WHERE chat_id = ? AND proposal_message_id = ?
                  AND status = 'playing'
                """,
                (chat_id, proposal_message_id),
            ).fetchone()
            if challenge is None:
                raise RuntimeError("Игра в кости уже завершена")
            self.connection.execute(
                """
                UPDATE balances SET balance = balance + ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (challenge["stake"] * 2, chat_id, winner_id),
            )
            self.connection.execute(
                """
                UPDATE dice_challenges SET status = 'completed'
                WHERE chat_id = ? AND proposal_message_id = ?
                """,
                (chat_id, proposal_message_id),
            )
            self.connection.commit()
            return int(
                self.connection.execute(
                    """
                    SELECT balance FROM balances
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (chat_id, winner_id),
                ).fetchone()["balance"]
            )

    async def refund_dice_challenge(
        self, chat_id: int, proposal_message_id: int
    ) -> None:
        """Вернуть обе ставки, если Telegram не смог отправить кубики."""
        async with self.lock:
            challenge = self.connection.execute(
                """
                SELECT challenger_id, opponent_id, stake
                FROM dice_challenges
                WHERE chat_id = ? AND proposal_message_id = ?
                  AND status = 'playing'
                """,
                (chat_id, proposal_message_id),
            ).fetchone()
            if challenge is None:
                return
            self.connection.execute(
                """
                UPDATE balances SET balance = balance + ?
                WHERE chat_id = ? AND user_id IN (?, ?)
                """,
                (
                    challenge["stake"],
                    chat_id,
                    challenge["challenger_id"],
                    challenge["opponent_id"],
                ),
            )
            self.connection.execute(
                """
                UPDATE dice_challenges SET status = 'failed'
                WHERE chat_id = ? AND proposal_message_id = ?
                """,
                (chat_id, proposal_message_id),
            )
            self.connection.commit()

    async def record_casino_game(
        self,
        chat_id: int,
        player_id: int,
        player_name: str,
        stake: int,
        payout: int,
        slot_value: int,
    ) -> None:
        """Записать завершённую ставку казино."""
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO game_history(
                    chat_id, game_type, player_id, player_name,
                    stake, player_payout, player_result, created_at
                )
                VALUES (?, 'casino', ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    player_id,
                    player_name,
                    stake,
                    payout,
                    slot_value,
                    time.time(),
                ),
            )
            self.connection.commit()

    async def record_dice_game(
        self,
        chat_id: int,
        challenge: dict,
        challenger_roll: int,
        opponent_roll: int,
    ) -> None:
        """Записать партию в кости одной строкой без дублирования."""
        challenger_won = challenger_roll > opponent_roll
        bank = challenge["stake"] * 2
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO game_history(
                    chat_id, game_type,
                    player_id, player_name,
                    opponent_id, opponent_name,
                    stake, player_payout, opponent_payout,
                    player_result, opponent_result, created_at
                )
                VALUES (?, 'dice', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    challenge["challenger_id"],
                    challenge["challenger_name"],
                    challenge["opponent_id"],
                    challenge["opponent_name"],
                    challenge["stake"],
                    bank if challenger_won else 0,
                    0 if challenger_won else bank,
                    challenger_roll,
                    opponent_roll,
                    time.time(),
                ),
            )
            self.connection.commit()

    async def get_next_work_payout_at(self) -> float:
        """Вернуть время следующего фонового начисления."""
        async with self.lock:
            row = self.connection.execute(
                """
                SELECT value FROM scheduler_state
                WHERE key = 'next_work_payout_at'
                """
            ).fetchone()
            return float(row["value"]) if row else 0.0

    async def apply_work_payout(
        self,
        target_chat_id: int | None = None,
        advance_schedule: bool = True,
    ) -> list[dict]:
        """Начислить оплату всем чатам либо одному указанному чату."""
        async with self.lock:
            chat_filter = (
                "" if target_chat_id is None else "AND b.chat_id = ?"
            )
            parameters = () if target_chat_id is None else (target_chat_id,)
            recipients = self.connection.execute(
                f"""
                SELECT b.chat_id, b.user_id, b.display_name
                FROM balances AS b
                JOIN chat_settings AS s ON s.chat_id = b.chat_id
                WHERE s.activated = 1
                    {chat_filter}
                ORDER BY b.chat_id, b.user_id
                """,
                parameters,
            ).fetchall()
            if not recipients:
                return []

            recipients_by_chat: dict[int, list[sqlite3.Row]] = {}
            for row in recipients:
                recipients_by_chat.setdefault(row["chat_id"], []).append(row)

            zero_balance_by_chat: dict[int, list[sqlite3.Row]] = {}
            for chat_id in recipients_by_chat:
                zero_balance_by_chat[chat_id] = self.connection.execute(
                    """
                    SELECT
                        b.user_id,
                        b.display_name,
                        COALESCE(MAX(g.created_at), 0) AS last_game_at
                    FROM balances AS b
                    LEFT JOIN game_history AS g
                        ON g.chat_id = b.chat_id
                        AND (
                            g.player_id = b.user_id
                            OR g.opponent_id = b.user_id
                        )
                    WHERE b.chat_id = ? AND b.balance = 0
                    GROUP BY b.user_id, b.display_name
                    ORDER BY last_game_at DESC, b.user_id DESC
                    LIMIT 10
                    """,
                    (chat_id,),
                ).fetchall()

            now = time.time()
            self.connection.executemany(
                """
                UPDATE balances SET balance = balance + ?
                WHERE chat_id = ? AND user_id = ?
                """,
                [
                    (WORK_PAYOUT_AMOUNT, row["chat_id"], row["user_id"])
                    for row in recipients
                ],
            )

            payouts = []
            for chat_id, chat_recipients in recipients_by_chat.items():
                cursor = self.connection.execute(
                    """
                    INSERT INTO work_payouts(
                        chat_id, amount, recipients_count, created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        WORK_PAYOUT_AMOUNT,
                        len(chat_recipients),
                        now,
                    ),
                )
                payout_id = cursor.lastrowid
                self.connection.executemany(
                    """
                    INSERT INTO work_payout_recipients(
                        payout_id, user_id, display_name
                    )
                    VALUES (?, ?, ?)
                    """,
                    [
                        (payout_id, row["user_id"], row["display_name"])
                        for row in chat_recipients
                    ],
                )
                payouts.append(
                    {
                        "chat_id": chat_id,
                        "recipients_count": len(chat_recipients),
                        "zero_balance_users": [
                            {
                                "user_id": int(row["user_id"]),
                                "display_name": row["display_name"],
                            }
                            for row in zero_balance_by_chat[chat_id]
                        ],
                    }
                )

            if advance_schedule:
                self.connection.execute(
                    """
                    INSERT INTO scheduler_state(key, value)
                    VALUES ('next_work_payout_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(now + WORK_PAYOUT_INTERVAL_SECONDS),),
                )
            self.connection.commit()
            return payouts

    async def get_activity_history(
        self, chat_id: int, user_id: int | None
    ) -> list[dict]:
        """Получить единый журнал игр и операций с балансом."""
        async with self.lock:
            if user_id is None:
                game_rows = self.connection.execute(
                    """
                    SELECT * FROM game_history
                    WHERE chat_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (chat_id, HISTORY_LIMIT),
                ).fetchall()
                balance_rows = self.connection.execute(
                    """
                    SELECT * FROM balance_history
                    WHERE chat_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (chat_id, HISTORY_LIMIT),
                ).fetchall()
                work_rows = self.connection.execute(
                    """
                    SELECT * FROM work_payouts
                    WHERE chat_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (chat_id, HISTORY_LIMIT),
                ).fetchall()
            else:
                game_rows = self.connection.execute(
                    """
                    SELECT * FROM game_history
                    WHERE chat_id = ? AND (player_id = ? OR opponent_id = ?)
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (chat_id, user_id, user_id, HISTORY_LIMIT),
                ).fetchall()
                balance_rows = self.connection.execute(
                    """
                    SELECT * FROM balance_history
                    WHERE chat_id = ? AND (actor_id = ? OR target_id = ?)
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (chat_id, user_id, user_id, HISTORY_LIMIT),
                ).fetchall()
                work_rows = self.connection.execute(
                    """
                    SELECT p.*
                    FROM work_payouts AS p
                    JOIN work_payout_recipients AS r ON r.payout_id = p.id
                    WHERE p.chat_id = ? AND r.user_id = ?
                    ORDER BY p.created_at DESC LIMIT ?
                    """,
                    (chat_id, user_id, HISTORY_LIMIT),
                ).fetchall()

            activities = [
                {"activity_kind": "game", **dict(row)} for row in game_rows
            ]
            activities.extend(
                {"activity_kind": "balance", **dict(row)}
                for row in balance_rows
            )
            activities.extend(
                {"activity_kind": "work", **dict(row)}
                for row in work_rows
            )
            activities.sort(key=lambda row: row["created_at"], reverse=True)
            return activities[:HISTORY_LIMIT]

    async def get_casino_analytics(
        self, chat_id: int, user_id: int | None = None
    ) -> dict | None:
        """Посчитать показатели слота для чата или отдельного игрока."""
        async with self.lock:
            user_filter = "" if user_id is None else "AND player_id = ?"
            parameters = (chat_id,) if user_id is None else (chat_id, user_id)
            row = self.connection.execute(
                f"""
                SELECT
                    COUNT(*) AS games,
                    COALESCE(SUM(stake), 0) AS stakes,
                    COALESCE(SUM(player_payout), 0) AS payouts,
                    COALESCE(SUM(stake - player_payout), 0)
                        AS casino_profit,
                    COALESCE(SUM(
                        CASE WHEN player_payout > 0 THEN 1 ELSE 0 END
                    ), 0) AS winning_games,
                    COALESCE(SUM(
                        CASE WHEN player_payout = 0 THEN 1 ELSE 0 END
                    ), 0) AS losing_games,
                    COALESCE(SUM(
                        CASE WHEN player_payout = 0 THEN stake ELSE 0 END
                    ), 0) AS lost_stakes,
                    MIN(created_at) AS first_game,
                    MAX(created_at) AS last_game
                FROM game_history
                WHERE chat_id = ? AND game_type = 'casino'
                    {user_filter}
                """,
                parameters,
            ).fetchone()
            return dict(row) if row["games"] else None

    async def get_dice_analytics(
        self, chat_id: int, user_id: int
    ) -> dict | None:
        """Посчитать показатели игрока в завершённых партиях в кости."""
        async with self.lock:
            row = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS games,
                    COALESCE(SUM(stake), 0) AS stakes,
                    COALESCE(SUM(
                        CASE
                            WHEN player_id = ? THEN player_payout
                            ELSE opponent_payout
                        END
                    ), 0) AS payouts,
                    COALESCE(SUM(
                        CASE
                            WHEN (
                                player_id = ? AND player_payout > 0
                            ) OR (
                                opponent_id = ? AND opponent_payout > 0
                            )
                            THEN 1 ELSE 0
                        END
                    ), 0) AS wins,
                    MIN(created_at) AS first_game,
                    MAX(created_at) AS last_game
                FROM game_history
                WHERE chat_id = ?
                    AND game_type = 'dice'
                    AND (player_id = ? OR opponent_id = ?)
                """,
                (
                    user_id,
                    user_id,
                    user_id,
                    chat_id,
                    user_id,
                    user_id,
                ),
            ).fetchone()
            return dict(row) if row["games"] else None

    async def seed_users(
        self, chat_id: int, users: list[tuple[int, str]], reset: bool
    ) -> int:
        async with self.lock:
            if reset:
                self.connection.execute(
                    "DELETE FROM balances WHERE chat_id = ?", (chat_id,)
                )
            self.connection.executemany(
                """
                INSERT INTO balances(chat_id, user_id, display_name, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET display_name = excluded.display_name
                """,
                [
                    (chat_id, user_id, display_name, INITIAL_BALANCE)
                    for user_id, display_name in users
                ],
            )
            self.connection.commit()
            return len(users)

    async def known_user_count(self, chat_id: int) -> int:
        """Посчитать пользователей, уже известных боту в этом чате."""
        async with self.lock:
            return int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM balances WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()[0]
            )

    async def reset_known_balances(self, chat_id: int) -> int:
        """Сбросить баланс всех известных боту пользователей чата."""
        async with self.lock:
            cursor = self.connection.execute(
                """
                UPDATE balances
                SET balance = ?, last_bet_at = 0
                WHERE chat_id = ?
                """,
                (INITIAL_BALANCE, chat_id),
            )
            self.connection.commit()
            return int(cursor.rowcount)

    async def change(
        self,
        chat_id: int,
        user_id: int,
        display_name: str,
        amount: int,
        set_value: bool,
        actor_id: int,
        actor_name: str,
    ) -> int:
        async with self.lock:
            current = self.connection.execute(
                "SELECT balance FROM balances WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
            old_balance = int(current["balance"]) if current else INITIAL_BALANCE
            new_balance = amount if set_value else old_balance + amount
            if new_balance < 0:
                raise ValueError("Баланс не может быть отрицательным")
            self.connection.execute(
                """
                INSERT INTO balances(chat_id, user_id, display_name, balance)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    balance = excluded.balance
                """,
                (chat_id, user_id, display_name, new_balance),
            )
            self.connection.execute(
                """
                INSERT INTO balance_history(
                    chat_id, operation_type,
                    actor_id, actor_name,
                    target_id, target_name,
                    amount, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    "set" if set_value else "grant",
                    actor_id,
                    actor_name,
                    user_id,
                    display_name,
                    amount,
                    time.time(),
                ),
            )
            self.connection.commit()
            return new_balance

    async def top(self, chat_id: int) -> list[sqlite3.Row]:
        async with self.lock:
            return self.connection.execute(
                """
                SELECT display_name, balance
                FROM balances WHERE chat_id = ?
                ORDER BY balance DESC, display_name ASC LIMIT 10
                """,
                (chat_id,),
            ).fetchall()


def display_name(user: User) -> str:
    """Получить удобное имя без обязательного username."""
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return full_name or (f"@{user.username}" if user.username else str(user.id))


def format_points(value: int, signed: bool = False) -> str:
    """Отформатировать очки с пробелами и необязательным знаком."""
    text = f"{abs(value):,}".replace(",", " ")
    if not signed:
        return text
    if value > 0:
        return f"+{text}"
    if value < 0:
        return f"−{text}"
    return "0"


def analytics_period(analytics: dict) -> str:
    """Отформатировать период накопленной игровой статистики."""
    first_game = datetime.fromtimestamp(
        analytics["first_game"]
    ).strftime("%d.%m.%Y %H:%M")
    last_game = datetime.fromtimestamp(
        analytics["last_game"]
    ).strftime("%d.%m.%Y %H:%M")
    return f"{first_game} — {last_game}"


def format_chat_casino_analytics(analytics: dict) -> str:
    """Подготовить общую администраторскую аналитику слота."""
    stakes = int(analytics["stakes"])
    payouts = int(analytics["payouts"])
    profit = int(analytics["casino_profit"])
    rtp = payouts / stakes * 100 if stakes else 0.0
    return (
        "📊 Аналитика казино\n"
        f"Период: {analytics_period(analytics)}\n"
        f"Сыграно вращений: {analytics['games']}\n"
        f"Общая сумма ставок: {format_points(stakes)}\n"
        "Полностью проиграно на неудачных вращениях: "
        f"{format_points(int(analytics['lost_stakes']))}\n"
        f"Выплачено выигрышей: {format_points(payouts)}\n"
        f"Чистый результат казино: {format_points(profit, signed=True)}\n"
        f"Выигрышных вращений: {analytics['winning_games']}\n"
        f"Проигрышных вращений: {analytics['losing_games']}\n"
        f"Фактический RTP: {rtp:.2f}%"
    )


def format_player_analytics(
    player_name: str,
    casino_analytics: dict | None,
    dice_analytics: dict | None,
) -> str:
    """Подготовить раздельную статистику игрока по слоту и костям."""
    lines = [f"📊 Аналитика игрока: {player_name}", "", "🎰 Казино"]
    if casino_analytics is None:
        lines.append("Игр пока нет.")
    else:
        stakes = int(casino_analytics["stakes"])
        payouts = int(casino_analytics["payouts"])
        net = payouts - stakes
        rtp = payouts / stakes * 100 if stakes else 0.0
        lines.extend(
            (
                f"Период: {analytics_period(casino_analytics)}",
                f"Вращений: {casino_analytics['games']}",
                f"Ставки: {format_points(stakes)}",
                f"Выплаты: {format_points(payouts)}",
                f"Результат игрока: {format_points(net, signed=True)}",
                f"Выигрышей: {casino_analytics['winning_games']}",
                f"Проигрышей: {casino_analytics['losing_games']}",
                f"RTP игрока: {rtp:.2f}%",
            )
        )

    lines.extend(("", "🎲 Кости"))
    if dice_analytics is None:
        lines.append("Игр пока нет.")
    else:
        games = int(dice_analytics["games"])
        stakes = int(dice_analytics["stakes"])
        payouts = int(dice_analytics["payouts"])
        wins = int(dice_analytics["wins"])
        lines.extend(
            (
                f"Период: {analytics_period(dice_analytics)}",
                f"Партий: {games}",
                f"Ставки: {format_points(stakes)}",
                f"Выплаты: {format_points(payouts)}",
                f"Результат игрока: {format_points(payouts - stakes, signed=True)}",
                f"Побед: {wins}",
                f"Поражений: {games - wins}",
                f"Процент побед: {wins / games * 100:.2f}%",
            )
        )
    return "\n".join(lines)


def format_history(rows: list[dict], viewer_id: int | None) -> str:
    """Подготовить общий журнал игр и операций с балансом."""
    lines = [f"📜 Последние события ({len(rows)}):"]
    for row in rows:
        played_at = datetime.fromtimestamp(row["created_at"]).strftime("%d.%m %H:%M")
        if row["activity_kind"] == "work":
            if viewer_id is None:
                description = (
                    f"начислено {row['recipients_count']} пользователям "
                    f"по {format_points(int(row['amount']))}"
                )
            else:
                description = (
                    f"работа: {format_points(int(row['amount']), signed=True)}"
                )
            lines.append(f"🛠 {played_at} · {description}")
            continue

        if row["activity_kind"] == "balance":
            amount = int(row["amount"])
            if row["operation_type"] == "transfer":
                if viewer_id is not None and row["target_id"] == viewer_id:
                    description = (
                        f"получено от {row['actor_name']}: "
                        f"{format_points(amount, signed=True)}"
                    )
                elif viewer_id is not None:
                    description = (
                        f"передано {row['target_name']}: "
                        f"{format_points(-amount, signed=True)}"
                    )
                else:
                    description = (
                        f"{row['actor_name']} → {row['target_name']}: "
                        f"{format_points(amount)}"
                    )
                lines.append(f"🤝 {played_at} · {description}")
            elif row["operation_type"] == "grant":
                lines.append(
                    f"🎁 {played_at} · {row['actor_name']} выдал "
                    f"{row['target_name']} {format_points(amount)}"
                )
            else:
                lines.append(
                    f"⚙️ {played_at} · {row['actor_name']} установил баланс "
                    f"{row['target_name']}: {format_points(amount)}"
                )
            continue

        stake = int(row["stake"])
        if row["game_type"] == "casino":
            payout = int(row["player_payout"])
            net = payout - stake
            lines.append(
                f"🎰 {played_at} · {row['player_name']}: "
                f"ставка {format_points(stake)}, "
                f"выигрыш {format_points(payout)}, "
                f"итог {format_points(net, signed=True)}"
            )
            continue

        if viewer_id is not None and row["opponent_id"] == viewer_id:
            player_name = row["opponent_name"]
            opponent_name = row["player_name"]
            player_roll = row["opponent_result"]
            opponent_roll = row["player_result"]
            payout = int(row["opponent_payout"])
        else:
            player_name = row["player_name"]
            opponent_name = row["opponent_name"]
            player_roll = row["player_result"]
            opponent_roll = row["opponent_result"]
            payout = int(row["player_payout"])

        if viewer_id is None:
            winner_name = (
                row["player_name"]
                if row["player_payout"]
                else row["opponent_name"]
            )
            lines.append(
                f"🎲 {played_at} · {row['player_name']} {row['player_result']}–"
                f"{row['opponent_result']} {row['opponent_name']}: "
                f"ставка {format_points(stake)}, победитель {winner_name}, "
                f"выигрыш {format_points(stake * 2)}"
            )
        else:
            net = payout - stake
            lines.append(
                f"🎲 {played_at} · {player_name} против {opponent_name} "
                f"{player_roll}–{opponent_roll}: "
                f"ставка {format_points(stake)}, "
                f"выигрыш {format_points(payout)}, "
                f"итог {format_points(net, signed=True)}"
            )
    return "\n".join(lines)


client = TelegramClient(str(BASE_DIR / SESSION_NAME), API_ID, API_HASH)
store = BalanceStore(BASE_DIR / "casino.sqlite3")
admin_id = ADMIN_ID
bot_username = ""
cleanup_tasks: set[asyncio.Task] = set()


def telegram_mention(user_id: int, display_name_value: str) -> str:
    """Сформировать Markdown-упоминание пользователя без username."""
    safe_name = re.sub(r"([\\\[\]])", r"\\\1", display_name_value)
    return f"[{safe_name}](tg://user?id={user_id})"


def work_payout_text(zero_balance_users: list[dict]) -> str:
    """Дополнить уведомление упоминаниями недавних игроков без очков."""
    if not zero_balance_users:
        return WORK_PAYOUT_MESSAGE
    mentions = "\n".join(
        f"{telegram_mention(user['user_id'], user['display_name'])} "
        "— вас особенно касается!"
        for user in zero_balance_users
    )
    return f"{WORK_PAYOUT_MESSAGE}\n\n{mentions}"
async def start_blind_bank_game(chat_id: int, room_id: int):
    await store.start_bb_betting(room_id)
    players = await store.get_bb_players(room_id)
    for p in players:
        try:
            await client.send_message(
                p["user_id"],
                f"🏛 *Слепой Банк* — Комната #{room_id}\n"
                f"Сделайте ставку командой:\n"
                f"`ставка {room_id} 10`, `ставка {room_id} 50` или `ставка {room_id} 100`",
            )
        except Exception:
            pass
    asyncio.create_task(bb_bet_timer(room_id, chat_id))


async def bb_bet_timer(room_id: int, chat_id: int):
    await asyncio.sleep(60)
    room = await store.get_bb_room(room_id)
    if not room or room["status"] != "betting":
        return
    players = await store.get_bb_players(room_id)
    if not all(p["ready"] for p in players):
        for p in players:
            if p["bet"] > 0:
                await store.add_points(chat_id, p["user_id"], p["bet"])
        try:
            await client.send_message(
                chat_id, f"⌛ Комната #{room_id} отменена: не все сделали ставки."
            )
        except Exception:
            pass
        return
    await proceed_bb_strength(room_id, chat_id)


async def proceed_bb_strength(room_id: int, chat_id: int):
    updates = await store.assign_bb_strength(room_id)
    for u in updates:
        try:
            await client.send_message(
                u["user_id"],
                f"🏛 Комната #{room_id} | Фаза силы\n"
                f"Ваша примерная сила: *{u['range']}*\n"
                f"У вас {TRANSFER_PHASE_SECONDS} сек на решение:\n"
                f"`перелив {room_id} ID` — передать 100% силы игроку\n"
                f"`перелив {room_id} 0` — удержать силу",
            )
        except Exception:
            pass
    asyncio.create_task(bb_transfer_timer(room_id, chat_id))


async def bb_transfer_timer(room_id: int, chat_id: int):
    await asyncio.sleep(TRANSFER_PHASE_SECONDS + 2)
    result = await store.finish_bb_room(room_id)
    if not result:
        return
    lines = [f"🏛 *Слепой Банк* — Комната #{room_id} | Результаты"]
    for uid, p in result["players"].items():
        t = ""
        if p.transfer_target_id:
            t_name = result["players"].get(p.transfer_target_id, BBPlayer(0, "?")).name
            t = f" → {t_name}"
        lines.append(
            f"• {p.name}: ставка {p.bet}, сила {p.strength:.1f}%{t}"
        )
    lines.append("")
    lines.append(result["description"])
    for uid, amount in result["payouts"].items():
        if amount > 0:
            lines.append(
                f"💰 {result['players'][uid].name} получает {amount} очков"
            )
    if result["winner_id"]:
        w = result["players"][result["winner_id"]]
        lines.append(
            f"🎭 {w.name} может обмануть партнёра: "
            f"`блеф {room_id} USER_ID СУММА` (в ЛС бота)"
        )
    try:
        await client.send_message(chat_id, "\n".join(lines))
    except Exception:
        pass

async def work_payout_loop() -> None:
    """Раз в полчаса начислять очки всем известным игрокам активных чатов."""
    while True:
        next_payout_at = await store.get_next_work_payout_at()
        delay = next_payout_at - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        payouts = await store.apply_work_payout()
        if not payouts:
            # До первой активации чата начислять некому. Повторяем проверку,
            # не сдвигая расписание, чтобы первое начисление было немедленным.
            await asyncio.sleep(5)
            continue

        await send_work_payout_notifications(payouts)


async def send_work_payout_notifications(payouts: list[dict]) -> int:
    """Отправить уведомления в разрешённые топики и вернуть их число."""
    sent_count = 0
    for payout in payouts:
        topic_ids = await store.notification_topic_ids(payout["chat_id"])
        for topic_id in topic_ids:
            try:
                await client.send_message(
                    payout["chat_id"],
                    work_payout_text(payout["zero_balance_users"]),
                    reply_to=topic_id or None,
                )
                sent_count += 1
            except Exception:
                # Ошибка отправки в один топик не останавливает остальные.
                pass
    return sent_count


async def delete_messages_later(
    chat, chat_id: int, message_ids: tuple[int, ...]
) -> None:
    """Удалить исходящие игровые сообщения через заданное время."""
    if not await store.is_auto_delete_enabled(chat_id):
        return
    await asyncio.sleep(GAME_MESSAGE_TTL_SECONDS)
    if not await store.is_auto_delete_enabled(chat_id):
        return
    try:
        await client.delete_messages(chat, list(message_ids))
    except Exception:
        # Недостаток прав или уже удалённое сообщение не должны останавливать бота.
        pass


def schedule_delete(chat, *messages) -> None:
    """Запланировать удаление и удерживать ссылку на фоновую задачу."""
    message_ids = tuple(
        message.id for message in messages if message is not None
    )
    if not message_ids:
        return
    chat_id = messages[0].chat_id
    task = asyncio.create_task(
        delete_messages_later(chat, chat_id, message_ids)
    )
    cleanup_tasks.add(task)
    task.add_done_callback(cleanup_tasks.discard)


def is_tagged_start(text: str) -> bool:
    """Распознать сообщение с упоминанием бота и словом «старт»."""
    if not bot_username:
        return False
    mention = re.search(
        rf"(?i)(?<!\w)@{re.escape(bot_username)}(?!\w)",
        text,
    )
    return bool(mention and re.search(r"(?iu)\bстарт\b", text))

@client.on(events.NewMessage)
async def blind_bank_dm_handler(event):
    """Приём скрытых ставок и переливов в ЛС."""
    if event.is_group:
        return
    text = event.raw_text or ""
    parts = text.strip().split()
    if len(parts) < 2:
        return

    if parts[0].lower() == "ставка" and len(parts) == 3:
        try:
            room_id = int(parts[1])
            bet = int(parts[2])
        except ValueError:
            return
        result = await store.set_bb_bet(room_id, event.sender_id, bet)
        if result == "ok":
            await event.reply("✅ Ставка принята. Ожидаем остальных...")
        elif result == "all_ready":
            await event.reply("✅ Все ставки сделаны! Распределяем силу...")
            room = await store.get_bb_room(room_id)
            if room:
                await proceed_bb_strength(room_id, room["chat_id"])
        elif result == "funds":
            await event.reply("❌ Недостаточно очков.")
        else:
            await event.reply(f"❌ Ошибка: {result}")
        raise events.StopPropagation

    if parts[0].lower() == "перелив" and len(parts) == 3:
        try:
            room_id = int(parts[1])
            target = int(parts[2])
        except ValueError:
            return
        target_id = target if target != 0 else None
        result = await store.set_bb_transfer(room_id, event.sender_id, target_id)
        if result == "ok":
            await event.reply("✅ Решение принято.")
        elif result == "all_ready":
            await event.reply("✅ Все решения приняты!")
        elif result == "expired":
            await event.reply("⌛ Время на перелив истекло.")
        else:
            await event.reply(f"❌ Ошибка: {result}")
        raise events.StopPropagation

    if parts[0].lower() == "блеф" and len(parts) == 4:
        try:
            room_id = int(parts[1])
            target_user = int(parts[2])
            amount = int(parts[3])
        except ValueError:
            return
        # Перевод от победителя (добровольный, может быть обманом)
        room = await store.get_bb_room(room_id)
        if not room or room["winner_id"] != event.sender_id:
            await event.reply("❌ Вы не победитель этой комнаты.")
            raise events.StopPropagation
        # Получаем имя цели
        try:
            entity = await client.get_entity(target_user)
            target_name = display_name(entity) if isinstance(entity, User) else str(target_user)
        except Exception:
            target_name = str(target_user)
        success, sb, rb = await store.transfer(
            room["chat_id"], event.sender_id, "Победитель", target_user, target_name, amount
        )
        if success:
            await event.reply(
                f"🎭 Переведено {amount} очков {target_name}.\n"
                f"Ваш баланс: {sb}."
            )
            try:
                await client.send_message(
                    target_user,
                    f"🎭 Победитель комнаты #{room_id} перевёл вам {amount} очков.\n"
                    f"Был ли это честный % от банка — вы никогда не узнаете."
                )
            except Exception:
                pass
        else:
            await event.reply("❌ Недостаточно средств.")
        raise events.StopPropagation

@client.on(events.NewMessage)
async def activation_gate(event) -> None:
    """Не пропускать события чата до явной активации администратором."""
    if not event.is_group:
        raise events.StopPropagation

    sender = await event.get_sender()
    if (
        isinstance(sender, User)
        and sender.id == admin_id
        and is_tagged_start(event.raw_text or "")
    ):
        await store.activate_chat(event.chat_id)
        await event.reply("▶️ Бот активирован и принимает команды в этом чате.")
        raise events.StopPropagation

    if not await store.is_chat_activated(event.chat_id):
        raise events.StopPropagation


async def target_from_command(event, args: list[str]) -> tuple[User, int] | None:
    """Разобрать цель админ-команды: reply либо пара <user_id> <очки>."""
    if event.is_reply and len(args) == 1:
        replied = await event.get_reply_message()
        sender = await replied.get_sender()
        if isinstance(sender, User):
            return sender, int(args[0])
    if len(args) == 2:
        entity = await client.get_entity(int(args[0]))
        if isinstance(entity, User):
            return entity, int(args[1])
    return None


async def casino_command(event) -> None:
    """Единый обработчик игровых и административных команд."""
    if not event.is_group:
        await event.reply("Эта игра работает только в групповых чатах.")
        return

    sender = await event.get_sender()
    chat = await event.get_chat()
    if not isinstance(sender, User) or not isinstance(chat, (Chat, Channel)):
        return

    command_line = (event.pattern_match.group(1) or "помощь").strip()
    parts = command_line.split()
    command = parts[0].lower()
    args = parts[1:]
    chat_id = event.chat_id
    topic_id = casino.message_topic_id(event.message)
    name = display_name(sender)

    topic_enabled = await store.is_topic_enabled(chat_id, topic_id)

    # Управление доступно администратору даже в остановленном чате.
    if command in {"стоп", "старт"}:
        if sender.id != admin_id:
            if topic_enabled:
                await event.reply("Эта команда доступна только администратору.")
            return
        enabled = command == "старт"
        await store.set_topic_enabled(chat_id, topic_id, enabled)
        if enabled:
            await event.reply(
                "▶️ Казино и игра в кости запущены в этом разделе."
            )
        else:
            await event.reply(
                "⏸ Казино и игра в кости остановлены в этом разделе."
            )
        return

    if command == "автоудаление":
        if sender.id != admin_id:
            if topic_enabled:
                await event.reply("Эта команда доступна только администратору.")
            return
        auto_delete_enabled = await store.toggle_auto_delete(chat_id)
        state_text = "включено" if auto_delete_enabled else "выключено"
        await event.reply(
            f"🧹 Автоматическое удаление сообщений {state_text} для этого чата."
        )
        return

    if command == "кд":
        if sender.id != admin_id:
            if topic_enabled:
                await event.reply("Эта команда доступна только администратору.")
            return
        if len(args) != 1 or not args[0].isdigit():
            await event.reply("Формат: `кз кд 20` или `каз кд 20`.")
            return
        cooldown_seconds = int(args[0])
        await store.set_bet_cooldown(chat_id, cooldown_seconds)
        if cooldown_seconds:
            await event.reply(
                f"⏱ Кулдаун казино установлен: {cooldown_seconds} сек."
            )
        else:
            await event.reply("⏱ Кулдаун казино отключён для этого чата.")
        return

    # В остановленном чате бот молча игнорирует все остальные команды.
    if not topic_enabled:
        return

    if command == "уведы":
        if sender.id != admin_id:
            await event.reply("Эта команда доступна только администратору.")
            return
        if args:
            await event.reply("Формат: `каз уведы`.")
            return
        topic_id = casino.message_topic_id(event.message)
        notifications_enabled = await store.toggle_topic_notifications(
            chat_id, topic_id
        )
        state_text = "включены" if notifications_enabled else "выключены"
        await event.reply(
            f"🔔 Периодические уведомления {state_text} в этом топике."
        )
        return

    if command == "зп":
        if sender.id != admin_id:
            await event.reply("Эта команда доступна только администратору.")
            return
        if args:
            await event.reply("Формат: `каз зп`.")
            return
        payouts = await store.apply_work_payout(
            target_chat_id=chat_id,
            advance_schedule=False,
        )
        if not payouts:
            await event.reply("В этом чате пока нет известных игроков.")
            return
        notification_topics = await store.notification_topic_ids(chat_id)
        sent_count = await send_work_payout_notifications(payouts)
        if not notification_topics:
            await event.reply(
                "✅ Очки начислены, но уведомления выключены во всех топиках."
            )
        elif not sent_count:
            await event.reply(
                "✅ Очки начислены, но отправить уведомление не удалось."
            )
        return

    if command in {"помощь", "help"}:
        await event.reply(HELP_TEXT)
        return

    if command == "призы":
        await event.reply(casino.prize_table())
        return

    if command == "баланс":
        balance = await store.get_or_create(chat_id, sender.id, name)
        assets = await store.get_assets(chat_id, sender.id)
        asset_icons = " ".join(
            ASSETS[asset_name][1]
            for asset_name, available in assets.items()
            if available
        )
        await event.reply(
            f"💰 {name}: {balance} очков\n"
            f"Ресурсы: {asset_icons or 'нет'}"
        )
        return

    if command == "деп":
        if len(args) != 1 or args[0].lower() not in ASSETS:
            await event.reply(
                "Формат: `каз деп малышка`, `каз деп мать`, "
                "`каз деп тачка` или `каз деп хата`."
            )
            return
        asset_name = args[0].lower()
        redeemed, balance = await store.redeem_asset(
            chat_id, sender.id, name, asset_name
        )
        _column, emoji, reward, title = ASSETS[asset_name]
        if not redeemed:
            await event.reply(f"{emoji} Ресурс «{title}» уже был обменян.")
            return
        reward_text = f"{reward:,}".replace(",", " ")
        await event.reply(
            f"{emoji} Ресурс «{title}» обменян на {reward_text} очков.\n"
            f"Баланс: {balance}."
        )
        return

    if command == "дать":
        if not event.is_reply or len(args) != 1:
            await event.reply(
                "Ответьте на сообщение получателя командой `каз дать 100`."
            )
            return
        try:
            amount = int(args[0])
        except ValueError:
            await event.reply("Сумма перевода должна быть целым числом.")
            return
        if amount <= 0:
            await event.reply("Сумма перевода должна быть больше нуля.")
            return

        replied = await event.get_reply_message()
        recipient = await replied.get_sender()
        if not isinstance(recipient, User) or recipient.bot:
            await event.reply("Переводить очки можно только пользователям.")
            return
        if recipient.id == sender.id:
            await event.reply("Нельзя переводить очки самому себе.")
            return

        recipient_name = display_name(recipient)
        success, sender_balance, recipient_balance = await store.transfer(
            chat_id,
            sender.id,
            name,
            recipient.id,
            recipient_name,
            amount,
        )
        if not success:
            await event.reply(
                f"Недостаточно очков. Текущий баланс: {sender_balance}."
            )
            return
        transfer_message = await event.reply(
            f"🤝 {name} передал {recipient_name} {amount} очков.\n"
            f"Баланс отправителя: {sender_balance}.\n"
            f"Баланс получателя: {recipient_balance}."
        )
        schedule_delete(chat, transfer_message)
        return

    if command == "лог":
        if not args:
            history_user_id = sender.id
        elif len(args) == 1 and args[0].lower() == "все":
            history_user_id = None
        else:
            await event.reply("Формат: `каз лог` или `каз лог все`.")
            return
        rows = await store.get_activity_history(chat_id, history_user_id)
        if not rows:
            await event.reply("История игр пока пуста.")
            return
        await event.reply(format_history(rows, history_user_id))
        return

    if command == "топ":
        rows = await store.top(chat_id)
        if not rows:
            await event.reply("Таблица пока пуста. Используйте `каз раздать`.")
            return
        lines = ["🏆 Балансы чата:"]
        lines.extend(
            f"{index}. {row['display_name']} — {row['balance']}"
            for index, row in enumerate(rows, start=1)
        )
        await event.reply("\n".join(lines))
        return

    if command == "аналитика":
        if args:
            await event.reply(
                "Формат: `каз аналитика` или эта же команда ответом "
                "на сообщение пользователя."
            )
            return
        if casino.is_explicit_message_reply(event.message):
            replied = await event.get_reply_message()
            target_user = await replied.get_sender()
            if not isinstance(target_user, User) or target_user.bot:
                await event.reply(
                    "Ответьте командой на сообщение обычного пользователя."
                )
                return
            casino_analytics = await store.get_casino_analytics(
                chat_id, target_user.id
            )
            dice_analytics = await store.get_dice_analytics(
                chat_id, target_user.id
            )
            await event.reply(
                format_player_analytics(
                    display_name(target_user),
                    casino_analytics,
                    dice_analytics,
                )
            )
            return

        analytics = await store.get_casino_analytics(chat_id)
        if analytics is None:
            await event.reply("Статистика казино в этом чате пока пуста.")
            return
        await event.reply(format_chat_casino_analytics(analytics))
        return

    # Короткая форма «каз 100» равнозначна «каз ставка 100».
    if command.isdigit():
        args = [command]
        command = "ставка"

    all_in = command in {"ва-банк", "вабанк"}
    if all_in:
        command = "ставка"

    if command == "ставка":
        if all_in:
            if args:
                await event.reply("Формат: `каз ва-банк` или `каз вабанк`")
                return
            requested_bet = None
        elif len(args) != 1:
            await event.reply(
                f"Формат: `каз {MIN_BET}` или `каз ставка {MIN_BET}`"
            )
            return
        else:
            try:
                requested_bet = int(args[0])
            except ValueError:
                await event.reply("Ставка должна быть целым числом.")
                return
            if requested_bet < MIN_BET:
                await event.reply(f"Минимальная ставка: {MIN_BET}.")
                return

        # Ставка резервируется до анимации, чтобы параллельными командами
        # нельзя было потратить один и тот же баланс несколько раз.
        (
            bet_status,
            balance_after_bet,
            cooldown_remaining,
            bet,
        ) = await store.reserve_bet(
            chat_id,
            sender.id,
            name,
            requested_bet,
            ignore_cooldown=sender.id == admin_id,
        )
        if bet_status == "cooldown":
            await event.reply(
                "Следующую ставку можно сделать через "
                f"{math.ceil(cooldown_remaining)} сек."
            )
            return
        if bet_status == "insufficient":
            await event.reply(
                f"Недостаточно очков. Текущий баланс: {balance_after_bet}."
            )
            return

        try:
            # InputMediaDice заставляет Telegram самостоятельно сгенерировать
            # и показать нативную анимацию 🎰. Результат приходит в media.value.
            slot_message = await client.send_file(
                chat,
                types.InputMediaDice("🎰"),
                reply_to=event.message,
            )
            if not isinstance(slot_message.media, MessageMediaDice):
                raise RuntimeError("Telegram не вернул результат слота")
            slot_value = slot_message.media.value
            result = casino.decode_slot(slot_value)
            multiplier, prize_title = casino.get_prize(result)
            payout = bet * multiplier

                    # --- ЯЙЦО ПРИ 2 ВИШЕНКАХ ---
        cherry_count = result.count(CHERRY_CODE) if isinstance(result, (tuple, list)) else 0
        if cherry_count == 2:
            spec = await store.get_or_create_spec(chat_id, sender.id)
            egg = create_pure_pet(spec, roll_egg_value(), 0)
            egg_success = await store.add_pet(chat_id, sender.id, egg)
            if egg_success:
                egg_msg = await event.reply(
                    f"🍒🍒 *Двойная вишня!* Из слота выпало 🥚 Яйцо ({SPEC_NAMES[spec]})!\n"
                    f"Оно заняло свободный слот на ферме. `каз ферма` для просмотра."
                )
                schedule_delete(chat, egg_msg)
        # --- КОНЕЦ ЯЙЦА ---
        
        except Exception:
            # При ошибке отправки пользователь не должен терять ставку.
            balance = await store.add_points(chat_id, sender.id, bet)
            await event.reply(
                "Не удалось запустить слот. Ставка возвращена.\n"
                f"Баланс: {balance}"
            )
            return

        # Значение известно сразу, но ответ ждёт окончания анимации клиента.
        await asyncio.sleep(casino.SLOT_ANIMATION_SECONDS)
        if payout:
            balance = await store.add_points(chat_id, sender.id, payout)
            await store.record_casino_game(
                chat_id, sender.id, name, bet, payout, slot_value
            )
            net = payout - bet
            await event.reply(
                f"{prize_title}! Выплата: {payout} "
                f"(чистый результат: +{net}).\n"
                f"Баланс: {balance}"
            )
        else:
            await store.record_casino_game(
                chat_id, sender.id, name, bet, 0, slot_value
            )
            result_message = await event.reply(
                f"Комбинация не сыграла. Списано: {bet}.\n"
                f"Баланс: {balance_after_bet}"
            )
            schedule_delete(chat, slot_message, result_message)
        return

    # Всё ниже доступно только аккаунту из ADMIN_ID.
    if sender.id != admin_id:
        await event.reply("Эта команда доступна только администратору.")
        return

    if command in {"раздать", "сброс"}:
        if command == "сброс":
            count = await store.reset_known_balances(chat_id)
            await event.reply(
                f"✅ Балансы сброшены. Пользователей обработано: {count}. "
                f"Начальный баланс: {INITIAL_BALANCE}."
            )
        else:
            count = await store.known_user_count(chat_id)
            await event.reply(
                f"✅ Начальный баланс автоматически выдаётся при первом "
                f"обращении пользователя. Уже известных пользователей: {count}."
            )
        return

    if command in {"выдать", "установить"}:
        try:
            target = await target_from_command(event, args)
        except (ValueError, TypeError):
            target = None
        if target is None:
            await event.reply(
                f"Ответьте на сообщение командой `каз {command} 500` "
                f"или укажите `каз {command} USER_ID 500`."
            )
            return
        user, amount = target
        if amount < 0:
            await event.reply("Укажите неотрицательное количество очков.")
            return
        try:
            balance = await store.change(
                chat_id,
                user.id,
                display_name(user),
                amount,
                set_value=command == "установить",
                actor_id=sender.id,
                actor_name=name,
            )
        except ValueError as error:
            await event.reply(str(error))
            return
        grant_message = await event.reply(
            f"✅ {display_name(user)}: новый баланс {balance}."
        )
        schedule_delete(chat, grant_message)
        return

        # ========== ГЕНЕТИЧЕСКАЯ МЕРЗОСТЬ ==========
    if command == "ферма":
        sub = args[0].lower() if args else "show"
        if sub == "show" or not args:
            spec = await store.get_or_create_spec(chat_id, sender.id)
            spec_emoji = SPEC_EMOJI[spec]
            spec_name = SPEC_NAMES[spec]
            pet_rows = await store.get_pets(chat_id, sender.id)
            accumulated, _ = await store.get_pet_income(chat_id, sender.id)
            lines = [
                f"🧬 *Генетическая Мерзость* | Специализация: {spec_emoji} {spec_name}",
                f"💰 Накоплено: `{accumulated:.1f}` очков (`каз ферма собрать`)",
                ""
            ]
            for i in range(MAX_SLOTS):
                row = next((r for r in pet_rows if r["slot_index"] == i), None)
                if row:
                    pet = Pet(
                        slot_index=row["slot_index"], name=row["name"],
                        stench=row["stench"], ugliness=row["ugliness"],
                        stickiness=row["stickiness"], generation=row["generation"],
                        is_egg=bool(row["is_egg"]), egg_hatch_at=row["egg_hatch_at"],
                        created_at=row["created_at"]
                    )
                    lines.append(pet.display(i))
                else:
                    lines.append(f"{SLOT_EMOJIS[i]} [Пусто]")
            lines.append("")
            lines.append(
                "Команды: `каз скрестить 1 2`, `каз ферма вылупить N`, "
                "`каз ферма собрать`, `каз ферма переименовать N Имя`"
            )
            await event.reply("\n".join(lines))
            return

        if sub == "собрать":
            amount, _ = await store.claim_pet_income(chat_id, sender.id)
            if amount > 0:
                await event.reply(f"🌾 Собрано `{amount}` очков с фермы!")
            else:
                await event.reply("🌾 Пока ничего не созрело. Подождите.")
            return

        if sub == "вылупить":
            if len(args) < 2 or not args[1].isdigit():
                await event.reply("Формат: `каз ферма вылупить 1`")
                return
            slot = int(args[1]) - 1
            if slot < 0 or slot >= MAX_SLOTS:
                await event.reply("Неверный слот. Используйте 1–4.")
                return
            success = await store.hatch_egg(chat_id, sender.id, slot)
            if success:
                await event.reply(f"🐣 Яйцо в слоте {slot+1} вылупилось!")
            else:
                await event.reply("🥚 Яйцо ещё не готово или слот пуст.")
            return

        if sub == "переименовать":
            if len(args) < 3:
                await event.reply("Формат: `каз ферма переименовать 1 Бугор`")
                return
            slot = int(args[1]) - 1
            new_name = " ".join(args[2:])
            success = await store.rename_pet(chat_id, sender.id, slot, new_name)
            if success:
                await event.reply(f"✅ Питомец в слоте {slot+1} теперь зовётся *{new_name}*!")
            else:
                await event.reply("❌ Не удалось переименовать. Проверьте слот.")
            return

    if command == "скрестить":
        if len(args) != 2 or not all(a.isdigit() for a in args):
            await event.reply("Формат: `каз скрестить 1 2` (слоты 1–4)")
            return
        s1, s2 = int(args[0]) - 1, int(args[1]) - 1
        if s1 == s2 or not (0 <= s1 < MAX_SLOTS and 0 <= s2 < MAX_SLOTS):
            await event.reply("Выберите два разных слота от 1 до 4.")
            return

        rows = await store.get_pets(chat_id, sender.id)
        r1 = next((r for r in rows if r["slot_index"] == s1), None)
        r2 = next((r for r in rows if r["slot_index"] == s2), None)
        if not r1 or not r2:
            await event.reply("Оба слота должны быть заняты.")
            return
        if r1["is_egg"] or r2["is_egg"]:
            await event.reply("Нельзя скрещивать яйца. Дождитесь вылупления.")
            return

        p1 = Pet(slot_index=s1, name=r1["name"], stench=r1["stench"],
                 ugliness=r1["ugliness"], stickiness=r1["stickiness"],
                 generation=r1["generation"], is_egg=False, egg_hatch_at=0, created_at=0)
        p2 = Pet(slot_index=s2, name=r2["name"], stench=r2["stench"],
                 ugliness=r2["ugliness"], stickiness=r2["stickiness"],
                 generation=r2["generation"], is_egg=False, egg_hatch_at=0, created_at=0)
        try:
            child = breed(p1, p2)
        except ValueError as e:
            await event.reply(f"❌ {e}")
            return

        await store.remove_pet(chat_id, sender.id, s1)
        await store.remove_pet(chat_id, sender.id, s2)
        child.slot_index = min(s1, s2)
        await store.add_pet(chat_id, sender.id, child)
        await event.reply(
            f"🔬 Скрещивание завершено!\n"
            f"Родители ушли на покой. В слоте {min(s1,s2)+1} появилось яйцо.\n"
            f"Предсказуемые гены: 💨{child.stench} 🤢{child.ugliness} 🍯{child.stickiness} "
            f"({'Гибрид' if child.generation == 1 else 'Чистый'})"
        )
        return

    # ========== СЛЕПОЙ БАНК ==========
    if command == "банк":
        if not args:
            await event.reply(
                "🏛 *Слепой Банк*\n"
                "`каз банк создать` — создать игру на 3 игрока\n"
                "`каз банк вступить <ID>` — присоединиться"
            )
            return

        sub = args[0].lower()
        if sub == "создать":
            room_id = await store.create_bb_room(chat_id, sender.id, name)
            await event.reply(
                f"🏛 Комната `#{room_id}` создана!\n"
                f"Ожидаем 2 игроков. `каз банк вступить {room_id}`"
            )
            return

        if sub == "вступить":
            if len(args) < 2 or not args[1].isdigit():
                await event.reply("Формат: `каз банк вступить 123`")
                return
            room_id = int(args[1])
            result = await store.join_bb_room(room_id, sender.id, name)
            if result == "not_found":
                await event.reply("❌ Комната не найдена.")
            elif result == "started":
                await event.reply("❌ Игра уже началась.")
            elif result == "full":
                await event.reply("❌ Комната заполнена.")
            elif result == "already":
                await event.reply("❌ Вы уже в комнате.")
            elif result == "ok":
                await event.reply(f"✅ Вы вошли в комнату `#{room_id}`. Ожидаем остальных.")
            elif result == "full_ready":
                await event.reply(f"✅ Комната `#{room_id}` заполнена! Игра начинается…")
                await start_blind_bank_game(chat_id, room_id)
            return
    
    await event.reply("Неизвестная команда. Используйте `каз помощь`.")


restore_dice_expirations = dice.register(
    client, store, display_name, schedule_delete
)
casino.register(client, casino_command)
guess_sound.register(
    client,
    FreesoundProvider(FREESOUND_API_KEY),
    BASE_DIR / "guess_sound.sqlite3",
    display_name,
    lambda: admin_id,
)


async def main() -> None:
    global bot_username
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    if not me.bot:
        raise RuntimeError(
            "SESSION_NAME указывает на пользовательскую сессию. "
            "Укажите новое имя сессии, например casino_bot."
        )
    if not me.username:
        raise RuntimeError("У Telegram-бота отсутствует username")
    bot_username = me.username
    await restore_dice_expirations()
    payout_task = asyncio.create_task(work_payout_loop())
    cleanup_tasks.add(payout_task)
    payout_task.add_done_callback(cleanup_tasks.discard)
    print(
        f"Бот @{bot_username} запущен. "
        f"Администратор: {admin_id}."
    )
    print("Для остановки нажмите Ctrl+C.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

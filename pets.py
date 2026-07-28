"""Система питомцев и селекции «Генетическая Мерзость»."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import List, Optional

SPECS = ["stench", "ugliness", "stickiness"]
SPEC_EMOJI = {
    "stench": "💨",
    "ugliness": "🤢",
    "stickiness": "🍯",
}
SPEC_NAMES = {
    "stench": "Вонь",
    "ugliness": "Уродство",
    "stickiness": "Липкость",
}

BASE_INCOME_PER_SEC = 0.1
EGG_HATCH_SECONDS = 3600
MAX_SLOTS = 4
SLOT_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]


@dataclass
class Pet:
    slot_index: int
    name: str
    stench: int
    ugliness: int
    stickiness: int
    generation: int          # 0 = чистопородный, 1 = гибрид (стерильный)
    is_egg: bool
    egg_hatch_at: float
    created_at: float

    def income_per_sec(self) -> float:
        if self.is_egg:
            return 0.0
        return (
            BASE_INCOME_PER_SEC
            * (1 + self.stench / 100)
            * (1 + self.ugliness / 100)
            * (1 + self.stickiness / 100)
        )

    def main_spec(self) -> str:
        vals = [
            (self.stench, "stench"),
            (self.ugliness, "ugliness"),
            (self.stickiness, "stickiness"),
        ]
        return max(vals)[1]

    def display(self, index: int) -> str:
        slot = SLOT_EMOJIS[index]
        if self.is_egg:
            remaining = max(0, self.egg_hatch_at - time.time())
            if remaining > 0:
                mins, secs = divmod(int(remaining), 60)
                return f"{slot} 🥚 Яйцо (вылупится через {mins:02d}:{secs:02d})"
            return (
                f"{slot} 🥚 Яйцо (готово! "
                f"`каз ферма вылупить {index + 1}`)"
            )
        gen = "♻️Гибрид" if self.generation == 1 else "Чистый"
        return (
            f"{slot} {self.name} ({gen})\n"
            f"   💨{self.stench} 🤢{self.ugliness} 🍯{self.stickiness} "
            f"| Доход: {self.income_per_sec():.3f}/сек"
        )


def roll_spec() -> str:
    return random.choice(SPECS)


def roll_egg_value() -> int:
    val = int(random.gauss(10, 4))
    return max(1, min(20, val))


def gaussian_breed_value(p1_val: int, p2_val: int) -> int:
    median = (p1_val + p2_val) / 2.0
    noise = random.gauss(0, 5)
    max_parent = max(p1_val, p2_val)
    raw = int(median + noise)
    # Жёсткий лимит: не более +33 от максимального родителя, минимум 1
    capped = min(raw, max_parent + 33)
    return max(1, min(100, capped))


def create_pure_pet(spec: str, value: int, slot_index: int) -> Pet:
    stats = {"stench": 1, "ugliness": 1, "stickiness": 1}
    stats[spec] = max(1, min(100, value))
    return Pet(
        slot_index=slot_index,
        name="Мутант",
        stench=stats["stench"],
        ugliness=stats["ugliness"],
        stickiness=stats["stickiness"],
        generation=0,
        is_egg=True,
        egg_hatch_at=time.time() + EGG_HATCH_SECONDS,
        created_at=time.time(),
    )


def breed(p1: Pet, p2: Pet) -> Pet:
    if p1.generation == 1 or p2.generation == 1:
        raise ValueError("Гибриды стерильны и не могут участвовать в селекции!")

    stench = gaussian_breed_value(p1.stench, p2.stench)
    ugliness = gaussian_breed_value(p1.ugliness, p2.ugliness)
    stickiness = gaussian_breed_value(p1.stickiness, p2.stickiness)

    # Определение поколения
    p1_main = p1.main_spec()
    p2_main = p2.main_spec()
    if p1.generation == 0 and p2.generation == 0 and p1_main == p2_main:
        generation = 0
    else:
        generation = 1

    return Pet(
        slot_index=-1,
        name="Мутант",
        stench=stench,
        ugliness=ugliness,
        stickiness=stickiness,
        generation=generation,
        is_egg=True,
        egg_hatch_at=time.time() + EGG_HATCH_SECONDS,
        created_at=time.time(),
    )

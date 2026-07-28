"""Кооперативная игра «Слепой Банк» на 3 игроков."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

BET_OPTIONS = [10, 50, 100]
TRANSFER_PHASE_SECONDS = 15


@dataclass
class BBPlayer:
    user_id: int
    name: str
    bet: int = 0
    strength: float = 0.0
    strength_range: str = ""
    transfer_target_id: Optional[int] = None
    ready: bool = False


@dataclass
class BBRoom:
    id: int
    chat_id: int
    status: str = "waiting"
    bank: int = 0
    players: Dict[int, BBPlayer] = field(default_factory=dict)
    created_at: float = 0.0
    phase_ends_at: float = 0.0
    winner_id: Optional[int] = None
    winner_take_all: bool = False


def distribute_strength() -> List[float]:
    while True:
        a, b, c = random.random(), random.random(), random.random()
        s = a + b + c
        shares = [a / s * 100, b / s * 100, c / s * 100]
        if all(x <= 50 for x in shares):
            return shares


def format_strength_range(strength: float) -> str:
    low = max(0.0, strength - 10)
    high = min(100.0, strength + 10)
    return f"{low:.1f}% – {high:.1f}%"


def resolve_room(room: BBRoom) -> Tuple[Optional[int], Dict[int, int], str]:
    final_strength = {uid: p.strength for uid, p in room.players.items()}

    # Применить тайные переливы
    for uid, p in room.players.items():
        if p.transfer_target_id is not None and p.transfer_target_id in final_strength:
            final_strength[p.transfer_target_id] += final_strength[uid]
            final_strength[uid] = 0.0

    max_uid = max(final_strength, key=final_strength.get)
    max_val = final_strength[max_uid]

    if max_val >= 55:
        winner = max_uid
        payouts = {uid: 0 for uid in room.players}
        payouts[winner] = room.bank
        desc = (
            f"🏆 {room.players[winner].name} набрал {max_val:.1f}% "
            f"и забирает весь банк {room.bank}!"
        )
    else:
        winner = None
        total = sum(final_strength.values())
        payouts = {}
        for uid, val in final_strength.items():
            payouts[uid] = int(room.bank * (val / total)) if total > 0 else 0
        desc = "💰 Банк разделён пропорционально фактической силе."

    return winner, payouts, desc

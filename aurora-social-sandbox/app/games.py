import secrets
from dataclasses import dataclass

@dataclass(frozen=True)
class GameDef:
    key: str
    name: str
    version: str
    rtp: float
    max_multiplier: int
    description: str

GAMES = {
    "prism-flip": GameDef("prism-flip", "Prism Flip", "1.0.0", 0.98, 2, "49% chance to pay 2× stake."),
    "nova-roll": GameDef("nova-roll", "Nova Roll", "1.0.0", 0.96, 6, "16% chance to pay 6× stake."),
    "comet-24": GameDef("comet-24", "Comet 24", "1.0.0", 0.96, 24, "4% chance to pay 24× stake."),
}

def settle(game_key: str, stake: int):
    game = GAMES[game_key]
    draw = secrets.randbelow(10_000)
    if game_key == "prism-flip":
        win = draw < 4_900
        mult = 2 if win else 0
    elif game_key == "nova-roll":
        win = draw < 1_600
        mult = 6 if win else 0
    else:
        win = draw < 400
        mult = 24 if win else 0
    return {
        "draw": draw,
        "range": 10_000,
        "won": win,
        "multiplier": mult,
        "payout": stake * mult,
        "game_version": game.version,
    }

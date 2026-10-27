"""Rule-based transaction categories and transfer detection.

Merchant category codes (MCC) win when the bank provides one; otherwise the first keyword
rule that matches the merchant/description decides. Order matters: "uber eats" must hit
eating out before "uber" hits transport, "amazon prime" subscriptions before "amazon" shopping.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

CATEGORIES = ("groceries", "eating_out", "transport", "subscriptions", "shopping", "travel", "other")

# fmt: off
KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "travel",
        (
            "ryanair", "vueling", "iberia", "easyjet", "air europa", "lufthansa", "volotea", "wizz air",
            "british airways", "klm", "air france", "tap air", "booking.com", "airbnb", "hotel", "hostel",
            "expedia", "edreams", "skyscanner", "trainline", "aena", "airport", "aeropuerto",
        ),
    ),
    (
        "subscriptions",
        (
            "netflix", "spotify", "apple.com/bill", "icloud", "google one", "google storage", "youtube premium",
            "disney plus", "disney+", "hbo", "prime video", "amazon prime", "audible", "chatgpt", "openai",
            "anthropic", "claude.ai", "notion", "github", "dropbox", "adobe", "microsoft 365", "xbox",
            "playstation", "nintendo", "patreon", "dazn", "basic-fit", "basic fit", "gym", "movistar",
            "vodafone", "orange", "digi mobil", "railway", "revolut premium", "revolut plus", "revolut metal",
            "plan fee", "microsoft", "1global", "skool.com",
        ),
    ),
    (
        "eating_out",
        (
            "uber eats", "ubereats", "uber *eats", "glovo", "just eat", "deliveroo", "restaurant", "restaurante",
            "cafe", "café", "cafeteria", "coffe", "starbucks", "mcdonald", "burger", "kfc", "telepizza", "domino",
            "pizza", "sushi", "taberna", "tapas", "cerveceria", "100 montaditos", "vips", "goiko", "five guys",
            "taco bell", "foster", "pans & company", " bar ", " food ", "pasta", " wok ", "pomodoro", "torre ie",
        ),
    ),
    (
        "transport",
        (
            "uber", "cabify", "bolt", "freenow", "free now", "taxi", "renfe", "ouigo", "iryo", "metro",
            " emt ", " tmb ", " alsa ", "bicing", " lime ", "blablacar", "repsol", "cepsa", "galp", "shell",
            " bp ", "gasolinera", "parking", "crtm",
            "empresa malaguena de t", "rail vending", "aparcamiento", "peaje", "autopista", "toll",
        ),
    ),
    (
        "groceries",
        (
            "mercadona", "carrefour", "lidl", "aldi", "eroski", "alcampo", "consum", "hipercor", "supercor",
            "condis", "bonpreu", "caprabo", "ahorramas", "bm supermercados", "froiz", "gadis", "masymas",
            " spar ", "tesco", "sainsbury", "waitrose", "auchan", "supermercado", "supermarket", "grocery",
            "fruteria", "carniceria", "panaderia", " dia ", "dia retail",
        ),
    ),
    (
        "shopping",
        (
            "amazon", "amzn", "zara", "primark", "pull and bear", "steamgames",
            "el corte ingles", "ikea", "decathlon", "fnac", "mediamarkt",
            "media markt", "aliexpress", "shein", "h&m", "mango", "pull&bear", "bershka", "uniqlo",
            "apple store", "pccomponentes", "leroy merlin", "flying tiger", "druni", "primor",
            "sephora", "game stop", "el ganso",
        ),
    ),
)

# fmt: on

# MCC ranges -> category (ISO 18245). Checked before keywords.
MCC_RULES: tuple[tuple[int, int, str], ...] = (
    (3000, 3350, "travel"),  # airlines
    (3351, 3500, "transport"),  # car rental
    (3501, 3999, "travel"),  # hotels
    (4111, 4131, "transport"),
    (4411, 4411, "travel"),
    (4511, 4511, "travel"),
    (4722, 4722, "travel"),
    (4784, 4789, "transport"),
    (4899, 4899, "subscriptions"),
    (5411, 5411, "groceries"),
    (5422, 5499, "groceries"),
    (5541, 5542, "transport"),
    (5812, 5814, "eating_out"),
    (5815, 5818, "subscriptions"),
    (7011, 7011, "travel"),
    (7523, 7523, "transport"),
    (7997, 7997, "subscriptions"),
    (5200, 5999, "shopping"),
)

TRADING212_PATTERNS = ("trading 212", "trading212", " t212 ")

# Revolut wording for moves between your own accounts, pockets and vaults.
INTERNAL_PATTERNS = (
    "pocket",
    "vault",
    "savings account",
    "exchanged to",
    "exchanged from",
    "exchange to",
    "own account",
    "between your accounts",
    "transfer to revolut account",
    "balance was still negative",  # Revolut auto-covering a negative balance from another pocket
    " mb:",  # "To EUR MB:<id>": Revolut money-box (pocket) reference
)

# "To EUR", "From USD ...": Revolut's own wording for moves between currency accounts/pockets.
CURRENCY_TRANSFER = re.compile(r"^ (to|from) (eur|usd|gbp|jpy|aed|chf|pln|sek|nok|dkk|czk|huf|ron|cad|aud)\b")


def _norm(text: str | None) -> str:
    return " " + " ".join((text or "").lower().split()) + " "


def category_for(text: str, mcc: str | int | None = None) -> str:
    code = _mcc(mcc)
    if code is not None:
        for lo, hi, cat in MCC_RULES:
            if lo <= code <= hi:
                return cat
    hay = _norm(text)
    for cat, words in KEYWORD_RULES:
        if any(w in hay for w in words):
            return cat
    return "other"


def is_trading212(text: str) -> bool:
    hay = _norm(text)
    return any(p in hay for p in TRADING212_PATTERNS)


def is_internal(text: str, own_identifiers: Iterable[str]) -> bool:
    """True for transfers between the owner's own accounts/pockets.

    `own_identifiers` are lowercase account names, labels, IBANs and owner names. Short
    generic names are ignored so a pocket called "Food" does not swallow every grocery shop:
    they only match as "to food" / "from food".
    """
    hay = _norm(text)
    if any(p in hay for p in INTERNAL_PATTERNS) or CURRENCY_TRANSFER.search(hay):
        return True
    compact = hay.replace(" ", "")
    for ident in own_identifiers:
        ident = ident.strip().lower()
        if not ident:
            continue
        if _looks_like_iban(ident):
            if ident.replace(" ", "") in compact:
                return True
        elif (len(ident) >= 8 and ident in hay) or f" to {ident} " in hay or f" from {ident} " in hay:
            return True
    return False


def merchant_key(name: str) -> str:
    """Group 'AMZN Mktp ES*1A2B3' and 'AMZN Mktp ES*9Z8Y7' together."""
    text = " ".join(name.split())
    text = re.sub(r"\s*[*#]\s*[A-Za-z0-9]*\d[A-Za-z0-9]*$", "", text)
    text = re.sub(r"[\s-]+\d{3,}$", "", text)
    return text.strip() or name.strip()


def _looks_like_iban(text: str) -> bool:
    return bool(re.fullmatch(r"[a-z]{2}\d{2}[a-z0-9 ]{10,40}", text))


def _mcc(value: str | int | None) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None

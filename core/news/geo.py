"""Countries, regions and topics for the World Intelligence Globe (SPEC §13, deterministic).

No model is involved: country detection is a keyword table (English and German names, demonyms,
capitals of the larger countries), topics are keyword sets. Centroids (lat/lon) drive the globe
markers; regions drive the region layer. Extend the table as needed - it is data, not logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    iso: str
    name: str
    region: str
    lat: float
    lon: float
    aliases: tuple[str, ...] = ()


# iso, name, region, lat, lon, aliases (lower-case, matched on word boundaries)
_TABLE: tuple[tuple[str, str, str, float, float, tuple[str, ...]], ...] = (
    (
        "US",
        "United States",
        "North America",
        39.8,
        -98.6,
        (
            "usa",
            "u.s.",
            "america",
            "american",
            "washington",
            "vereinigte staaten",
            "amerika",
            "amerikanisch",
            "white house",
            "pentagon",
        ),
    ),
    ("CA", "Canada", "North America", 56.1, -106.3, ("kanada", "canadian", "kanadisch", "ottawa")),
    ("MX", "Mexico", "North America", 23.6, -102.5, ("mexiko", "mexican", "mexikanisch")),
    (
        "BR",
        "Brazil",
        "South America",
        -14.2,
        -51.9,
        ("brasilien", "brazilian", "brasilianisch", "brasília", "brasilia"),
    ),
    (
        "AR",
        "Argentina",
        "South America",
        -38.4,
        -63.6,
        ("argentinien", "argentine", "argentinisch", "buenos aires"),
    ),
    ("CL", "Chile", "South America", -35.7, -71.5, ("chilean", "chilenisch", "santiago de chile")),
    (
        "CO",
        "Colombia",
        "South America",
        4.6,
        -74.3,
        ("kolumbien", "colombian", "kolumbianisch", "bogotá", "bogota"),
    ),
    ("VE", "Venezuela", "South America", 6.4, -66.6, ("venezuelan", "caracas")),
    (
        "GB",
        "United Kingdom",
        "Europe",
        55.4,
        -3.4,
        (
            "uk",
            "britain",
            "british",
            "england",
            "großbritannien",
            "grossbritannien",
            "britisch",
            "london",
            "scotland",
            "schottland",
            "wales",
        ),
    ),
    ("IE", "Ireland", "Europe", 53.1, -8.2, ("irland", "irish", "irisch", "dublin")),
    (
        "FR",
        "France",
        "Europe",
        46.2,
        2.2,
        ("frankreich", "french", "französisch", "franzoesisch", "paris"),
    ),
    (
        "DE",
        "Germany",
        "Europe",
        51.2,
        10.5,
        (
            "deutschland",
            "german",
            "deutsch",
            "berlin",
            "bundesregierung",
            "bundestag",
            "munich",
            "münchen",
            "hamburg",
            "frankfurt",
        ),
    ),
    (
        "AT",
        "Austria",
        "Europe",
        47.5,
        14.6,
        ("österreich", "oesterreich", "austrian", "österreichisch", "wien", "vienna"),
    ),
    (
        "CH",
        "Switzerland",
        "Europe",
        46.8,
        8.2,
        ("schweiz", "swiss", "schweizer", "bern", "zürich", "zurich", "geneva", "genf"),
    ),
    (
        "NL",
        "Netherlands",
        "Europe",
        52.1,
        5.3,
        ("niederlande", "dutch", "niederländisch", "holland", "amsterdam", "den haag", "the hague"),
    ),
    (
        "BE",
        "Belgium",
        "Europe",
        50.5,
        4.5,
        ("belgien", "belgian", "belgisch", "brussels", "brüssel"),
    ),
    (
        "ES",
        "Spain",
        "Europe",
        40.5,
        -3.7,
        ("spanien", "spanish", "spanisch", "madrid", "barcelona"),
    ),
    ("PT", "Portugal", "Europe", 39.4, -8.2, ("portuguese", "portugiesisch", "lisbon", "lissabon")),
    (
        "IT",
        "Italy",
        "Europe",
        41.9,
        12.6,
        ("italien", "italian", "italienisch", "rome", "rom", "milan", "mailand"),
    ),
    ("PL", "Poland", "Europe", 51.9, 19.1, ("polen", "polish", "polnisch", "warsaw", "warschau")),
    (
        "CZ",
        "Czechia",
        "Europe",
        49.8,
        15.5,
        ("tschechien", "czech", "tschechisch", "prague", "prag"),
    ),
    ("SE", "Sweden", "Europe", 60.1, 18.6, ("schweden", "swedish", "schwedisch", "stockholm")),
    ("NO", "Norway", "Europe", 60.5, 8.5, ("norwegen", "norwegian", "norwegisch", "oslo")),
    (
        "DK",
        "Denmark",
        "Europe",
        56.3,
        9.5,
        ("dänemark", "daenemark", "danish", "dänisch", "copenhagen", "kopenhagen"),
    ),
    ("FI", "Finland", "Europe", 61.9, 25.7, ("finnland", "finnish", "finnisch", "helsinki")),
    (
        "GR",
        "Greece",
        "Europe",
        39.1,
        21.8,
        ("griechenland", "greek", "griechisch", "athens", "athen"),
    ),
    (
        "TR",
        "Türkiye",
        "Europe",
        39.0,
        35.2,
        ("turkey", "türkei", "tuerkei", "turkish", "türkisch", "ankara", "istanbul"),
    ),
    ("UA", "Ukraine", "Europe", 48.4, 31.2, ("ukrainian", "ukrainisch", "kyiv", "kiew", "kiev")),
    (
        "RU",
        "Russia",
        "Europe",
        61.5,
        105.3,
        ("russland", "russian", "russisch", "moscow", "moskau", "kremlin", "kreml"),
    ),
    ("HU", "Hungary", "Europe", 47.2, 19.5, ("ungarn", "hungarian", "ungarisch", "budapest")),
    (
        "RO",
        "Romania",
        "Europe",
        45.9,
        25.0,
        ("rumänien", "romanian", "rumänisch", "bucharest", "bukarest"),
    ),
    (
        "RS",
        "Serbia",
        "Europe",
        44.0,
        21.0,
        ("serbien", "serbian", "serbisch", "belgrade", "belgrad"),
    ),
    (
        "IL",
        "Israel",
        "Middle East",
        31.0,
        34.9,
        ("israeli", "israelisch", "jerusalem", "tel aviv", "gaza"),
    ),
    (
        "PS",
        "Palestine",
        "Middle East",
        31.9,
        35.2,
        ("palästina", "palestinian", "palästinensisch", "west bank", "westjordanland"),
    ),
    ("SA", "Saudi Arabia", "Middle East", 23.9, 45.1, ("saudi-arabien", "saudi", "riyadh", "riad")),
    (
        "AE",
        "United Arab Emirates",
        "Middle East",
        23.4,
        53.8,
        ("uae", "emirates", "emirate", "dubai", "abu dhabi"),
    ),
    ("IR", "Iran", "Middle East", 32.4, 53.7, ("iranian", "iranisch", "tehran", "teheran")),
    ("IQ", "Iraq", "Middle East", 33.2, 43.7, ("irak", "iraqi", "irakisch", "baghdad", "bagdad")),
    (
        "SY",
        "Syria",
        "Middle East",
        34.8,
        39.0,
        ("syrien", "syrian", "syrisch", "damascus", "damaskus"),
    ),
    ("LB", "Lebanon", "Middle East", 33.9, 35.9, ("libanon", "lebanese", "libanesisch", "beirut")),
    ("QA", "Qatar", "Middle East", 25.4, 51.2, ("katar", "qatari", "doha")),
    (
        "EG",
        "Egypt",
        "Africa",
        26.8,
        30.8,
        ("ägypten", "aegypten", "egyptian", "ägyptisch", "cairo", "kairo"),
    ),
    (
        "ZA",
        "South Africa",
        "Africa",
        -30.6,
        22.9,
        (
            "südafrika",
            "suedafrika",
            "south african",
            "südafrikanisch",
            "johannesburg",
            "cape town",
            "kapstadt",
        ),
    ),
    ("NG", "Nigeria", "Africa", 9.1, 8.7, ("nigerian", "nigerianisch", "lagos", "abuja")),
    ("KE", "Kenya", "Africa", -0.02, 37.9, ("kenia", "kenyan", "kenianisch", "nairobi")),
    (
        "ET",
        "Ethiopia",
        "Africa",
        9.1,
        40.5,
        ("äthiopien", "aethiopien", "ethiopian", "addis ababa"),
    ),
    ("MA", "Morocco", "Africa", 31.8, -7.1, ("marokko", "moroccan", "marokkanisch", "rabat")),
    (
        "DZ",
        "Algeria",
        "Africa",
        28.0,
        1.7,
        ("algerien", "algerian", "algerisch", "algiers", "algier"),
    ),
    ("SD", "Sudan", "Africa", 12.9, 30.2, ("sudanese", "sudanesisch", "khartoum", "khartum")),
    ("CD", "DR Congo", "Africa", -4.0, 21.8, ("kongo", "congo", "kinshasa", "congolese")),
    (
        "CN",
        "China",
        "Asia",
        35.9,
        104.2,
        ("chinese", "chinesisch", "beijing", "peking", "shanghai", "hong kong", "hongkong"),
    ),
    ("JP", "Japan", "Asia", 36.2, 138.3, ("japanese", "japanisch", "tokyo", "tokio")),
    (
        "KR",
        "South Korea",
        "Asia",
        35.9,
        127.8,
        ("südkorea", "suedkorea", "south korean", "südkoreanisch", "seoul", "korea"),
    ),
    (
        "KP",
        "North Korea",
        "Asia",
        40.3,
        127.5,
        ("nordkorea", "north korean", "nordkoreanisch", "pyongyang", "pjöngjang"),
    ),
    (
        "IN",
        "India",
        "Asia",
        20.6,
        79.0,
        ("indien", "indian", "indisch", "new delhi", "neu-delhi", "mumbai", "delhi"),
    ),
    ("PK", "Pakistan", "Asia", 30.4, 69.3, ("pakistani", "pakistanisch", "islamabad", "karachi")),
    ("BD", "Bangladesh", "Asia", 23.7, 90.4, ("bangladesch", "bangladeshi", "dhaka")),
    ("AF", "Afghanistan", "Asia", 33.9, 67.7, ("afghan", "afghanisch", "kabul")),
    (
        "ID",
        "Indonesia",
        "Asia",
        -0.8,
        113.9,
        ("indonesien", "indonesian", "indonesisch", "jakarta"),
    ),
    (
        "PH",
        "Philippines",
        "Asia",
        12.9,
        121.8,
        ("philippinen", "philippine", "filipino", "philippinisch", "manila"),
    ),
    ("VN", "Vietnam", "Asia", 14.1, 108.3, ("vietnamese", "vietnamesisch", "hanoi")),
    ("TH", "Thailand", "Asia", 15.9, 100.99, ("thai", "thailändisch", "bangkok")),
    ("MY", "Malaysia", "Asia", 4.2, 101.98, ("malaysian", "malaysisch", "kuala lumpur")),
    ("SG", "Singapore", "Asia", 1.35, 103.8, ("singapur", "singaporean")),
    ("TW", "Taiwan", "Asia", 23.7, 121.0, ("taiwanese", "taiwanesisch", "taipei", "taipeh")),
    ("KZ", "Kazakhstan", "Asia", 48.0, 66.9, ("kasachstan", "kazakh", "kasachisch", "astana")),
    (
        "AU",
        "Australia",
        "Oceania",
        -25.3,
        133.8,
        ("australien", "australian", "australisch", "canberra", "sydney", "melbourne"),
    ),
    (
        "NZ",
        "New Zealand",
        "Oceania",
        -40.9,
        174.9,
        ("neuseeland", "new zealander", "neuseeländisch", "wellington", "auckland"),
    ),
)

COUNTRIES: dict[str, Country] = {
    iso: Country(iso, name, region, lat, lon, aliases)
    for iso, name, region, lat, lon, aliases in _TABLE
}

_ALIAS_INDEX: list[tuple[re.Pattern[str], str]] = []
for _c in COUNTRIES.values():
    for _alias in (_c.name.lower(), _c.iso.lower(), *_c.aliases):
        if len(_alias) <= 2 and _alias not in ("uk", "us"):
            continue  # skip 2-letter noise except the two common ones
        _ALIAS_INDEX.append(
            (re.compile(rf"(?<![a-zäöüß]){re.escape(_alias)}(?![a-zäöüß])", re.I), _c.iso)
        )

TOPICS: dict[str, tuple[str, ...]] = {
    "ai": (
        "ai",
        "artificial intelligence",
        "künstliche intelligenz",
        "ki",
        "llm",
        "openai",
        "anthropic",
        "claude",
        "gpt",
        "machine learning",
        "model",
        "modell",
        "chatbot",
        "robot",
    ),
    "tech": (
        "tech",
        "technology",
        "technologie",
        "software",
        "chip",
        "semiconductor",
        "halbleiter",
        "apple",
        "google",
        "microsoft",
        "nvidia",
        "samsung",
        "startup",
        "cyber",
        "hack",
        "app",
        "internet",
        "quantum",
        "quanten",
    ),
    "politics": (
        "election",
        "wahl",
        "parliament",
        "parlament",
        "government",
        "regierung",
        "president",
        "präsident",
        "minister",
        "chancellor",
        "kanzler",
        "senate",
        "senat",
        "congress",
        "kongress",
        "party",
        "partei",
        "vote",
        "coalition",
        "koalition",
        "referendum",
    ),
    "economy": (
        "economy",
        "wirtschaft",
        "inflation",
        "market",
        "markt",
        "stocks",
        "aktien",
        "börse",
        "bank",
        "interest rate",
        "zins",
        "trade",
        "handel",
        "tariff",
        "zoll",
        "gdp",
        "bip",
        "recession",
        "rezession",
        "oil",
        "öl",
        "energy prices",
    ),
    "security": (
        "war",
        "krieg",
        "attack",
        "angriff",
        "military",
        "militär",
        "missile",
        "rakete",
        "drone",
        "drohne",
        "ceasefire",
        "waffenruhe",
        "troops",
        "truppen",
        "nato",
        "terror",
        "explosion",
        "strike",
        "invasion",
        "sanctions",
        "sanktionen",
    ),
    "climate": (
        "climate",
        "klima",
        "flood",
        "flut",
        "hochwasser",
        "wildfire",
        "waldbrand",
        "hurricane",
        "storm",
        "sturm",
        "heat",
        "hitze",
        "drought",
        "dürre",
        "earthquake",
        "erdbeben",
        "emissions",
        "emissionen",
        "co2",
        "renewable",
        "erneuerbar",
    ),
    "health": (
        "health",
        "gesundheit",
        "virus",
        "pandemic",
        "pandemie",
        "vaccine",
        "impfstoff",
        "hospital",
        "krankenhaus",
        "who",
        "outbreak",
        "ausbruch",
        "cancer",
        "krebs",
        "disease",
        "krankheit",
    ),
    "science": (
        "science",
        "wissenschaft",
        "nasa",
        "esa",
        "space",
        "weltraum",
        "rocket",
        "spacex",
        "telescope",
        "teleskop",
        "study",
        "studie",
        "research",
        "forschung",
        "physics",
        "physik",
        "mars",
        "moon",
        "mond",
        "satellite",
        "satellit",
        "lunar",
        "orbit",
        "launch",
        "probe",
        "sonde",
        "jaxa",
    ),
    "sports": (
        "football",
        "fußball",
        "soccer",
        "olympics",
        "olympia",
        "champions league",
        "bundesliga",
        "tennis",
        "formula 1",
        "formel 1",
        "world cup",
        "wm",
        "nba",
        "nfl",
        "match",
        "spiel",
    ),
}
_TOPIC_INDEX: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"(?<![a-zäöüß]){re.escape(k)}(?![a-zäöüß])", re.I), topic)
    for topic, keys in TOPICS.items()
    for k in keys
]


def detect_countries(text: str) -> list[str]:
    """ISO-2 codes in order of first appearance (deduplicated)."""
    hits: list[tuple[int, str]] = []
    for rx, iso in _ALIAS_INDEX:
        m = rx.search(text or "")
        if m:
            hits.append((m.start(), iso))
    out: list[str] = []
    for _, iso in sorted(hits):
        if iso not in out:
            out.append(iso)
    return out


def detect_topics(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for rx, topic in _TOPIC_INDEX:
        if topic not in found and rx.search(text or ""):
            found.append(topic)
    return tuple(found) or ("general",)


def country(iso: str | None) -> Country | None:
    return COUNTRIES.get((iso or "").upper()) if iso else None


def resolve_country(text: str) -> str | None:
    """Single best country for a user query like 'news germany' / 'nachrichten aus japan'."""
    found = detect_countries(text)
    return found[0] if found else None

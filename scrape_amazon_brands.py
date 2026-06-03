#!/usr/bin/env python3
"""
Amazon / 导购网站手套品牌抓取工具

原理：
  1. 默认模式（推荐）：通过 DuckDuckGo 搜索手套导购/review文章，
     从搜索结果 snippet 中提取品牌名。完全免费，不会被反爬。
  2. Amazon 模式（备选）：用 Playwright 直接访问 Amazon 搜索页，
     从产品标题或品牌筛选栏提取品牌。可能被 Amazon 反爬/验证码。
  3. 拿到品牌名后，通过 DuckDuckGo 搜索品牌官网域名。
  4. 输出 CSV，可直接复制到 lead-finder 的"批量导入域名"。

用法示例：
  # 推荐：DDG 搜索 review 网站（稳定）
  python scrape_amazon_brands.py \
    --keyword "football gloves" \
    --pages 3 \
    --find-domains \
    --output brands.csv

  # 多个关键词一起搜
  python scrape_amazon_brands.py \
    --keyword "football gloves" --keyword "baseball batting gloves" --keyword "work safety gloves" \
    --pages 5 \
    --find-domains

  # Amazon 直接抓取（需 Playwright，可能被反爬）
  python scrape_amazon_brands.py \
    --keyword "football gloves" \
    --amazon \
    --pages 2 \
    --find-domains
"""

import argparse
import csv
import re
import sys
import time
import urllib.parse
from typing import List, Optional, Set

try:
    import requests
except ImportError:
    print("[ERROR] requests not installed. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

GLOVE_KEYWORDS = [
    "glove", "gloves",
    "hand protection", "handwear",
    "baseball", "football", "softball", "lacrosse", "hockey",
    "soccer", "goalkeeper", "goalie",
    "ski", "snowboard", "winter sport",
    "cycling", "bike", "motorcycle",
    "work", "safety", "industrial", "construction",
    "gym", "fitness", "weightlifting",
    "boxing", "mma", "martial art",
    "batting", "fielding",
    "goal keeping", "goalkeeping",
]

# Words that are definitely NOT brand names
STOP_WORDS = {
    "the", "and", "for", "with", "your", "more", "home", "about", "contact",
    "search", "menu", "cart", "login", "register", "best", "top", "review",
    "reviews", "guide", "buying", "buy", "seller", "sellers", "amazon",
    "price", "prices", "cheap", "expensive", "quality", "new", "old",
    "see", "show", "all", "results", "previous", "next", "page", "of",
    "free", "shipping", "prime", "deal", "deals", "sale", "today",
    "customer", "customers", "rating", "ratings", "star", "stars",
    "feature", "features", "detail", "details", "description", "specs",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
    "oct", "nov", "dec", "january", "february", "march", "april",
    "june", "july", "august", "september", "october", "november", "december",
    "yes", "no", "not", "are", "was", "were", "been", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should",
    "this", "that", "these", "those", "they", "them", "their", "there",
    "then", "than", "when", "where", "what", "which", "who", "how",
    "why", "from", "into", "onto", "upon", "over", "under", "above",
    "below", "between", "among", "within", "without", "through",
    "during", "before", "after", "since", "until", "while", "because",
    "although", "though", "unless", "whether", "either", "neither",
    "both", "each", "every", "some", "any", "many", "much", "most",
    "more", "less", "few", "several", "various", "different", "same",
    "such", "only", "just", "also", "even", "still", "yet", "already",
    "almost", "quite", "rather", "pretty", "very", "too", "so",
    "enough", "almost", "nearly", "hardly", "barely", "scarcely",
    "here", "now", "soon", "later", "early", "late", "long", "short",
    "high", "low", "big", "small", "large", "little", "old", "young",
    "new", "good", "bad", "better", "best", "worse", "worst",
    "great", "nice", "fine", "well", "badly", "wrong", "right",
    "true", "false", "real", "fake", "actual", "exact", "specific",
    "general", "usual", "normal", "common", "popular", "famous",
    "known", "unknown", "clear", "obvious", "certain", "sure",
    "possible", "impossible", "likely", "unlikely", "probable",
    "available", "unavailable", "ready", "prepared", "complete",
    "incomplete", "full", "empty", "total", "partial", "whole",
    "half", "double", "single", "multiple", "various", "diverse",
    "similar", "different", "related", "unrelated", "connected",
    "separate", "together", "alone", "only", "unique", "special",
    "regular", "irregular", "standard", "nonstandard", "typical",
    "atypical", "traditional", "modern", "current", "present",
    "past", "future", "former", "latter", "first", "last", "final",
    "initial", "original", "copy", "version", "edition", "update",
    "upgrade", "improvement", "change", "difference", "similarity",
    "comparison", "contrast", "advantage", "disadvantage", "benefit",
    "drawback", "pro", "con", "plus", "minus", "positive", "negative",
    "active", "passive", "direct", "indirect", "main", "major",
    "minor", "primary", "secondary", "essential", "necessary",
    "optional", "required", "mandatory", "voluntary", "automatic",
    "manual", "digital", "analog", "online", "offline", "local",
    "global", "internal", "external", "domestic", "foreign",
    "national", "international", "regional", "local", "urban",
    "rural", "central", "peripheral", "core", "edge", "front",
    "back", "side", "top", "bottom", "upper", "lower", "inner",
    "outer", "left", "right", "up", "down", "near", "far", "close",
    "distant", "deep", "shallow", "wide", "narrow", "thick", "thin",
    "heavy", "light", "strong", "weak", "hard", "soft", "solid",
    "liquid", "gas", "firm", "loose", "tight", "flexible", "rigid",
    "smooth", "rough", "sharp", "blunt", "flat", "round", "square",
    "straight", "curved", "horizontal", "vertical", "parallel",
    "perpendicular", "diagonal", "reverse", "inverse", "opposite",
    "identical", "equivalent", "equal", "unequal", "fair", "unfair",
    "just", "unjust", "legal", "illegal", "official", "unofficial",
    "formal", "informal", "professional", "amateur", "expert",
    "beginner", "advanced", "basic", "intermediate", "easy",
    "difficult", "simple", "complex", "complicated", "sophisticated",
    "plain", "fancy", "elegant", "graceful", "awkward", "clumsy",
    "skillful", "skilled", "talented", "gifted", "experienced",
    "inexperienced", "trained", "untrained", "educated", "uneducated",
    "literate", "illiterate", "aware", "unaware", "conscious",
    "unconscious", "mindful", "mindless", "careful", "careless",
    "cautious", "reckless", "safe", "dangerous", "harmful",
    "harmless", "toxic", "nontoxic", "clean", "dirty", "pure",
    "impure", "fresh", "stale", "raw", "cooked", "processed",
    "natural", "artificial", "synthetic", "organic", "inorganic",
    "biological", "chemical", "physical", "mental", "spiritual",
    "emotional", "rational", "logical", "irrational", "reasonable",
    "unreasonable", "sensible", "absurd", "practical", "theoretical",
    "applied", "abstract", "concrete", "specific", "vague",
    "ambiguous", "clear", "precise", "accurate", "inaccurate",
    "exact", "approximate", "correct", "incorrect", "true",
    "false", "valid", "invalid", "relevant", "irrelevant",
    "appropriate", "inappropriate", "suitable", "unsuitable",
    "fit", "unfit", "qualified", "unqualified", "competent",
    "incompetent", "capable", "incapable", "able", "unable",
    "powerful", "powerless", "strong", "weak", "influential",
    "obscure", "famous", "infamous", "notorious", "wellknown",
    "unknown", "recognized", "unrecognized", "acknowledged",
    "denied", "accepted", "rejected", "approved", "disapproved",
    "confirmed", "unconfirmed", "verified", "unverified",
    "proven", "unproven", "tested", "untested", "tried",
    "untried", "used", "unused", "new", "old", "ancient",
    "modern", "contemporary", "current", "past", "future",
    "upcoming", "ongoing", "completed", "finished", "incomplete",
    "unfinished", "pending", "delayed", "postponed", "advanced",
    "behind", "ahead", "early", "late", "on", "time", "timely",
    "untimely", "prompt", "slow", "fast", "quick", "rapid",
    "swift", "speedy", "gradual", "sudden", "instant",
    "immediate", "spontaneous", "planned", "deliberate",
    "intentional", "unintentional", "accidental", "deliberate",
    "voluntary", "involuntary", "forced", "compulsory",
    "mandatory", "optional", "elective", "selective",
    "exclusive", "inclusive", "broad", "narrow", "wide",
    "limited", "unlimited", "restricted", "unrestricted",
    "controlled", "uncontrolled", "regulated", "deregulated",
    "supervised", "unsupervised", "managed", "unmanaged",
    "organized", "disorganized", "structured", "unstructured",
    "systematic", "unsystematic", "methodical", "haphazard",
    "random", "chaotic", "orderly", "neat", "tidy", "messy",
    "clean", "dirty", "clear", "cloudy", "transparent",
    "opaque", "visible", "invisible", "apparent", "hidden",
    "obvious", "subtle", "noticeable", "unnoticeable",
    "prominent", "inconspicuous", "outstanding", "ordinary",
    "exceptional", "unexceptional", "remarkable", "unremarkable",
    "extraordinary", "normal", "abnormal", "standard",
    "substandard", "superior", "inferior", "excellent",
    "poor", "outstanding", "mediocre", "satisfactory",
    "unsatisfactory", "acceptable", "unacceptable",
    "adequate", "inadequate", "sufficient", "insufficient",
    "enough", "plenty", "scarce", "abundant", "plentiful",
    "sparse", "dense", "crowded", "empty", "occupied",
    "vacant", "available", "unavailable", "free", "busy",
    "idle", "active", "inactive", "alive", "dead",
    "living", "nonliving", "organic", "inorganic", "biological",
    "mechanical", "electrical", "electronic", "digital",
    "analog", "virtual", "real", "actual", "potential",
    "kinetic", "dynamic", "static", "stationary", "mobile",
    "portable", "fixed", "permanent", "temporary", "lasting",
    "fleeting", "enduring", "transient", "stable", "unstable",
    "steady", "unsteady", "constant", "variable", "changing",
    "unchanging", "consistent", "inconsistent", "uniform",
    "nonuniform", "homogeneous", "heterogeneous", "mixed",
    "pure", "combined", "separate", "joined", "divided",
    "united", "fragmented", "whole", "partial", "complete",
    "incomplete", "entire", "total", "full", "empty",
    "comprehensive", "limited", "extensive", "brief",
    "concise", "verbose", "detailed", "summary", "outline",
    "overview", "introduction", "conclusion", "beginning",
    "end", "start", "finish", "origin", "destination",
    "source", "target", "cause", "effect", "reason",
    "result", "purpose", "goal", "aim", "objective",
    "intention", "motive", "incentive", "motivation",
    "drive", "ambition", "aspiration", "desire", "wish",
    "hope", "dream", "plan", "strategy", "tactic",
    "approach", "method", "technique", "procedure",
    "process", "system", "mechanism", "structure",
    "framework", "model", "pattern", "design", "style",
    "format", "layout", "arrangement", "organization",
    "composition", "constitution", "configuration",
    "formation", "shape", "form", "figure", "outline",
    "profile", "silhouette", "appearance", "look",
    "aspect", "facade", "surface", "exterior", "interior",
    "inside", "outside", "external", "internal", "outer",
    "inner", "visible", "hidden", "exposed", "covered",
    "open", "closed", "sealed", "unsealed", "locked",
    "unlocked", "secured", "unsecured", "protected",
    "unprotected", "guarded", "unguarded", "defended",
    "undefended", "safe", "unsafe", "secure", "insecure",
    "stable", "unstable", "balanced", "unbalanced",
    "equal", "unequal", "even", "uneven", "level",
    "flat", "smooth", "rough", "plain", "steep",
    "gradual", "sharp", "blunt", "pointed", "rounded",
    "angular", "circular", "spherical", "cylindrical",
    "cubic", "rectangular", "triangular", "oval",
    "elliptical", "spiral", "curved", "straight",
    "direct", "indirect", "shortcut", "detour",
    "route", "path", "way", "course", "direction",
    "orientation", "position", "location", "place",
    "spot", "site", "point", "area", "region",
    "zone", "sector", "district", "neighborhood",
    "vicinity", "surroundings", "environment",
    "setting", "context", "background", "scene",
    "view", "sight", "landscape", "scenery", "panorama",
    "prospect", "outlook", "perspective", "angle",
    "aspect", "facet", "side", "dimension", "element",
    "component", "constituent", "ingredient", "part",
    "piece", "section", "segment", "portion", "share",
    "fraction", "percentage", "ratio", "proportion",
    "rate", "frequency", "density", "concentration",
    "intensity", "strength", "magnitude", "scale",
    "scope", "range", "reach", "extent", "degree",
    "level", "grade", "rank", "class", "category",
    "type", "kind", "sort", "variety", "species",
    "genus", "family", "group", "set", "collection",
    "series", "sequence", "order", "arrangement",
    "alignment", "row", "line", "column", "queue",
    "string", "chain", "train", "procession", "parade",
    "file", "stream", "flow", "current", "tide",
    "wave", "surge", "rush", "flood", "torrent",
    "trickle", "drip", "drop", "stream", "river",
    "brook", "creek", "channel", "canal", "conduit",
    "pipeline", "tube", "pipe", "hose", "cable",
    "wire", "line", "cord", "rope", "chain", "link",
    "connection", "bond", "tie", "knot", "loop",
    "circle", "ring", "band", "strip", "strap",
    "belt", "ribbon", "tape", "film", "sheet",
    "plate", "panel", "board", "slab", "block",
    "brick", "stone", "rock", "pebble", "gravel",
    "sand", "dust", "powder", "grain", "particle",
    "atom", "molecule", "cell", "organ", "tissue",
    "muscle", "bone", "nerve", "vein", "artery",
    "vessel", "tube", "duct", "gland", "node",
    "center", "hub", "core", "heart", "middle",
    "midst", "interior", "inside", "within", "among",
    "between", "amid", "surrounded", "enclosed",
    "wrapped", "packed", "filled", "empty", "hollow",
    "solid", "firm", "hard", "soft", "spongy",
    "fluffy", "fuzzy", "hairy", "bald", "smooth",
    "silky", "rough", "coarse", "fine", "thick",
    "thin", "wide", "narrow", "broad", "slender",
    "slim", "lean", "fat", "thin", "skinny",
    "plump", "chubby", "stout", "stocky", "muscular",
    "brawny", "burly", "husky", "lanky", "gangly",
    "awkward", "graceful", "elegant", "clumsy",
    "nimble", "agile", "swift", "quick", "slow",
    "sluggish", "lethargic", "energetic", "vigorous",
    "lively", "active", "busy", "industrious",
    "diligent", "lazy", "idle", "leisurely",
    "relaxed", "calm", "peaceful", "tranquil",
    "serene", "quiet", "silent", "still", "noisy",
    "loud", "boisterous", "rowdy", "raucous",
    "deafening", "ear-splitting", "soft", "gentle",
    "mild", "moderate", "extreme", "intense",
    "fierce", "furious", "wild", "tame", "domestic",
    "domesticated", "trained", "untamed", "savage",
    "ferocious", "brutal", "cruel", "kind", "gentle",
    "humane", "compassionate", "sympathetic",
    "empathetic", "caring", "loving", "affectionate",
    "fond", "devoted", "dedicated", "committed",
    "loyal", "faithful", "true", "trustworthy",
    "reliable", "dependable", "responsible",
    "accountable", "answerable", "liable", "guilty",
    "innocent", "blameless", "faultless", "flawless",
    "perfect", "imperfect", "defective", "faulty",
    "broken", "damaged", "destroyed", "ruined",
    "wrecked", "demolished", "shattered", "crushed",
    "smashed", "cracked", "split", "torn", "ripped",
    "frayed", "worn", "old", "new", "fresh", "stale",
    "rotten", "spoiled", "sour", "bitter", "sweet",
    "salty", "spicy", "hot", "cold", "warm", "cool",
    "icy", "freezing", "boiling", "scalding", "tepid",
    "lukewarm", "pleasant", "unpleasant", "nice",
    "nasty", "awful", "terrible", "horrible",
    "dreadful", "appalling", "shocking", "surprising",
    "unexpected", "expected", "predictable",
    "unpredictable", "certain", "uncertain", "sure",
    "unsure", "doubtful", "skeptical", "suspicious",
    "dubious", "questionable", "debatable",
    "controversial", "contentious", "disputed",
    "undisputed", "accepted", "rejected", "denied",
    "refused", "declined", "turned", "down", "granted",
    "given", "provided", "supplied", "furnished",
    "equipped", "armed", "prepared", "ready",
    "unprepared", "unready", "reluctant", "willing",
    "eager", "keen", "enthusiastic", "passionate",
    "fervent", "ardent", "zealous", "fanatical",
    "obsessive", "compulsive", "addicted", "hooked",
    "dependent", "independent", "selfreliant",
    "autonomous", "sovereign", "free", "liberated",
    "emancipated", "released", "freed", "escaped",
    "fled", "runaway", "wandering", "roaming",
    "rambling", "roving", "nomadic", "migratory",
    "settled", "established", "rooted", "grounded",
    "based", "founded", "built", "constructed",
    "created", "made", "produced", "manufactured",
    "fabricated", "assembled", "crafted", "handmade",
    "homemade", "custom", "customized", "personalized",
    "tailored", "bespoke", "offtheshelf", "ready",
    "made", "massproduced", "generic", "branded",
    "own", "private", "label", "white", "black",
    "gray", "grey", "red", "blue", "green", "yellow",
    "orange", "purple", "pink", "brown", "black",
    "white", "silver", "gold", "bronze", "copper",
    "metal", "metallic", "plastic", "rubber",
    "silicone", "leather", "synthetic", "natural",
    "cotton", "wool", "silk", "linen", "nylon",
    "polyester", "spandex", "latex", "neoprene",
    "kevlar", "carbon", "fiber", "glass", "wood",
    "bamboo", "paper", "cardboard", "foam", "gel",
    "air", "water", "oil", "gas", "steam", "ice",
    "snow", "rain", "wind", "storm", "sun", "moon",
    "star", "cloud", "sky", "earth", "ground",
    "soil", "dirt", "mud", "clay", "sand", "rock",
    "stone", "mountain", "hill", "valley", "plain",
    "plateau", "canyon", "cliff", "coast", "shore",
    "beach", "island", "peninsula", "cape", "bay",
    "gulf", "lake", "pond", "pool", "swamp", "marsh",
    "bog", "fen", "meadow", "field", "pasture",
    "prairie", "savanna", "tundra", "taiga", "jungle",
    "forest", "woods", "grove", "orchard", "vineyard",
    "garden", "park", "yard", "lawn", "court",
    "field", "ground", "pitch", "diamond", "gridiron",
    "rink", "court", "arena", "stadium", "gym",
    "gymnasium", "dome", "hall", "center", "centre",
    "complex", "facility", "venue", "site", "place",
    "location", "spot", "point", "station", "stop",
    "terminal", "depot", "warehouse", "storehouse",
    "depot", "shed", "barn", "silo", "tank",
    "container", "bin", "box", "crate", "case",
    "carton", "package", "parcel", "packet",
    "pouch", "bag", "sack", "purse", "wallet",
    "case", "briefcase", "suitcase", "luggage",
    "baggage", "cargo", "freight", "shipment",
    "load", "consignment", "delivery", "dispatch",
    "send", "ship", "transport", "carry", "bear",
    "haul", "tow", "drag", "pull", "push", "lift",
    "raise", "elevate", "lower", "drop", "fall",
    "rise", "ascend", "descend", "climb", "crawl",
    "creep", "sneak", "tiptoe", "walk", "step",
    "stride", "stroll", "saunter", "amble", "wander",
    "roam", "ramble", "meander", "hike", "trek",
    "march", "parade", "process", "run", "jog",
    "trot", "gallop", "sprint", "dash", "race",
    "chase", "pursue", "follow", "track", "trail",
    "trace", "hunt", "search", "seek", "look",
    "find", "discover", "detect", "locate", "spot",
    "identify", "recognize", "know", "understand",
    "comprehend", "grasp", "seize", "catch",
    "capture", "trap", "snare", "net", "bag",
    "hook", "line", "sinker", "bait", "lure",
    "decoy", "trap", "ambush", "surprise", "attack",
    "assault", "strike", "hit", "punch", "slap",
    "kick", "stab", "shoot", "fire", "blast",
    "explode", "detonate", "burst", "rupture",
    "break", "crack", "split", "tear", "rip",
    "shred", "cut", "slice", "dice", "chop",
    "hack", "hew", "carve", "whittle", "shape",
    "form", "mold", "cast", "forge", "weld",
    "solder", "braze", "glue", "paste", "stick",
    "adhere", "attach", "fasten", "secure", "tie",
    "bind", "wrap", "bundle", "pack", "package",
    "box", "crate", "can", "jar", "bottle",
    "container", "vessel", "holder", "carrier",
    "bearer", "porter", "courier", "messenger",
    "runner", "rider", "driver", "pilot", "captain",
    "skipper", "commander", "leader", "chief",
    "head", "boss", "manager", "director",
    "supervisor", "overseer", "foreman", "chief",
    "principal", "main", "primary", "major",
    "minor", "junior", "senior", "superior",
    "inferior", "equal", "peer", "colleague",
    "associate", "partner", "ally", "friend",
    "companion", "comrade", "mate", "pal",
    "buddy", "chum", "crony", "confidant",
    "advisor", "consultant", "counselor", "guide",
    "mentor", "teacher", "instructor", "coach",
    "trainer", "tutor", "professor", "lecturer",
    "educator", "scholar", "academic", "intellectual",
    "thinker", "philosopher", "theorist", "expert",
    "specialist", "professional", "practitioner",
    "operator", "worker", "laborer", "employee",
    "staff", "personnel", "crew", "team", "squad",
    "unit", "force", "corps", "division", "regiment",
    "battalion", "company", "platoon", "squad",
    "patrol", "guard", "sentry", "watchman",
    "lookout", "scout", "spy", "agent", "operative",
    "detective", "investigator", "inspector",
    "examiner", "auditor", "assessor", "appraiser",
    "evaluator", "judge", "critic", "reviewer",
    "commentator", "analyst", "observer", "witness",
    "bystander", "onlooker", "spectator", "viewer",
    "audience", "crowd", "mob", "throng", "horde",
    "swarm", "flock", "herd", "pack", "school",
    "shoal", "colony", "nest", "den", "lair",
    "burrow", "hole", "cave", "cavern", "tunnel",
    "passage", "corridor", "hallway", "aisle",
    "lane", "alley", "path", "track", "trail",
    "route", "course", "channel", "canal",
    "conduit", "pipeline", "duct", "tube",
    "cylinder", "barrel", "drum", "keg", "cask",
    "vat", "tank", "reservoir", "basin", "bowl",
    "dish", "plate", "platter", "tray", "saucer",
    "cup", "mug", "glass", "tumbler", "goblet",
    "flute", "stemware", "beaker", "pitcher",
    "jug", "jar", "bottle", "flask", "thermos",
    "canteen", "fountain", "spring", "well",
    "pump", "spout", "faucet", "tap", "valve",
    "nozzle", "jet", "spray", "shower", "bath",
    "tub", "basin", "sink", "lavatory", "toilet",
    "restroom", "bathroom", "washroom", "locker",
    "room", "chamber", "compartment", "cabin",
    "cubicle", "booth", "stall", "pen", "cage",
    "coop", "hutch", "kennel", "stable", "barn",
    "shed", "garage", "carport", "porch", "deck",
    "patio", "terrace", "balcony", "veranda",
    "gallery", "arcade", "colonnade", "cloister",
    "courtyard", "quad", "plaza", "square",
    "piazza", "forum", "market", "bazaar",
    "souk", "mall", "mart", "store", "shop",
    "boutique", "salon", "studio", "atelier",
    "workshop", "factory", "mill", "plant",
    "foundry", "refinery", "smelter", "forge",
    "furnace", "kiln", "oven", "stove", "range",
    "cooker", "heater", "radiator", "boiler",
    "incinerator", "crematorium", "reactor",
    "generator", "turbine", "engine", "motor",
    "machine", "mechanism", "device", "apparatus",
    "appliance", "instrument", "tool", "utensil",
    "implement", "equipment", "gear", "kit",
    "outfit", "rig", "setup", "system", "network",
    "web", "grid", "matrix", "array", "matrix",
    "lattice", "framework", "structure",
    "infrastructure", "superstructure",
    "substructure", "foundation", "base",
    "basis", "ground", "bedrock", "cornerstone",
    "keystone", "linchpin", "backbone", "pillar",
    "column", "post", "pole", "beam", "girder",
    "joist", "rafter", "truss", "arch", "vault",
    "dome", "roof", "ceiling", "floor", "wall",
    "partition", "divider", "screen", "panel",
    "board", "slab", "sheet", "film", "membrane",
    "layer", "coat", "covering", "wrapping",
    "packaging", "casing", "housing", "shell",
    "case", "sheath", "sleeve", "jacket", "cover",
    "lid", "top", "cap", "cork", "stopper",
    "plug", "seal", "gasket", "washer", "ring",
    "band", "strap", "belt", "sash", "girdle",
    "cummerbund", "waistband", "elastic",
    "drawstring", "lace", "cord", "string",
    "thread", "yarn", "fiber", "filament",
    "strand", "ribbon", "tape", "stripe", "band",
    "bar", "rod", "stick", "staff", "cane",
    "club", "bat", "racket", "paddle", "oar",
    "pole", "stake", "peg", "pin", "nail",
    "screw", "bolt", "rivet", " staple",
    "tack", "brad", "dowel", "spike", "spine",
    "thorn", "prickle", "quill", "bristle",
    "hair", "fur", "wool", "feather", "down",
    "plume", "crest", "mane", "tail", "coat",
    "hide", "skin", "pelt", "leather", "suede",
    "nubuck", "patent", "vinyl", "pleather",
    "faux", "imitation", "synthetic", "artificial",
    "manmade", "natural", "genuine", "authentic",
    "real", "original", "true", "pure", "solid",
    "bona", "fide", "legitimate", "legal",
    "licit", "lawful", "valid", "binding",
    "enforceable", "mandatory", "compulsory",
    "obligatory", "required", "necessary",
    "needed", "essential", "vital", "critical",
    "crucial", "key", "central", "core",
    "fundamental", "basic", "elementary",
    "primary", "principal", "main", "major",
    "chief", "foremost", "leading", "premier",
    "top", "first", "number", "one", "alpha",
    "prime", "initial", "original", "prototype",
    "model", "pattern", "template", "archetype",
    "exemplar", "paragon", "ideal", "standard",
    "benchmark", "yardstick", "criterion",
    "measure", "gauge", "meter", "indicator",
    "index", "sign", "signal", "symbol", "mark",
    "token", "emblem", "badge", "logo", "brand",
    "trademark", "label", "tag", "sticker",
    "decal", "insignia", "crest", "shield",
    "banner", "flag", "pennant", "streamer",
    "ribbon", "medal", "award", "trophy", "prize",
    "cup", "plate", "belt", "crown", "tiara",
    "diadem", "coronet", "wreath", "garland",
    "bouquet", "arrangement", "display",
    "exhibit", "show", "demonstration",
    "presentation", "performance", "act",
    "action", "deed", "feat", "achievement",
    "accomplishment", "success", "victory",
    "triumph", "win", "conquest", "defeat",
    "loss", "failure", "setback", "disappointment",
    "frustration", "defeat", "rout", "debacle",
    "fiasco", "disaster", "catastrophe",
    "calamity", "tragedy", "misfortune",
    "mishap", "accident", "incident", "event",
    "occurrence", "happening", "episode",
    "affair", "matter", "issue", "topic",
    "subject", "theme", "point", "question",
    "problem", "concern", "worry", "anxiety",
    "fear", "dread", "terror", "horror",
    "panic", "alarm", "fright", "scare",
    "shock", "surprise", "amazement", "astonishment",
    "wonder", "awe", "admiration", "respect",
    "esteem", "regard", "consideration",
    "attention", "notice", "observation",
    "perception", "awareness", "consciousness",
    "realization", "recognition", "understanding",
    "comprehension", "apprehension", "grasp",
    "mastery", "command", "control", "dominion",
    "rule", "reign", "regime", "government",
    "administration", "management", "direction",
    "guidance", "leadership", "supervision",
    "oversight", "charge", "care", "custody",
    "keeping", "possession", "ownership",
    "property", "belongings", "effects",
    "goods", "wares", "merchandise", "commodities",
    "products", "produce", "stock", "inventory",
    "supply", "supplies", "provision", "provisions",
    "rations", "food", "fare", "diet", "cuisine",
    "cooking", "baking", "roasting", "grilling",
    "frying", "boiling", "steaming", "poaching",
    "braising", "stewing", "simmering", "sautéing",
    "searing", "scorching", "burning", "charring",
    "toasting", "roasting", "broiling", "melting",
    "freezing", "chilling", "cooling", "heating",
    "warming", "thawing", "defrosting", "drying",
    "dehydrating", "preserving", "canning",
    "jarring", "bottling", "packaging",
    "processing", "refining", "purifying",
    "filtering", "straining", "sieving",
    "sifting", "sorting", "grading", "classifying",
    "categorizing", "organizing", "arranging",
    "ordering", "systematizing", "standardizing",
    "normalizing", "regulating", "adjusting",
    "adapting", "modifying", "altering",
    "changing", "transforming", "converting",
    "transmuting", "translating", "interpreting",
    "rendering", "depicting", "portraying",
    "representing", "symbolizing", "signifying",
    "meaning", "denoting", "indicating",
    "suggesting", "implying", "inferring",
    "deducing", "concluding", "determining",
    "deciding", "resolving", "settling",
    "fixing", "establishing", "instituting",
    "founding", "creating", "making", "forming",
    "shaping", "fashioning", "molding", "casting",
    "forging", "building", "constructing",
    "erecting", "assembling", "fabricating",
    "manufacturing", "producing", "generating",
    "originating", "initiating", "starting",
    "beginning", "commencing", "launching",
    "introducing", "presenting", "offering",
    "providing", "supplying", "furnishing",
    "giving", "granting", "bestowing",
    "conferring", "awarding", "accord",
    "accorded", "according", "matching",
    "corresponding", "agreeing", "harmonizing",
    "coordinating", "synchronizing", "aligning",
    "tuning", "calibrating", "balancing",
    "equalizing", "leveling", "smoothing",
    "polishing", "refining", "perfecting",
    "completing", "finishing", "ending",
    "closing", "concluding", "terminating",
    "ceasing", "stopping", "halting", "pausing",
    "waiting", "delaying", "postponing",
    "deferring", "suspending", "interrupting",
    "disrupting", "disturbing", "bothering",
    "annoying", "irritating", "aggravating",
    "exasperating", "infuriating", "enraging",
    "angering", "offending", "insulting",
    "affronting", "slighting", "snubbing",
    "ignoring", "neglecting", "overlooking",
    "disregarding", "dismissing", "rejecting",
    "refusing", "denying", "declining",
    "repudiating", "renouncing", "abandoning",
    "forsaking", "deserting", "leaving",
    "departing", "exiting", "going",
    "coming", "arriving", "reaching",
    "attaining", "achieving", "accomplishing",
    "fulfilling", "satisfying", "meeting",
    "matching", "fitting", "suiting",
    "adapted", "suited", "appropriate",
    "proper", "correct", "right", "accurate",
    "exact", "precise", "specific", "particular",
    "certain", "definite", "clear", "obvious",
    "evident", "apparent", "plain", "manifest",
    "patent", "palpable", "tangible", "concrete",
    "substantial", "material", "physical",
    "bodily", "corporeal", "somatic", "fleshly",
    "carnal", "sensual", "sexual", "erotic",
    "amorous", "romantic", "passionate",
    "ardent", "fervent", "intense", "extreme",
    "utmost", "supreme", "ultimate", "final",
    "last", "eventual", "prospective", "future",
    "impending", "imminent", "forthcoming",
    "approaching", "nearing", "closing",
    "looming", "threatening", "menacing",
    "dangerous", "perilous", "hazardous",
    "risky", "precarious", "uncertain",
    "doubtful", "questionable", "dubious",
    "suspicious", "skeptical", "distrustful",
    "mistrustful", "wary", "cautious",
    "careful", "prudent", "judicious",
    "sensible", "reasonable", "rational",
    "logical", "coherent", "consistent",
    "compatible", "consonant", "congruent",
    "harmonious", "peaceful", "tranquil",
    "serene", "calm", "placid", "quiet",
    "still", "silent", "hushed", "muted",
    "muffled", "subdued", "restrained",
    "controlled", "disciplined", "orderly",
    "neat", "tidy", "organized", "systematic",
    "methodical", "precise", "meticulous",
    "scrupulous", "punctilious", "careful",
    "thorough", "exhaustive", "complete",
    "comprehensive", "inclusive", "extensive",
    "broad", "wide", "vast", "immense",
    "enormous", "huge", "giant", "gigantic",
    "colossal", "titanic", "monumental",
    "massive", "substantial", "considerable",
    "significant", "important", "notable",
    "noteworthy", "remarkable", "striking",
    "impressive", "memorable", "unforgettable",
    "momentous", "consequential", "weighty",
    "grave", "serious", "severe", "critical",
    "acute", "urgent", "pressing", "compelling",
    "imperative", "mandatory", "obligatory",
    "binding", "contractual", "legal",
    "lawful", "legitimate", "licit",
    "permitted", "allowed", "authorized",
    "approved", "sanctioned", "endorsed",
    "supported", "backed", "funded",
    "financed", "sponsored", "patronized",
    "promoted", "advocated", "recommended",
    "advised", "suggested", "proposed",
    "offered", "tendered", "submitted",
    "presented", "introduced", "launched",
    "initiated", "begun", "started",
    "commenced", "originated", "created",
    "invented", "devised", "designed",
    "developed", "engineered", "built",
    "constructed", "fabricated", "assembled",
    "manufactured", "produced", "made",
    "generated", "formed", "shaped",
    "molded", "cast", "forged", "wrought",
    "crafted", "handcrafted", "created",
    "authored", "composed", "written",
    "penned", "drafted", "drawn", "painted",
    "sketched", "illustrated", "depicted",
    "portrayed", "represented", "shown",
    "displayed", "exhibited", "demonstrated",
    "performed", "executed", "carried",
    "conducted", "managed", "handled",
    "dealt", "treated", "processed",
    "worked", "operated", "functioned",
    "ran", "drove", "rode", "flew",
    "sailed", "navigated", "steered",
    "piloted", "guided", "led", "directed",
    "commanded", "controlled", "ruled",
    "governed", "administered", "regulated",
    "supervised", "monitored", "watched",
    "observed", "witnessed", "seen",
    "viewed", "looked", "gazed", "stared",
    "glared", "peered", "glanced", "peeked",
    "peeped", "snooped", "spied", "scanned",
    "surveyed", "inspected", "examined",
    "scrutinized", "studied", "analyzed",
    "investigated", "researched", "explored",
    "probed", "delved", "dug", "mined",
    "excavated", "extracted", "removed",
    "taken", "gotten", "obtained", "acquired",
    "gained", "earned", "won", "secured",
    "procured", "purchased", "bought",
    "sold", "traded", "exchanged", "bartered",
    "swapped", "substituted", "replaced",
    "superseded", "succeeded", "followed",
    "came", "went", "moved", "traveled",
    "journeyed", "voyaged", "toured",
    "visited", "stayed", "remained",
    "lingered", "dwelt", "resided",
    "lived", "existed", "survived",
    "endured", "lasted", "persisted",
    "continued", "maintained", "sustained",
    "preserved", "conserved", "protected",
    "guarded", "defended", "shielded",
    "screened", "sheltered", "harbored",
    "housed", "accommodated", "lodged",
    "quartered", "billeted", "stationed",
    "posted", "positioned", "placed",
    "put", "set", "laid", "stood",
    "sat", "lay", "rested", "leaned",
    "supported", "held", "carried",
    "borne", "transported", "conveyed",
    "transmitted", "transferred", "moved",
    "shifted", "switched", "changed",
    "converted", "transformed", "turned",
    "became", "grew", "developed", "evolved",
    "progressed", "advanced", "improved",
    "enhanced", "upgraded", "refined",
    "polished", "perfected", "completed",
    "finished", "done", "over", "ended",
    "finished", "through", "completed",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_brand(name: str) -> str:
    name = name.strip()
    # Remove trailing punctuation
    name = re.sub(r"[,.;:!?]+$", "", name)
    return name.strip()


def is_valid_brand(text: str) -> bool:
    text = text.strip()
    if len(text) < 2 or len(text) > 40:
        return False
    if text.lower() in STOP_WORDS:
        return False
    # Must start with uppercase letter
    if not text[0].isupper():
        return False
    # Should not be all lowercase
    if text.islower():
        return False
    # Should not be mostly numbers
    if sum(c.isdigit() for c in text) > len(text) * 0.4:
        return False
    # Should look like a brand (contains letters)
    if not any(c.isalpha() for c in text):
        return False
    return True


def is_glove_related(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in GLOVE_KEYWORDS)


def strip_html_tags(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text).strip()


# ---------------------------------------------------------------------------
# DuckDuckGo search
# ---------------------------------------------------------------------------

def search_ddg(query: str, page: int = 0) -> str:
    params = {"q": query}
    if page > 0:
        params["s"] = page * 10
        params["dc"] = page * 10 + 1
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params=params,
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"    [DDG ERROR] {query}: {e}")
        return ""


# ---------------------------------------------------------------------------
# Brand extraction from DDG HTML (review sites)
# ---------------------------------------------------------------------------

def extract_result_blocks(html: str) -> List[str]:
    blocks = re.findall(
        r'<div[^>]*class="result results_links[^"]*"[^>]*>(.*?)</div>\s*(?=<div[^>]*class="result results_links|<div[^>]*class="clear"|<div class="nav-link")',
        html,
        re.DOTALL,
    )
    return blocks


def extract_brands_from_snippet(snippet: str) -> Set[str]:
    brands = set()
    snippet = strip_html_tags(snippet)

    # Pattern 1: "brands like Nike, Adidas, and Under Armour"
    m = re.search(r'brands?\s+(?:like|such as|including)\s+([^\.\n]+)', snippet, re.I)
    if m:
        parts = re.split(r',|\sand\s|\sor\s', m.group(1))
        for part in parts:
            brand = normalize_brand(part)
            if is_valid_brand(brand):
                brands.add(brand)

    # Pattern 2: "manufacturers such as Nike, Adidas"
    m = re.search(r'manufacturers?\s+(?:like|such as|including)\s+([^\.\n]+)', snippet, re.I)
    if m:
        parts = re.split(r',|\sand\s|\sor\s', m.group(1))
        for part in parts:
            brand = normalize_brand(part)
            if is_valid_brand(brand):
                brands.add(brand)

    # Pattern 3: "by Nike" / "from Adidas" / "made by Wilson"
    for m in re.finditer(r'\b(?:by|from|made by|created by|designed by)\s+([A-Z][A-Za-z0-9\.\-&]{1,25})\b', snippet):
        brand = normalize_brand(m.group(1))
        if is_valid_brand(brand):
            brands.add(brand)

    # Pattern 4: "Nike gloves" / "Adidas football gloves" / "Wilson baseball"
    for m in re.finditer(r'\b([A-Z][A-Za-z0-9\.\-&]{1,20})\s+(?:gloves?|football|baseball|softball|batting|fielding|goalkeeping|work|safety|cycling|ski|boxing)', snippet):
        brand = normalize_brand(m.group(1))
        if is_valid_brand(brand):
            brands.add(brand)

    # Pattern 5: "Nike's" / "Adidas's"
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9\.\-&]{1,20})['’]s\b", snippet):
        brand = normalize_brand(m.group(1))
        if is_valid_brand(brand):
            brands.add(brand)

    # Pattern 6: standalone brand names near "glove"
    # Look for capitalized words within 5 words of "glove"
    words = snippet.split()
    for i, word in enumerate(words):
        clean = re.sub(r"[^\w\-&]", "", word)
        if is_valid_brand(clean):
            # Check if "glove" is nearby
            start = max(0, i - 5)
            end = min(len(words), i + 6)
            context = " ".join(words[start:end]).lower()
            if "glove" in context or "sport" in context or "equipment" in context:
                brands.add(normalize_brand(clean))

    return brands


# ---------------------------------------------------------------------------
# Amazon direct scraping (Playwright)
# ---------------------------------------------------------------------------

def scrape_amazon_with_playwright(keyword: str, pages: int = 1) -> Set[str]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[ERROR] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return set()

    brands: Set[str] = set()

    with sync_playwright() as p:
        print("[Browser] Launching Chromium...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=HEADERS["User-Agent"],
        )
        page = context.new_page()

        for page_num in range(1, pages + 1):
            url = f"https://www.amazon.com/s?k={urllib.parse.quote_plus(keyword)}&page={page_num}"
            print(f"[Browser] Navigating to Amazon page {page_num} ...")
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                time.sleep(3)
            except PWTimeout:
                print(f"[Browser] Timeout on page {page_num}, skipping")
                continue

            # Check for captcha
            if "captcha" in page.content().lower() or page.locator("input[name='cvf_captcha_input']").count() > 0:
                print("[Browser] Amazon CAPTCHA detected! Try again later or use DDG mode.")
                break

            # Method 1: Extract from product titles
            print("[Browser] Extracting product titles...")
            titles = page.locator('[data-component-type="s-search-result"] h2 a span').all_text_contents()
            for title in titles:
                title = title.strip()
                if not title:
                    continue
                # Brand is usually first 1-3 words, until we hit a lowercase word
                words = title.split()
                brand_words = []
                for word in words:
                    clean = re.sub(r"[^\w\-&]", "", word)
                    if clean and clean[0].isupper() and clean.lower() not in STOP_WORDS:
                        brand_words.append(clean)
                    else:
                        break
                if brand_words:
                    brand = " ".join(brand_words)
                    if is_valid_brand(brand):
                        brands.add(brand)

            # Method 2: Extract from brand filter sidebar
            print("[Browser] Extracting brand filters...")
            try:
                brand_spans = page.locator('#brandsRefinements li span.a-size-base').all_text_contents()
                for text in brand_spans:
                    text = text.strip()
                    if is_valid_brand(text):
                        brands.add(text)
            except Exception:
                pass

            # Method 3: Try alternative selectors for brand sidebar
            try:
                alt_spans = page.locator('[data-cel-widget*="brands"] li span.a-size-base').all_text_contents()
                for text in alt_spans:
                    text = text.strip()
                    if is_valid_brand(text):
                        brands.add(text)
            except Exception:
                pass

            print(f"[Browser] Page {page_num}: found {len(brands)} unique brands so far")

        browser.close()

    return brands


# ---------------------------------------------------------------------------
# Domain finding
# ---------------------------------------------------------------------------

def find_domain_ddg(brand_name: str) -> Optional[str]:
    query = f"{brand_name} official website"
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        html = resp.text

        m = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', html)
        if not m:
            m = re.search(r'<a[^>]+href="(https?://[^"]+)"', html)
        if not m:
            return None

        url = m.group(1)
        if "duckduckgo.com/l/" in url or "duckduckgo.com/d.js" in url:
            ru = re.search(r'uddg=([^&]+)', url)
            if ru:
                url = urllib.parse.unquote(ru.group(1))

        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        blocked = {"facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
                   "youtube.com", "wikipedia.org", "amazon.com", "ebay.com", "alibaba.com",
                   "zoominfo.com", "crunchbase.com", "bbb.org", "yellowpages.com", "yelp.com",
                   "tripadvisor.com", "pinterest.com", "reddit.com", "quora.com",
                   "etsy.com", "walmart.com", "target.com", "homedepot.com",
                   "bestbuy.com", "costco.com", "wayfair.com", "macys.com"}
        if domain in blocked:
            return None
        return domain
    except Exception as e:
        print(f"    [DDG ERROR] {brand_name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_csv(brands: List[str], domains: dict, sources: dict, output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["brand", "domain", "source_query", "glove_related"])
        for name in brands:
            writer.writerow([
                name,
                domains.get(name, ""),
                sources.get(name, ""),
                "yes" if is_glove_related(name) else "no",
            ])
    print(f"\n[OK] Exported {len(brands)} rows to: {output_path}")


def export_domain_list(domains: List[str], output_path: str):
    clean = sorted(set(d for d in domains if d))
    with open(output_path, "w", encoding="utf-8") as f:
        for d in clean:
            f.write(d + "\n")
    print(f"[OK] Exported {len(clean)} domains to: {output_path}")
    print("  Copy these domains and paste into lead-finder's '批量导入域名' field.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape glove brands from Amazon/review sites")
    parser.add_argument("--keyword", type=str, action="append", required=True,
                        help="Search keyword (can specify multiple)")
    parser.add_argument("--amazon", action="store_true",
                        help="Use Playwright to scrape Amazon directly (may hit CAPTCHA)")
    parser.add_argument("--pages", type=int, default=3,
                        help="Number of search result pages (default: 3)")
    parser.add_argument("--find-domains", action="store_true",
                        help="Search for brand websites via DuckDuckGo")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Delay between domain searches (seconds)")
    parser.add_argument("--all", action="store_true",
                        help="Include all brands, not just glove-related")
    parser.add_argument("--output", type=str, default="amazon_brands.csv",
                        help="Output CSV path")
    parser.add_argument("--domain-list", type=str,
                        help="Also export plain domain list")
    args = parser.parse_args()

    all_brands: Set[str] = set()
    brand_sources: dict = {}

    if args.amazon:
        # Amazon Playwright mode
        for keyword in args.keyword:
            print(f"\n{'='*60}")
            print(f"[Amazon Mode] Keyword: {keyword}")
            print(f"{'='*60}")
            brands = scrape_amazon_with_playwright(keyword, pages=args.pages)
            for b in brands:
                all_brands.add(b)
                if b not in brand_sources:
                    brand_sources[b] = f"amazon:{keyword}"
            print(f"[Extract] Found {len(brands)} brands from Amazon for '{keyword}'")
    else:
        # DDG review search mode (default)
        for keyword in args.keyword:
            print(f"\n{'='*60}")
            print(f"[DDG Review Mode] Keyword: {keyword}")
            print(f"{'='*60}")

            # Build search queries for review sites
            queries = [
                f'best {keyword} brands',
                f'top {keyword} manufacturers',
                f'{keyword} buying guide brands',
                f'{keyword} review brands',
                f'site:gearhungry.com "{keyword}"',
                f'site:thegearhunt.com "{keyword}"',
                f'site:outdoorgearlab.com "{keyword}"',
                f'site:thomasnet.com "{keyword}" manufacturer',
            ]

            for q in queries:
                print(f"\n[Query] {q}")
                for page in range(args.pages):
                    print(f"  [Page {page + 1}/{args.pages}] Searching...", end=" ", flush=True)
                    html = search_ddg(q, page)
                    if not html:
                        print("failed")
                        continue

                    blocks = extract_result_blocks(html)
                    print(f"found {len(blocks)} results")

                    for block in blocks:
                        # Extract snippet
                        snippet_match = re.search(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)
                        snippet = snippet_match.group(1) if snippet_match else ""
                        # Also extract title
                        title_match = re.search(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL)
                        title = title_match.group(1) if title_match else ""

                        text = f"{title} {snippet}"
                        brands = extract_brands_from_snippet(text)
                        for b in brands:
                            all_brands.add(b)
                            if b not in brand_sources:
                                brand_sources[b] = q

                    time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"[Extract] Found {len(all_brands)} unique brand names")
    print(f"{'='*60}")

    if not all_brands:
        print("[WARNING] No brands found. Try different keywords or use --amazon mode.")
        sys.exit(0)

    # Filter glove-related
    brands = sorted(all_brands)
    if args.all:
        filtered = brands
    else:
        filtered = [b for b in brands if is_glove_related(b)]
        print(f"[Filter] {len(filtered)}/{len(brands)} brands match glove keywords")
        if filtered:
            print("  Matched:")
            for b in filtered[:15]:
                print(f"    - {b}")
            if len(filtered) > 15:
                print(f"    ... and {len(filtered) - 15} more")

    if not filtered:
        print("[WARNING] No glove-related brands found. Try --all to see all results.")
        sys.exit(0)

    # Find domains
    domains = {}
    if args.find_domains:
        print(f"\n[Domain Search] Finding websites for {len(filtered)} brands...")
        for idx, name in enumerate(filtered, 1):
            print(f"  [{idx}/{len(filtered)}] {name} ...", end=" ", flush=True)
            domain = find_domain_ddg(name)
            if domain:
                domains[name] = domain
                print(domain)
            else:
                print("not found")
            time.sleep(args.delay)
    else:
        print("\n[Tip] Pass --find-domains to automatically search for brand websites")

    # Export
    export_csv(filtered, domains, brand_sources, args.output)
    if args.domain_list:
        export_domain_list([domains.get(b, "") for b in filtered], args.domain_list)

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  Total brands      : {len(brands)}")
    print(f"  Glove-related     : {len(filtered)}")
    print(f"  Domains found     : {sum(1 for d in domains.values() if d)}")
    print(f"  Output CSV        : {args.output}")
    if args.domain_list:
        print(f"  Domain list       : {args.domain_list}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

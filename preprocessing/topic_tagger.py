"""Tag travel text with topics (flights, trains, food...) from a fixed taxonomy.

Keyword-only: a blank spaCy tokenizer + case-insensitive PhraseMatcher over TOPIC_KEYWORDS.
Same lexicon is used for (1) LLM hints in the post filter, (2) chunk-level topic_mentions,
(3) topic detection in user questions, so all three agree.

Output per text: topics ordered by number of keyword hits (ties keep TOPICS order). [] if none.
"""
from collections import Counter

import spacy
from spacy.matcher import PhraseMatcher

# Plural / verb forms listed explicitly (blank tokenizer has no lemmatizer).
# Avoid bare words that are mostly non-topic in travel chat ("tip" = advice, "pass" = verb).
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "flights": [
        "flight", "flights", "fly", "flying", "flew", "airline", "airlines", "airfare", "airport", "airports",
        "layover", "layovers", "stopover", "connecting flight", "red-eye", "red eye", "boarding pass",
        "economy class", "business class", "premium economy", "skiplagging", "skiplag", "direct flight",
        "nonstop", "jet lag", "jetlag", "carry-on", "carry on", "checked bag", "checked bags",
        "cheapest way to get there", "plane", "planes", "plane ticket", "plane tickets",
    ],
    "trains": [
        "train", "trains", "rail", "railway", "railways", "jr pass", "japan rail pass", "rail pass",
        "eurail", "interrail", "shinkansen", "bullet train", "night train", "sleeper train", "amtrak",
        "high speed train", "high-speed train", "renfe", "trenitalia", "sncf", "db bahn", "deutsche bahn",
        "train station", "train ticket", "train tickets",
    ],
    "buses": [
        "bus", "buses", "coach", "coaches", "flixbus", "greyhound", "megabus", "overnight bus",
        "night bus", "bus station", "bus ticket", "bus tickets", "minibus", "chicken bus",
    ],
    "driving": [
        "car rental", "rental car", "rent a car", "renting a car", "rented a car", "hire car", "car hire",
        "road trip", "road trips", "roadtrip", "driving", "drive", "drove", "international driving permit",
        "idp", "toll", "tolls", "parking", "campervan", "camper van", "rv", "motorbike", "scooter rental",
    ],
    "local_transport": [
        "metro", "subway", "tube", "underground", "tram", "trams", "taxi", "taxis", "cab", "cabs", "uber",
        "lyft", "tuk tuk", "tuk-tuk", "ferry", "ferries", "public transport",
        "public transportation", "transit", "metro card", "oyster card", "suica", "pasmo", "airport transfer",
        "shuttle", "get around", "getting around",
    ],
    "accommodation": [
        "hotel", "hotels", "hostel", "hostels", "airbnb", "airbnbs", "guesthouse", "guesthouses",
        "guest house", "resort", "resorts", "motel", "ryokan", "bnb", "b&b", "booking.com", "accommodation",
        "accommodations", "where to stay", "place to stay", "check-in",
        "dorm", "dorms", "homestay", "couchsurfing", "camping", "campsite", "all inclusive", "all-inclusive",
    ],
    "food": [
        "food", "foods", "eat", "eating", "ate", "restaurant", "restaurants", "dish", "dishes",
        "street food", "cuisine", "breakfast", "lunch", "dinner", "brunch", "cafe", "cafes", "coffee",
        "michelin", "vegan", "vegetarian", "gluten", "halal", "food tour", "market", "markets", "tapas",
        "sushi", "ramen", "pizza", "pasta", "wine", "foodie", "where to eat", "snacks", "dessert",
    ],
    "nightlife": [
        "nightlife", "bar", "bars", "pub", "pubs", "club", "clubs", "clubbing", "party", "partying",
        "nightclub", "nightclubs", "rooftop bar", "pub crawl", "beer", "cocktail", "cocktails", "drinking",
    ],
    "sightseeing": [
        "famous", "must see", "must-see", "must do", "must-do", "attractions", "attraction", "landmarks",
        "landmark", "things to do", "worth visiting", "sightseeing", "sights", "museum",
        "museums", "temple", "temples", "castle", "castles", "cathedral", "old town", "tour", "tours",
        "day trip", "day trips", "tourist trap", "tourist traps", "theme park", "disney", "viewpoint",
        "palace", "ruins", "gallery", "galleries", "hidden gems", "bucket list",
    ],
    "outdoors": [
        "hike", "hikes", "hiking", "trek", "trekking", "trail", "trails", "national park", "national parks",
        "beach", "beaches", "snorkel", "snorkeling", "snorkelling", "diving", "scuba", "surf", "surfing",
        "ski", "skiing", "snowboarding", "kayak", "kayaking", "safari", "mountain", "mountains", "waterfall",
        "waterfalls", "island hopping", "glacier", "volcano", "wildlife",
    ],
    "itinerary": [
        "itinerary", "itineraries", "route", "how many days", "how many nights", "days in", "nights in",
        "1 week", "2 weeks", "3 weeks", "one week", "two weeks", "three weeks", "10 days", "two days",
        "three days", "day itinerary", "plan my trip", "planning a trip", "trip planning",
        "split my time", "too rushed",
    ],
    "budget": [
        "budget", "cheap", "cheaper", "cheapest", "expensive", "affordable", "cost", "costs", "price",
        "prices", "pricey", "how much", "per day", "save money", "saving money", "splurge", "worth the money",
        "overpriced",
    ],
    "money": [
        "currency", "cash", "atm", "atms", "credit card", "credit cards", "debit card", "debit cards",
        "exchange rate", "money exchange", "exchange money", "currency exchange", "foreign transaction fee",
        "wise card", "revolut", "tipping", "tipped", "gratuity", "euros", "yen", "pesos", "dollars", "baht",
        "contactless", "apple pay",
    ],
    "visa": [
        "visa", "visas", "evisa", "e-visa", "esta", "etias", "passport", "passports", "border",
        "border crossing", "immigration", "customs", "entry requirements", "schengen", "visa on arrival",
        "overstay", "residence permit", "transit visa", "passport validity", "global entry", "tsa precheck",
    ],
    "safety": [
        "safe", "safety", "unsafe", "dangerous", "danger", "scam", "scams", "scammed", "pickpocket",
        "pickpockets", "pickpocketing", "crime", "theft", "stolen", "robbed", "is it safe", "travel advisory",
        "solo female", "harassment", "sketchy",
    ],
    "health": [
        "vaccine", "vaccines", "vaccination", "vaccinations", "malaria", "travel insurance", "insurance",
        "sick", "illness", "hospital", "doctor", "pharmacy", "medication", "medicine", "altitude sickness",
        "food poisoning", "travelers diarrhea", "covid", "mosquito", "mosquitoes", "dengue", "allergy",
    ],
    "weather": [
        "weather", "season", "seasons", "rainy season", "dry season", "monsoon", "best time", "best time to visit",
        "winter", "summer", "spring", "autumn", "fall foliage", "rain", "rainy", "snow", "humid", "humidity",
        "temperature", "temperatures", "hurricane", "typhoon", "off season", "shoulder season",
        "peak season", "high season", "low season", "cherry blossom", "cherry blossoms",
    ],
    "packing": [
        "pack", "packing", "luggage", "suitcase", "suitcases", "backpack", "carry-on bag", "packing list",
        "what to bring", "what to wear", "clothes", "shoes", "adapter", "power adapter", "plug adapter",
        "gear", "personal item",
    ],
    "connectivity": [
        "sim", "sim card", "sim cards", "esim", "e-sim", "airalo", "wifi", "wi-fi", "data plan", "roaming",
        "international plan", "phone plan", "pocket wifi", "hotspot", "internet", "vpn", "cell service",
    ],
    "language_culture": [
        "language", "speak english", "english speaking", "learn the language", "phrases", "translate",
        "translation", "google translate", "etiquette", "culture", "cultural", "customs and etiquette",
        "dress code", "modest", "rude", "polite", "local norms", "offensive", "religion", "ramadan",
    ],
}
TOPICS = list(TOPIC_KEYWORDS)

_nlp = spacy.blank("en")
_matcher = PhraseMatcher(_nlp.vocab, attr="LOWER")
for _topic, _terms in TOPIC_KEYWORDS.items():
    _matcher.add(_topic, list(_nlp.tokenizer.pipe(_terms)))
_ORDER = {t: i for i, t in enumerate(TOPICS)}


def _topics(doc) -> list[str]:
    counts = Counter(_nlp.vocab.strings[match_id] for match_id, _, _ in _matcher(doc))
    return sorted(counts, key=lambda t: (-counts[t], _ORDER[t]))


def tag_topic(text: str) -> list[str]:
    return _topics(_nlp(text))


def tag_topics(texts, batch_size: int = 256) -> list[list[str]]:
    return [_topics(doc) for doc in _nlp.pipe(texts, batch_size=batch_size)]

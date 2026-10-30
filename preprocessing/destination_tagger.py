"""Tag travel posts with the countries / states they talk about.

Pipeline per post:
  1. spaCy NER pulls GPE / LOC / FAC spans.
  2. PhraseMatchers add country names, aliases (US, UK, Holland...), multi-word
     states and LANDMARKS that NER misses (lowercase text, titles, etc.).
  3. Non-place entities (PERSON, ORG, NORP...) block overlaps, so "Air Canada",
     "Qatar Airways" or "Jordan" the person do not become places.
  4. Each span is resolved against a gazetteer: countries (geonamescache),
     states / provinces / regions (GeoNames admin1), cities (geonamescache,
     mapped up to their state + country) and LANDMARKS.
  5. Ambiguous names (Georgia, Victoria, Valencia...) prefer a candidate in a
     country that is mentioned unambiguously elsewhere in the same post.

Output per post: list of country + state names (ASCII), never cities. [] if none.
candidate_options() instead returns numbered Options (mention -> tags), keeping the
non-city alternatives of ambiguous spans, for an LLM to filter.
tag_keys_many() returns filter keys instead of bare names ("Georgia|United States" for
the state, "Georgia" for the country) so same-named places never collide in a DB filter.
"""
import unicodedata
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import geonamescache
import spacy
from spacy.matcher import PhraseMatcher
from spacy.util import filter_spans

ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"
ADMIN1_PATH = Path(__file__).with_name("admin1CodesASCII.txt")

PLACE_LABELS = {"GPE", "LOC", "FAC"}
BLOCKER_LABELS = {"PERSON", "ORG", "NORP", "PRODUCT", "WORK_OF_ART", "EVENT", "LAW", "LANGUAGE"}

# Cities are only trusted on their own (no supporting country in the post) when big enough.
MIN_LONE_CITY_POP = 100_000
# Even with a supporting country, tiny towns ("Roosevelt", NY) are usually hotels / streets / people.
MIN_CONTEXT_CITY_POP = 30_000
# A city name shared by several states of one country (St. Charles: IL / MO / MD) only maps to
# a state when its biggest namesake is this many times bigger than the next; else country only.
CITY_STATE_DOMINANCE = 3
# "Middle Tennessee", "Northern Italy", "Coastal British Columbia" -> retry without the prefix.
DIRECTION_PREFIXES = (
    "north", "south", "east", "west", "northern", "southern", "eastern", "western", "central",
    "middle", "upstate", "downstate", "coastal", "rural", "northeast", "northwest", "southeast",
    "southwest", "northeastern", "northwestern", "southeastern", "southwestern", "mainland",
)
# Alternate city names (e.g. "Firenze", "Bombay") are only indexed for big cities.
MIN_ALT_NAME_POP = 100_000
# candidate_options(): non-city readings offered per ambiguous span besides the chosen one.
MAX_ALTERNATIVES = 3

KIND_PRIORITY = {"country": 0, "landmark": 1, "admin1": 2, "city": 3}

COUNTRY_RENAMES = {
    "The Netherlands": "Netherlands",
    "Palestinian Territory": "Palestine",
    "Timor Leste": "East Timor",
}
ADMIN1_RENAMES = {"State of Berlin": "Berlin"}
DEFUNCT_COUNTRIES = {"CS", "AN"}  # Serbia and Montenegro, Netherlands Antilles

# alias -> ISO code. Matched case-insensitively.
COUNTRY_ALIASES = {
    "united states of america": "US", "america": "US", "the states": "US",
    "great britain": "GB", "britain": "GB",
    "holland": "NL", "the netherlands": "NL",
    "czech republic": "CZ",
    "korea": "KR",
    "burma": "MM",
    "cote d'ivoire": "CI",
    "macedonia": "MK",
    "turkiye": "TR",
    "bosnia": "BA",
    "vatican city": "VA",
    "cape verde": "CV",
    "swaziland": "SZ",
    "east timor": "TL",
    "viet nam": "VN",
    "russian federation": "RU",
    "mainland china": "CN",
}
# Abbreviations matched case-sensitively so "us" (pronoun) never matches.
COUNTRY_ABBREVIATIONS = {
    "US": "US", "U.S.": "US", "USA": "US", "U.S.A.": "US",
    "UK": "GB", "U.K.": "GB",
    "UAE": "AE", "U.A.E.": "AE",
}
# Country names that are also common words / first names: only matched capitalised.
CASE_SENSITIVE_COUNTRIES = {
    "Chad", "Jordan", "Turkey", "China", "Georgia", "Guinea", "Niger", "Jersey",
    "Guernsey", "Mali", "Iceland", "Monaco",
}
# Phrases that contain a country/state word but are not that place.
NON_PLACES = [
    "Asia", "Europe", "Africa", "Oceania", "Caribbean", "the Caribbean", "Scandinavia", "Balkans",
    "the Balkans", "Mediterranean", "the Mediterranean", "Pacific", "Atlantic",
    "South America", "North America", "Central America", "Latin America",
    "Southeast Asia", "South East Asia", "East Asia", "Middle East", "New England",
    "Indian Ocean", "Turkish Airlines", "Qatar Airways", "Air Canada", "Air India",
    "Air France", "Japan Airlines", "Singapore Airlines", "Korean Air", "China Airlines",
    "Emirates", "Etihad", "Turkey Day",
]

# Landmarks / regions / city aliases -> tags (state names must match GeoNames admin1 ASCII names).
LANDMARKS = {
    # India
    "taj mahal": ["Uttar Pradesh", "India"],
    "agra fort": ["Uttar Pradesh", "India"],
    "varanasi ghats": ["Uttar Pradesh", "India"],
    "red fort": ["Delhi", "India"],
    "qutub minar": ["Delhi", "India"],
    "india gate": ["Delhi", "India"],
    "gateway of india": ["Maharashtra", "India"],
    "hawa mahal": ["Rajasthan", "India"],
    "golden temple": ["Punjab", "India"],
    "hampi": ["Karnataka", "India"],
    "ladakh": ["Ladakh", "India"],
    "kashmir": ["Jammu and Kashmir", "India"],
    "andaman islands": ["Andaman and Nicobar", "India"],
    # Asia
    "angkor wat": ["Siem Reap", "Cambodia"],
    "great wall of china": ["China"],
    "the great wall": ["China"],
    "forbidden city": ["Beijing", "China"],
    "mount fuji": ["Japan"],
    "mt fuji": ["Japan"],
    "fushimi inari": ["Kyoto", "Japan"],
    "ha long bay": ["Quang Ninh", "Vietnam"],
    "halong bay": ["Quang Ninh", "Vietnam"],
    "petronas towers": ["Kuala Lumpur", "Malaysia"],
    "marina bay sands": ["Singapore"],
    "burj khalifa": ["Dubai", "United Arab Emirates"],
    "mount everest": ["Nepal"],
    "everest base camp": ["Nepal"],
    "annapurna": ["Nepal"],
    "borobudur": ["Central Java", "Indonesia"],
    "komodo": ["East Nusa Tenggara", "Indonesia"],
    "gili islands": ["West Nusa Tenggara", "Indonesia"],
    "lombok": ["West Nusa Tenggara", "Indonesia"],
    "palawan": ["Philippines"],
    "el nido": ["Philippines"],
    "boracay": ["Philippines"],
    "koh samui": ["Surat Thani", "Thailand"],
    "koh phangan": ["Surat Thani", "Thailand"],
    "koh tao": ["Surat Thani", "Thailand"],
    "phi phi": ["Krabi", "Thailand"],
    "petra": ["Jordan"],
    "wadi rum": ["Jordan"],
    "cappadocia": ["Turkey"],
    "pamukkale": ["Denizli", "Turkey"],
    "hagia sophia": ["Istanbul", "Turkey"],
    # Europe
    "eiffel tower": ["Ile-de-France", "France"],
    "louvre": ["Ile-de-France", "France"],
    "versailles": ["Ile-de-France", "France"],
    "mont saint-michel": ["Normandy", "France"],
    "mont st michel": ["Normandy", "France"],
    "french riviera": ["Provence-Alpes-Cote d'Azur", "France"],
    "cote d'azur": ["Provence-Alpes-Cote d'Azur", "France"],
    "provence": ["Provence-Alpes-Cote d'Azur", "France"],
    "loire valley": ["Centre-Val de Loire", "France"],
    "chamonix": ["Auvergne-Rhone-Alpes", "France"],
    "colosseum": ["Lazio", "Italy"],
    "vatican": ["Vatican"],
    "amalfi coast": ["Campania", "Italy"],
    "amalfi": ["Campania", "Italy"],
    "positano": ["Campania", "Italy"],
    "capri": ["Campania", "Italy"],
    "pompeii": ["Campania", "Italy"],
    "cinque terre": ["Liguria", "Italy"],
    "lake como": ["Lombardy", "Italy"],
    "lake garda": ["Italy"],
    "dolomites": ["Italy"],
    "leaning tower of pisa": ["Tuscany", "Italy"],
    "sagrada familia": ["Catalonia", "Spain"],
    "alhambra": ["Andalusia", "Spain"],
    "ibiza": ["Balearic Islands", "Spain"],
    "mallorca": ["Balearic Islands", "Spain"],
    "majorca": ["Balearic Islands", "Spain"],
    "tenerife": ["Canary Islands", "Spain"],
    "algarve": ["Faro", "Portugal"],
    "madeira": ["Madeira", "Portugal"],
    "azores": ["Azores", "Portugal"],
    "big ben": ["England", "United Kingdom"],
    "stonehenge": ["England", "United Kingdom"],
    "lake district": ["England", "United Kingdom"],
    "cotswolds": ["England", "United Kingdom"],
    "scottish highlands": ["Scotland", "United Kingdom"],
    "isle of skye": ["Scotland", "United Kingdom"],
    "loch ness": ["Scotland", "United Kingdom"],
    "cliffs of moher": ["Ireland"],
    "ring of kerry": ["Ireland"],
    "giant's causeway": ["Northern Ireland", "United Kingdom"],
    "santorini": ["South Aegean", "Greece"],
    "mykonos": ["South Aegean", "Greece"],
    "acropolis": ["Attica", "Greece"],
    "crete": ["Crete", "Greece"],
    "neuschwanstein": ["Bavaria", "Germany"],
    "black forest": ["Baden-Wurttemberg", "Germany"],
    "swiss alps": ["Switzerland"],
    "matterhorn": ["Switzerland"],
    "zermatt": ["Valais", "Switzerland"],
    "interlaken": ["Bern", "Switzerland"],
    "jungfrau": ["Bern", "Switzerland"],
    "hallstatt": ["Upper Austria", "Austria"],
    "plitvice": ["Croatia"],
    "dubrovnik": ["Dubrovnik-Neretva", "Croatia"],
    "lake bled": ["Slovenia"],
    "northern lights": [],
    "lofoten": ["Nordland", "Norway"],
    "blue lagoon": ["Iceland"],
    "golden circle": ["Iceland"],
    # Americas
    "grand canyon": ["Arizona", "United States"],
    "yellowstone": ["Wyoming", "United States"],
    "yosemite": ["California", "United States"],
    "zion national park": ["Utah", "United States"],
    "bryce canyon": ["Utah", "United States"],
    "glacier national park": ["Montana", "United States"],
    "statue of liberty": ["New York", "United States"],
    "times square": ["New York", "United States"],
    "central park": ["New York", "United States"],
    "golden gate bridge": ["California", "United States"],
    "las vegas strip": ["Nevada", "United States"],
    "disney world": ["Florida", "United States"],
    "walt disney world": ["Florida", "United States"],
    "disneyland": ["California", "United States"],
    "florida keys": ["Florida", "United States"],
    "key west": ["Florida", "United States"],
    "smoky mountains": ["Tennessee", "United States"],
    "napa valley": ["California", "United States"],
    "maui": ["Hawaii", "United States"],
    "oahu": ["Hawaii", "United States"],
    "kauai": ["Hawaii", "United States"],
    "niagara falls": ["Ontario", "Canada", "New York", "United States"],
    "banff": ["Alberta", "Canada"],
    "jasper national park": ["Alberta", "Canada"],
    "lake louise": ["Alberta", "Canada"],
    "whistler": ["British Columbia", "Canada"],
    "machu picchu": ["Cuzco Department", "Peru"],
    "sacred valley": ["Cuzco Department", "Peru"],
    "inca trail": ["Cuzco Department", "Peru"],
    "galapagos": ["Galapagos", "Ecuador"],
    "patagonia": ["Argentina", "Chile"],
    "torres del paine": ["Region of Magallanes", "Chile"],
    "atacama": ["Chile"],
    "iguazu falls": ["Argentina", "Brazil"],
    "christ the redeemer": ["Rio de Janeiro", "Brazil"],
    "chichen itza": ["Yucatan", "Mexico"],
    "tulum": ["Quintana Roo", "Mexico"],
    "cancun": ["Quintana Roo", "Mexico"],
    "playa del carmen": ["Quintana Roo", "Mexico"],
    "riviera maya": ["Quintana Roo", "Mexico"],
    "cozumel": ["Quintana Roo", "Mexico"],
    "salar de uyuni": ["Potosi Department", "Bolivia"],
    # Africa / Oceania / Middle East
    "pyramids of giza": ["Giza", "Egypt"],
    "giza": ["Giza", "Egypt"],
    "serengeti": ["Tanzania"],
    "kilimanjaro": ["Kilimanjaro", "Tanzania"],
    "zanzibar": ["Tanzania"],
    "victoria falls": ["Zambia", "Zimbabwe"],
    "table mountain": ["Western Cape", "South Africa"],
    "kruger national park": ["Mpumalanga", "South Africa"],
    "marrakech": ["Marrakesh-Safi", "Morocco"],
    "sahara": [],
    "great barrier reef": ["Queensland", "Australia"],
    "uluru": ["Northern Territory", "Australia"],
    "sydney opera house": ["New South Wales", "Australia"],
    "milford sound": ["Southland", "New Zealand"],
    "queenstown": ["Otago", "New Zealand"],
    "bora bora": ["French Polynesia"],
    # City / district aliases
    "nyc": ["New York", "United States"],
    "washington dc": ["District of Columbia", "United States"],
    "washington d.c.": ["District of Columbia", "United States"],
}
# Short / ambiguous aliases matched case-sensitively.
CASE_SENSITIVE_LANDMARKS = {
    "DC": ["District of Columbia", "United States"],
    "D.C.": ["District of Columbia", "United States"],
    "NYC": ["New York", "United States"],
    "CDMX": ["Mexico City", "Mexico"],
}


def fold(text: str) -> str:
    """Lowercase + strip accents so 'Île-de-France' == 'ile-de-france'."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower().strip()


def clean_span_text(text: str) -> str:
    text = text.strip(" \t\n.,;:!?\"'()[]")
    for suffix in ("'s", "’s"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if fold(text).startswith("the "):
        text = text[4:]
    return text.strip()


class Option(NamedTuple):
    """One LLM-selectable destination: str() -> '1. "Mexico City", "CDMX" -> Mexico City, Mexico'."""
    id: int
    mentions: tuple[str, ...]
    tags: tuple[str, ...]

    def __str__(self) -> str:
        mentions = ", ".join(f'"{m}"' for m in self.mentions)
        return f"{self.id}. {mentions} -> {', '.join(self.tags)}"


def candidate_rank(c: tuple) -> tuple:
    """Countries > landmarks > states > cities, then bigger first."""
    return KIND_PRIORITY[c[0]], -c[3]


def load_admin1() -> dict[str, tuple[str, str]]:
    """'US.NY' -> ('New York', 'US'). Downloads GeoNames admin1 file once."""
    if not ADMIN1_PATH.exists():
        urllib.request.urlretrieve(ADMIN1_URL, ADMIN1_PATH)
    admin1 = {}
    with open(ADMIN1_PATH, encoding="utf-8") as f:
        for line in f:
            code, name, ascii_name, _ = line.rstrip("\n").split("\t")
            name = ascii_name or name
            admin1[code] = (ADMIN1_RENAMES.get(name, name), code.split(".")[0])
    return admin1


class DestinationTagger:
    def __init__(self, model: str = "en_core_web_md"):
        self.nlp = spacy.load(model, exclude=["parser", "lemmatizer", "tagger", "attribute_ruler", "senter"])
        gc = geonamescache.GeonamesCache()
        countries = {cc: c for cc, c in gc.get_countries().items() if cc not in DEFUNCT_COUNTRIES}
        self.country_name = {cc: COUNTRY_RENAMES.get(c["name"], c["name"]) for cc, c in countries.items()}
        self.admin1 = load_admin1()
        self.states_by_country: dict[str, set[str]] = defaultdict(set)
        for name, cc in self.admin1.values():
            if cc in self.country_name:
                self.states_by_country[self.country_name[cc]].add(name)
        cities = gc.get_cities()

        # name -> list of candidates (kind, country_code, tags, weight)
        self.index: dict[str, list[tuple]] = defaultdict(list)

        for cc, name in self.country_name.items():
            self.index[fold(name)].append(("country", cc, (name,), 0))
            self.index[fold(countries[cc]["name"])].append(("country", cc, (name,), 0))
        for alias, cc in COUNTRY_ALIASES.items():
            self.index[fold(alias)].append(("country", cc, (self.country_name[cc],), 0))
        for alias, cc in COUNTRY_ABBREVIATIONS.items():
            self.index[fold(alias)].append(("country", cc, (self.country_name[cc],), 0))

        admin1_pop = defaultdict(int)
        for city in cities.values():
            admin1_pop[f"{city['countrycode']}.{city['admin1code']}"] += city["population"]
        for code, (name, cc) in self.admin1.items():
            if cc not in self.country_name:
                continue
            self.index[fold(name)].append(("admin1", cc, (name, self.country_name[cc]), admin1_pop[code]))

        city_index = defaultdict(list)
        for city in cities.values():
            cc = city["countrycode"]
            if cc not in self.country_name:
                continue
            state = self.admin1.get(f"{cc}.{city['admin1code']}")
            names = {fold(city["name"])}
            if city["population"] >= MIN_ALT_NAME_POP:
                names |= {fold(a) for a in city["alternatenames"] if a.isascii() and len(a) >= 4 and a[0].isupper()}
            for n in names:
                city_index[n].append((cc, state[0] if state else None, city["population"]))
        for n, entries in city_index.items():
            for cc, state, pop in entries:
                rivals = sorted((p for c, s, p in entries if c == cc and s != state), reverse=True)
                dominant = not rivals or pop >= CITY_STATE_DOMINANCE * rivals[0]
                tags = (state, self.country_name[cc]) if state and dominant else (self.country_name[cc],)
                self.index[n].append(("city", cc, tags, pop))

        for name, tags in {**LANDMARKS, **CASE_SENSITIVE_LANDMARKS}.items():
            ccs = {cc for cc, n in self.country_name.items() if n in tags}
            self.index[fold(name)] = [("landmark", next(iter(ccs)) if len(ccs) == 1 else None, tuple(tags), 0)]
        self.non_places = {fold(clean_span_text(p)) for p in NON_PLACES}
        for phrase in self.non_places:
            self.index.pop(phrase, None)

        self._check_landmarks()
        self._build_matchers()

    def _check_landmarks(self):
        known = {name for name, _ in self.admin1.values()} | set(self.country_name.values())
        bad = {t for tags in {**LANDMARKS, **CASE_SENSITIVE_LANDMARKS}.values() for t in tags if t not in known}
        if bad:
            print(f"[destination_tagger] warning: landmark tags not in GeoNames: {sorted(bad)}")

    def _build_matchers(self):
        make = self.nlp.make_doc
        self.lower_matcher = PhraseMatcher(self.nlp.vocab, attr="LOWER")
        self.orth_matcher = PhraseMatcher(self.nlp.vocab, attr="ORTH")
        self.block_matcher = PhraseMatcher(self.nlp.vocab, attr="LOWER")

        lower_terms, orth_terms = set(COUNTRY_ALIASES) | set(LANDMARKS), set(COUNTRY_ABBREVIATIONS) | set(CASE_SENSITIVE_LANDMARKS)
        for name in self.country_name.values():
            (orth_terms if name in CASE_SENSITIVE_COUNTRIES else lower_terms).add(name)
        # Multi-word states (New Mexico, New South Wales...) so "Mexico"/"Wales" alone never wins inside them.
        for name, cc in self.admin1.values():
            if " " in name and cc in {"US", "CA", "AU", "IN", "GB"}:
                orth_terms.add(name)

        self.lower_matcher.add("PLACE", [make(t) for t in lower_terms])
        self.orth_matcher.add("PLACE", [make(t) for t in orth_terms])
        self.block_matcher.add("BLOCK", [make(t) for t in NON_PLACES])

    def _candidate_spans(self, doc):
        persons, places, blockers = [], [], []
        for ent in doc.ents:
            if ent.label_ in PLACE_LABELS:
                places.append(ent)
            elif ent.label_ == "PERSON":
                persons.append(ent)
            elif ent.label_ in BLOCKER_LABELS:
                # spaCy sometimes labels a bare country as ORG ("Japan is great") -> keep it as a place.
                cands = self._lookup(ent.text)
                (places if any(c[0] in ("country", "admin1") for c in cands) else blockers).append(ent)
        for matcher, bucket in ((self.lower_matcher, places), (self.orth_matcher, places), (self.block_matcher, blockers)):
            bucket.extend(doc[s:e] for _, s, e in matcher(doc))
        # Equal-length ties keep list order: person > place > other blockers. Longest span wins overall.
        kept = filter_spans(persons + places + blockers)
        place_ids = {(s.start, s.end) for s in places}
        return [s for s in kept if (s.start, s.end) in place_ids]

    def _lookup(self, text: str) -> list[tuple]:
        key = fold(clean_span_text(text))
        if key in self.non_places:
            return []
        if key in self.index:
            return self.index[key]
        first, _, rest = key.partition(" ")
        if first in DIRECTION_PREFIXES and rest:
            return [c for c in self.index.get(rest, []) if c[0] != "city"]
        return []

    def _choices(self, spans) -> list[tuple]:
        """Per resolvable span: (span, best candidate or None if a too-small city, all candidates)."""
        span_cands = [(span, cands) for span in spans if (cands := self._lookup(span.text))]

        # Countries the post mentions unambiguously -> used to break ties elsewhere.
        context = set()
        for _, cands in span_cands:
            ccs = {c[1] for c in cands}
            if len(ccs) == 1 and None not in ccs:
                context |= ccs

        choices = []
        for span, cands in span_cands:
            in_context = [c for c in cands if c[1] in context or c[0] == "landmark"]
            pool = in_context or cands
            best = min(pool, key=candidate_rank)
            if best[0] == "city" and best[3] < (MIN_CONTEXT_CITY_POP if in_context else MIN_LONE_CITY_POP):
                best = None
            choices.append((span, best, cands))
        return choices

    def _resolve(self, spans) -> list[str]:
        tags = [t for _, best, _ in self._choices(spans) if best for t in best[2]]
        return list(dict.fromkeys(tags))

    def dest_keys(self, tags) -> list[str]:
        """Tag tuple -> filter keys. ("Catalonia", "Spain") -> ["Catalonia|Spain", "Spain"],
        ("Georgia",) -> ["Georgia"], ("Ontario", "Canada", "New York", "United States") -> 4 keys."""
        keys, i = [], 0
        while i < len(tags):
            name = tags[i]
            country = tags[i + 1] if i + 1 < len(tags) else None
            if country and name in self.states_by_country.get(country, ()):
                if name != country:  # state "Taiwan" of country Taiwan -> country key only
                    keys.append(f"{name}|{country}")
                keys.append(country)
                i += 2
            else:
                keys.append(name)
                i += 1
        return list(dict.fromkeys(keys))

    def _resolve_keys(self, spans) -> list[str]:
        keys = [k for _, best, _ in self._choices(spans) if best for k in self.dest_keys(best[2])]
        return list(dict.fromkeys(keys))

    def _options(self, spans) -> list[Option]:
        # tags -> mentions, so "US" and "USA" share one option.
        mentions_by_tags: dict[tuple, list[str]] = {}
        for span, best, cands in self._choices(spans):
            # Alternatives in best's country are redundant (state "Mexico" vs country Mexico).
            best_cc = best[1] if best else None
            alternatives = dict.fromkeys(
                c[2] for c in sorted(cands, key=candidate_rank) if c[0] != "city" and c[1] != best_cc
            )
            mention = clean_span_text(span.text)
            for tags in ([best[2]] if best else []) + list(alternatives)[:MAX_ALTERNATIVES]:
                tags = tuple(dict.fromkeys(tags))  # state "Taiwan" + country Taiwan -> Taiwan
                if not tags:  # tagless landmarks ("northern lights")
                    continue
                mentions = mentions_by_tags.setdefault(tags, [])
                if fold(mention) not in map(fold, mentions):
                    mentions.append(mention)
        return [Option(i, tuple(m), t) for i, (t, m) in enumerate(mentions_by_tags.items(), start=1)]

    def tag(self, text: str) -> list[str]:
        return self._resolve(self._candidate_spans(self.nlp(text)))

    def tag_many(self, texts, batch_size: int = 64) -> list[list[str]]:
        return [self._resolve(self._candidate_spans(doc)) for doc in self.nlp.pipe(texts, batch_size=batch_size)]

    def tag_keys(self, text: str) -> list[str]:
        return self._resolve_keys(self._candidate_spans(self.nlp(text)))

    def tag_keys_many(self, texts, batch_size: int = 64) -> list[list[str]]:
        return [self._resolve_keys(self._candidate_spans(doc)) for doc in self.nlp.pipe(texts, batch_size=batch_size)]

    def candidate_options(self, texts, batch_size: int = 64) -> list[list[Option]]:
        """Per post, numbered options for the LLM filter. Chosen reading first, then non-city alternatives."""
        return [self._options(self._candidate_spans(doc)) for doc in self.nlp.pipe(texts, batch_size=batch_size)]

    def _default_tuples(self, spans) -> list[tuple[str, ...]]:
        tuples = (tuple(dict.fromkeys(best[2])) for _, best, _ in self._choices(spans) if best)
        return list(dict.fromkeys(t for t in tuples if t))

    def candidates_many(self, texts, batch_size: int = 64) -> list[tuple[list[Option], list[tuple[str, ...]]]]:
        """Per post: (LLM options, spaCy's own picks as unflattened tag tuples = fallback if the LLM fails)."""
        out = []
        for doc in self.nlp.pipe(texts, batch_size=batch_size):
            spans = self._candidate_spans(doc)
            out.append((self._options(spans), self._default_tuples(spans)))
        return out

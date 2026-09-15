"""Keyless theater work discovery using Wikidata's public Action API."""

import re

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources, TheaterForms
from app.providers import services

BASE_URL = "https://www.wikidata.org/w/api.php"
FORM_IDS = {
    "Q25379": "play",
    "Q2743": "musical",
    "Q1344": "opera",
    "Q15079786": "ballet",
}
WORK_IDS = [*FORM_IDS, "Q58483083", "Q58483088", "Q116476516"]
EXCLUDED_TYPES = {"Q5", "Q11424", "Q5398426", "Q4167410", "Q4167836"}
CREATOR_ROLES = {
    "P50": "Authors",
    "P86": "Composers",
    "P87": "Librettists",
    "P676": "Lyricists",
    "P1809": "Choreographers",
}
PAGE_SIZE = 20
MAX_BATCHES = 3


def request_data(params):
    """Fetch one bounded request and surface provider back-pressure."""
    try:
        data = services.api_request(
            Sources.WIKIDATA.value,
            "GET",
            BASE_URL,
            params={"format": "json", **params},
            headers={"User-Agent": "Yamtrack (https://github.com/FuzzyGrim/Yamtrack)"},
        )
    except requests.RequestException as error:
        raise services.ProviderAPIError(Sources.WIKIDATA.value, error) from error
    if "error" in data:
        raise services.ProviderAPIError(
            Sources.WIKIDATA.value,
            ValueError(data["error"].get("info", "Invalid API response")),
        )
    return data


def entities(identifiers):
    """Read public entities in supported batches, following provider redirects."""
    identifiers = list(dict.fromkeys(identifiers))
    result = {}
    for offset in range(0, len(identifiers), 50):
        batch = identifiers[offset : offset + 50]
        response = request_data(
            {
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "info|labels|descriptions|claims",
                "languages": "en",
                "languagefallback": 1,
                "redirects": "yes",
            }
        )
        result.update(response.get("entities", {}))
    return result


def values(entity, property_id):
    """Read non-deprecated concrete claim values."""
    return [
        claim["mainsnak"]["datavalue"]["value"]
        for claim in entity.get("claims", {}).get(property_id, [])
        if claim.get("rank") != "deprecated"
        and "datavalue" in claim.get("mainsnak", {})
    ]


def identifiers(entity, property_id):
    """Read entity references from a claim."""
    return [
        value["id"]
        for value in values(entity, property_id)
        if isinstance(value, dict) and "id" in value
    ]


def forms(entity):
    """Require explicit work form evidence without guessing unresolved forms."""
    if "missing" in entity:
        return []
    types = identifiers(entity, "P31")
    if EXCLUDED_TYPES.intersection(types):
        return []
    if values(entity, "P272") and values(entity, "P161") and values(entity, "P57"):
        return []
    evidence = identifiers(entity, "P7937") + types
    return list(
        dict.fromkeys(FORM_IDS[value] for value in evidence if value in FORM_IDS)
    )


def label(entity):
    """Read a display label, preserving the provider's language fallback."""
    labels = entity.get("labels", {})
    return labels.get("en", next(iter(labels.values()), {})).get(
        "value", entity.get("id", "")
    )


def transform(entity, related):
    """Return Yamtrack metadata for a classified work."""
    work_forms = forms(entity)
    details = {"forms": ", ".join(TheaterForms(value).label for value in work_forms)}
    creators = []
    for property_id, role in CREATOR_ROLES.items():
        names = [
            label(related[identifier])
            for identifier in identifiers(entity, property_id)
            if identifier in related and "missing" not in related[identifier]
        ]
        if names:
            details[role] = ", ".join(names)
            creators.extend(names)
    languages = [
        label(related[identifier])
        for identifier in identifiers(entity, "P364")
        if identifier in related and "missing" not in related[identifier]
    ]
    if languages:
        details["original_language"] = ", ".join(languages)
    return {
        "media_id": entity["id"],
        "source": Sources.WIKIDATA.value,
        "source_url": f"https://www.wikidata.org/wiki/{entity['id']}",
        "media_type": MediaTypes.THEATER.value,
        "title": label(entity),
        "image": settings.IMG_NONE,
        "theater_forms": work_forms,
        "work_description": " / ".join([details["forms"], *dict.fromkeys(creators)]),
        "synopsis": entity.get("descriptions", {})
        .get("en", {})
        .get("value", "No synopsis available."),
        "max_progress": 1,
        "score": None,
        "score_count": None,
        "details": details,
        "related": {},
    }


def hydrate(work_entities):
    """Hydrate labels once per batch of eligible works."""
    references = [
        identifier
        for entity in work_entities
        for property_id in [*CREATOR_ROLES, "P364"]
        for identifier in identifiers(entity, property_id)
    ]
    related = entities(references)
    return [transform(entity, related) for entity in work_entities]


def theater(media_id):
    """Resolve a work identity and reject unsupported or ambiguous records."""
    if not re.fullmatch(r"Q[1-9][0-9]*", media_id):
        services.raise_not_found_error(Sources.WIKIDATA.value, media_id, "theater")
    key = f"wikidata_theater_{media_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    entity = entities([media_id]).get(media_id, {})
    if not forms(entity):
        services.raise_not_found_error(
            Sources.WIKIDATA.value, media_id, "theater work with a supported form"
        )
    result = hydrate([entity])[0]
    cache.set(f"wikidata_theater_{result['media_id']}", result, 3600)
    return result


def search(query, page):
    """Filter a bounded candidate window before canonical result pagination."""
    literal = " ".join(query.split())[:200]
    key = f"wikidata_search_v1_{literal}"
    cached = cache.get(key)
    if cached is None:
        escaped = literal.replace("\\", "\\\\").replace('"', '\\"')
        filters = [f"P31={identifier}" for identifier in WORK_IDS]
        filters.extend(f"P7937={identifier}" for identifier in FORM_IDS)
        params = {
            "action": "query",
            "list": "search",
            "srnamespace": 0,
            "srlimit": 50,
            "srprop": "",
            "srsearch": f'inlabel:"{escaped}@*" haswbstatement:' + "|".join(filters),
        }
        selected = {}
        continuation = {}
        for _batch in range(MAX_BATCHES):
            data = request_data({**params, **continuation})
            hits = data.get("query", {}).get("search", [])
            work_entities = entities([hit["title"] for hit in hits])
            for hit in hits:
                entity = work_entities.get(hit["title"], {})
                if forms(entity):
                    selected.setdefault(entity["id"], entity)
            continuation = data.get("continue", {})
            if not continuation:
                break
        results = hydrate(list(selected.values()))
        cached = {"results": results, "limited": bool(continuation)}
        cache.set(key, cached, 900)
    page = max(1, page)
    results = cached["results"]
    response = helpers.format_search_response(
        page,
        PAGE_SIZE,
        len(results),
        results[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
    )
    response["limited"] = cached["limited"]
    return response

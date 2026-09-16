"""Exact-work Wikipedia article thumbnails with explicit source rights metadata."""

import re
from time import monotonic
from urllib.parse import quote, unquote, urlsplit

import requests
from django.core.cache import cache
from django.utils import timezone

from app.providers import commons, services

VERSION = 1
LANGUAGES = ("en", "fr", "de", "es", "it", "nl")
FILE_NAMESPACES = {
    "en": "File",
    "fr": "Fichier",
    "de": "Datei",
    "es": "Archivo",
    "it": "File",
    "nl": "Bestand",
}
LANGUAGE_LIMIT = 3
REQUEST_LIMIT = 12
TIME_LIMIT = 20
THUMBNAIL_WIDTH = 300
NON_FREE_NOTICE = (
    "Non-free copyrighted artwork. Wikipedia supplies a use-specific rationale, "
    "not a transferable license. Display here does not establish fair use for "
    "another context or jurisdiction. Rights remain with the copyright holder."
)


class UnavailableError(Exception):
    """Incomplete source metadata must remain retryable."""


def cache_key(work_id):
    """Identify the cached article selection for a canonical work."""
    return f"wikipedia_artwork_v{VERSION}_subject1_{work_id}"


def invalidate(source, media_type, work_id):
    """Refresh article selection only for provider-backed Theater metadata sync."""
    if source == "wikidata" and media_type == "theater":
        cache.delete(cache_key(work_id))


def page_url(language, title):
    """Build source links from validated site identity and exact page titles."""
    encoded = quote(title.replace(" ", "_"), safe="")
    return f"https://{language}.wikipedia.org/wiki/{encoded}"


def request_pages(language, params, budget):
    """Bound optional Wikipedia calls and respect shared source back-pressure."""
    remaining = budget["deadline"] - monotonic()
    if (
        remaining <= 0
        or budget["calls"] >= REQUEST_LIMIT
        or cache.get("wikipedia_retry_after")
    ):
        raise UnavailableError
    budget["calls"] += 1
    try:
        data = services.api_request(
            "wikidata",
            "GET",
            f"https://{language}.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "formatversion": 2,
                "redirects": 1,
                **params,
            },
            headers={"User-Agent": "Yamtrack (https://github.com/FuzzyGrim/Yamtrack)"},
            timeout=min(8, remaining),
        )
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        if response is not None and response.status_code in {429, 503}:
            value = response.headers.get("Retry-After", "60")
            cache.set(
                "wikipedia_retry_after",
                "limited",
                min(86400, max(5, int(value))) if value.isdigit() else 60,
            )
        raise UnavailableError from error
    if not isinstance(data, dict) or "error" in data:
        raise UnavailableError
    continuation = data.get("continue", {})
    if not isinstance(continuation, dict) or set(continuation) - {
        "iistart",
        "continue",
    }:
        raise UnavailableError
    query = data.get("query")
    if not isinstance(query, dict) or not isinstance(query.get("pages"), list):
        raise UnavailableError
    if not all(isinstance(page, dict) for page in query["pages"]):
        raise UnavailableError
    return query


def find_page(query, title):
    """Follow only normalization and redirects returned for the requested title."""
    for field in ("normalized", "redirects"):
        entries = query.get(field, [])
        if not isinstance(entries, list):
            raise UnavailableError
        for _step in range(len(entries) + 1):
            target = next(
                (
                    entry.get("to")
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("from") == title
                ),
                title,
            )
            if target == title:
                break
            if not isinstance(target, str):
                raise UnavailableError
            title = target
    matches = [page for page in query["pages"] if page.get("title") == title]
    if len(matches) != 1:
        raise UnavailableError
    return matches[0]


def valid_file_source(url, language, file_page):
    """Match the resolved file title on its actual local or shared repository."""
    shared = file_page.get("imagerepository") == "shared"
    host = "commons.wikimedia.org" if shared else f"{language}.wikipedia.org"
    if not commons.safe_url(url, host):
        return False
    path = unquote(urlsplit(url).path).replace("_", " ")
    namespaces = {"File", "Image", "File" if shared else FILE_NAMESPACES[language]}
    filename = file_page["title"].split(":", 1)[1].replace("_", " ")
    return any(path == f"/wiki/{namespace}:{filename}" for namespace in namespaces)


def build_artwork(work_id, language, article, file_page):
    """Retain article identity and source rights without granting reuse rights."""
    info = file_page["imageinfo"][0]
    expected_host = (
        "commons.wikimedia.org"
        if file_page.get("imagerepository") == "shared"
        else f"{language}.wikipedia.org"
    )
    rights = {
        key: commons.credit_text(value["value"], source_base=f"https://{expected_host}")
        for key, value in info["extmetadata"].items()
    }
    image = info.get("thumburl", "")
    parsed = urlsplit(image)
    source_url = info.get("descriptionurl", "")
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"upload.wikimedia.org", "thumb.wikimedia.org"}
        or not parsed.path.startswith(
            (f"/wikipedia/{language}/", "/wikipedia/commons/")
        )
        or parsed.fragment
        or not commons.safe_url(source_url, expected_host)
        or not valid_file_source(source_url, language, file_page)
        or info.get("mime") not in {"image/jpeg", "image/png", "image/webp"}
        or min(info["width"], info["height"], info["thumbwidth"], info["thumbheight"])
        <= 0
        or info["thumbwidth"] > THUMBNAIL_WIDTH
        or "badfile" in info
        or rights.get("DeletionReason")
        or not rights.get("LicenseShortName")
        or commons.unrelated_portrait(file_page)
    ):
        return {}
    non_free = (
        rights.get("NonFree", "").casefold() == "true"
        or "fair use" in rights["LicenseShortName"].casefold()
    )
    license_url = rights.get("LicenseUrl", "")
    if license_url.startswith("//"):
        license_url = "https:" + license_url
    if not any(
        commons.safe_url(license_url, host)
        for host in (expected_host, "creativecommons.org")
    ):
        license_url = source_url
    return {
        "provider": "wikipedia",
        "policy": VERSION,
        "work_id": work_id,
        "image": image,
        "source_url": source_url,
        "title": file_page["title"].removeprefix("File:"),
        "artist": rights.get("Artist", ""),
        "credit": rights.get("Credit", ""),
        "attribution": rights.get("Attribution", ""),
        "permission": rights.get("Permission", ""),
        "license": "Non-free; Wikipedia rationale"
        if non_free
        else rights["LicenseShortName"],
        "license_url": source_url if non_free else license_url,
        "non_free": non_free,
        "rights": rights,
        "basis_notice": NON_FREE_NOTICE
        if non_free
        else "Use is subject to the source file's license and notices.",
        "notices": " ".join(
            rights.get(key, "")
            for key in ("UsageTerms", "Restrictions", "LicenseNotices")
        ).strip(),
        "article_url": page_url(language, article["title"]),
        "language": language,
        "evidence": "Wikipedia article QID and PageImages",
        "selection_complete": True,
        "poster_search_complete": True,
        "article": article,
        "file": file_page,
        "selected_filename": article["pageimage"],
    }


def checked_artwork(work_id, language, article, file_page):
    """Treat malformed nested API values as unavailable optional artwork."""
    try:
        if article["pageprops"]["wikibase_item"] != work_id or article["ns"] != 0:
            return {}
        if (
            "disambiguation" in article["pageprops"]
            or file_page["ns"] != commons.FILE_NAMESPACE
        ):
            return {}
        records = (
            (article,)
            if file_page.get("imagerepository") == "shared"
            else (article, file_page)
        )
        for record in records:
            if (
                type(record["pageid"]) is not int
                or record["pageid"] <= 0
                or type(record["lastrevid"]) is not int
            ):
                raise UnavailableError
        if file_page.get("imagerepository") not in {"local", "shared"}:
            raise UnavailableError
        if (
            not isinstance(file_page["imageinfo"][0].get("sha1"), str)
            or not file_page["imageinfo"][0]["sha1"]
        ):
            raise UnavailableError
        return build_artwork(work_id, language, article, file_page)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as error:
        raise UnavailableError from error


def site_artworks(language, entries, budget):
    """Resolve a page of exact articles and their files in two batched requests."""
    result = {}
    query = request_pages(
        language,
        {
            "titles": "|".join(title for _identifier, title in entries),
            "prop": "pageprops|pageimages|info",
            "ppprop": "wikibase_item|disambiguation",
            "piprop": "name",
            "pilicense": "any",
        },
        budget,
    )
    selected = {}
    for identifier, title in entries:
        try:
            article = find_page(query, title)
        except UnavailableError:
            result[identifier] = None
            continue
        if "missing" in article:
            result[identifier] = {}
            continue
        props = article.get("pageprops", {})
        if (
            not isinstance(props, dict)
            or not isinstance(props.get("wikibase_item"), str)
            or ("pageimage" in article and not isinstance(article["pageimage"], str))
        ):
            result[identifier] = None
            continue
        if (
            props.get("wikibase_item") != identifier
            or "disambiguation" in props
            or not article.get("pageimage")
        ):
            result[identifier] = {}
        else:
            selected[identifier] = article
    if not selected:
        return result
    try:
        files = request_pages(
            language,
            {
                "titles": "|".join(
                    dict.fromkeys(
                        "File:" + article["pageimage"] for article in selected.values()
                    )
                ),
                "prop": "imageinfo|info",
                "iiprop": "url|size|mime|sha1|extmetadata",
                "iiurlwidth": THUMBNAIL_WIDTH,
                "iilimit": 1,
                "iiextmetadatalanguage": "en",
            },
            budget,
        )
    except UnavailableError:
        return {**result, **dict.fromkeys(selected)}
    for identifier, article in selected.items():
        try:
            file_page = find_page(files, "File:" + article["pageimage"])
            result[identifier] = checked_artwork(
                identifier, language, article, file_page
            )
        except UnavailableError:
            result[identifier] = None
    return result


def artworks(works):
    """Prefer exact article imagery without hiding works during outages."""
    result = {}
    pending = {}
    budget = {"calls": 0, "deadline": monotonic() + TIME_LIMIT}
    for work in works:
        identifier = work["media_id"]
        links = work.get("wikipedia_sitelinks", {})
        signature = [work.get("work_revision"), links]
        cached = cache.get(cache_key(identifier))
        if cached is not None and cached.get("signature") == signature:
            result[identifier] = cached["artwork"]
        else:
            pending[identifier] = (
                signature,
                [
                    (language, links[language])
                    for language in LANGUAGES
                    if language in links
                ][:LANGUAGE_LIMIT],
            )
            result[identifier] = {}
    failures = set()
    for language in LANGUAGES:
        entries = [
            (identifier, title)
            for identifier, (_signature, links) in pending.items()
            for site, title in links
            if site == language and not result[identifier]
        ]
        if not entries:
            continue
        try:
            found = site_artworks(language, entries, budget)
            failures.update(
                identifier for identifier, value in found.items() if value is None
            )
            result.update(found)
        except (UnavailableError, TypeError, ValueError, AttributeError):
            failures.update(identifier for identifier, _title in entries)
    cache_results(result, pending, failures)
    return result


def incomplete(artwork):
    """Distinguish confirmed absence from a failed or partial article selection."""
    return artwork is None or (
        bool(artwork) and not artwork.get("selection_complete", True)
    )


def cache_results(result, pending, failures):
    """Cache complete selections and mark incomplete per-work results retryable."""
    for identifier, (signature, _links) in pending.items():
        if result[identifier]:
            result[identifier]["retrieved_at"] = timezone.now().isoformat()
        if identifier in failures:
            result[identifier] = (
                {**result[identifier], "selection_complete": False}
                if result[identifier]
                else None
            )
        else:
            cache.set(
                cache_key(identifier),
                {"signature": signature, "artwork": result[identifier]},
                3600,
            )


def restored_artwork(artwork, work_id, image):
    """Restore source assertions without certifying fair use or global identity."""
    if (
        not isinstance(artwork, dict)
        or artwork.get("provider") != "wikipedia"
        or artwork.get("policy") != VERSION
    ):
        return {}
    language = artwork.get("language")
    evidence_id = artwork.get("evidence_work_id", work_id)
    if (
        language not in LANGUAGES
        or not isinstance(evidence_id, str)
        or not re.fullmatch(r"Q[1-9][0-9]*", evidence_id)
    ):
        return {}
    try:
        expected = checked_artwork(
            evidence_id, language, artwork["article"], artwork["file"]
        )
    except (UnavailableError, KeyError):
        return {}
    if (
        not expected
        or artwork.get("work_id") != work_id
        or artwork.get("image") != image
    ):
        return {}
    if not isinstance(artwork.get("retrieved_at"), str) or any(
        artwork.get(key) != value
        for key, value in expected.items()
        if key not in {"work_id", "selection_complete"}
    ):
        return {}
    return artwork.copy()

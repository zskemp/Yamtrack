"""Work-linked Commons artwork with explicit reuse metadata."""

import logging
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import strip_tags

from app.models import Sources
from app.providers import services

logger = logging.getLogger(__name__)
BASE_URL = "https://commons.wikimedia.org/w/api.php"
POLICY_VERSION = 3
MIN_IMAGE_DIMENSION = 200


class ArtworkUnavailableError(Exception):
    """Transient provider failure, distinct from a rejected asset."""


LICENSES = {
    "/publicdomain/zero/1.0/": ("CC0 1.0", "Cc-zero"),
    "/licenses/by/2.0/": ("CC BY 2.0", "Cc-by-2.0"),
    "/licenses/by/4.0/": ("CC BY 4.0", "Cc-by-4.0"),
    "/licenses/by-sa/3.0/": ("CC BY-SA 3.0", "Cc-by-sa-3.0"),
    "/licenses/by-sa/4.0/": ("CC BY-SA 4.0", "Cc-by-sa-4.0"),
}
WARNING_PATTERNS = (
    "copyright violation",
    "copyright violations",
    "copyvio",
    "deletion",
    "no permission",
    "no source",
    "no license",
    "no author",
    "missing permission",
    "missing source",
    "missing author",
    "missing license",
    "permission pending",
    "permission received",
    "disputed",
    "personality rights",
    "trademark",
    "license review needed",
    "license review failed",
    "unreviewed",
)
ALLOWED_TEMPLATES = {
    "information",
    "artwork",
    "photograph",
    "self",
    "own",
    "en",
    "de",
    "fr",
    "es",
    "it",
    "ru",
    "cc-by-sa-4.0",
    "cc-by-sa-3.0",
    "cc-by-4.0",
    "cc-by-2.0",
    "cc-zero",
    "cc-by-sa-layout",
    "cc-by-layout",
    "cc-zero-layout",
    "license template tag",
    "infobox template tag",
    "flickrreview",
    "flickr",
    "flickr uploaded by",
    "location",
    "object location",
    "taken on",
    "according to exif data",
    "int",
    "lang",
    "original upload log",
    "pd-self",
    "gfdl",
    "gfdl-1.2",
}


class CreditText(HTMLParser):
    """Preserve notice links as printable URLs without accepting executable HTML."""

    def __init__(self):
        """Initialize plain text and nested link buffers."""
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        """Retain safe notice links and block boundaries."""
        if tag == "a":
            target = dict(attrs).get("href", "")
            if target.startswith("//"):
                target = "https:" + target
            if target.startswith("/"):
                target = "https://commons.wikimedia.org" + target
            try:
                parsed = urlsplit(target)
                valid = (
                    parsed.scheme in {"http", "https"}
                    and parsed.hostname
                    and not parsed.username
                )
            except ValueError:
                valid = False
            self.links.append(target if valid else "")
        elif tag in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        """Append the referenced URL after its credited label."""
        if tag == "a" and self.links:
            target = self.links.pop()
            if target:
                self.parts.append(f" ({target})")

    def handle_data(self, data):
        """Keep notice text for escaped template rendering."""
        self.parts.append(data)


def credit_text(value):
    """Convert credited HTML to escaped text while retaining supplied URLs."""
    if not isinstance(value, str):
        return str(value) if isinstance(value, (int, float, bool)) else ""
    parser = CreditText()
    parser.feed(value)
    return "".join(parser.parts).strip()


def known_templates(page):
    """Unknown file notices require review instead of silently passing."""
    return all(
        not entry["title"].startswith("Template:")
        or entry["title"].removeprefix("Template:").casefold().replace("_", " ")
        in ALLOWED_TEMPLATES
        for entry in page.get("templates", [])
    )


def safe_url(value, host):
    """Accept only the expected HTTPS source without credentials or custom ports."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc == host and not parsed.fragment


def text(value):
    """Keep provider attribution as plain text, never executable markup."""
    return unescape(strip_tags(value)).strip()


def qualified_image(page):
    """Reject incomplete rights information and known warning categories/templates."""
    info = next(iter(page.get("imageinfo", [])), {})
    metadata = info.get("extmetadata", {})
    value = {
        key: credit_text(field.get("value", "")) for key, field in metadata.items()
    }
    if (
        "badfile" in info
        or not known_templates(page)
        or value.get("Restrictions")
        or value.get("DeletionReason")
        or value.get("NonFree", "").lower() in {"true", "1", "yes"}
    ):
        return None
    tags = [
        entry["title"].casefold().replace("_", " ")
        for kind in ("categories", "templates")
        for entry in page.get(kind, [])
    ]
    if "template:costume" in tags or any(
        pattern in tag for tag in tags for pattern in WARNING_PATTERNS
    ):
        return None
    license_url = value.get("LicenseUrl", "").replace("http://", "https://", 1)
    parsed = urlsplit(
        license_url if safe_url(license_url, "creativecommons.org") else ""
    )
    license_path = parsed.path.rstrip("/") + "/"
    license_entry = LICENSES.get(license_path)
    if (
        not safe_url(license_url, "creativecommons.org")
        or not license_entry
        or parsed.query
    ):
        return None
    license_name, template = license_entry
    artist = value.get("Artist", "")
    if (
        not any(tag == f"template:{template.casefold()}" for tag in tags)
        or not artist
        or artist.casefold() in {"unknown", "unknown author", "anonymous"}
    ):
        return None
    image_url = info.get("thumburl") or info.get("url", "")
    source_url = info.get("descriptionurl", "")
    width, height = info.get("width", 0), info.get("height", 0)
    if (
        not any(
            safe_url(image_url, host)
            for host in ("upload.wikimedia.org", "thumb.wikimedia.org")
        )
        or not safe_url(source_url, "commons.wikimedia.org")
        or min(width, height) < MIN_IMAGE_DIMENSION
        or info.get("mime")
        not in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }
    ):
        return None
    return {
        "image": image_url,
        "source_url": source_url,
        "title": value.get("ObjectName") or page["title"].removeprefix("File:"),
        "artist": artist,
        "credit": value.get("Credit", ""),
        "attribution": value.get("Attribution", ""),
        "permission": value.get("Permission", ""),
        "license": license_name,
        "license_url": "https://creativecommons.org" + license_path,
        "page_id": page["pageid"],
        "revision": page.get("lastrevid"),
        "sha1": info.get("sha1", ""),
        "timestamp": info.get("timestamp", ""),
        "retrieved_at": timezone.now().isoformat(),
        "width": width,
        "height": height,
        "rights": value,
        "policy": POLICY_VERSION,
    }


def require_available(metadata):
    """Prevent explicit metadata sync from persisting a failed artwork lookup."""
    if metadata.get("artwork_unavailable"):
        raise services.ProviderAPIError(
            Sources.WIKIDATA.value,
            ArtworkUnavailableError(),
            "Artwork provider unavailable. Please retry sync later",
        )


def restored_artwork(artwork, work_id, image):
    """Validate portable credits without needing a live provider lookup."""
    if not isinstance(artwork, dict):
        return {}
    required = (
        "image",
        "source_url",
        "title",
        "artist",
        "license",
        "license_url",
        "work_id",
    )
    if any(
        not isinstance(artwork.get(key), str) or not artwork[key] for key in required
    ):
        return {}
    if artwork["work_id"] != work_id or artwork["image"] != image:
        return {}
    if not complete_credit(artwork):
        return {}
    valid_licenses = {
        "https://creativecommons.org" + path: name
        for path, (name, _template) in LICENSES.items()
    }
    if (
        valid_licenses.get(artwork["license_url"]) != artwork["license"]
        or not safe_url(artwork["source_url"], "commons.wikimedia.org")
        or not any(
            safe_url(image, host)
            for host in ("upload.wikimedia.org", "thumb.wikimedia.org")
        )
    ):
        return {}
    result = artwork.copy()
    for key in ("title", "artist", "credit", "attribution", "permission"):
        result[key] = (
            text(artwork.get(key, "")) if isinstance(artwork.get(key, ""), str) else ""
        )
    return result


def complete_credit(artwork):
    """Require the complete exported rights record, not just a license label."""
    rights = artwork.get("rights")
    if not isinstance(rights, dict) or artwork.get("policy") != POLICY_VERSION:
        return False
    fields = {
        "artist": "Artist",
        "credit": "Credit",
        "attribution": "Attribution",
        "permission": "Permission",
    }
    return all(
        key in artwork
        and isinstance(artwork[key], str)
        and artwork[key] == rights.get(source, "")
        for key, source in fields.items()
    ) and all(
        key in artwork
        for key in (
            "page_id",
            "revision",
            "sha1",
            "retrieved_at",
            "evidence",
            "work_revision",
        )
    )


def request_data(params):
    """Use existing transport while respecting Wikimedia back-pressure."""
    if cache.get("commons_retry_after"):
        raise ArtworkUnavailableError
    try:
        response = services.api_request(
            Sources.WIKIDATA.value,
            "GET",
            BASE_URL,
            params={"format": "json", **params},
            headers={"User-Agent": "Yamtrack (https://github.com/FuzzyGrim/Yamtrack)"},
        )
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        if response is not None and response.status_code in {429, 503}:
            retry_after = response.headers.get("Retry-After", "60")
            cache.set(
                "commons_retry_after",
                "limited",
                max(5, min(int(retry_after), 86400)) if retry_after.isdigit() else 60,
            )
        logger.warning("Commons artwork request failed")
        raise ArtworkUnavailableError from error
    if "error" in response:
        cache.set("commons_retry_after", "limited", 60)
        raise ArtworkUnavailableError
    return response


def depicted_files(work_id):
    """Find at most three exact-work depictions and verify their statements."""
    response = request_data(
        {
            "action": "query",
            "list": "search",
            "srnamespace": 6,
            "srlimit": 3,
            "srsearch": f"haswbstatement:P180={work_id}",
        }
    )
    hits = response.get("query", {}).get("search", [])[:3]
    if not hits:
        return []
    entities = request_data(
        {
            "action": "wbgetentities",
            "props": "claims",
            "ids": "|".join(f"M{hit['pageid']}" for hit in hits),
        }
    ).get("entities", {})
    return [
        hit["title"].removeprefix("File:")
        for hit in hits
        if any(
            claim.get("rank") != "deprecated"
            and claim.get("mainsnak", {})
            .get("datavalue", {})
            .get("value", {})
            .get("id")
            == work_id
            for claim in entities.get(f"M{hit['pageid']}", {})
            .get("statements", {})
            .get("P180", [])
        )
    ]


def image_pages(filenames):
    """Fetch file rights and warning metadata in small complete batches."""
    pages = []
    for offset in range(0, len(filenames), 3):
        response = request_data(
            {
                "action": "query",
                "redirects": 1,
                "titles": "|".join(
                    f"File:{filename}" for filename in filenames[offset : offset + 3]
                ),
                "prop": "imageinfo|categories|templates|info",
                "cllimit": 500,
                "tllimit": 500,
                "iiprop": "url|size|mime|timestamp|sha1|extmetadata|badfile",
                "iiurlwidth": 300,
                "iiextmetadatalanguage": "en",
            }
        )
        if "continue" not in response:
            pages.extend(response.get("query", {}).get("pages", {}).values())
    return pages


def artwork(work_id, filenames, revision, work_forms=()):
    """Preserve the distinction between unavailable and confirmed absent images."""
    try:
        return select_artwork(work_id, filenames, revision, work_forms)
    except ArtworkUnavailableError:
        return None


def suitable_for_work(page, work_forms, *, enrichment=False):
    """Reject explicit adaptation/form conflicts independently of image rights."""
    info = next(iter(page.get("imageinfo", [])), {})
    description = text(
        info.get("extmetadata", {}).get("ImageDescription", {}).get("value", "")
    ).casefold()
    if re.search(
        r"\b(advertisement|audience|coin|rewrite|parody|adaptation|"
        r"stage set|set design|front stage|film)\b",
        description,
    ):
        return False
    if any(
        re.search(rf"\b{form}\b", description) and form not in work_forms
        for form in ("opera", "ballet", "musical")
    ):
        return False
    return not enrichment or bool(
        re.search(
            r"\b(performance|production|opera|ballet|poster|illustration)\b",
            description,
        )
    )


def select_artwork(work_id, filenames, revision, work_forms):
    """Select a reusable direct work image, bounded to three candidates."""
    filenames = sorted(
        {filename for filename in filenames if isinstance(filename, str)}
    )[:3]
    key = f"commons_v{POLICY_VERSION}_{work_id}"
    cached = cache.get(key)
    if (
        cached is not None
        and cached.get("work_revision") == revision
        and cached.get("work_forms") == list(work_forms)
    ):
        return cached
    candidates = [
        qualified_image(page)
        for page in image_pages(filenames)
        if suitable_for_work(page, work_forms)
    ]
    candidates = [candidate for candidate in candidates if candidate]
    evidence = "P18"
    if not candidates:
        evidence = "P180"
        for page in image_pages(depicted_files(work_id)):
            if not suitable_for_work(page, work_forms, enrichment=True):
                continue
            candidate = qualified_image(page)
            if candidate:
                candidates.append(candidate)
    candidates.sort(
        key=lambda candidate: (
            "poster" not in candidate["title"].casefold(),
            abs(candidate["width"] / candidate["height"] - 2 / 3),
            candidate["page_id"],
        )
    )
    selected = candidates[0] if candidates else {}
    if selected:
        selected.update(
            work_id=work_id,
            work_revision=revision,
            evidence=evidence,
            work_forms=list(work_forms),
        )
        cache.set(key, selected, 3600)
    return selected

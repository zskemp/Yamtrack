"""Work-linked Commons artwork with explicit reuse metadata."""

import logging
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import strip_tags

from app.models import Sources
from app.providers import services

logger = logging.getLogger(__name__)
BASE_URL = "https://commons.wikimedia.org/w/api.php"
POLICY_VERSION = 5
LEGACY_POLICY_VERSION = 3
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
    "license review needed",
    "license review failed",
    "unreviewed",
    "pd old auto: no death date",
    "pd-old-auto without death date",
)
PUBLIC_DOMAIN_BASES = {
    "pd-textlogo": (
        "Simple text/logo below the copyright originality threshold; "
        "trademark rights may still apply."
    ),
    "pd-old-auto-expired": (
        "Commons identifies expired copyright in the source country and the "
        "United States; other jurisdictions may differ."
    ),
    "pd-old-100-expired": (
        "Commons identifies an author deceased over 100 years ago and "
        "expired United States copyright."
    ),
    "pd-old-70-expired": (
        "Commons identifies an author deceased over 70 years ago and expired "
        "United States copyright; longer terms may apply elsewhere."
    ),
}
STANDARD_RESTRICTIONS = {
    "personality",
    "personality rights",
    "trademark",
    "trademarked",
    "costume",
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


def reuse_grant(value, tags):
    """Use explicit source grants or named public-domain bases, not availability."""
    if value.get("Copyrighted", "").casefold() == "false":
        for template, notice in PUBLIC_DOMAIN_BASES.items():
            if f"template:{template}" in tags:
                return {
                    "license": "Public domain",
                    "license_url": f"https://commons.wikimedia.org/wiki/Template:{template}",
                    "basis": template,
                    "basis_notice": notice,
                }
    license_url = value.get("LicenseUrl", "").replace("http://", "https://", 1)
    parsed = urlsplit(
        license_url if safe_url(license_url, "creativecommons.org") else ""
    )
    license_path = parsed.path.rstrip("/") + "/"
    license_entry = LICENSES.get(license_path)
    if not license_entry or parsed.query:
        return None
    license_name, template = license_entry
    if not any(
        tag == f"template:{template.casefold()}"
        or tag.startswith(f"template:{template.casefold()}-migrated")
        for tag in tags
    ):
        return None
    if not value.get("Artist") or value["Artist"].casefold() in {
        "unknown",
        "unknown author",
        "anonymous",
    }:
        return None
    return {
        "license": license_name,
        "license_url": "https://creativecommons.org" + license_path,
        "basis": "",
        "basis_notice": "",
    }


def reuse_notices(value, tags):
    """Keep standard notices; unknown substantive restrictions require review."""
    restrictions = {
        entry.strip().casefold()
        for entry in value.get("Restrictions", "").split("|")
        if entry.strip()
    }
    if restrictions - STANDARD_RESTRICTIONS:
        return None
    for template, restriction in [
        ("personality rights", "personality"),
        ("trademarked", "trademark"),
        ("costume", "costume"),
    ]:
        if f"template:{template}" in tags:
            restrictions.add(restriction)
    notices = []
    if restrictions:
        notices.append(
            "Source notices: "
            + ", ".join(sorted(restrictions))
            + ". Copyright permission does not grant endorsement, personality, "
            "trademark or separate design rights. See the source file for "
            "use-specific restrictions."
        )
    if any("migrated-with-disclaimers" in tag for tag in tags):
        if not value.get("LicenseNotices"):
            return None
        notices.append(value["LicenseNotices"])
    return " ".join(notices)


def license_notices(page, license_name):
    """Capture file-specific migrated-license notices from the rendered grant."""
    response = request_data(
        {
            "action": "parse",
            "pageid": page["pageid"],
            "prop": "text",
            "disablelimitreport": 1,
        }
    )
    parsed = response.get("parse", {})
    if parsed.get("revid") and page.get("lastrevid") != parsed["revid"]:
        return ""
    soup = BeautifulSoup(parsed.get("text", {}).get("*", ""), "html.parser")
    for block in soup.select(".licensetpl"):
        name = block.select_one(".licensetpl_short")
        if name and name.get_text(strip=True) == license_name:
            return credit_text(str(block))
    return ""


def qualified_image(page):
    """Reject incomplete rights information and known warning categories/templates."""
    info = next(iter(page.get("imageinfo", [])), {})
    metadata = info.get("extmetadata", {})
    value = {
        key: credit_text(field.get("value", "")) for key, field in metadata.items()
    }
    if (
        "badfile" in info
        or value.get("DeletionReason")
        or value.get("NonFree", "").lower() in {"true", "1", "yes"}
    ):
        return None
    tags = [
        entry["title"].casefold().replace("_", " ")
        for kind in ("categories", "templates")
        for entry in page.get(kind, [])
    ]
    if any(pattern in tag for tag in tags for pattern in WARNING_PATTERNS):
        return None
    grant = reuse_grant(value, tags)
    if grant and any("migrated-with-disclaimers" in tag for tag in tags):
        value["LicenseNotices"] = license_notices(page, grant["license"])
    notices = reuse_notices(value, tags)
    if grant is None or notices is None:
        return None
    artist = value.get("Artist", "")
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
        **grant,
        "notices": notices,
        "page_id": page["pageid"],
        "revision": page.get("lastrevid"),
        "sha1": info.get("sha1", ""),
        "timestamp": info.get("timestamp", ""),
        "retrieved_at": timezone.now().isoformat(),
        "width": width,
        "height": height,
        "rights": value,
        "source_tags": tags,
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
    invalid_legacy = artwork.get("policy") == LEGACY_POLICY_VERSION and (
        not artwork.get("artist")
        or valid_licenses.get(artwork["license_url"]) != artwork["license"]
    )
    valid_licenses.update(
        {
            f"https://commons.wikimedia.org/wiki/Template:{basis}": "Public domain"
            for basis in PUBLIC_DOMAIN_BASES
        }
    )
    if (
        invalid_legacy
        or valid_licenses.get(artwork["license_url"]) != artwork["license"]
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
    if not isinstance(rights, dict) or artwork.get("policy") not in {
        3,
        4,
        POLICY_VERSION,
    }:
        return False
    if artwork.get("policy") != LEGACY_POLICY_VERSION and any(
        not isinstance(artwork.get(key), str)
        for key in ("notices", "basis", "basis_notice")
    ):
        return False
    if artwork.get("policy") != LEGACY_POLICY_VERSION and not consistent_reuse_record(
        artwork
    ):
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


def consistent_reuse_record(artwork):
    """Keep notices and public-domain bases consistent with exported evidence."""
    tags = artwork.get("source_tags")
    rights = artwork.get("rights")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        return False
    if not all(isinstance(value, str) for value in rights.values()):
        return False
    grant = reuse_grant(rights, tags)
    return (
        grant is not None
        and all(artwork.get(key) == value for key, value in grant.items())
        and artwork.get("notices") == reuse_notices(rights, tags)
        and not any(pattern in tag for tag in tags for pattern in WARNING_PATTERNS)
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
    """Select a reusable work image from at most five direct candidates."""
    filenames = list(
        dict.fromkeys(filename for filename in filenames if isinstance(filename, str))
    )[:5]
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
    evidence = "P18/P154"
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

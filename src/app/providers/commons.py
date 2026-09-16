"""Work-linked Commons artwork with explicit reuse metadata."""

import logging
import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import strip_tags

from app.models import Sources
from app.providers import services

logger = logging.getLogger(__name__)
BASE_URL = "https://commons.wikimedia.org/w/api.php"
POLICY_VERSION = 8
LEGACY_POLICY_VERSION = 3
PD_ART_NOTICE_POLICY_VERSION = 8
MIN_IMAGE_DIMENSION = 200
DEPICTION_CANDIDATE_LIMIT = 12
CATEGORY_CANDIDATE_LIMIT = 10
CATEGORY_NAMESPACE = 14
FILE_NAMESPACE = 6
MIN_CREATOR_WORDS = 2
ARTWORK_REQUEST_LIMIT = 24
ARTWORK_TIME_LIMIT = 20
ARTWORK_HTTP_TIMEOUT = 8
_request_budget = ContextVar("commons_request_budget", default=None)


class ArtworkUnavailableError(Exception):
    """Transient provider failure, distinct from a rejected asset."""


@dataclass
class ArtworkBudget:
    """Share enrichment effort across every work in a request."""

    deadline: float
    remaining: int = ARTWORK_REQUEST_LIMIT

    def reserve(self):
        """Reserve one HTTP call with a timeout bounded by the remaining time."""
        remaining_time = self.deadline - monotonic()
        if self.remaining <= 0 or remaining_time <= 0:
            raise ArtworkUnavailableError
        self.remaining -= 1
        return min(remaining_time, ARTWORK_HTTP_TIMEOUT, settings.REQUEST_TIMEOUT)


@contextmanager
def request_budget():
    """Scope an artwork budget to a page without leaking across users/threads."""
    token = _request_budget.set(ArtworkBudget(monotonic() + ARTWORK_TIME_LIMIT))
    try:
        yield
    finally:
        _request_budget.reset(token)


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
    "pd-us-dust-jacket": (
        "Commons identifies this book jacket as public domain in the United "
        "States because it was published without the required copyright notice. "
        "This is not a grant for the book text or other jurisdictions."
    ),
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
    if "template:pd-art" in tags:
        notices.append(
            "Commons PD-Art assessment covers a faithful reproduction of "
            "two-dimensional public-domain art; reproduction rights can differ "
            "by jurisdiction. https://commons.wikimedia.org/wiki/Commons:Reuse_of_PD-Art_photographs"
        )
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


def upgrade_reproduction_notice(artwork):
    """Reconstruct new caveats only after checking the exact legacy notice."""
    if (
        artwork.get("policy") not in {4, 5, 6, 7}
        or not isinstance(artwork.get("rights"), dict)
        or not all(isinstance(value, str) for value in artwork["rights"].values())
        or not isinstance(artwork.get("source_tags"), list)
        or not all(isinstance(tag, str) for tag in artwork["source_tags"])
        or "template:pd-art" not in artwork["source_tags"]
    ):
        return artwork
    previous_tags = [tag for tag in artwork["source_tags"] if tag != "template:pd-art"]
    if artwork.get("notices") != reuse_notices(artwork["rights"], previous_tags):
        return artwork
    return {
        **artwork,
        "notices": reuse_notices(artwork["rights"], artwork["source_tags"]),
        "policy": PD_ART_NOTICE_POLICY_VERSION,
    }


def restored_artwork(artwork, work_id, image):
    """Validate portable credits without needing a live provider lookup."""
    if not isinstance(artwork, dict):
        return {}
    artwork = upgrade_reproduction_notice(artwork)
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
        5,
        6,
        7,
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
    if artwork.get("evidence") == "P373/description" and not complete_category_evidence(
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
    budget = _request_budget.get()
    timeout = (
        budget.reserve()
        if budget
        else min(ARTWORK_HTTP_TIMEOUT, settings.REQUEST_TIMEOUT)
    )
    try:
        response = services.api_request(
            Sources.WIKIDATA.value,
            "GET",
            BASE_URL,
            params={"format": "json", **params},
            headers={"User-Agent": "Yamtrack (https://github.com/FuzzyGrim/Yamtrack)"},
            timeout=timeout,
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
    """Find a bounded set of exact-work depictions and verify their statements."""
    response = request_data(
        {
            "action": "query",
            "list": "search",
            "srnamespace": 6,
            "srlimit": DEPICTION_CANDIDATE_LIMIT,
            "srsearch": f"haswbstatement:P180={work_id}",
        }
    )
    hits = response.get("query", {}).get("search", [])[:DEPICTION_CANDIDATE_LIMIT]
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


def merge_file_metadata(merged, response):
    """Reject malformed fragments before merging rights-bearing file metadata."""
    pages = response.get("query", {}).get("pages")
    if not isinstance(pages, dict) or not pages:
        raise ArtworkUnavailableError
    if merged and set(pages) != set(merged):
        raise ArtworkUnavailableError
    for page_id, page in pages.items():
        if not isinstance(page, dict):
            raise ArtworkUnavailableError
        previous = merged.setdefault(page_id, {})
        if previous and "missing" in page:
            raise ArtworkUnavailableError
        if "missing" not in page and (
            (response.get("continue") and not isinstance(page.get("lastrevid"), int))
            or (previous and previous.get("lastrevid") != page.get("lastrevid"))
        ):
            raise ArtworkUnavailableError
        for key, value in page.items():
            if key in {"templates", "categories"}:
                if not isinstance(value, list):
                    raise ArtworkUnavailableError
                previous.setdefault(key, []).extend(value)
            else:
                previous[key] = value


def image_pages(filenames):
    """Fetch file rights and warning metadata in small complete batches."""
    for offset in range(0, len(filenames), 3):
        params = {
            "action": "query",
            "redirects": 1,
            "titles": "|".join(
                f"File:{filename}" for filename in filenames[offset : offset + 3]
            ),
            "prop": "imageinfo|categories|templates|info",
            "cllimit": 500,
            "tllimit": 500,
            "iiprop": "url|size|mime|timestamp|sha1|extmetadata|badfile",
            "iilimit": 1,
            "iiurlwidth": 300,
            "iiextmetadatalanguage": "en",
        }
        merged = {}
        continuation = {}
        for _page in range(2):
            response = request_data({**params, **continuation})
            if "continue" in response and not isinstance(response["continue"], dict):
                raise ArtworkUnavailableError
            merge_file_metadata(merged, response)
            continuation = {
                key: value
                for key, value in response.get("continue", {}).items()
                if key in {"clcontinue", "tlcontinue"}
            }
            if not continuation:
                break
            continuation["continue"] = "||"
        if continuation:
            raise ArtworkUnavailableError
        yield from merged.values()


def artwork(work_id, filenames, revision, work_forms=(), *, category_context=None):
    """Preserve the distinction between unavailable and confirmed absent images."""
    try:
        return select_artwork(
            work_id, filenames, revision, work_forms, category_context
        )
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


def verified_candidates(filenames, work_forms, *, enrichment=False):
    """Keep complete verified candidates if a later optional batch is unavailable."""
    candidates = []
    try:
        for page in image_pages(filenames):
            if not suitable_for_work(page, work_forms, enrichment=enrichment):
                continue
            candidate = qualified_image(page)
            if candidate:
                candidates.append(candidate)
    except ArtworkUnavailableError:
        return candidates, True
    return candidates, False


def normalized_words(value):
    """Normalize spelling and punctuation without fuzzy title/name equivalence."""
    return " ".join(
        re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())
    )


def category_match(page, context, work_forms):
    """Require an explicit depicted-work title, form and creator description."""
    info = next(iter(page.get("imageinfo", [])), {})
    description = text(
        info.get("extmetadata", {}).get("ImageDescription", {}).get("value", "")
    )
    normalized = normalized_words(description)
    for title in context.get("titles", [])[:16]:
        for creator in context.get("creators", [])[:32]:
            if len(normalized_words(creator).split()) < MIN_CREATOR_WORDS:
                continue
            for form in work_forms:
                if form not in {"play", "opera", "musical", "ballet"}:
                    continue
                pattern = (
                    r"^(?:illustration|photograph|photo|poster|scene|performance) "
                    r"(?:of|from|for) "
                    + re.escape(normalized_words(title))
                    + r" (?:a |an )?"
                    + form
                    + " by "
                    + re.escape(normalized_words(creator))
                    + r"(?=$| illustrator | photographed | published | performed | at )"
                )
                if re.search(pattern, normalized):
                    return {
                        "matched_title": title,
                        "matched_creator": creator,
                        "matched_form": form,
                        "match_description": description,
                        "match_source_html": info["extmetadata"]["ImageDescription"][
                            "value"
                        ],
                    }
    return None


def complete_category_evidence(artwork):
    """Check imported category evidence for completeness and internal consistency."""
    text_fields = (
        "category_title",
        "category_work_id",
        "matched_title",
        "matched_creator",
        "matched_form",
        "match_description",
        "match_source_html",
    )
    if any(
        not isinstance(artwork.get(key), str) or not artwork[key] for key in text_fields
    ):
        return False
    if any(
        type(artwork.get(key)) is not int or artwork[key] <= 0
        for key in ("category_id", "category_revision")
    ):
        return False
    if (
        not artwork["category_title"].startswith("Category:")
        or artwork["category_work_id"]
        != artwork.get("evidence_work_id", artwork.get("work_id"))
        or not isinstance(artwork.get("work_forms"), list)
        or artwork["matched_form"] not in artwork["work_forms"]
        or credit_text(artwork["match_source_html"])
        != artwork["rights"].get("ImageDescription")
    ):
        return False
    page = {
        "imageinfo": [
            {
                "extmetadata": {
                    "ImageDescription": {"value": artwork["match_source_html"]}
                }
            }
        ]
    }
    match = category_match(
        page,
        {
            "titles": [artwork["matched_title"]],
            "creators": [artwork["matched_creator"]],
        },
        [artwork["matched_form"]],
    )
    return (
        bool(match)
        and all(artwork.get(key) == value for key, value in match.items())
        and suitable_for_work(page, artwork["work_forms"])
    )


def category_query(params):
    """Validate optional discovery envelopes before reading nested source fields."""
    response = request_data(params)
    if not isinstance(response, dict) or not isinstance(response.get("query"), dict):
        raise ArtworkUnavailableError
    return response


def category_candidates(work_id, context, work_forms):
    """Discover direct files only through a reciprocal source-linked category."""
    if not context or not context.get("creators") or not context.get("categories"):
        return [], False
    candidates = []
    try:
        category_title = "Category:" + context["categories"][0].removeprefix(
            "Category:"
        )
        response = category_query(
            {"action": "query", "titles": category_title, "prop": "pageprops|info"}
        )
        pages = response.get("query", {}).get("pages")
        category = (
            next(iter(pages.values()))
            if isinstance(pages, dict) and len(pages) == 1
            else {}
        )
        if (
            not isinstance(category, dict)
            or not isinstance(category.get("lastrevid"), int)
            or not isinstance(category.get("pageid"), int)
            or not isinstance(category.get("title"), str)
            or not isinstance(category.get("pageprops", {}), dict)
        ):
            return [], True
        pageprops = category.get("pageprops", {})
        if (
            category.get("ns") != CATEGORY_NAMESPACE
            or pageprops.get("wikibase_item") != work_id
        ):
            return [], False
        members = category_query(
            {
                "action": "query",
                "list": "categorymembers",
                "cmpageid": category["pageid"],
                "cmtype": "file",
                "cmlimit": CATEGORY_CANDIDATE_LIMIT,
            }
        )
        files = members.get("query", {}).get("categorymembers")
        if not isinstance(files, list) or any(
            not isinstance(entry, dict) for entry in files
        ):
            return [], True
        filenames = [
            entry["title"].removeprefix("File:")
            for entry in files[:CATEGORY_CANDIDATE_LIMIT]
            if entry.get("ns") == FILE_NAMESPACE and isinstance(entry.get("title"), str)
        ]
        for page in image_pages(filenames):
            match = category_match(page, context, work_forms)
            if not match or not suitable_for_work(page, work_forms):
                continue
            candidate = qualified_image(page)
            if candidate:
                candidate.update(
                    **match,
                    category_id=category["pageid"],
                    category_title=category["title"],
                    category_revision=category["lastrevid"],
                    category_work_id=work_id,
                )
                candidates.append(candidate)
    except ArtworkUnavailableError:
        return candidates, True
    return candidates, bool(members.get("continue"))


def depiction_candidates(work_id, work_forms):
    """Let independent category discovery proceed after a depiction lookup fails."""
    try:
        return verified_candidates(depicted_files(work_id), work_forms, enrichment=True)
    except ArtworkUnavailableError:
        return [], True


def select_artwork(work_id, filenames, revision, work_forms, category_context=None):
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
    candidates, unavailable = verified_candidates(filenames, work_forms)
    evidence = "P18/P154"
    if not candidates:
        evidence = "P180"
        candidates, depiction_unavailable = depiction_candidates(work_id, work_forms)
        unavailable = unavailable or depiction_unavailable
    if not candidates:
        evidence = "P373/description"
        candidates, category_unavailable = category_candidates(
            work_id, category_context, work_forms
        )
        unavailable = unavailable or category_unavailable
    if not candidates and unavailable:
        raise ArtworkUnavailableError
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
            selection_complete=not unavailable,
        )
        if not unavailable:
            cache.set(key, selected, 3600)
    return selected

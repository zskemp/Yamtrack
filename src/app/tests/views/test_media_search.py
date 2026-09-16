import csv
import json
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Barrier, local
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from app.models import (
    Item,
    MediaTypes,
    Movie,
    Sources,
    Theater,
    TheaterRedirect,
)
from lists.models import CustomList


class MediaSearchViewTests(TestCase):
    """Test the media search view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.search")
    def test_media_search_view(self, mock_search):
        """Test the media search view."""
        mock_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "238",
                    "title": "Test Movie",
                    "media_type": MediaTypes.MOVIE.value,
                    "source": Sources.TMDB.value,
                    "image": "http://example.com/image.jpg",
                },
            ],
        }

        response = self.client.get(
            reverse("search") + "?media_type=movie&q=test",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/search.html")

        self.user.refresh_from_db()
        self.assertEqual(self.user.last_search_type, MediaTypes.MOVIE.value)

        mock_search.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "test",
            1,
            Sources.TMDB.value,
        )


class TheaterRedirectConcurrencyTests(TransactionTestCase):
    """Exercise concurrent HTTP observations against a locking database backend."""

    @skipUnlessDBFeature("has_select_for_update")
    def test_competing_redirects_preserve_one_consistent_identity(self):
        """Concurrent conflicting responses cannot split evidence from attendance."""
        user = get_user_model().objects.create_user(username="concurrent-attendee")
        old = Item.objects.create(
            media_id="Q998",
            source="wikidata",
            media_type="theater",
            title="Saved work",
            image="",
            theater_forms=["play"],
        )
        attendance = Theater.objects.create(item=old, user=user, notes="Keep my visit")
        barrier = Barrier(2)
        thread_state = local()
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        fixture["work"]["claims"].pop("P18")

        def source_response(url, **_kwargs):
            if "commons.wikimedia.org" in url:
                payload = {"query": {"search": []}}
            else:
                barrier.wait(timeout=15)
                payload = {
                    "entities": {
                        "Q998": {
                            **fixture["work"],
                            "id": thread_state.target,
                            "redirects": {"from": "Q998", "to": thread_state.target},
                        }
                    }
                }
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        def request_target(target):
            try:
                thread_state.target = target
                client = Client()
                client.force_login(user)
                return client.get(
                    reverse(
                        "media_details",
                        args=["wikidata", "theater", "Q998", "saved-work"],
                    )
                ).status_code
            finally:
                connections.close_all()

        cache.clear()
        self.addCleanup(cache.clear)
        with (
            patch("app.providers.services.session.get", side_effect=source_response),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            responses = list(executor.map(request_target, ["Q822850", "Q19320959"]))
        self.assertEqual(sorted(responses), [200, 500])
        attendance.refresh_from_db()
        self.assertEqual(
            attendance.item.media_id,
            TheaterRedirect.objects.get(alias_id="Q998").canonical_id,
        )
        self.assertEqual(attendance.notes, "Keep my visit")
        self.assertEqual(Item.objects.filter(media_type="theater").count(), 1)


class TheaterDiscoveryTests(TestCase):
    """Exercise discovery and tracking with deterministic external HTTP."""

    def setUp(self):
        """Provide work, adaptation, alias and non-work source responses."""
        cache.clear()
        self.user = get_user_model().objects.create_user(username="theater-reader")
        self.client.force_login(self.user)
        self.entities = {
            "Q19320959": {
                "id": "Q19320959",
                "lastrevid": 2545377861,
                "labels": {"en": {"value": "Hamilton"}},
                "descriptions": {"en": {"value": "stage musical"}},
                "claims": {
                    "P31": [
                        {"mainsnak": {"datavalue": {"value": {"id": "Q58483083"}}}}
                    ],
                    "P7937": [{"mainsnak": {"datavalue": {"value": {"id": "Q2743"}}}}],
                    "P86": [{"mainsnak": {"datavalue": {"value": {"id": "Q1646482"}}}}],
                },
            },
            "Q1646482": {
                "id": "Q1646482",
                "labels": {"en": {"value": "Lin-Manuel Miranda"}},
            },
            "Q999": {
                "id": "Q999",
                "labels": {"en": {"value": "Hamilton film"}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q11424"}}}}],
                },
            },
        }
        self.entities["Q998"] = {
            **self.entities["Q19320959"],
            "redirects": {"from": "Q998", "to": "Q19320959"},
        }
        self.search_ids = ["Q999", "Q998", "Q19320959"]
        self.http = patch(
            "app.providers.services.session.get", side_effect=self.source_response
        )
        self.http.start()
        self.addCleanup(self.http.stop)
        self.addCleanup(cache.clear)

    def source_response(self, url, params, **_kwargs):
        """Return fixed Action API responses at the external boundary."""
        if url == "https://commons.wikimedia.org/w/api.php":
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"query":{"search":[]}}'
            return response
        self.assertEqual(url, "https://www.wikidata.org/w/api.php")
        if params["action"] == "query":
            data = {
                "query": {
                    "searchinfo": {"totalhits": 3},
                    "search": [{"title": identifier} for identifier in self.search_ids],
                }
            }
        else:
            data = {
                "entities": {
                    identifier: self.entities.get(
                        identifier, {"id": identifier, "missing": ""}
                    )
                    for identifier in params["ids"].split("|")
                }
            }
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(data).encode()
        return response

    def test_wikidata_requests_only_fields_needed_for_related_entities(self):
        """Slim type and creator records preserve work metadata and label fallback."""
        work = self.entities["Q19320959"]
        work["claims"]["P31"] = [
            {"mainsnak": {"datavalue": {"value": {"id": "Q90001"}}}}
        ]
        work["claims"]["P364"] = [
            {"mainsnak": {"datavalue": {"value": {"id": "Q90002"}}}}
        ]
        self.entities["Q90001"] = {
            "id": "Q90001",
            "lastrevid": 123,
            "claims": {
                "P279": [{"mainsnak": {"datavalue": {"value": {"id": "Q2743"}}}}]
            },
        }
        self.entities["Q90002"] = {
            "id": "Q90002",
            "labels": {"nl": {"value": "Nederlands"}},
        }
        self.entities["Q1646482"]["aliases"] = {"en": [{"value": "Lin Manuel Miranda"}]}
        self.search_ids = ["Q19320959"]
        calls = []

        def source_response(url, params, **kwargs):
            response = self.source_response(url, params, **kwargs)
            if params["action"] != "wbgetentities":
                return response
            identifiers = set(params["ids"].split("|"))
            props = params["props"]
            calls.append((identifiers, props))
            expected = {
                frozenset({"Q90001"}): "info|claims",
                frozenset({"Q1646482", "Q90002"}): "labels|aliases",
                frozenset(
                    {"Q19320959"}
                ): "info|labels|aliases|descriptions|claims|sitelinks",
                frozenset({"Q90002"}): "labels",
            }
            self.assertEqual(props, expected[frozenset(identifiers)])
            payload = response.json()
            for entity in payload["entities"].values():
                for field in (
                    "labels",
                    "aliases",
                    "claims",
                    "descriptions",
                    "sitelinks",
                ):
                    if field not in props.split("|"):
                        entity.pop(field, None)
                if "info" not in props.split("|"):
                    entity.pop("lastrevid", None)
                if params.get("languages") == "en":
                    entity["labels"] = {
                        language: label
                        for language, label in entity.get("labels", {}).items()
                        if language == "en"
                    }
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
            item = response.context["data"]["results"][0]["item"]
            self.assertEqual(item["title"], "Hamilton")
            self.assertEqual(item["theater_forms"], ["musical"])
            self.assertEqual(item["synopsis"], "stage musical")
            self.assertEqual(item["details"]["Composers"], "Lin-Manuel Miranda")
            self.assertEqual(item["details"]["original_language"], "Nederlands")
            self.assertIn(
                "Lin Manuel Miranda", item["artwork_category_context"]["creators"]
            )
            self.assertFalse(item["labels_incomplete"])
            self.assertIn(({"Q90002"}, "labels"), calls)
            cache.clear()
            details = self.client.get(
                reverse(
                    "media_details",
                    args=["wikidata", "theater", "Q19320959", "hamilton"],
                )
            )
            self.assertContains(details, "Lin-Manuel Miranda")

    def _batch_source(self):
        """Provide distinct files and controllable failures at the HTTP boundary."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.search_ids = ["Q822850", "Q822851", "Q822852"]
        for index, identifier in enumerate(self.search_ids):
            work = json.loads(json.dumps(fixture["work"]))
            work.update(id=identifier, labels={"en": {"value": f"Stage work {index}"}})
            work["claims"]["P18"][0]["mainsnak"]["datavalue"]["value"] = (
                f"Stage {index}.jpg"
            )
            self.entities[identifier] = work
        calls = []
        state = {"mode": "success", "elapsed": 0}

        def source_response(url, params, **kwargs):
            mode = state["mode"]
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            calls.append(params["titles"])
            titles = params["titles"].split("|")
            if mode == "batch_timeout" and len(titles) > 1:
                raise requests.Timeout
            if mode == "file_timeout" and "File:Stage 1.jpg" in titles:
                raise requests.Timeout
            if mode == "throttled":
                response = requests.Response()
                response.status_code = 429
                response.headers["Retry-After"] = "60"
                return response
            if mode == "late_budget":
                state["elapsed"] = 31
            pages = {}
            for title in params["titles"].split("|"):
                index = int(title.removeprefix("File:Stage ").removesuffix(".jpg"))
                page = json.loads(
                    json.dumps(fixture["commons"]["query"]["pages"]["123"])
                )
                page.update(pageid=123 + index, title=title)
                self._change_batched_file(page, mode, index)
                image = (
                    f"https://thumb.wikimedia.org/wikipedia/commons/stage-{index}.png"
                )
                page["imageinfo"][0].update(url=image, thumburl=image)
                pages[str(page["pageid"])] = page
            response = requests.Response()
            response.status_code = 200
            payload = {"query": {"pages": pages}}
            if mode == "redirect":
                payload["query"]["redirects"] = [
                    {"from": "File:Stage 1.jpg", "to": "File:Renamed.jpg"}
                ]
            response._content = json.dumps(payload).encode()
            return response

        return source_response, calls, state

    def test_commons_batches_direct_files_across_works(self):
        """Shared metadata requests keep each work's file and credits separate."""
        source_response, calls, _state = self._batch_source()
        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
            results = response.context["data"]["results"]
            self.assertEqual(len(results), 3)
            self.assertEqual(
                calls, ["File:Stage 0.jpg|File:Stage 1.jpg|File:Stage 2.jpg"]
            )
            for index, result in enumerate(results):
                self.assertEqual(
                    result["item"]["image"],
                    f"https://thumb.wikimedia.org/wikipedia/commons/stage-{index}.png",
                )
                self.assertEqual(
                    result["item"]["theater_artwork"]["work_id"], self.search_ids[index]
                )
            self.client.get(reverse("search"), {"media_type": "theater", "q": "stage"})
            self.assertEqual(len(calls), 1)
            cache.delete("commons_v11_Q822851")
            self.client.get(reverse("search"), {"media_type": "theater", "q": "stage"})
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1], "File:Stage 1.jpg")

    def test_commons_normalization_then_redirect_keeps_file_mapping(self):
        """Map normalized aliases to their resolved file without crossing works."""
        source_response, calls, state = self._batch_source()
        state["mode"] = "redirect"
        self.entities["Q822851"]["claims"]["P18"][0]["mainsnak"]["datavalue"][
            "value"
        ] = "stage_1.jpg"

        def normalized_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return source_response(url, params, **kwargs)
            response = source_response(
                url,
                {
                    **params,
                    "titles": params["titles"].replace(
                        "File:stage_1.jpg", "File:Stage 1.jpg"
                    ),
                },
                **kwargs,
            )
            payload = response.json()
            payload["query"]["normalized"] = [
                {"from": "File:stage_1.jpg", "to": "File:Stage 1.jpg"}
            ]
            response._content = json.dumps(payload).encode()
            return response

        with patch(
            "app.providers.services.session.get", side_effect=normalized_response
        ):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
        self.assertEqual(len(calls), 1)
        artwork = response.context["data"]["results"][1]["item"]["theater_artwork"]
        self.assertEqual(artwork["title"], "Renamed.jpg")
        self.assertEqual(artwork["work_id"], "Q822851")

    def test_commons_insufficient_recovery_capacity_avoids_batching(self):
        """Fall back to independent calls when the retry reserve cannot be met."""
        source_response, calls, state = self._batch_source()
        state["mode"] = "file_timeout"
        with (
            patch("app.providers.services.session.get", side_effect=source_response),
            patch("app.providers.commons.BATCH_RECOVERY_REQUESTS", 49),
        ):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
        self.assertEqual(
            calls, ["File:Stage 0.jpg", "File:Stage 1.jpg", "File:Stage 2.jpg"]
        )
        results = response.context["data"]["results"]
        self.assertEqual(
            [bool(result["item"]["theater_artwork"]) for result in results],
            [True, False, True],
        )

    def test_commons_batch_failures_are_isolated_and_bounded(self):
        """Recover good neighbors without retrying throttled requests."""
        source_response, calls, state = self._batch_source()
        with patch("app.providers.services.session.get", side_effect=source_response):
            for mode, expected_calls, expected_images in (
                ("batch_timeout", 4, [True, True, True]),
                ("file_timeout", 4, [True, False, True]),
                ("malformed", 4, [True, False, True]),
                ("wrong_form", 1, [True, False, True]),
                ("redirect", 1, [True, True, True]),
                ("throttled", 1, [False, False, False]),
            ):
                with self.subTest(mode=mode):
                    cache.clear()
                    calls.clear()
                    state["mode"] = mode
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "stage"}
                    )
                    results = response.context["data"]["results"]
                    self.assertEqual(
                        [bool(result["item"]["theater_artwork"]) for result in results],
                        expected_images,
                    )
                    self.assertEqual(len(calls), expected_calls)
                    if mode in {"file_timeout", "malformed", "throttled"}:
                        self.assertTrue(results[1]["item"]["artwork_unavailable"])

    def test_commons_recovered_candidates_survive_an_earlier_failed_file(self):
        """Use recovered candidates without caching an incomplete selection."""
        source_response, calls, state = self._batch_source()
        work = self.entities["Q822850"]
        work["claims"]["P18"] = [
            {"mainsnak": {"datavalue": {"value": filename}}}
            for filename in ("Stage 1.jpg", "Stage 0.jpg")
        ]
        state["mode"] = "file_timeout"
        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
            results = response.context["data"]["results"]
            self.assertEqual(len(calls), 4)
            self.assertTrue(results[0]["item"]["theater_artwork"])
            self.assertTrue(results[0]["item"]["artwork_partial"])
            self.assertFalse(results[1]["item"]["theater_artwork"])
            self.assertTrue(results[2]["item"]["theater_artwork"])
            state["mode"] = "success"
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
            results = response.context["data"]["results"]
            self.assertEqual(len(calls), 5)
            self.assertTrue(
                all(result["item"]["theater_artwork"] for result in results)
            )
            self.assertFalse(
                any(result["item"]["artwork_partial"] for result in results)
            )

    def test_commons_short_budget_uses_single_files(self):
        """A late request does not risk a good file on an unrelated shared batch."""
        source_response, calls, state = self._batch_source()
        state["mode"] = "late_budget"
        clock_started = False

        def clock():
            nonlocal clock_started
            if not clock_started:
                clock_started = True
                return 0
            return state["elapsed"] or 20

        with (
            patch("app.providers.services.session.get", side_effect=source_response),
            patch("app.providers.commons.monotonic", side_effect=clock),
        ):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
        self.assertEqual(calls, ["File:Stage 0.jpg"])
        self.assertTrue(
            response.context["data"]["results"][0]["item"]["theater_artwork"]
        )

    def test_commons_shared_files_preserve_work_identity(self):
        """Deduplicate requests while keeping each work's artwork independent."""
        source_response, calls, _state = self._batch_source()
        self.entities["Q822851"]["claims"]["P18"][0]["mainsnak"]["datavalue"][
            "value"
        ] = "Stage 0.jpg"
        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
        results = response.context["data"]["results"]
        self.assertEqual(calls, ["File:Stage 0.jpg|File:Stage 2.jpg"])
        self.assertEqual(results[0]["item"]["image"], results[1]["item"]["image"])
        for identifier, result in zip(self.search_ids, results, strict=True):
            self.assertEqual(result["item"]["theater_artwork"]["work_id"], identifier)

    def test_commons_batch_continuation_keeps_later_warnings(self):
        """A warning in a later metadata fragment rejects only that file."""
        source_response, calls, _state = self._batch_source()

        def continued_response(url, params, **kwargs):
            response = source_response(url, params, **kwargs)
            if "commons.wikimedia.org" not in url:
                return response
            payload = response.json()
            if "tlcontinue" not in params:
                payload["continue"] = {"tlcontinue": "next", "continue": "||"}
            else:
                payload["query"]["pages"]["124"]["templates"].append(
                    {"title": "Template:Copyright violation"}
                )
            response._content = json.dumps(payload).encode()
            return response

        with patch(
            "app.providers.services.session.get", side_effect=continued_response
        ):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
        results = response.context["data"]["results"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [bool(result["item"]["theater_artwork"]) for result in results],
            [True, False, True],
        )

    @staticmethod
    def _change_batched_file(page, mode, index):
        """Simulate one file's independent metadata without mocking selection."""
        if index != 1:
            return
        if mode == "malformed":
            page["templates"] = None
        elif mode == "wrong_form":
            page["imageinfo"][0]["extmetadata"]["ImageDescription"] = {
                "value": "Performance of an opera"
            }
        elif mode == "redirect":
            page["title"] = "File:Renamed.jpg"

    def test_missing_direct_image_never_searches_commons(self):
        """Keep imageless works visible without exploratory image requests."""

        def source_response(url, params, **kwargs):
            self.assertNotEqual(url, "https://commons.wikimedia.org/w/api.php")
            return self.source_response(url, params, **kwargs)

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Hamilton")
            details = self.client.get(
                reverse(
                    "media_details",
                    args=["wikidata", "theater", "Q19320959", "hamilton"],
                )
            )
            self.assertEqual(details.status_code, 200)

    def test_direct_image_and_legacy_artwork_survive_without_discovery(self):
        """Resolve known files only and preserve retired evidence in saved backups."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        page = fixture["commons"]["query"]["pages"]["123"]
        description = (
            "Illustration of The House of Bernarda Alba, "
            "a play by Federico Garcia Lorca."
        )
        page["imageinfo"][0]["extmetadata"]["ImageDescription"] = {"value": description}
        calls = []

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            self.assertEqual(params["action"], "query")
            self.assertEqual(params["prop"], "imageinfo|categories|templates|info")
            self.assertNotIn("list", params)
            calls.append(params)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(fixture["commons"]).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertContains(response, page["imageinfo"][0]["url"])
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q822850",
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Planning",
                },
            )
            self.assertEqual(len(calls), 1)
            item = Item.objects.get(media_id="Q822850")
            direct = item.theater_artwork
            self.assertEqual(direct["evidence"], "P18/P154")
            legacy = {
                **direct,
                "evidence": "P373/description",
                "category_id": 456,
                "category_revision": 700,
                "category_title": "Category:The House of Bernarda Alba",
                "category_work_id": "Q822850",
                "matched_title": "The House of Bernarda Alba",
                "matched_creator": "Federico Garcia Lorca",
                "matched_form": "play",
                "match_description": description,
                "match_source_html": description,
            }
            fixture["work"]["claims"].pop("P18")
            fixture["work"]["claims"]["P373"] = [
                {"mainsnak": {"datavalue": {"value": "The House of Bernarda Alba"}}}
            ]
            for evidence in ("P180", "P373/description"):
                with self.subTest(evidence=evidence):
                    item.theater_artwork = {**legacy, "evidence": evidence}
                    item.save(update_fields=["theater_artwork"])
                    cache.clear()
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Bernarda"}
                    )
                    self.assertContains(response, item.image)
                    details = self.client.get(
                        reverse(
                            "media_details",
                            args=["wikidata", "theater", "Q822850", "bernarda"],
                        )
                    )
                    self.assertContains(details, item.image)
                    cache.clear()
                    synced = self.client.post(
                        reverse(
                            "sync_metadata", args=["wikidata", "theater", "Q822850"]
                        )
                    )
                    self.assertLess(synced.status_code, 400)
                    item.refresh_from_db()
                    self.assertEqual(
                        item.theater_artwork, {**legacy, "evidence": evidence}
                    )
                    self.assertEqual(len(calls), 1)
            self._assert_category_artwork_restores(legacy)

    def test_imported_nonstring_evidence_does_not_break_artwork_refresh(self):
        """Legacy malformed evidence cannot crash a subsequent source refresh."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(fixture["commons"]).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q822850",
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Planning",
                },
            )
            exported = b"".join(
                self.client.get(reverse("export_csv")).streaming_content
            )
            for evidence in ([], {}):
                with self.subTest(evidence=evidence):
                    rows = list(csv.DictReader(StringIO(exported.decode())))
                    artwork = json.loads(rows[0]["theater_artwork"])
                    artwork["evidence"] = evidence
                    rows[0]["theater_artwork"] = json.dumps(artwork)
                    content = StringIO()
                    writer = csv.DictWriter(content, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)
                    Item.objects.get(media_id="Q822850").delete()
                    self.client.post(
                        reverse("import_yamtrack"),
                        {
                            "mode": "new",
                            "yamtrack_csv": SimpleUploadedFile(
                                "legacy.csv", content.getvalue().encode()
                            ),
                        },
                    )
                    cache.clear()
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Bernarda"}
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, "Test Photographer")

    def test_wikipedia_poster_search_tracking_and_offline_restore(self):
        """Exact article posters retain non-free status and attendance identity."""
        self.entities["Q19320959"]["sitelinks"] = {
            "enwiki": {"title": "Hamilton (musical)"}
        }
        self.search_ids = ["Q19320959", "Q999"]
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )["wikipedia"]
        image = "https://upload.wikimedia.org/wikipedia/en/thumb/a/ab/Hamilton-poster.jpg/250px-Hamilton-poster.jpg"
        calls = []

        def source_response(url, params, **kwargs):
            if url != "https://en.wikipedia.org/w/api.php":
                return self.source_response(url, params, **kwargs)
            calls.append(params)
            response = requests.Response()
            response.status_code = 200
            payload = {
                "query": {
                    "pages": [
                        fixture["article"]
                        if params["prop"] == "pageprops|pageimages|info"
                        else fixture["file"]
                    ]
                }
            }
            if params["prop"] != "pageprops|pageimages|info":
                payload["continue"] = {
                    "iistart": "2020-01-01T00:00:00Z",
                    "continue": "||info",
                }
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
            self.assertContains(response, image)
            self.assertContains(response, "Non-free")
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q19320959",
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Completed",
                    "notes": "My visit",
                },
            )
            details = self.client.get(
                reverse(
                    "media_details",
                    args=["wikidata", "theater", "Q19320959", "hamilton"],
                )
            )
            self.assertContains(details, image)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["pilicense"], "any")
        work = Item.objects.get(media_id="Q19320959")
        self.assertTrue(work.theater_artwork["non_free"])
        self._assert_saved_wikipedia_poster_survives_outage(source_response, image)
        content = b"".join(self.client.get(reverse("export_csv")).streaming_content)
        work.delete()
        with patch(
            "app.providers.services.session.get",
            side_effect=AssertionError("Offline restore"),
        ):
            self.client.post(
                reverse("import_yamtrack"),
                {
                    "mode": "new",
                    "yamtrack_csv": SimpleUploadedFile("theater.csv", content),
                },
            )
        attendance = Theater.objects.get(user=self.user)
        self.assertEqual(attendance.notes, "My visit")
        self.assertEqual(attendance.item.image, image)
        self.assertTrue(attendance.item.theater_artwork["non_free"])
        self._assert_wikipedia_export_rejects_altered_credit(content)

    def _assert_saved_wikipedia_poster_survives_outage(self, source_response, image):
        """Keep a saved poster during optional article lookup outages."""

        def outage(url, params, **kwargs):
            if url == "https://en.wikipedia.org/w/api.php":
                raise requests.Timeout
            return source_response(url, params, **kwargs)

        cache.clear()
        with patch("app.providers.services.session.get", side_effect=outage):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
            self.assertContains(response, image)
            details = self.client.get(
                reverse(
                    "media_details",
                    args=["wikidata", "theater", "Q19320959", "hamilton"],
                )
            )
            self.assertContains(details, image)
        self.assertEqual(Item.objects.get(media_id="Q19320959").image, image)

    def _assert_wikipedia_export_rejects_altered_credit(self, content):
        """Omit altered article artwork without losing restored attendance."""
        for key, value in (
            ("non_free", False),
            ("basis_notice", ""),
            ("source_url", "javascript:alert(1)"),
            ("article_url", "https://example.com"),
        ):
            with self.subTest(key=key):
                rows = list(csv.DictReader(StringIO(content.decode())))
                artwork = json.loads(rows[0]["theater_artwork"])
                artwork[key] = value
                rows[0]["theater_artwork"] = json.dumps(artwork)
                upload = StringIO()
                writer = csv.DictWriter(upload, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
                Item.objects.get(media_id="Q19320959").delete()
                with patch(
                    "app.providers.services.session.get",
                    side_effect=AssertionError("Offline restore"),
                ):
                    self.client.post(
                        reverse("import_yamtrack"),
                        {
                            "mode": "new",
                            "yamtrack_csv": SimpleUploadedFile(
                                "theater.csv", upload.getvalue().encode()
                            ),
                        },
                    )
                attendance = Theater.objects.get(user=self.user)
                self.assertEqual(attendance.notes, "My visit")
                self.assertFalse(attendance.item.theater_artwork)

    def test_wikipedia_batches_preserve_valid_posters_when_another_file_is_invalid(
        self,
    ):
        """A bad file cannot suppress another work's verified article image."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )["wikipedia"]
        self.search_ids = ["Q19320959", "Q94000"]
        for identifier, title in (
            ("Q19320959", "Hamilton (musical)"),
            ("Q94000", "Other musical"),
        ):
            self.entities[identifier] = {
                **self.entities["Q19320959"],
                "id": identifier,
                "sitelinks": {"enwiki": {"title": title}},
            }
        calls = []

        def source_response(url, params, **kwargs):
            if url != "https://en.wikipedia.org/w/api.php":
                return self.source_response(url, params, **kwargs)
            calls.append(params)
            if params["prop"] == "pageprops|pageimages|info":
                pages = [
                    fixture["article"],
                    {
                        **fixture["article"],
                        "title": "Other musical",
                        "pageprops": {"wikibase_item": "Q94000"},
                        "pageimage": "Other.jpg",
                    },
                ]
            else:
                pages = [
                    fixture["file"],
                    {"title": "File:Other.jpg", "ns": 6, "imageinfo": None},
                ]
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({"query": {"pages": pages}}).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "musical"}
            )
        results = response.context["data"]["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(
            results[0]["item"]["image"], fixture["file"]["imageinfo"][0]["thumburl"]
        )
        self.assertFalse(results[0]["item"]["artwork_partial"])
        self.assertTrue(results[1]["item"]["artwork_partial"])
        self.assertEqual(len(calls), 2)
        self.assertIn("Hamilton (musical)|Other musical", calls[0]["titles"])

    def test_wikipedia_unavailable_and_rejected_metadata_never_hides_works(self):
        """Reject wrong articles and unsafe files; retry incomplete source responses."""
        original = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )["wikipedia"]
        self.entities["Q19320959"]["sitelinks"] = {
            "enwiki": {"title": "Hamilton (musical)"}
        }
        self.search_ids = ["Q19320959"]
        fixture = original
        mode = ""

        def source_response(url, params, **kwargs):
            if url != "https://en.wikipedia.org/w/api.php":
                return self.source_response(url, params, **kwargs)
            if mode == "timeout":
                raise requests.Timeout
            payload = {
                "query": {
                    "pages": [
                        fixture["article"]
                        if params["prop"] == "pageprops|pageimages|info"
                        else fixture["file"]
                    ]
                }
            }
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(None if mode == "null" else payload).encode()
            return response

        for case in (
            "qid",
            "disambiguation",
            "unsafe_url",
            "video",
            "missing_metadata",
            "timeout",
            "null",
        ):
            with self.subTest(case=case):
                cache.clear()
                fixture = json.loads(json.dumps(original))
                mode = case
                if case == "qid":
                    fixture["article"]["pageprops"]["wikibase_item"] = "Q999"
                elif case == "disambiguation":
                    fixture["article"]["pageprops"]["disambiguation"] = ""
                elif case in ("unsafe_url", "video"):
                    field, value = {
                        "unsafe_url": ("thumburl", "https://example.com/poster.jpg"),
                        "video": ("mime", "video/webm"),
                    }[case]
                    fixture["file"]["imageinfo"][0][field] = value
                elif case == "missing_metadata":
                    fixture["file"]["imageinfo"] = None
                with patch(
                    "app.providers.services.session.get", side_effect=source_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Hamilton"}
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context["data"]["total_results"], 1)
                    self.assertFalse(
                        response.context["data"]["results"][0]["item"][
                            "theater_artwork"
                        ]
                    )
                    if case in ("timeout", "null", "missing_metadata"):
                        mode, fixture = "", original
                        recovered = self.client.get(
                            reverse("search"),
                            {"media_type": "theater", "q": "Hamilton"},
                        )
                        self.assertEqual(
                            recovered.context["data"]["results"][0]["item"][
                                "theater_artwork"
                            ]["provider"],
                            "wikipedia",
                        )

    def test_wikipedia_multilingual_shared_file_and_redirect_preserve_identity(self):
        """Follow exact article redirects and shared files without title guesses."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )["wikipedia"]
        self.entities["Q19320959"]["sitelinks"] = {
            "enwiki": {"title": "Hamilton (musical)"},
            "frwiki": {"title": "Hamilton ancien"},
        }
        self.search_ids = ["Q19320959"]
        fixture["article"]["title"] = "Hamilton nouveau"
        fixture["file"].update(imagerepository="shared", missing=True)
        fixture["file"].pop("pageid")
        fixture["file"].pop("lastrevid")
        info = fixture["file"]["imageinfo"][0]
        info.update(
            thumburl="https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Hamilton-poster.jpg/250px-Hamilton-poster.jpg",
            descriptionurl="https://commons.wikimedia.org/wiki/File:Hamilton-poster.jpg",
        )
        info["extmetadata"].update(
            NonFree={"value": "false"}, LicenseShortName={"value": "CC BY-SA 4.0"}
        )

        def source_response(url, params, **kwargs):
            if url == "https://en.wikipedia.org/w/api.php":
                payload = {
                    "query": {
                        "pages": [{"title": "Hamilton (musical)", "missing": True}]
                    }
                }
            elif url == "https://fr.wikipedia.org/w/api.php":
                if params["prop"] == "pageprops|pageimages|info":
                    payload = {
                        "query": {
                            "redirects": [
                                {"from": "Hamilton ancien", "to": "Hamilton nouveau"}
                            ],
                            "pages": [fixture["article"]],
                        }
                    }
                else:
                    payload = {"query": {"pages": [fixture["file"]]}}
            else:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
        artwork = response.context["data"]["results"][0]["item"]["theater_artwork"]
        self.assertEqual(artwork["language"], "fr")
        self.assertFalse(artwork["non_free"])
        self.assertEqual(artwork["work_id"], "Q19320959")
        self.assertContains(response, "Hamilton_nouveau")
        self.assertFalse(TheaterRedirect.objects.exists())
        cache.clear()
        fixture["file"].update(imagerepository="local", pageid=1000, lastrevid=100)
        fixture["file"].pop("missing")
        info["extmetadata"]["Artist"] = {
            "value": '<a href="/wiki/Utilisateur:Photographe">Photographe</a>'
        }
        info.update(
            thumburl="https://upload.wikimedia.org/wikipedia/fr/thumb/a/ab/Hamilton-poster.jpg/250px-Hamilton-poster.jpg",
            descriptionurl="https://fr.wikipedia.org/wiki/Fichier:Hamilton-poster.jpg",
        )
        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
        self.assertEqual(
            response.context["data"]["results"][0]["item"]["image"], info["thumburl"]
        )
        self.assertContains(
            response, "https://fr.wikipedia.org/wiki/Utilisateur:Photographe"
        )

    def test_wikipedia_article_failures_are_isolated_per_work(self):
        """Malformed articles do not discard the valid neighbor in the same batch."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )["wikipedia"]
        self.search_ids = ["Q19320959", "Q94000"]
        for identifier, title in (
            ("Q19320959", "Hamilton (musical)"),
            ("Q94000", "Other"),
        ):
            self.entities[identifier] = {
                **self.entities["Q19320959"],
                "id": identifier,
                "sitelinks": {"enwiki": {"title": title}},
            }
        bad_article = None

        def source_response(url, params, **kwargs):
            if url != "https://en.wikipedia.org/w/api.php":
                return self.source_response(url, params, **kwargs)
            pages = (
                [fixture["article"], bad_article]
                if params["prop"] == "pageprops|pageimages|info"
                else [fixture["file"]]
            )
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({"query": {"pages": pages}}).encode()
            return response

        for changes in ({"pageprops": None}, {"pageimage": []}, {"pageprops": {}}):
            cache.clear()
            bad_article = {
                **fixture["article"],
                "title": "Other",
                "pageprops": {"wikibase_item": "Q94000"},
                **changes,
            }
            with patch(
                "app.providers.services.session.get", side_effect=source_response
            ):
                response = self.client.get(
                    reverse("search"), {"media_type": "theater", "q": "Hamilton"}
                )
            results = response.context["data"]["results"]
            self.assertEqual(
                results[0]["item"]["image"], fixture["file"]["imageinfo"][0]["thumburl"]
            )
            self.assertTrue(results[1]["item"]["artwork_unavailable"])

    def test_wikipedia_site_batches_and_partial_fallback_preserve_saved_poster(self):
        """Batch each site once and keep saved art when a preferred site fails."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )["wikipedia"]
        self.search_ids = ["Q19320959", "Q94000"]
        self.entities["Q19320959"]["sitelinks"] = {
            "enwiki": {"title": "Hamilton (musical)"},
            "frwiki": {"title": "Hamilton (musical)"},
        }
        self.entities["Q94000"] = {
            **self.entities["Q19320959"],
            "id": "Q94000",
            "sitelinks": {"frwiki": {"title": "Other"}},
        }
        original_image = fixture["file"]["imageinfo"][0]["thumburl"]
        saved = Item.objects.create(
            media_id="Q19320959",
            source="wikidata",
            media_type="theater",
            title="Hamilton",
            image=original_image,
            theater_forms=["musical"],
            theater_artwork={"provider": "wikipedia", "image": original_image},
        )
        Theater.objects.create(item=saved, user=self.user, status="Planning")
        french_calls = []

        def source_response(url, params, **kwargs):
            if url == "https://en.wikipedia.org/w/api.php":
                raise requests.Timeout
            if url != "https://fr.wikipedia.org/w/api.php":
                return self.source_response(url, params, **kwargs)
            french_calls.append(params)
            if params["prop"] == "pageprops|pageimages|info":
                pages = [
                    {
                        **fixture["article"],
                        "title": title,
                        "pageprops": {
                            "wikibase_item": "Q94000"
                            if title == "Other"
                            else "Q19320959"
                        },
                    }
                    for title in params["titles"].split("|")
                ]
            else:
                file_page = json.loads(json.dumps(fixture["file"]))
                file_page["imagerepository"] = "shared"
                file_page["imageinfo"][0].update(
                    thumburl="https://upload.wikimedia.org/wikipedia/commons/a/ab/fallback.jpg",
                    descriptionurl="https://commons.wikimedia.org/wiki/File:Hamilton-poster.jpg",
                )
                pages = [file_page]
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({"query": {"pages": pages}}).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
        self.assertEqual(len(french_calls), 2)
        self.assertEqual(
            response.context["data"]["results"][0]["item"]["image"], original_image
        )
        self.assertEqual(
            response.context["data"]["results"][1]["item"]["theater_artwork"][
                "provider"
            ],
            "wikipedia",
        )
        saved.refresh_from_db()
        self.assertEqual(saved.image, original_image)

    def test_missing_english_labels_use_source_language_without_changing_identity(self):
        """A work and its creator can display source labels when English is absent."""
        self.entities["Q105448367"] = {
            **self.entities["Q19320959"],
            "id": "Q105448367",
            "labels": {},
        }
        self.entities["Q1646482"]["labels"] = {}
        self.search_ids = ["Q105448367"]
        requested_labels = []

        def source_response(url, params, **kwargs):
            if (
                url == "https://www.wikidata.org/w/api.php"
                and params.get("props") == "labels"
            ):
                requested_labels.append(params)
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps(
                    {
                        "entities": {
                            "Q105448367": {
                                "id": "Q105448367",
                                "labels": {"nl": {"value": "Dear Fox"}},
                            },
                            "Q1646482": {
                                "id": "Q1646482",
                                "labels": {"es": {"value": "Nombre del autor"}},
                            },
                        }
                    }
                ).encode()
                return response
            return self.source_response(url, params, **kwargs)

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Dear"}
            )
            self.assertContains(response, "Dear Fox")
            self.assertContains(response, "Nombre del autor")
            details = self.client.get(
                reverse(
                    "media_details",
                    args=["wikidata", "theater", "Q105448367", "dear-fox"],
                )
            )
            self.assertContains(details, "Dear Fox")
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q105448367",
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Planning",
                },
            )
        self.assertEqual(Item.objects.get(media_id="Q105448367").title, "Dear Fox")
        self.assertEqual(len(requested_labels), 1)
        self.assertNotIn("languages", requested_labels[0])

    def test_label_fallback_failure_preserves_work_and_recovers_without_cache_delay(
        self,
    ):
        """Optional labels cannot erase works or change their provider identities."""
        self.entities["Q19320959"]["labels"] = {}
        self.search_ids = ["Q19320959"]
        label_payload = None

        def source_response(url, params, **kwargs):
            if (
                url == "https://www.wikidata.org/w/api.php"
                and params.get("props") == "labels"
            ):
                if label_payload == "timeout":
                    raise requests.Timeout
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps(label_payload).encode()
                return response
            return self.source_response(url, params, **kwargs)

        for payload in (
            "timeout",
            None,
            False,
            42,
            [],
            {"error": []},
            {"error": None},
            {
                "entities": {
                    "Q19320959": {"id": "Q19320959", "labels": {"nl": {"value": None}}}
                }
            },
            {"entities": {"Q19320959": []}},
            {
                "entities": {
                    "Q19320959": {
                        "id": "Q999",
                        "labels": {"en": {"value": "Wrong work"}},
                    }
                }
            },
        ):
            with self.subTest(payload=payload):
                cache.clear()
                label_payload = payload
                with patch(
                    "app.providers.services.session.get", side_effect=source_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Hamilton"}
                    )
                    self.assertEqual(response.status_code, 200)
                    selected = response.context["data"]["results"][0]["item"]
                    self.assertEqual(selected["media_id"], "Q19320959")
                    self.assertEqual(selected["title"], "Q19320959")
                    label_payload = {
                        "entities": {
                            "Q19320959": {
                                "id": "Q19320959",
                                "labels": {"fr": {"value": "Hamilton"}},
                            }
                        }
                    }
                    recovered = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Hamilton"}
                    )
                    self.assertEqual(
                        recovered.context["data"]["results"][0]["item"]["title"],
                        "Hamilton",
                    )
        self.assertFalse(TheaterRedirect.objects.exists())

    def test_missing_label_lookup_is_bounded_without_hiding_works(self):
        """A later request can finish labels beyond one bounded batch."""
        self.search_ids = [f"Q{99100 + number}" for number in range(51)]
        for identifier in self.search_ids:
            self.entities[identifier] = {
                **self.entities["Q19320959"],
                "id": identifier,
                "labels": {},
            }
        label_batches = []

        def source_response(url, params, **kwargs):
            if (
                url == "https://www.wikidata.org/w/api.php"
                and params.get("props") == "labels"
            ):
                requested = params["ids"].split("|")
                label_batches.append(requested)
                payload = {
                    "entities": {
                        identifier: {
                            "id": identifier,
                            "labels": {"nl": {"value": f"Stage {identifier}"}},
                        }
                        for identifier in requested
                    }
                }
            elif (
                url == "https://www.wikidata.org/w/api.php"
                and params.get("list") == "search"
            ):
                offset = params.get("sroffset", 0)
                payload = {
                    "query": {
                        "search": [
                            {"title": identifier}
                            for identifier in self.search_ids[offset : offset + 50]
                        ]
                    }
                }
                if not offset:
                    payload["continue"] = {"sroffset": 50}
            else:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            for expected_title in ("Q99150", "Stage Q99150"):
                response = self.client.get(
                    reverse("search"),
                    {"media_type": "theater", "q": "Stage", "page": 3},
                )
                self.assertEqual(response.context["data"]["total_results"], 51)
                self.assertEqual(
                    response.context["data"]["results"][-1]["item"]["title"],
                    expected_title,
                )
        self.assertEqual([len(batch) for batch in label_batches], [50, 1])

    def test_mixed_stage_work_remains_discoverable_without_staging_fingerprint(self):
        """A directly classified musical/work remains trackable with a mixed type."""
        self.entities["Q20899421"] = {
            "id": "Q20899421",
            "lastrevid": 2532421479,
            "labels": {"en": {"value": "Dear Evan Hansen"}},
            "claims": {
                "P31": [
                    {
                        "rank": "normal",
                        "mainsnak": {"datavalue": {"value": {"id": identifier}}},
                    }
                    for identifier in ("Q58483083", "Q7777570")
                ],
                "P7937": [{"mainsnak": {"datavalue": {"value": {"id": "Q2743"}}}}],
            },
        }
        self.search_ids = ["Q20899421", "Q999"]
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Dear"}
        )
        self.assertEqual(
            [work["item"]["media_id"] for work in response.context["data"]["results"]],
            ["Q20899421"],
        )
        details = self.client.get(
            reverse(
                "media_details",
                args=["wikidata", "theater", "Q20899421", "dear-evan-hansen"],
            )
        )
        self.assertContains(details, "Musical")
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "Q20899421",
                "source": "wikidata",
                "media_type": "theater",
                "status": "Completed",
                "venue": "Local Stage",
            },
        )
        attendance = Theater.objects.get(user=self.user)
        self.assertEqual(attendance.item.media_id, "Q20899421")
        self.assertEqual(attendance.item.theater_forms, ["musical"])
        self.assertEqual(attendance.venue, "Local Stage")

    def test_mixed_work_exception_keeps_production_and_medium_guards(self):
        """The direct mixed-type exception cannot promote known non-work records."""
        self.search_ids = ["Q19320959"]
        self.entities["Q99001"] = {
            "id": "Q99001",
            "lastrevid": 100,
            "claims": {
                "P279": [{"mainsnak": {"datavalue": {"value": {"id": "Q7777570"}}}}]
            },
        }
        self.entities["Q99002"] = {
            "id": "Q99002",
            "lastrevid": 100,
            "claims": {
                "P279": [{"mainsnak": {"datavalue": {"value": {"id": "Q58483083"}}}}]
            },
        }
        for types, extra_property in (
            (["Q7777570"], None),
            (["Q7725634", "Q7777570"], None),
            (["Q99002", "Q7777570"], None),
            (["Q58483083", "Q7777570"], "deprecated"),
            (["Q58483083", "Q99001"], None),
            (["Q58483083", "Q7777570", "Q11424"], None),
            (["Q58483083", "Q7777570", "Q35140"], None),
            (["Q58483083", "Q43099500"], None),
            (["Q58483083", "Q7777570"], "P136"),
            (["Q58483083", "Q7777570"], "P7937"),
            (["Q58483083", "Q7777570"], "staging"),
        ):
            with self.subTest(types=types, extra_property=extra_property):
                cache.clear()
                claims = {
                    "P31": [
                        {"mainsnak": {"datavalue": {"value": {"id": identifier}}}}
                        for identifier in types
                    ],
                    "P7937": [{"mainsnak": {"datavalue": {"value": {"id": "Q2743"}}}}],
                }
                if extra_property == "deprecated":
                    claims["P31"][0]["rank"] = "deprecated"
                elif extra_property == "staging":
                    for property_id in ("P272", "P161", "P57"):
                        claims[property_id] = [
                            {"mainsnak": {"datavalue": {"value": {"id": "Q1646482"}}}}
                        ]
                elif extra_property:
                    claims.setdefault(extra_property, []).append(
                        {"mainsnak": {"datavalue": {"value": {"id": "Q7777570"}}}}
                    )
                self.entities["Q19320959"]["claims"] = claims
                response = self.client.get(
                    reverse("search"), {"media_type": "theater", "q": "Hamilton"}
                )
                self.assertEqual(response.context["data"]["total_results"], 0)
                saved = self.client.post(
                    reverse("media_save"),
                    {
                        "media_id": "Q19320959",
                        "source": "wikidata",
                        "media_type": "theater",
                        "status": "Planning",
                    },
                )
                self.assertEqual(saved.status_code, 500)
                self.assertFalse(Theater.objects.filter(user=self.user).exists())

    def test_specific_source_types_resolve_without_crossing_work_boundaries(self):
        """Recognize specific stage works while excluding production subclasses."""
        classes = {
            "Q90001": ["Q1344"],
            "Q90002": ["Q7777570"],
            "Q90003": ["Q2743"],
            "Q2743": ["Q25379"],
            "Q58483083": ["Q7777570"],
        }
        for identifier, parents in classes.items():
            self.entities[identifier] = {
                "id": identifier,
                "lastrevid": 100,
                "claims": {
                    "P279": [
                        {"mainsnak": {"datavalue": {"value": {"id": parent}}}}
                        for parent in parents
                    ]
                },
            }
        for identifier, title, type_id in [
            ("Q91001", "Regional Opera", "Q90001"),
            ("Q91002", "Regional Production", "Q90002"),
            ("Q91003", "Regional Musical", "Q90003"),
        ]:
            self.entities[identifier] = {
                "id": identifier,
                "labels": {"en": {"value": title}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": type_id}}}}]
                },
            }
        self.entities["Q91002"]["claims"]["P7937"] = [
            {"mainsnak": {"datavalue": {"value": {"id": "Q2743"}}}},
        ]
        self.search_ids = ["Q91001", "Q91002", "Q91003", "Q19320959"]
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Regional"}
        )
        works = {
            result["item"]["media_id"]: result["item"]
            for result in response.context["data"]["results"]
        }
        self.assertEqual(set(works), {"Q91001", "Q91003", "Q19320959"})
        self.assertEqual(works["Q91001"]["theater_forms"], ["opera"])
        self.assertEqual(works["Q91003"]["theater_forms"], ["musical"])
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "Q91001",
                "source": "wikidata",
                "media_type": "theater",
                "status": "Planning",
            },
        )
        self.assertEqual(Item.objects.get(media_id="Q91001").theater_forms, ["opera"])

    def test_explicit_ballet_survives_unresolved_secondary_work_types(self):
        """Swan Lake's broad musical-work ancestry cannot erase its Ballet type."""
        self.entities["Q199786"] = {
            "id": "Q199786",
            "lastrevid": 2526633021,
            "labels": {"en": {"value": "Swan Lake"}},
            "claims": {
                "P31": [
                    {"mainsnak": {"datavalue": {"value": {"id": identifier}}}}
                    for identifier in [
                        "Q58483083",
                        "Q58483088",
                        "Q15079786",
                        "Q105543609",
                    ]
                ]
            },
        }
        self.search_ids = ["Q199786"]
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Swan Lake"}
        )
        self.assertContains(response, "Swan Lake")
        self.assertEqual(
            response.context["data"]["results"][0]["item"]["theater_forms"], ["ballet"]
        )

    def test_unresolved_type_graphs_never_guess_work_forms(self):
        """Cycles, excessive ancestry and production conflicts remain untrackable."""
        classes = {
            "Q92001": ["Q92002"],
            "Q92002": ["Q92001"],
            "Q92003": ["Q1344", "Q7777570"],
            "Q92004": ["Q92005"],
            "Q92005": ["Q92006"],
            "Q92006": ["Q92007"],
            "Q92007": ["Q1344"],
        }
        for identifier, parents in classes.items():
            self.entities[identifier] = {
                "id": identifier,
                "lastrevid": 100,
                "claims": {
                    "P279": [
                        {"mainsnak": {"datavalue": {"value": {"id": parent}}}}
                        for parent in parents
                    ],
                },
            }
        self.search_ids = ["Q19320959"]
        for number, type_id in enumerate(
            ["Q92001", "Q92003", "Q92004", "Q92999"], start=93000
        ):
            identifier = f"Q{number}"
            self.entities[identifier] = {
                "id": identifier,
                "labels": {"en": {"value": "Unresolved work"}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": type_id}}}}],
                    "P7937": [{"mainsnak": {"datavalue": {"value": {"id": "Q1344"}}}}],
                },
            }
            self.search_ids.append(identifier)
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "work"}
        )
        self.assertEqual(
            [
                entry["item"]["media_id"]
                for entry in response.context["data"]["results"]
            ],
            ["Q19320959"],
        )
        for identifier in self.search_ids[1:]:
            response = self.client.post(
                reverse("media_save"),
                {
                    "media_id": identifier,
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Planning",
                },
            )
            self.assertEqual(response.status_code, 500)
        self.assertFalse(Item.objects.filter(media_type="theater").exists())

    def test_label_fallback_finds_specific_subtype_after_filtered_search_misses(self):
        """An ordinary alias query can discover a work with only a specific class."""
        self.entities["Q94001"] = {
            "id": "Q94001",
            "lastrevid": 100,
            "claims": {
                "P279": [{"mainsnak": {"datavalue": {"value": {"id": "Q1344"}}}}]
            },
        }
        self.entities["Q94002"] = {
            "id": "Q94002",
            "labels": {"en": {"value": "Regional Opera"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q94001"}}}}]
            },
        }
        self.search_ids = ["Q94002", "Q999"]

        filtered_ids = []
        fallback_unavailable = ""

        def source_response(url, params, **kwargs):
            if (
                url == "https://www.wikidata.org/w/api.php"
                and "haswbstatement" in params.get("srsearch", "")
            ):
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps(
                    {
                        "query": {
                            "search": [
                                {"title": identifier} for identifier in filtered_ids
                            ]
                        }
                    }
                ).encode()
                return response
            if (
                fallback_unavailable
                and url == "https://www.wikidata.org/w/api.php"
                and (
                    fallback_unavailable == params.get("list")
                    or fallback_unavailable in params.get("ids", "").split("|")
                )
            ):
                raise requests.Timeout
            return self.source_response(url, params, **kwargs)

        for filtered_ids in ([], ["Q19320959"]):
            with self.subTest(filtered_ids=filtered_ids):
                cache.clear()
                with patch(
                    "app.providers.services.session.get", side_effect=source_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Local alias"}
                    )
                self.assertContains(response, "Regional Opera")
                self.assertNotContains(response, "Hamilton film")
                self.assertEqual(
                    [
                        work["item"]["media_id"]
                        for work in response.context["data"]["results"]
                    ],
                    [*filtered_ids, "Q94002"],
                )

        for unavailable_stage in ("search", "Q94002", "Q94001"):
            with self.subTest(unavailable=unavailable_stage):
                cache.clear()
                fallback_unavailable = unavailable_stage
                with patch(
                    "app.providers.services.session.get", side_effect=source_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Local alias"}
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, "Hamilton")
                    self.assertTrue(response.context["data"]["limited"])
                    fallback_unavailable = ""
                    recovered = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Local alias"}
                    )
                    self.assertContains(recovered, "Regional Opera")

        cache.clear()
        TheaterRedirect.objects.create(
            alias_id="Q94002", canonical_id="Q999", revision=10
        )
        self.entities["Q94002"].update(
            id="Q19320959",
            lastrevid=100,
            redirects={"from": "Q94002", "to": "Q19320959"},
        )
        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Local alias"}
            )
        self.assertContains(response, "Conflicting Theater identity", status_code=500)
        self.assertEqual(
            TheaterRedirect.objects.get(alias_id="Q94002").canonical_id, "Q999"
        )

    def test_label_backfill_respects_full_pages_and_search_budget(self):
        """Backfill never adds a batch after a full page or three filtered batches."""
        for number in range(20):
            identifier = f"Q{98000 + number}"
            self.entities[identifier] = {
                **self.entities["Q19320959"],
                "id": identifier,
                "labels": {"en": {"value": f"Stage Work {number}"}},
            }
        search_requests = []
        batch_count = 1

        def source_response(url, params, **kwargs):
            if (
                url == "https://www.wikidata.org/w/api.php"
                and params.get("list") == "search"
            ):
                search_requests.append(params)
                self.assertIn("haswbstatement", params["srsearch"])
                payload = {
                    "query": {
                        "search": [
                            {"title": identifier} for identifier in self.search_ids
                        ]
                    }
                }
                if len(search_requests) < batch_count:
                    payload["continue"] = {"sroffset": len(search_requests) * 50}
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps(payload).encode()
                return response
            return self.source_response(url, params, **kwargs)

        for result_count, batch_count in ((20, 1), (1, 3)):
            with self.subTest(result_count=result_count, batch_count=batch_count):
                cache.clear()
                search_requests.clear()
                self.search_ids = [
                    f"Q{98000 + number}" for number in range(result_count)
                ]
                with patch(
                    "app.providers.services.session.get", side_effect=source_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Stage"}
                    )
                self.assertEqual(
                    response.context["data"]["total_results"], result_count
                )
                self.assertEqual(len(search_requests), batch_count)

    def test_type_budget_is_shared_across_provider_pages(self):
        """Later result pages cannot reset the bounded class lookup budget."""
        self.search_ids = []
        for number in range(60):
            identifier, type_id = f"Q{95000 + number}", f"Q{96000 + number}"
            self.entities[type_id] = {
                "id": type_id,
                "lastrevid": 100,
                "claims": {
                    "P279": [{"mainsnak": {"datavalue": {"value": {"id": "Q1344"}}}}],
                },
            }
            self.entities[identifier] = {
                "id": identifier,
                "labels": {"en": {"value": f"Opera {number}"}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": type_id}}}}],
                },
            }
            self.search_ids.append(identifier)
        requested_classes = set()

        def source_response(url, params, **kwargs):
            if "wikidata.org" in url and params.get("action") == "query":
                offset = params.get("sroffset", 0)
                payload = {
                    "query": {
                        "search": [
                            {"title": identifier}
                            for identifier in self.search_ids[offset : offset + 30]
                        ]
                    }
                }
                if not offset:
                    payload["continue"] = {"sroffset": 30, "continue": "-||"}
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps(payload).encode()
                return response
            requested_classes.update(
                identifier
                for identifier in params.get("ids", "").split("|")
                if identifier.startswith("Q96")
            )
            return self.source_response(url, params, **kwargs)

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Opera"}
            )
        self.assertEqual(len(requested_classes), 50)
        self.assertEqual(response.context["data"]["total_results"], 50)
        self.assertEqual(response.context["data"]["total_pages"], 3)
        self.assertContains(response, "Search limit reached")

    def test_deprecated_class_parents_do_not_establish_stage_form(self):
        """Discard deprecated ancestry rather than inheriting a former form."""
        self.entities["Q97001"] = {
            "id": "Q97001",
            "lastrevid": 100,
            "claims": {
                "P279": [
                    {
                        "rank": "deprecated",
                        "mainsnak": {"datavalue": {"value": {"id": "Q1344"}}},
                    }
                ],
            },
        }
        self.entities["Q97002"] = {
            "id": "Q97002",
            "labels": {"en": {"value": "Former opera"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q97001"}}}}],
            },
        }
        self.search_ids = ["Q19320959", "Q97002"]
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Opera"}
        )
        self.assertEqual(
            [
                entry["item"]["media_id"]
                for entry in response.context["data"]["results"]
            ],
            ["Q19320959"],
        )

    def test_later_batches_expand_already_loaded_class_ancestors(self):
        """Shared lookup caching must not change depth-relative classification."""
        for identifier, parent in [
            ("Q99001", "Q99002"),
            ("Q99002", "Q99003"),
            ("Q99003", "Q99004"),
            ("Q99004", "Q1344"),
        ]:
            self.entities[identifier] = {
                "id": identifier,
                "lastrevid": 100,
                "claims": {
                    "P279": [{"mainsnak": {"datavalue": {"value": {"id": parent}}}}],
                },
            }
        for identifier, type_id in [("Q99101", "Q99001"), ("Q99102", "Q99002")]:
            self.entities[identifier] = {
                "id": identifier,
                "labels": {"en": {"value": "Regional work"}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": type_id}}}}],
                },
            }

        def source_response(url, params, **kwargs):
            if "wikidata.org" in url and params.get("action") == "query":
                later = "sroffset" in params
                payload = {
                    "query": {"search": [{"title": "Q99102" if later else "Q99101"}]}
                }
                if not later:
                    payload["continue"] = {"sroffset": 1, "continue": "-||"}
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps(payload).encode()
                return response
            return self.source_response(url, params, **kwargs)

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Regional"}
            )
            self.assertEqual(
                [
                    entry["item"]["media_id"]
                    for entry in response.context["data"]["results"]
                ],
                ["Q99102"],
            )
            self.entities["Q99004"]["claims"]["P279"][0]["mainsnak"]["datavalue"][
                "value"
            ]["id"] = "Q7777570"
            self.entities["Q99102"]["claims"]["P7937"] = [
                {"mainsnak": {"datavalue": {"value": {"id": "Q1344"}}}}
            ]
            self.entities["Q99102"]["claims"]["P31"].append(
                {"mainsnak": {"datavalue": {"value": {"id": "Q116476516"}}}}
            )
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Regional"}
            )
            self.assertEqual(response.context["data"]["results"], [])

    def test_form_and_genre_subtypes_cannot_hide_medium_conflicts(self):
        """Production ancestry in any classification property overrides a form."""
        self.entities["Q99201"] = {
            "id": "Q99201",
            "lastrevid": 100,
            "claims": {
                "P279": [
                    {"mainsnak": {"datavalue": {"value": {"id": parent}}}}
                    for parent in ["Q25379", "Q7777570"]
                ],
            },
        }
        self.search_ids = []
        for identifier, property_id in [("Q99202", "P7937"), ("Q99203", "P136")]:
            self.entities[identifier] = {
                "id": identifier,
                "labels": {"en": {"value": "Conflicting work"}},
                "claims": {
                    "P31": [
                        {"mainsnak": {"datavalue": {"value": {"id": "Q116476516"}}}}
                    ],
                    property_id: [
                        {"mainsnak": {"datavalue": {"value": {"id": "Q99201"}}}}
                    ],
                },
            }
            self.search_ids.append(identifier)
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "work"}
        )
        self.assertEqual(response.context["data"]["results"], [])

    def _assert_category_artwork_restores(self, artwork):
        """Restore the category fixture through public backup endpoints offline."""
        exported = b"".join(self.client.get(reverse("export_csv")).streaming_content)
        Item.objects.get(media_id="Q822850").delete()
        with patch(
            "app.providers.services.session.get",
            side_effect=AssertionError("Restore must remain offline"),
        ):
            self.client.post(
                reverse("import_yamtrack"),
                {
                    "mode": "new",
                    "yamtrack_csv": SimpleUploadedFile("category.csv", exported),
                },
            )
        self.assertEqual(
            Item.objects.get(media_id="Q822850").theater_artwork,
            {**artwork, "evidence_work_id": "Q822850"},
        )
        for field, value in (
            ("category_revision", None),
            ("match_description", None),
            ("matched_creator", None),
            ("matched_creator", "Different Author"),
            ("matched_title", "Different Play"),
            ("matched_form", "opera"),
        ):
            with self.subTest(field=field, value=value):
                rows = list(csv.DictReader(StringIO(exported.decode())))
                incomplete = json.loads(rows[0]["theater_artwork"])
                if value is None:
                    incomplete.pop(field)
                else:
                    incomplete[field] = value
                rows[0]["theater_artwork"] = json.dumps(incomplete)
                broken = StringIO()
                writer = csv.DictWriter(broken, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
                Item.objects.get(media_id="Q822850").delete()
                self.client.post(
                    reverse("import_yamtrack"),
                    {
                        "mode": "new",
                        "yamtrack_csv": SimpleUploadedFile(
                            "broken.csv", broken.getvalue().encode()
                        ),
                    },
                )
                self.assertEqual(
                    Item.objects.get(media_id="Q822850").theater_artwork, {}
                )

    def test_source_assessed_dust_jacket_basis_preserves_its_scope(self):
        """A named jacket grant carries its US-only and reproduction caveats."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        page = fixture["commons"]["query"]["pages"]["123"]
        page["templates"] = [
            {"title": "Template:PD-US-dust-jacket"},
            {"title": "Template:PD-Art"},
        ]
        metadata = page["imageinfo"][0]["extmetadata"]
        metadata.pop("LicenseUrl")
        metadata.update(
            {
                "LicenseShortName": {"value": "Public domain"},
                "Copyrighted": {"value": "False"},
                "Permission": {
                    "value": (
                        "First-edition jacket published in the United States "
                        "without a separate copyright notice."
                    )
                },
            }
        )

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(fixture["commons"]).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q822850",
                    "media_type": "theater",
                    "source": "wikidata",
                    "status": "Completed",
                },
            )
        item = Item.objects.get(media_id="Q822850")
        self.assertEqual(item.image, page["imageinfo"][0]["url"])
        self.assertEqual(item.theater_artwork["basis"], "pd-us-dust-jacket")
        self.assertIn("United States", item.theater_artwork["basis_notice"])
        self.assertIn("PD-Art", item.theater_artwork["notices"])
        self.assertIn(
            "without a separate copyright notice", item.theater_artwork["permission"]
        )

    def test_image_history_continuation_does_not_replace_current_artwork(self):
        """Older file uploads are not missing rights metadata for the current image."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            if params.get("list") == "search":
                payload = {"query": {"search": []}}
            elif "iistart" in params:
                payload = {
                    "query": {"pages": {"123": {"pageid": 123, "imageinfo": []}}}
                }
            else:
                payload = {
                    **fixture["commons"],
                    "continue": {
                        "iistart": "2009-04-01T21:51:17Z",
                        "continue": "||categories|templates|info",
                    },
                }
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
        self.assertContains(
            response, fixture["commons"]["query"]["pages"]["123"]["imageinfo"][0]["url"]
        )
        self.assertContains(response, "Test Photographer")

    def test_artwork_metadata_continuation_preserves_complete_rights_checks(self):
        """Paginated file metadata is merged before accepting or rejecting an image."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        page = fixture["commons"]["query"]["pages"]["123"]
        final_templates = page.pop("templates")
        continuation_pending = False
        final_revision = page["lastrevid"]
        malformed_continuation = False

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            if params.get("list") == "search":
                payload = {"query": {"search": []}}
            elif "tlcontinue" in params:
                payload = {
                    "query": {
                        "pages": {
                            "123": {
                                "pageid": 123,
                                "title": page["title"],
                                "templates": final_templates,
                                "lastrevid": final_revision,
                            }
                        }
                    }
                }
                if continuation_pending:
                    payload["continue"] = {"tlcontinue": "123|More", "continue": "||"}
                if malformed_continuation:
                    payload = {}
            else:
                payload = {
                    **fixture["commons"],
                    "continue": {"tlcontinue": "123|Template", "continue": "||"},
                }
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertContains(response, page["imageinfo"][0]["url"])
            self.assertContains(response, "Test Photographer")
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q822850",
                    "media_type": "theater",
                    "source": "wikidata",
                    "status": "Completed",
                },
            )
            continuation_pending = True
            cache.clear()
            incomplete = self.client.get(
                reverse(
                    "media_details", args=["wikidata", "theater", "Q822850", "bernarda"]
                )
            )
            self.assertEqual(
                Item.objects.get(media_id="Q822850").image, page["imageinfo"][0]["url"]
            )
            continuation_pending = False
            malformed_continuation = True
            cache.clear()
            incomplete = self.client.get(
                reverse(
                    "media_details", args=["wikidata", "theater", "Q822850", "bernarda"]
                )
            )
            self.assertTrue(incomplete.context["media"]["artwork_unavailable"])
            self.assertContains(incomplete, "Test Photographer")
            malformed_continuation = False
            final_revision += 1
            cache.clear()
            self.client.get(
                reverse(
                    "media_details", args=["wikidata", "theater", "Q822850", "bernarda"]
                )
            )
            self.assertEqual(
                Item.objects.get(media_id="Q822850").image, page["imageinfo"][0]["url"]
            )
            final_revision = page["lastrevid"]
            final_templates.append({"title": "Template:No permission since"})
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertNotContains(response, page["imageinfo"][0]["url"])
            self.assertContains(response, "The House of Bernarda Alba")

    def test_invalid_file_continuations_leave_work_results_available(self):
        """Missing files and malformed continuation fragments never qualify images."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        mode = "missing_file"

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            if params.get("list") == "search":
                payload = {"query": {"search": []}}
            elif "tlcontinue" in params:
                pages = {
                    "missing_file": {
                        "-1": {"title": "File:Test stage photograph.jpg", "missing": ""}
                    },
                    "same_id_missing": {"123": {"pageid": 123, "missing": ""}},
                    "missing_revision": {"123": {"pageid": 123, "templates": []}},
                }
                payload = {"query": {"pages": pages[mode]}}
            else:
                continuation = (
                    ["invalid"]
                    if mode == "invalid_token"
                    else {"tlcontinue": "123|Next", "continue": "||"}
                )
                payload = {**fixture["commons"], "continue": continuation}
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            for mode in (
                "missing_file",
                "same_id_missing",
                "missing_revision",
                "invalid_token",
            ):
                with self.subTest(mode=mode):
                    cache.clear()
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Bernarda"}
                    )
                    self.assertContains(response, "The House of Bernarda Alba")
                    result = response.context["data"]["results"][0]["item"]
                    self.assertTrue(result["artwork_unavailable"])
                    self.assertFalse(result["theater_artwork"])

    def test_later_artwork_batch_failure_keeps_an_already_verified_image(self):
        """A failed optional candidate batch cannot discard a verified image."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        fixture["work"]["claims"]["P18"] = [
            {"mainsnak": {"datavalue": {"value": filename}}}
            for filename in [
                "Test stage photograph.jpg",
                "Second.jpg",
                "Third.jpg",
                "Unavailable.jpg",
            ]
        ]
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        recovered = False
        poster_url = "https://thumb.wikimedia.org/wikipedia/commons/stage-poster.png"

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            payload = fixture["commons"]
            if "Unavailable.jpg" in params.get("titles", ""):
                if not recovered:
                    raise requests.Timeout
                payload = json.loads(json.dumps(payload))
                poster = payload["query"]["pages"]["123"]
                poster.update(pageid=124, title="File:Stage Poster.png")
                poster["imageinfo"][0].update(url=poster_url, thumburl=poster_url)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            details = self.client.get(
                reverse(
                    "media_details", args=["wikidata", "theater", "Q822850", "bernarda"]
                )
            )
            self.assertContains(details, "Test Photographer")
            recovered = True
            retry = self.client.get(
                reverse(
                    "media_details", args=["wikidata", "theater", "Q822850", "bernarda"]
                )
            )
            self.assertContains(retry, poster_url)
        self.assertContains(
            response, fixture["commons"]["query"]["pages"]["123"]["imageinfo"][0]["url"]
        )
        self.assertContains(response, "Test Photographer")

    def test_artwork_deadline_is_shared_across_displayed_works(self):
        """Slow image enrichment stops without losing eligible work results."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.search_ids = ["Q822850", "Q822851", "Q822852", "Q822853"]
        for identifier in self.search_ids:
            work = json.loads(json.dumps(fixture["work"]))
            work.update(id=identifier, labels={"en": {"value": identifier}})
            work["claims"]["P18"][0]["mainsnak"]["datavalue"]["value"] = (
                f"{identifier}.jpg"
            )
            self.entities[identifier] = work
        elapsed = 0

        def source_response(url, params, **kwargs):
            nonlocal elapsed
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            elapsed += 31
            pages = {}
            for index, title in enumerate(params["titles"].split("|")):
                page = json.loads(
                    json.dumps(fixture["commons"]["query"]["pages"]["123"])
                )
                page.update(pageid=index + 123, title=title)
                pages[str(index + 123)] = page
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({"query": {"pages": pages}}).encode()
            return response

        with (
            patch("app.providers.services.session.get", side_effect=source_response),
            patch(
                "app.providers.commons.monotonic",
                side_effect=lambda: elapsed,
                create=True,
            ),
        ):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
        results = response.context["data"]["results"]
        self.assertEqual(len(results), 4)
        self.assertTrue(
            all(result["item"]["theater_artwork"] for result in results[:3])
        )
        self.assertFalse(results[3]["item"]["theater_artwork"])
        self.assertTrue(results[3]["item"]["artwork_unavailable"])
        self.assertContains(response, "Q822853")

    def test_artwork_request_budget_limits_a_page_without_hiding_works(self):
        """Fast responses still respect the page call cap and next requests reset it."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.search_ids = [f"Q{number}" for number in range(40000, 40020)]
        for identifier in self.search_ids:
            work = json.loads(json.dumps(fixture["work"]))
            work["id"] = identifier
            work["claims"]["P18"] = [
                {"mainsnak": {"datavalue": {"value": f"{identifier}-{number}.jpg"}}}
                for number in range(5)
            ]
            self.entities[identifier] = work
        calls = []

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 8)
            calls.append(params)
            pages = {}
            for index, title in enumerate(params["titles"].split("|")):
                page = json.loads(
                    json.dumps(fixture["commons"]["query"]["pages"]["123"])
                )
                page.update(pageid=index + 123, title=title)
                pages[str(index + 123)] = page
            payload = {"query": {"pages": pages}}
            if "tlcontinue" not in params:
                payload["continue"] = {"tlcontinue": "next", "continue": "||"}
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with (
            patch("app.providers.services.session.get", side_effect=source_response),
            patch("app.providers.commons.monotonic", return_value=0),
        ):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "stage"}
            )
            results = response.context["data"]["results"]
            self.assertEqual(len(results), 20)
            self.assertEqual(len(calls), 48)
            illustrated = sum(
                bool(result["item"]["theater_artwork"]) for result in results
            )
            self.assertGreater(illustrated, 0)
            self.assertLess(illustrated, 20)
            details = self.client.get(
                reverse(
                    "media_details", args=["wikidata", "theater", "Q40019", "stage"]
                )
            )
            self.assertContains(details, "Test Photographer")

    def test_libretto_editions_do_not_inherit_their_parent_ballet_identity(self):
        """Real P629 libretto records are not interchangeable ballet works."""
        records = [
            ("Q19240000", 2491025360, "Yanko libretto edition", "Q21056532"),
            ("Q19187271", 2491023377, "La Peri libretto edition", "Q1158756"),
        ]
        self.search_ids = []
        for identifier, revision, title, parent in records:
            self.entities[identifier] = {
                "id": identifier,
                "lastrevid": revision,
                "labels": {"en": {"value": title}},
                "claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q3331189"}}}}],
                    "P629": [{"mainsnak": {"datavalue": {"value": {"id": parent}}}}],
                },
            }
            self.entities[parent] = {
                "id": parent,
                "labels": {"en": {"value": "Underlying ballet"}},
                "claims": {
                    "P7937": [
                        {"mainsnak": {"datavalue": {"value": {"id": "Q15079786"}}}}
                    ]
                },
            }
            self.search_ids.extend([identifier, parent])
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "ballet"}
        )
        self.assertEqual(
            {
                result["item"]["media_id"]
                for result in response.context["data"]["results"]
            },
            {"Q21056532", "Q1158756"},
        )
        self.assertFalse(TheaterRedirect.objects.exists())

    def test_named_source_relationships_do_not_create_speculative_equivalence(self):
        """Real staging/derivation records retain their supported granularity."""
        records = [
            (
                "Q300532",
                "A Raisin in the Sun",
                2515858838,
                {"P31": ["Q7725634"], "P7937": ["Q25379"], "P50": ["Q461758"]},
            ),
            (
                "Q105430183",
                "A Raisin in the Sun",
                2538267855,
                {
                    "P31": ["Q7725634"],
                    "P7937": ["Q25379"],
                    "P50": ["Q461758"],
                    "P272": ["Q2989791"],
                    "P57": ["Q100450648"],
                    "P161": ["Q16637374"],
                    "P144": ["Q300534"],
                },
            ),
            (
                "Q199786",
                "Swan Lake",
                2526633021,
                {"P31": ["Q15079786"], "P86": ["Q7315"], "P4969": ["Q1044928"]},
            ),
            (
                "Q1044928",
                "Swan Lake",
                2519039384,
                {"P31": ["Q15079786"], "P144": ["Q199786"], "P1809": ["Q1139570"]},
            ),
            (
                "Q193705",
                "The Nutcracker",
                2515962484,
                {"P31": ["Q58483088"], "P7937": ["Q15079786"], "P86": ["Q7315"]},
            ),
            (
                "Q7754522",
                "The Nutcracker",
                2279128594,
                {"P31": ["Q15079786"], "P1809": ["Q310184"]},
            ),
        ]
        self.search_ids = []
        for identifier, title, revision, properties in records:
            self.search_ids.append(identifier)
            self.entities[identifier] = {
                "id": identifier,
                "lastrevid": revision,
                "labels": {"en": {"value": title}},
                "claims": {
                    property_id: [
                        {
                            "rank": "normal",
                            "mainsnak": {"datavalue": {"value": {"id": value}}},
                        }
                        for value in values
                    ]
                    for property_id, values in properties.items()
                },
            }
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "stage works"}
        )
        self.assertEqual(
            [
                result["item"]["media_id"]
                for result in response.context["data"]["results"]
            ],
            ["Q300532", "Q199786", "Q1044928", "Q193705", "Q7754522"],
        )
        for identifier in ("Q199786", "Q1044928", "Q193705", "Q7754522"):
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": identifier,
                    "media_type": "theater",
                    "source": "wikidata",
                    "status": "Completed",
                },
            )
        self.assertEqual(Theater.objects.filter(user=self.user).count(), 4)
        self.assertEqual(Item.objects.filter(media_type="theater").count(), 4)
        self.assertFalse(TheaterRedirect.objects.exists())

    def test_us_public_domain_assessment_preserves_jurisdiction_and_source(self):
        """An explicit US assessment retains source context through offline restore."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        page = fixture["commons"]["query"]["pages"]["123"]
        page["templates"] = [{"title": "Template:PD-US"}]
        metadata = page["imageinfo"][0]["extmetadata"]
        metadata.pop("LicenseUrl")
        metadata.update(
            LicenseShortName={"value": "Public domain"},
            Copyrighted={"value": "False"},
            Artist={"value": "Source Publisher"},
            Credit={"value": "Theatre Magazine, January 1919, pages 178-179"},
        )

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(
                {"query": {"search": []}}
                if params.get("list") == "search"
                else fixture["commons"]
            ).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertContains(response, page["imageinfo"][0]["url"])
            self.assertContains(response, "outside the United States")
            self.assertContains(response, "Theatre Magazine, January 1919")
            self._assert_us_assessment_rejections(page, source_response)
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q822850",
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Completed",
                },
            )
        content = b"".join(self.client.get(reverse("export_csv")).streaming_content)
        Item.objects.get(media_id="Q822850").delete()
        with patch(
            "app.providers.services.session.get",
            side_effect=AssertionError("Restore must stay offline"),
        ):
            self.client.post(
                reverse("import_yamtrack"),
                {
                    "mode": "new",
                    "yamtrack_csv": SimpleUploadedFile("us-artwork.csv", content),
                },
            )
        restored = Item.objects.get(media_id="Q822850")
        self.assertEqual(restored.image, page["imageinfo"][0]["url"])
        self.assertEqual(restored.theater_artwork["basis"], "pd-us")
        self.assertIn(
            "outside the United States", restored.theater_artwork["basis_notice"]
        )
        self.assertEqual(
            restored.theater_artwork["credit"], metadata["Credit"]["value"]
        )

    def _assert_us_assessment_rejections(self, page, source_response):
        """Keep incomplete and disputed US assessments out of search artwork."""
        original = json.loads(json.dumps(page))
        for field, value in (
            ("Artist", ""),
            ("Artist", "Unknown author"),
            ("Credit", "Theatre Magazine, undated"),
            ("Credit", ""),
            ("Copyrighted", "True"),
            ("Restrictions", "unresolved copyright"),
            ("templates", [{"title": "Template:PD-old"}]),
            (
                "templates",
                [
                    {"title": "Template:PD-US"},
                    {"title": "Template:Copyright violation"},
                ],
            ),
        ):
            with self.subTest(field=field, value=value):
                page.clear()
                page.update(json.loads(json.dumps(original)))
                if field == "templates":
                    page[field] = value
                else:
                    page["imageinfo"][0]["extmetadata"][field] = {"value": value}
                page["imageinfo"][0]["extmetadata"]["DateTimeOriginal"] = {
                    "value": "1919-01-01"
                }
                cache.clear()
                with patch(
                    "app.providers.services.session.get", side_effect=source_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Bernarda"}
                    )
                self.assertContains(response, "The House of Bernarda Alba")
                self.assertFalse(
                    response.context["data"]["results"][0]["item"]["theater_artwork"]
                )
        page.clear()
        page.update(original)
        cache.clear()

    def test_public_domain_logo_and_standard_notices_remain_usable(self):
        """Published public-domain bases and standard notices do not hide images."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        page = fixture["commons"]["query"]["pages"]["123"]
        metadata = page["imageinfo"][0]["extmetadata"]
        page["templates"] = [
            {"title": "Template:PD-textlogo"},
            {"title": "Template:LangSwitch"},
        ]
        metadata.pop("LicenseUrl")
        metadata["LicenseShortName"] = {"value": "Public domain"}
        metadata["Copyrighted"] = {"value": "False"}

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(
                {"query": {"search": []}}
                if params.get("list") == "search"
                else fixture["commons"]
            ).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertContains(response, page["imageinfo"][0]["url"])
            self.assertContains(response, "Public domain")
            self.assertContains(response, "originality")
            page["templates"] = [{"title": "Template:PD-old-auto-expired"}]
            page["categories"] = [{"title": "Category:PD Old auto: no death date"}]
            cache.clear()
            rejected = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertNotContains(rejected, page["imageinfo"][0]["url"])
            page["templates"] = [{"title": "Template:PD-textlogo"}]
            page["categories"] = []
            cache.clear()
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q822850",
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Completed",
                    "notes": "Public domain artwork round trip",
                },
            )
            exported = b"".join(
                self.client.get(reverse("export_csv")).streaming_content
            )
            Item.objects.get(media_id="Q822850").delete()
            with patch(
                "app.providers.services.session.get",
                side_effect=AssertionError("Restore must stay offline"),
            ):
                self.client.post(
                    reverse("import_yamtrack"),
                    {
                        "mode": "new",
                        "yamtrack_csv": SimpleUploadedFile("theater.csv", exported),
                    },
                )
            restored = Item.objects.get(media_id="Q822850")
            self.assertEqual(restored.theater_artwork["basis"], "pd-textlogo")
            self.assertEqual(restored.image, page["imageinfo"][0]["url"])
            page["templates"] = [
                {"title": "Template:Cc-by-sa-4.0"},
                {"title": "Template:Personality rights"},
                {"title": "Template:ISOdate"},
            ]
            metadata["LicenseShortName"] = {"value": "CC BY-SA 4.0"}
            metadata["LicenseUrl"] = {
                "value": "https://creativecommons.org/licenses/by-sa/4.0/"
            }
            metadata["Restrictions"] = {"value": "personality|trademark"}
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertContains(response, page["imageinfo"][0]["url"])
            self.assertContains(response, "personality")
            page["templates"].append({"title": "Template:No permission since"})
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertNotContains(response, page["imageinfo"][0]["url"])

    def test_migrated_license_preserves_file_specific_disclaimer(self):
        """Migrated image grants retain the actual source disclaimer URL."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        page = fixture["commons"]["query"]["pages"]["123"]
        page["templates"] = [
            {"title": "Template:Cc-by-sa-3.0-migrated-with-disclaimers"}
        ]
        metadata = page["imageinfo"][0]["extmetadata"]
        metadata["LicenseUrl"] = {
            "value": "https://creativecommons.org/licenses/by-sa/3.0/"
        }
        metadata["LicenseShortName"] = {"value": "CC BY-SA 3.0"}
        disclaimer = "https://en.wikipedia.org/wiki/Wikipedia:General_disclaimer"

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            payload = (
                {
                    "parse": {
                        "text": {
                            "*": '<table class="licensetpl">'
                            '<span class="licensetpl_short">CC BY-SA 3.0</span>'
                            ' Subject to <a href="'
                            + disclaimer
                            + '">disclaimers</a>.</table>'
                        }
                    }
                }
                if params["action"] == "parse"
                else fixture["commons"]
            )
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertContains(response, disclaimer)
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q822850",
                    "media_type": "theater",
                    "source": "wikidata",
                    "status": "Completed",
                },
            )
        self.assertIn(disclaimer, Item.objects.get().theater_artwork["notices"])

    def test_explicit_medium_and_granularity_override_form_claims(self):
        """Production and broadcast types cannot become stage works via a form."""
        for number, excluded_type in enumerate(
            [
                "Q7777570",
                "Q43099500",
                "Q35140",
                "Q2635894",
                "Q109349450",
                "Q356055",
                "Q7697093",
            ],
            start=2000,
        ):
            identifier = f"Q{number}"
            self.entities[identifier] = {
                "id": identifier,
                "labels": {"en": {"value": "Nonstage Record"}},
                "claims": {
                    "P31": [
                        {"mainsnak": {"datavalue": {"value": {"id": excluded_type}}}}
                    ],
                    "P7937": [{"mainsnak": {"datavalue": {"value": {"id": "Q2743"}}}}],
                },
            }
            self.search_ids.append(identifier)
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertContains(response, "Hamilton")
        self.assertNotContains(response, "Nonstage Record")

    def test_reviewed_specific_types_and_work_genres_supply_forms(self):
        """Reviewed source classifications recognize forms without guessing."""
        cases = [
            ("Q3000", "Italian Opera Work", "Q785522", None, "Opera"),
            ("Q3001", "Genre Musical Work", "Q58483083", "Q2743", "Musical"),
            ("Q3002", "Ballet Feerie Work", "Q58483088", "Q123578591", "Ballet"),
            ("Q3003", "Revue Work", "Q116476516", "Q918727", "Other"),
        ]
        for identifier, title, work_type, genre, _label in cases:
            claims = {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": work_type}}}}]
            }
            if genre:
                claims["P136"] = [{"mainsnak": {"datavalue": {"value": {"id": genre}}}}]
            self.entities[identifier] = {
                "id": identifier,
                "labels": {"en": {"value": title}},
                "claims": claims,
            }
            self.search_ids.append(identifier)
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Work"}
        )
        for _identifier, title, _work_type, _genre, label in cases:
            self.assertContains(response, title)
            self.assertContains(response, label)
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "Q3003",
                "source": "wikidata",
                "media_type": "theater",
                "status": "Planning",
            },
        )
        self.assertEqual(Item.objects.get(media_id="Q3003").theater_forms, ["other"])

    def test_search_details_and_tracking_use_one_work_identity(self):
        """Ordinary title search excludes film and resolves redirects on save."""
        listed = self.client.get(
            reverse("lists_modal", args=["wikidata", "theater", "Q998"])
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(Item.objects.get().media_id, "Q19320959")
        self.assertEqual(Item.objects.get().theater_forms, ["musical"])
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [work["item"]["media_id"] for work in response.context["data"]["results"]],
            ["Q19320959"],
        )
        self.assertContains(response, "Musical")
        self.assertContains(response, "Lin-Manuel Miranda")
        self.assertNotContains(response, "Hamilton film")
        details = self.client.get(
            reverse("media_details", args=["wikidata", "theater", "Q998", "hamilton"])
        )
        self.assertEqual(details.context["media"]["media_id"], "Q19320959")
        for identifier in ["Q998", "Q19320959"]:
            self.client.post(
                reverse("media_save"),
                {
                    "media_type": "theater",
                    "source": "wikidata",
                    "media_id": identifier,
                    "status": "Completed",
                    "venue": "Local Theatre",
                },
            )
        self.assertEqual(Item.objects.filter(media_type="theater").count(), 1)
        self.assertEqual(Theater.objects.filter(item__media_id="Q19320959").count(), 2)
        self.assertEqual(
            Item.objects.get(media_type="theater").theater_forms, ["musical"]
        )

    def test_saved_redirect_preserves_attendance_history_and_memberships(self):
        """Verified redirects reconcile saved works without merging attendances."""
        old = Item.objects.create(
            media_id="Q998",
            source="wikidata",
            media_type="theater",
            title="Old Hamilton",
            image="",
            theater_forms=["musical"],
        )
        target = Item.objects.create(
            media_id="Q19320959",
            source="wikidata",
            media_type="theater",
            title="Hamilton",
            image="",
            theater_forms=["musical"],
        )
        first = Theater.objects.create(
            item=old, user=self.user, notes="First visit", venue="First Theatre"
        )
        second = Theater.objects.create(
            item=target, user=self.user, notes="Second visit"
        )
        other = get_user_model().objects.create_user(username="other-attendee")
        private = Theater.objects.create(item=old, user=other, notes="Private visit")
        history_ids = list(first.history.values_list("history_id", flat=True))
        custom_list = CustomList.objects.create(name="Stage works", owner=self.user)
        custom_list.items.add(old, target)
        other.notification_excluded_items.add(old)
        old_export = b"".join(self.client.get(reverse("export_csv")).streaming_content)

        response = self.client.get(
            reverse("media_details", args=["wikidata", "theater", "Q998", "hamilton"])
        )
        self.assertContains(response, "First visit")
        self.assertContains(response, "Second visit")
        self.assertNotContains(response, "Private visit")
        for attendance in (first, second, private):
            attendance.refresh_from_db()
            self.assertEqual(attendance.item_id, target.pk)
        self.assertEqual(
            list(first.history.values_list("history_id", flat=True)), history_ids
        )
        self.assertEqual(
            list(custom_list.items.values_list("pk", flat=True)), [target.pk]
        )
        self.assertEqual(
            list(other.notification_excluded_items.values_list("pk", flat=True)),
            [target.pk],
        )
        self.assertEqual(Item.objects.filter(media_type="theater").count(), 1)
        reader = get_user_model().objects.create_user(username="restore-attendee")
        self.client.force_login(reader)
        with patch("app.providers.services.session.get", side_effect=requests.Timeout):
            self.client.post(
                reverse("import_yamtrack"),
                {
                    "mode": "new",
                    "yamtrack_csv": SimpleUploadedFile("old.csv", old_export),
                },
            )
            listing = self.client.get(
                reverse("medialist", args=[reader.username, "theater"])
            )
            self.assertEqual(listing.status_code, 200)
            modal = self.client.get(
                reverse("lists_modal", args=["wikidata", "theater", "Q998"])
            )
            self.assertEqual(modal.status_code, 200)
        self.assertEqual(
            set(Theater.objects.filter(user=reader).values_list("item_id", flat=True)),
            {target.pk},
        )
        self.assertEqual(Theater.objects.filter(user=reader).count(), 2)
        new_export = b"".join(self.client.get(reverse("export_csv")).streaming_content)
        self.assertEqual(
            {row["media_id"] for row in csv.DictReader(StringIO(new_export.decode()))},
            {"Q19320959"},
        )

    def test_redirect_preserves_artwork_and_original_evidence_during_outage(self):
        """Inherit saved credits without rewriting their evidence subject."""
        old = Item.objects.create(
            media_id="Q998",
            source="wikidata",
            media_type="theater",
            title="Hamilton",
            image="https://thumb.wikimedia.org/stage.jpg",
            theater_forms=["musical"],
            theater_artwork={
                "work_id": "Q998",
                "work_revision": 100,
                "evidence": "P18",
                "artist": "Saved Photographer",
            },
        )
        target = Item.objects.create(
            media_id="Q19320959",
            source="wikidata",
            media_type="theater",
            title="Hamilton",
            image="",
            theater_forms=["musical"],
        )
        Theater.objects.create(item=old, user=self.user, notes="My visit")

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" in url:
                raise requests.Timeout
            return self.source_response(url, params, **kwargs)

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse(
                    "media_details", args=["wikidata", "theater", "Q998", "hamilton"]
                )
            )
        self.assertContains(response, "Saved Photographer")
        target.refresh_from_db()
        self.assertEqual(target.image, "https://thumb.wikimedia.org/stage.jpg")
        self.assertEqual(target.theater_artwork["work_id"], "Q19320959")
        self.assertEqual(target.theater_artwork["evidence_work_id"], "Q998")
        self.assertEqual(target.theater_artwork["work_revision"], 100)

    def test_cached_search_resolves_newly_verified_aliases(self):
        """Cached duplicate results converge once another lookup verifies identity."""
        self.entities["Q998"] = {**self.entities["Q19320959"], "id": "Q998"}
        first = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertEqual(len(first.context["data"]["results"]), 2)
        self.entities["Q998"] = {
            **self.entities["Q19320959"],
            "redirects": {"from": "Q998", "to": "Q19320959"},
        }
        self.client.get(
            reverse("media_details", args=["wikidata", "theater", "Q998", "hamilton"])
        )
        second = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertEqual(
            [entry["item"]["media_id"] for entry in second.context["data"]["results"]],
            ["Q19320959"],
        )

    def test_unexpected_reference_aborts_redirect_without_data_loss(self):
        """Unexpected catalog references cannot be silently cascade-deleted."""
        old = Item.objects.create(
            media_id="Q998",
            source="wikidata",
            media_type="theater",
            title="Old Hamilton",
            image="",
            theater_forms=["musical"],
        )
        Item.objects.create(
            media_id="Q19320959",
            source="wikidata",
            media_type="theater",
            title="Hamilton",
            image="",
            theater_forms=["musical"],
        )
        attendance = Theater.objects.create(
            item=old, user=self.user, notes="Keep my history"
        )
        unexpected = Movie.objects.create(item=old, user=self.user, status="Planning")
        response = self.client.get(
            reverse("media_details", args=["wikidata", "theater", "Q998", "hamilton"])
        )
        self.assertContains(response, "saved records were not changed", status_code=500)
        attendance.refresh_from_db()
        self.assertEqual(attendance.item_id, old.pk)
        self.assertTrue(Movie.objects.filter(pk=unexpected.pk).exists())
        self.assertFalse(TheaterRedirect.objects.filter(alias_id="Q998").exists())

    def test_redirect_chain_retains_original_evidence_and_one_work(self):
        """Later redirects resolve old URLs without rewriting their evidence."""
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "Q998",
                "media_type": "theater",
                "source": "wikidata",
                "status": "Completed",
                "notes": "Original visit",
            },
        )
        self.entities["Q997"] = {**self.entities["Q19320959"], "id": "Q997"}
        self.entities["Q19320959"] = {
            **self.entities["Q997"],
            "redirects": {"from": "Q19320959", "to": "Q997"},
        }
        self.search_ids = ["Q19320959", "Q997"]
        cache.clear()
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertEqual(len(response.context["data"]["results"]), 1)
        self.assertEqual(Theater.objects.get().item.media_id, "Q997")
        response = self.client.get(
            reverse("media_details", args=["wikidata", "theater", "Q998", "hamilton"])
        )
        self.assertContains(response, "Original visit")
        self.assertEqual(
            TheaterRedirect.objects.get(alias_id="Q998").canonical_id, "Q19320959"
        )
        self.entities["Q998"] = {
            **self.entities["Q997"],
            "redirects": {"from": "Q998", "to": "Q997"},
        }
        self.search_ids = ["Q998", "Q19320959", "Q997"]
        cache.clear()
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["data"]["results"]), 1)
        self.assertEqual(
            TheaterRedirect.objects.get(alias_id="Q998").canonical_id, "Q19320959"
        )

    def test_conflicting_saved_redirect_leaves_records_untouched(self):
        """Contradictory provider identity cannot silently reassign saved work."""
        self.client.get(
            reverse("media_details", args=["wikidata", "theater", "Q998", "hamilton"])
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "Q19320959",
                "media_type": "theater",
                "source": "wikidata",
                "status": "Completed",
                "notes": "Keep this visit",
            },
        )
        self.entities["Q998"] = {
            **self.entities["Q19320959"],
            "id": "Q997",
            "redirects": {"from": "Q998", "to": "Q997"},
        }
        cache.clear()
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertContains(response, "Conflicting Theater identity", status_code=500)
        self.assertEqual(Theater.objects.get().item.media_id, "Q19320959")
        self.assertEqual(Theater.objects.get().notes, "Keep this visit")

    def test_work_linked_artwork_keeps_credit_through_tracking(self):
        """An eligible Commons image carries its source and license to the library."""
        self.entities["Q19320959"]["claims"]["P18"] = [
            {"mainsnak": {"datavalue": {"value": "Stage photograph.jpg"}}},
        ]
        image = "https://thumb.wikimedia.org/wikipedia/commons/a/ab/Stage.jpg"
        source_url = "https://commons.wikimedia.org/wiki/File:Stage_photograph.jpg"
        commons = {
            "query": {
                "pages": {
                    "123": {
                        "pageid": 123,
                        "title": "File:Stage photograph.jpg",
                        "templates": [{"title": "Template:Cc-by-sa-4.0"}],
                        "imageinfo": [
                            {
                                "url": image,
                                "thumburl": image,
                                "descriptionurl": source_url,
                                "width": 600,
                                "height": 900,
                                "mime": "image/jpeg",
                                "extmetadata": {
                                    "Artist": {"value": "Example Photographer"},
                                    "Credit": {"value": "Own work"},
                                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                    "LicenseUrl": {
                                        "value": "https://creativecommons.org/licenses/by-sa/4.0/"
                                    },
                                    "AttributionRequired": {"value": "true"},
                                    "Restrictions": {"value": ""},
                                },
                            }
                        ],
                    }
                }
            },
        }

        def http_response(url, params, **kwargs):
            if url != "https://commons.wikimedia.org/w/api.php":
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(
                {"query": {"search": []}} if params.get("list") == "search" else commons
            ).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=http_response):
            search = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
            self.assertContains(search, image)
            self.assertContains(search, "Example Photographer")
            self.assertContains(search, "CC BY-SA 4.0")
            details = self.client.get(
                reverse(
                    "media_details",
                    args=["wikidata", "theater", "Q19320959", "hamilton"],
                )
            )
            self.assertContains(details, "Example Photographer")
            self.assertContains(details, "CC BY-SA 4.0")
            self.client.get(
                reverse("lists_modal", args=["wikidata", "theater", "Q19320959"])
            )
            self.client.post(
                reverse("media_save"),
                {
                    "media_id": "Q19320959",
                    "media_type": "theater",
                    "source": "wikidata",
                    "status": "Completed",
                },
            )
        work = Item.objects.get(media_id="Q19320959")
        self.assertEqual(work.image, image)
        self.assertEqual(work.theater_artwork["source_url"], source_url)
        library = self.client.get(
            reverse("medialist", args=[self.user.username, "theater"])
        )
        self.assertContains(library, "Example Photographer")
        self.assertContains(library, "CC BY-SA 4.0")
        cache.clear()

        def commons_outage(url, params, **kwargs):
            if "commons.wikimedia.org" in url:
                raise requests.Timeout
            return self.source_response(url, params, **kwargs)

        with patch("app.providers.services.session.get", side_effect=commons_outage):
            details = self.client.get(
                reverse(
                    "media_details",
                    args=["wikidata", "theater", "Q19320959", "hamilton"],
                )
            )
            self.assertContains(details, "Example Photographer")
        work.refresh_from_db()
        self.assertEqual(work.image, image)
        self.assertEqual(work.theater_artwork["artist"], "Example Photographer")
        for layout in ("table", "grid"):
            response = self.client.get(
                reverse("medialist", args=[self.user.username, "theater"]),
                {"layout": layout},
            )
            self.assertContains(response, "Example Photographer")
        metadata = commons["query"]["pages"]["123"]["imageinfo"][0]["extmetadata"]
        for field, value in [
            ("Restrictions", "unresolved third-party copyright"),
            ("LicenseUrl", "https://creativecommons.org/licenses/by-nc/4.0/"),
            ("Artist", ""),
        ]:
            with self.subTest(rejected=field):
                original = metadata[field]["value"]
                metadata[field]["value"] = value
                cache.clear()
                with patch(
                    "app.providers.services.session.get", side_effect=http_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Hamilton"}
                    )
                self.assertContains(response, "Hamilton")
                self.assertNotContains(response, image)
                metadata[field]["value"] = original

    def test_source_described_poster_beats_misleading_filename(self):
        """Prefer an identified poster without changing its work or credit."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        photograph = fixture["commons"]["query"]["pages"]["123"]
        photograph["title"] = "File:Poster opening night.jpg"
        photograph["imageinfo"][0]["extmetadata"]["ImageDescription"] = {
            "value": "Photograph of the performance with a poster in the background"
        }
        poster = json.loads(json.dumps(photograph))
        poster.update(pageid=124, title="File:Archive 1945.jpg")
        poster_info = poster["imageinfo"][0]
        poster_image = "https://thumb.wikimedia.org/wikipedia/commons/poster.png"
        poster_info.update(
            url=poster_image,
            thumburl=poster_image,
            descriptionurl="https://commons.wikimedia.org/wiki/File:Archive_1945.jpg",
            width=700,
            height=1000,
        )
        poster_info["extmetadata"].update(
            ImageDescription={
                "value": "<p>Poster for The House of Bernarda Alba, a play.</p>"
            },
            Artist={"value": "Poster Designer"},
        )
        fixture["commons"]["query"]["pages"]["124"] = poster
        fixture["work"]["claims"]["P18"] = [
            {"mainsnak": {"datavalue": {"value": page["title"].removeprefix("File:")}}}
            for page in (photograph, poster)
        ]

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(fixture["commons"]).encode()
            return response

        photograph_metadata = photograph["imageinfo"][0]["extmetadata"]
        for description, object_titles, expected_page in (
            ("<p>Poster for The House of Bernarda Alba, a play.</p>", None, poster),
            (
                '<a href="/wiki/Poster">Poster</a> for The House of Bernarda Alba',
                None,
                poster,
            ),
            (
                (
                    "Poster for The House of Bernarda Alba, a play. "
                    "Advertisement for the original production."
                ),
                None,
                poster,
            ),
            ("Poster designer at a performance of the play", None, photograph),
            (
                "Poster for a film adaptation of the play. Advertisement.",
                None,
                photograph,
            ),
            (
                "Not a poster for the play, a photograph of its performance",
                None,
                photograph,
            ),
            ("", None, photograph),
            ("", ("Stage photograph", "Production poster"), poster),
        ):
            with self.subTest(description=description, object_titles=object_titles):
                poster_info["extmetadata"]["ImageDescription"]["value"] = description
                if not description:
                    photograph_metadata.pop("ImageDescription", None)
                if object_titles:
                    photograph_metadata["ObjectName"] = {"value": object_titles[0]}
                    poster_info["extmetadata"]["ObjectName"] = {
                        "value": object_titles[1]
                    }
                cache.clear()
                with patch(
                    "app.providers.services.session.get", side_effect=source_response
                ):
                    response = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Bernarda"}
                    )
                selected = response.context["data"]["results"][0]["item"]
                expected_info = expected_page["imageinfo"][0]
                self.assertEqual(selected["image"], expected_info["url"])
                self.assertContains(response, expected_info["url"])
                self.assertContains(
                    response, expected_info["extmetadata"]["Artist"]["value"]
                )
                artwork = selected["theater_artwork"]
                self.assertEqual(artwork["work_id"], "Q822850")
                self.assertEqual(artwork["evidence"], "P18/P154")
                self.assertEqual(artwork["source_url"], expected_info["descriptionurl"])

    def test_outage_preserves_local_attendance_and_manual_creation(self):
        """Provider downtime cannot prevent editing an existing attendance."""
        item = Item.objects.create(
            media_id="Q19320959",
            source="wikidata",
            media_type="theater",
            title="Hamilton",
            image="",
            theater_forms=["musical"],
        )
        attendance = Theater.objects.create(
            item=item, user=self.user, status="Planning"
        )
        self.client.post(
            reverse("media_save"),
            {
                "instance_id": attendance.pk,
                "media_id": item.media_id,
                "source": "wikidata",
                "media_type": "theater",
                "status": "In progress",
            },
        )
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        with patch("app.providers.services.session.get", side_effect=requests.Timeout):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Hamilton"}
            )
            self.assertEqual(response.status_code, 500)
            self.assertContains(response, "Wikidata", status_code=500)
            self.client.post(
                reverse("media_save"),
                {
                    "instance_id": attendance.pk,
                    "media_id": item.media_id,
                    "source": "wikidata",
                    "media_type": "theater",
                    "status": "Completed",
                    "venue": "Local Theatre",
                },
            )
            attendance.refresh_from_db()
            self.assertEqual(attendance.status, "Completed")
            self.assertEqual(attendance.venue, "Local Theatre")
            self.client.post(
                reverse("create_entry"),
                {
                    "title": "Uncataloged",
                    "media_type": "theater",
                    "theater_forms": ["other"],
                    "status": "Completed",
                },
            )
            self.assertTrue(Item.objects.filter(title="Uncataloged").exists())

    def test_distinct_adaptations_and_uncertain_forms_are_not_merged(self):
        """Same-title adaptations survive while unclassified records cannot save."""
        self.entities["Q997"] = {
            **self.entities["Q19320959"],
            "id": "Q997",
            "claims": {
                "P7937": [{"mainsnak": {"datavalue": {"value": {"id": "Q1344"}}}}]
            },
        }
        self.entities["Q996"] = {
            "id": "Q996",
            "labels": {"en": {"value": "Uncertain Work"}},
            "claims": {
                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q58483083"}}}}]
            },
        }
        self.search_ids += ["Q997", "Q996"]
        response = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertEqual(
            [work["item"]["media_id"] for work in response.context["data"]["results"]],
            ["Q19320959", "Q997"],
        )
        self.assertContains(response, "Opera")
        response = self.client.post(
            reverse("media_save"),
            {
                "media_id": "Q996",
                "media_type": "theater",
                "source": "wikidata",
                "status": "Completed",
            },
        )
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Item.objects.filter(media_id="Q996").exists())

    def test_pagination_retains_imageless_distinct_works(self):
        """Pagination counts validated unique works, never raw provider hits."""
        for number in range(1000, 1023):
            identifier = f"Q{number}"
            self.entities[identifier] = {
                **self.entities["Q19320959"],
                "id": identifier,
                "labels": {"en": {"value": f"Stage Work {number}"}},
            }
            self.search_ids.append(identifier)
        first = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Stage Work"}
        )
        second = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Stage Work", "page": 2}
        )
        first_ids = [
            work["item"]["media_id"] for work in first.context["data"]["results"]
        ]
        second_ids = [
            work["item"]["media_id"] for work in second.context["data"]["results"]
        ]
        self.assertEqual(len(first_ids), 20)
        self.assertEqual(len(second_ids), 4)
        self.assertFalse(set(first_ids).intersection(second_ids))
        self.assertEqual(second.context["data"]["total_results"], 24)

    def test_sync_refreshes_metadata_without_creating_alias_items(self):
        """Sync bypasses cached labels and leaves attendance and identity intact."""
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "Q998",
                "media_type": "theater",
                "source": "wikidata",
                "status": "Planning",
                "venue": "Saved Venue",
            },
        )
        self.entities["Q19320959"]["labels"] = {"en": {"value": "Hamilton Updated"}}
        response = self.client.post(
            reverse("sync_metadata", args=["wikidata", "theater", "Q998"]),
            {"next": "/"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Item.objects.get().title, "Hamilton Updated")
        self.assertEqual(Theater.objects.get().venue, "Saved Venue")
        modal = self.client.get(
            reverse("track_modal", args=["wikidata", "theater", "Q998"]),
            {"return_url": "/"},
        )
        self.assertContains(modal, "Saved Venue")
        self.assertEqual(Item.objects.count(), 1)

    def test_rate_limit_and_api_errors_are_visible_not_empty_results(self):
        """Back-pressure terminates the request and does not cache empty results."""
        for status, body in [
            (429, {}),
            (200, {"error": {"code": "maxlag", "info": "Replica lag"}}),
        ]:
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                response._content = json.dumps(body).encode()
                response.headers["Retry-After"] = "60"
                with patch("app.providers.services.session.get", return_value=response):
                    result = self.client.get(
                        reverse("search"), {"media_type": "theater", "q": "Hamilton"}
                    )
                self.assertContains(result, "Wikidata", status_code=500)
        recovered = self.client.get(
            reverse("search"), {"media_type": "theater", "q": "Hamilton"}
        )
        self.assertContains(recovered, "Lin-Manuel Miranda")

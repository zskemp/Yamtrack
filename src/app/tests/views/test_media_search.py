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
            response._content = json.dumps(commons).encode()
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
            ("Restrictions", "personality rights"),
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

    def test_depiction_enrichment_requires_exact_work_and_safe_credits(self):
        """Commons matching rejects other adaptations and warning-tagged files."""
        fixture = json.loads(
            (Path(__file__).parents[1] / "mock_data/theater_artwork.json").read_text()
        )
        fixture["work"]["claims"].pop("P18")
        self.entities["Q822850"] = fixture["work"]
        self.search_ids = ["Q822850"]
        page = fixture["commons"]["query"]["pages"]["123"]
        info = page["imageinfo"][0]
        info["extmetadata"]["GPSLatitude"] = {"value": 33.89}
        info["extmetadata"]["ImageDescription"] = {
            "value": "Theatrical performance of the work"
        }
        info["extmetadata"]["Artist"] = {
            "value": '<a href="javascript:alert(1)">Test Photographer</a>'
        }
        depicted_work = "Q822850"

        def source_response(url, params, **kwargs):
            if "commons.wikimedia.org" not in url:
                return self.source_response(url, params, **kwargs)
            if params["action"] == "wbgetentities":
                payload = {
                    "entities": {
                        "M123": {
                            "statements": {
                                "P180": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {
                                                "value": {"id": depicted_work}
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            elif params.get("list") == "search":
                payload = {
                    "query": {"search": [{"pageid": 123, "title": page["title"]}]}
                }
            else:
                payload = fixture["commons"]
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            return response

        with patch("app.providers.services.session.get", side_effect=source_response):
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertContains(response, info["url"])
            self.assertContains(response, "Test Photographer")
            self.assertNotContains(response, "javascript:")
            info["extmetadata"]["ImageDescription"]["value"] = (
                "Performance of an opera based on Bernarda Alba"
            )
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertNotContains(response, info["url"])
            info["extmetadata"]["ImageDescription"]["value"] = (
                "Set design for a performance of the work"
            )
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertNotContains(response, info["url"])
            info["extmetadata"]["ImageDescription"]["value"] = (
                "Theatrical performance of the work"
            )
            depicted_work = "Q999"
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertNotContains(response, info["url"])
            self.assertContains(response, "The House of Bernarda Alba")
            depicted_work = "Q822850"
            page["templates"].append({"title": "Template:Personality rights"})
            cache.clear()
            response = self.client.get(
                reverse("search"), {"media_type": "theater", "q": "Bernarda"}
            )
            self.assertNotContains(response, info["url"])

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

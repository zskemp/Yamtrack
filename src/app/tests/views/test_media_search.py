import json
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Item,
    MediaTypes,
    Sources,
    Theater,
)


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
                "media_id": "Q19320959",
                "media_type": "theater",
                "source": "wikidata",
                "status": "Planning",
                "venue": "Saved Venue",
            },
        )
        self.entities["Q19320959"]["labels"] = {"en": {"value": "Hamilton Updated"}}
        response = self.client.post(
            reverse("sync_metadata", args=["wikidata", "theater", "Q19320959"]),
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

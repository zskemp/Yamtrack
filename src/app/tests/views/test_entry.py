from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)


class CreateEntryViewTests(TestCase):
    """Test the create entry view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_create_entry_get(self):
        """Test the GET method of create_entry view."""
        response = self.client.get(reverse("create_entry"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/create_entry.html")
        self.assertIn("media_types", response.context)

        self.assertEqual(response.context["media_types"], MediaTypes.values)

    def test_create_entry_post_movie(self):
        """Test creating a movie entry."""
        form_data = {
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "status": Status.COMPLETED.value,
            "score": 8,
            "progress": 1,
            "start_date": "2023-01-01T00:00",
            "end_date": "2023-01-02T00:00",
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="Test Movie",
                media_type=MediaTypes.MOVIE.value,
            ).exists(),
        )

        movie = Movie.objects.get(item__title="Test Movie")
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.score, 8)
        self.assertEqual(movie.progress, 1)
        self.assertEqual(movie.user, self.user)

    def test_create_manual_theater_with_multiple_forms(self):
        """An imageless hybrid stage work can be saved and tracked."""
        response = self.client.post(
            reverse("create_entry"),
            {
                "title": "Local Stage Work",
                "media_type": "theater",
                "theater_forms": ["play", "musical"],
                "status": Status.COMPLETED.value,
                "score": 8,
                "notes": "Opening night",
            },
            follow=True,
        )

        self.assertContains(response, "Local Stage Work added successfully.")
        item = Item.objects.get(title="Local Stage Work")
        self.assertEqual(item.theater_forms, ["play", "musical"])
        self.assertEqual(item.theater_set.get(user=self.user).notes, "Opening night")
        library = self.client.get(reverse("medialist", args=["test", "theater"]))
        self.assertContains(library, "Local Stage Work")
        details = self.client.get(
            reverse(
                "media_details",
                args=["manual", "theater", item.media_id, "local-stage-work"],
            ),
        )
        self.assertContains(details, "Play, Musical")

    def test_repeat_theater_attendance_preserves_personal_details(self):
        """Repeat visits retain independent details and enforce ownership."""
        self.client.post(
            reverse("create_entry"),
            {
                "title": "Repeated Work",
                "media_type": "theater",
                "theater_forms": ["ballet"],
                "status": Status.COMPLETED.value,
                "end_date": "2026-09-01",
                "venue": "First Theatre",
                "location": "London",
                "production": "First Company",
                "notes": "First visit",
            },
        )
        item = Item.objects.get(title="Repeated Work")
        first = item.theater_set.get(user=self.user)
        payload = {
            "media_id": item.media_id,
            "source": "manual",
            "media_type": "theater",
            "status": Status.COMPLETED.value,
            "venue": "Second Theatre",
            "production": "Second Company",
            "notes": "Second visit",
            "score": 9,
        }
        self.client.post(reverse("media_save"), payload)
        self.assertEqual(item.theater_set.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.venue, "First Theatre")
        self.assertEqual(first.end_date.date().isoformat(), "2026-09-01")
        second = item.theater_set.exclude(pk=first.pk).get()
        self.assertEqual(second.venue, "Second Theatre")
        self.assertIsNone(second.end_date)
        payload.update(instance_id=second.pk, venue="", production="Updated Company")
        self.client.post(reverse("media_save"), payload)
        second.refresh_from_db()
        self.assertEqual(second.venue, "")
        self.assertEqual(second.history.first().production, "Updated Company")
        stranger = get_user_model().objects.create_user(username="stranger")
        self.client.force_login(stranger)
        self.assertEqual(
            self.client.post(reverse("media_save"), payload).status_code, 404
        )
        self.assertEqual(
            self.client.post(reverse("media_delete"), payload).status_code,
            404,
        )
        self.client.force_login(self.user)
        self.client.post(reverse("media_delete"), payload)
        self.assertEqual(item.theater_set.count(), 1)
        self.assertTrue(Item.objects.filter(pk=item.pk).exists())
        for name, arguments in [
            ("medialist", ["test", "theater"]),
            ("statistics", []),
            ("journal", []),
        ]:
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=arguments))
                self.assertEqual(response.status_code, 200)

    def test_manual_theater_requires_valid_forms(self):
        """Missing and unknown classifications cannot create a theater work."""
        for classifications in ([], ["film"]):
            with self.subTest(classifications=classifications):
                self.client.post(
                    reverse("create_entry"),
                    {
                        "title": "Invalid Work",
                        "media_type": "theater",
                        "theater_forms": classifications,
                        "status": Status.PLANNING.value,
                    },
                )
                self.assertFalse(Item.objects.filter(title="Invalid Work").exists())

    def test_create_entry_post_tv(self):
        """Test creating a TV show entry."""
        form_data = {
            "title": "Test TV Show",
            "media_type": MediaTypes.TV.value,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="Test TV Show",
                media_type=MediaTypes.TV.value,
            ).exists(),
        )

        tv = TV.objects.get(item__title="Test TV Show")
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(tv.score, 7)
        self.assertEqual(tv.user, self.user)

    def test_create_entry_post_season(self):
        """Test creating a season entry with parent TV."""
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "parent_tv": parent_tv.id,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="TV Show",
                media_type=MediaTypes.SEASON.value,
                season_number=1,
            ).exists(),
        )

        season = Season.objects.get(item__title="TV Show")
        self.assertEqual(season.status, Status.IN_PROGRESS.value)
        self.assertEqual(season.score, 7)
        self.assertEqual(season.user, self.user)
        self.assertEqual(season.related_tv, parent_tv)

    def test_create_entry_post_episode(self):
        """Test creating an episode entry with parent season."""
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Show",
            season_number=1,
        )
        parent_season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=parent_tv,
            status=Status.IN_PROGRESS.value,
        )

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.EPISODE.value,
            "season_number": 1,
            "episode_number": 1,
            "parent_season": parent_season.id,
            "end_date": "2023-01-02T00:00",
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        self.assertRedirects(response, reverse("create_entry"))

        self.assertTrue(
            Item.objects.filter(
                title="TV Show",
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=1,
            ).exists(),
        )

        episode = Episode.objects.get(item__title="TV Show")
        self.assertEqual(episode.related_season, parent_season)
        end_date_local = timezone.localtime(episode.end_date)
        self.assertEqual(end_date_local.strftime("%Y-%m-%d %H:%M"), "2023-01-02 00:00")

    def test_create_entry_post_duplicate_item(self):
        """Test creating a duplicate item."""
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Show",
            season_number=1,
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=parent_tv,
            status=Status.IN_PROGRESS.value,
        )

        initial_count = Item.objects.count()

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "parent_tv": parent_tv.id,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
            "repeats": 0,
        }

        with transaction.atomic():
            self.client.post(reverse("create_entry"), form_data)

        self.assertEqual(Item.objects.count(), initial_count)

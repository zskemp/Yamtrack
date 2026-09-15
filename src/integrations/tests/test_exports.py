import csv
import json
from datetime import UTC, datetime
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Q
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Anime,
    Book,
    Episode,
    Game,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
    Theater,
)


class TheaterExportRestoreTest(TestCase):
    """Own-data endpoints retain work forms and independent attendances."""

    def test_manual_attendance_round_trip(self):
        """A fresh restore needs no external lookup or original catalog rows."""
        owner = get_user_model().objects.create_user(username="owner")
        self.client.force_login(owner)
        self.client.post(
            reverse("create_entry"),
            {
                "title": "Local Hybrid",
                "media_type": "theater",
                "theater_forms": ["play", "musical"],
                "status": "Completed",
                "venue": "First Theatre",
                "end_date": "2026-09-01",
                "notes": "First visit\nSecond line",
            },
        )
        item = Item.objects.get(title="Local Hybrid")
        self.client.post(
            reverse("media_save"),
            {
                "media_id": item.media_id,
                "media_type": "theater",
                "source": "manual",
                "status": "Planning",
                "venue": "Second Theatre",
                "location": "Paris",
                "production": "Local Company",
                "notes": "Return visit",
                "score": 8,
            },
        )
        response = self.client.get(reverse("export_csv"))
        content = b"".join(response.streaming_content)
        rows = list(csv.DictReader(StringIO(content.decode())))
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0]["theater_forms"]), ["play", "musical"])
        item.delete()
        recipient = get_user_model().objects.create_user(username="recipient")
        self.client.force_login(recipient)
        response = self.client.post(
            reverse("import_yamtrack"),
            {
                "mode": "new",
                "yamtrack_csv": SimpleUploadedFile(
                    "theater.csv", content, content_type="text/csv"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Theater.objects.filter(user=recipient).count(), 2)
        self.assertEqual(Theater.objects.filter(user=owner).count(), 0)
        restored = Item.objects.get(title="Local Hybrid")
        self.assertEqual(restored.theater_forms, ["play", "musical"])
        first = Theater.objects.get(user=recipient, venue="First Theatre")
        self.assertEqual(first.end_date.date().isoformat(), "2026-09-01")
        self.assertEqual(first.notes, "First visit\nSecond line")
        second = Theater.objects.get(user=recipient, venue="Second Theatre")
        self.assertEqual(second.production, "Local Company")
        self.assertEqual(second.location, "Paris")
        self.assertEqual(second.score, 8)
        broken = StringIO()
        writer = csv.DictWriter(broken, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            row["theater_forms"] = '["film"]'
            writer.writerow(row)
        self.client.post(
            reverse("import_yamtrack"),
            {
                "mode": "overwrite",
                "yamtrack_csv": SimpleUploadedFile(
                    "invalid.csv", broken.getvalue().encode()
                ),
            },
        )
        self.assertEqual(Theater.objects.filter(user=recipient).count(), 2)
        self.assertEqual(
            Item.objects.get(pk=restored.pk).theater_forms, ["play", "musical"]
        )
        broken = StringIO()
        writer = csv.DictWriter(broken, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            row.update(title="", theater_forms='["play"]')
            writer.writerow(row)
        self.client.post(
            reverse("import_yamtrack"),
            {
                "mode": "overwrite",
                "yamtrack_csv": SimpleUploadedFile(
                    "invalid.csv", broken.getvalue().encode()
                ),
            },
        )
        self.assertEqual(Theater.objects.filter(user=recipient).count(), 2)
        self.assertEqual(Item.objects.get(pk=restored.pk).title, "Local Hybrid")


class ExportCSVTest(TestCase):
    """Test exporting media to CSV."""

    def setUp(self):
        """Create necessary data for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_superuser(**self.credentials)
        self.client.login(**self.credentials)

        item_movie = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="https://image.url",
        )
        Movie.objects.create(
            item=item_movie,
            user=self.user,
            score=9,
            status=Status.COMPLETED.value,
            notes="Nice",
            start_date=datetime(2023, 6, 1, 0, 0, tzinfo=UTC),
            end_date=datetime(2023, 6, 1, 0, 0, tzinfo=UTC),
        )

        item_season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="https://image.url",
            season_number=1,
        )

        season = Season.objects.create(
            item=item_season,
            user=self.user,
            score=9,
            status=Status.IN_PROGRESS.value,
            notes="Nice",
        )

        item_episode = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Friends",
            image="https://image.url",
            season_number=1,
            episode_number=1,
        )
        Episode.objects.create(
            item=item_episode,
            related_season=season,
            end_date=datetime(2023, 6, 1, 0, 0, tzinfo=UTC),
        )

        item_anime = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Cowboy Bebop",
            image="https://image.url",
        )
        Anime.objects.create(
            item=item_anime,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=2,
            start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
        )

        item_manga = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.MANGA.value,
            title="Berserk",
            image="https://image.url",
        )
        Manga.objects.create(
            item=item_manga,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=2,
            start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
        )

        item_game = Item.objects.create(
            media_id="1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="The Witcher 3: Wild Hunt",
            image="https://image.url",
        )
        Game.objects.create(
            item=item_game,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=120,
            start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
        )

        item_book = Item.objects.create(
            media_id="OL21733390M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Fantastic Mr. Fox",
            image="https://image.url",
        )
        Book.objects.create(
            item=item_book,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=120,
            start_date=datetime(2021, 6, 1, 0, 0, tzinfo=UTC),
        )

    def test_export_csv(self):
        """Basic test exporting media to CSV."""
        # Generate the CSV file by accessing the export view
        response = self.client.get(reverse("export_csv"))

        # Assert that the response is successful (status code 200)
        self.assertEqual(response.status_code, 200)

        # Assert that the response content type is text/csv
        self.assertEqual(response["Content-Type"], "text/csv")

        # Read the streaming content and decode it
        content = b"".join(response.streaming_content).decode("utf-8")

        # Create a CSV reader from the CSV content
        reader = csv.DictReader(StringIO(content))

        db_media_ids = set(
            Item.objects.filter(
                Q(tv__user=self.user)
                | Q(movie__user=self.user)
                | Q(season__user=self.user)
                | Q(episode__related_season__user=self.user)
                | Q(anime__user=self.user)
                | Q(manga__user=self.user)
                | Q(game__user=self.user)
                | Q(book__user=self.user),
            ).values_list("media_id", flat=True),
        )

        # Verify each row in the CSV exists in the database
        for row in reader:
            media_id = row["media_id"]
            self.assertIn(media_id, db_media_ids)

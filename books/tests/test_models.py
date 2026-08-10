import pytest
from django.db import IntegrityError, transaction

from books.models import Category


@pytest.mark.django_db
class TestCategoryUniqueness:
    def test_duplicate_source_type_label_without_code_is_rejected(self):
        """Google Books subject tags have no `code`. Two categories that only
        differ by an absent code must still collide on the unique constraint,
        otherwise upsert loses its idempotency (see PR #9 review)."""
        Category.objects.create(
            source=Category.Source.GOOGLE_BOOKS,
            type=Category.Type.SUBJECT_TAG,
            label="Fiction",
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Category.objects.create(
                    source=Category.Source.GOOGLE_BOOKS,
                    type=Category.Type.SUBJECT_TAG,
                    label="Fiction",
                )

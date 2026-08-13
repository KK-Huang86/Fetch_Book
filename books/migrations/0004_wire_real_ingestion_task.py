from django.db import migrations

OLD_TASK_NAME = "daily-ingestion-placeholder"
NEW_TASK_NAME = "daily-ingestion"


def wire_real_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=OLD_TASK_NAME).update(
        name=NEW_TASK_NAME,
        task="books.tasks.ingest_current_month",
    )


def revert_to_placeholder(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=NEW_TASK_NAME).update(
        name=OLD_TASK_NAME,
        task="books.tasks.ping",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("books", "0003_beat_schedule_placeholder"),
    ]

    operations = [
        migrations.RunPython(wire_real_task, revert_to_placeholder),
    ]

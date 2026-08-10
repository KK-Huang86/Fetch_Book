from django.db import migrations

TASK_NAME = "daily-ingestion-placeholder"


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="3",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Taipei",
    )
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={
            "crontab": schedule,
            # Placeholder task — replaced by the real scheduled ingestion
            # task (books.tasks.ingest_current_month or similar) in issue #8.
            "task": "books.tasks.ping",
            "enabled": True,
        },
    )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("django_celery_beat", "0019_alter_periodictasks_options"),
        ("books", "0002_alter_category_code_alter_ingestionfailure_stage"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]

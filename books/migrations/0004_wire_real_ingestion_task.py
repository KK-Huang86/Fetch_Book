from django.db import migrations

OLD_TASK_NAME = "daily-ingestion-placeholder"
NEW_TASK_NAME = "daily-ingestion"
REAL_TASK = "books.tasks.trigger_daily_ingestion"
PLACEHOLDER_TASK = "books.tasks.ping"


def _get_or_create_schedule(apps):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="3",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Taipei",
    )
    return schedule


def wire_real_task(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    schedule = _get_or_create_schedule(apps)

    # update_or_create rather than filter().update(): if the placeholder
    # from 0003 was ever deleted or renamed by hand, filter().update()
    # would silently do nothing and leave the environment with no
    # schedule at all. This guarantees the real task's PeriodicTask
    # exists after this migration runs, regardless of prior state.
    PeriodicTask.objects.update_or_create(
        name=NEW_TASK_NAME,
        defaults={"task": REAL_TASK, "crontab": schedule, "enabled": True},
    )
    # Clean up the placeholder under its old name so a fresh env (which
    # got it from 0003) doesn't end up with two schedules.
    PeriodicTask.objects.filter(name=OLD_TASK_NAME).exclude(name=NEW_TASK_NAME).delete()


def revert_to_placeholder(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    schedule = _get_or_create_schedule(apps)

    PeriodicTask.objects.update_or_create(
        name=OLD_TASK_NAME,
        defaults={"task": PLACEHOLDER_TASK, "crontab": schedule, "enabled": True},
    )
    PeriodicTask.objects.filter(name=NEW_TASK_NAME).exclude(name=OLD_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("books", "0003_beat_schedule_placeholder"),
    ]

    operations = [
        migrations.RunPython(wire_real_task, revert_to_placeholder),
    ]

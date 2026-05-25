import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0001_initial")]

    operations = [
        migrations.RenameField(
            model_name="surveyresponse",
            old_name="submitted_at",
            new_name="created_at",
        ),
        migrations.AddField(
            model_name="surveyresponse",
            name="submitted_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.CreateModel(
            name="SurveyConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "cooldown_days",
                    models.PositiveIntegerField(
                        default=30,
                        help_text="Minimum days a player must wait before re-surveying a subject.",
                    ),
                ),
                ("survey_points_first", models.PositiveIntegerField(default=100)),
                ("survey_points_second", models.PositiveIntegerField(default=50)),
                ("survey_points_subsequent", models.PositiveIntegerField(default=25)),
            ],
            options={
                "verbose_name": "Survey configuration",
                "verbose_name_plural": "Survey configuration",
            },
        ),
    ]

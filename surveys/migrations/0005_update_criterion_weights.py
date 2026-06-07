from django.db import migrations


WEIGHT_MAP = {
    "Has the candidate voted consistently to reduce carbon emissions?": 100,
    "Has the candidate opposed subsidies for fossil fuel industries?": 75,
}


def update_weights(apps, schema_editor):
    Criterion = apps.get_model("surveys", "Criterion")
    for question, weight in WEIGHT_MAP.items():
        Criterion.objects.filter(question=question).update(weight=weight)


class Migration(migrations.Migration):
    dependencies = [
        ("surveys", "0004_surveyconfig_min_survey_threshold"),
    ]

    operations = [
        migrations.RunPython(update_weights, migrations.RunPython.noop),
    ]

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("data", "0003_merge_20250729_1256"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExerciseDolosReport",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("include_all", models.BooleanField(default=False)),
                ("report_id", models.TextField()),
                ("submissions_included", models.PositiveIntegerField(default=0)),
                ("generated_at", models.DateTimeField(auto_now=True)),
                (
                    "exercise",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dolos_reports",
                        to="data.exercise",
                    ),
                ),
            ],
            options={
                "unique_together": {("exercise", "include_all")},
            },
        ),
    ]
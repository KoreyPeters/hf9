import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_alter_player_sqid")]

    operations = [
        migrations.AddField(
            model_name="player",
            name="display_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="player",
            name="email_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="player",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="player",
            name="jurisdiction_country",
            field=models.CharField(blank=True, max_length=2),
        ),
        migrations.AddField(
            model_name="player",
            name="jurisdiction_region",
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AlterField(
            model_name="player",
            name="email",
            field=models.EmailField(max_length=254, unique=True, verbose_name="email address"),
        ),
        migrations.CreateModel(
            name="EmailVerification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("player", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="email_verifications",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.AddIndex(
            model_name="emailverification",
            index=models.Index(fields=["token_hash"], name="accounts_em_token_h_idx"),
        ),
        migrations.CreateModel(
            name="PasskeyCredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("credential_id", models.BinaryField(unique=True)),
                ("public_key", models.BinaryField()),
                ("sign_count", models.PositiveIntegerField(default=0)),
                ("aaguid", models.CharField(blank=True, max_length=36)),
                ("device_name", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("player", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="passkeys",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
    ]

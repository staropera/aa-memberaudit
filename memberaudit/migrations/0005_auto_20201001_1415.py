# Generated by Django 3.1.1 on 2020-10-01 14:15

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("memberaudit", "0004_charactersyncstatus_sync_ok"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="character",
            name="last_error",
        ),
        migrations.RemoveField(
            model_name="character",
            name="last_sync",
        ),
        migrations.AddConstraint(
            model_name="corporationhistory",
            constraint=models.UniqueConstraint(
                fields=("character", "record_id"),
                name="functional_pk_character_corporation_history",
            ),
        ),
    ]

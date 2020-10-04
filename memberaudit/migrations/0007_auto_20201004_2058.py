# Generated by Django 3.1.1 on 2020-10-04 20:58

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("memberaudit", "0006_auto_20201004_2021"),
    ]

    operations = [
        migrations.AlterField(
            model_name="characterupdatestatus",
            name="topic",
            field=models.CharField(
                choices=[
                    ("CD", "character details"),
                    ("CH", "corporation history"),
                    ("JC", "jump clones"),
                    ("MA", "mails"),
                    ("SK", "skills"),
                    ("WB", "wallet balance"),
                    ("WJ", "wallet journal"),
                ],
                max_length=2,
            ),
        ),
    ]

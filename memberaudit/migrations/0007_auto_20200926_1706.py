# Generated by Django 3.1.1 on 2020-09-26 17:06

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('memberaudit', '0006_auto_20200926_1652'),
    ]

    operations = [
        migrations.RenameField(
            model_name='skill',
            old_name='skill',
            new_name='eve_type',
        ),
    ]
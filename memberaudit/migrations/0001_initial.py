# Generated by Django 2.2.8 on 2019-12-22 02:17

import django.core.validators
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('authentication', '0016_ownershiprecord'),
    ]

    operations = [
        migrations.CreateModel(
            name='Memberaudit',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ],
            options={
                'managed': False,
                'default_permissions': (),
                'permissions': (('basic_access', 'Can access this app'),),
            },
        ),
        migrations.CreateModel(
            name='EveEntity',
            fields=[
                ('id', models.IntegerField(primary_key=True, serialize=False, validators=[django.core.validators.MinValueValidator(0)])),
                ('category', models.CharField(choices=[('alliance', 'Alliance'), ('corporation', 'Corporation'), ('character', 'Character')], max_length=32)),
                ('name', models.CharField(max_length=254)),
            ],
        ),
        migrations.CreateModel(
            name='Owner',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_sync', models.DateTimeField(blank=True, default=None, null=True)),
                ('last_error', models.TextField(blank=True, default=None, null=True)),
                ('character', models.OneToOneField(help_text='character registered to member audit', on_delete=django.db.models.deletion.CASCADE, related_name='memberaudit_owner', to='authentication.CharacterOwnership')),
            ],
        ),
        migrations.CreateModel(
            name='MailingList',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('list_id', models.IntegerField(validators=[django.core.validators.MinValueValidator(0)])),
                ('name', models.CharField(max_length=254)),
                ('owner', models.ForeignKey(help_text='character this mailling list belongs to', on_delete=django.db.models.deletion.CASCADE, to='memberaudit.Owner')),
            ],
            options={
                'unique_together': {('owner', 'list_id')},
            },
        ),
        migrations.CreateModel(
            name='Mail',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mail_id', models.IntegerField(blank=True, default=None, null=True, validators=[django.core.validators.MinValueValidator(0)])),
                ('is_read', models.BooleanField(blank=True, default=None, null=True)),
                ('subject', models.CharField(blank=True, default=None, max_length=255, null=True)),
                ('body', models.TextField(blank=True, default=None, null=True)),
                ('timestamp', models.DateTimeField(blank=True, default=None, null=True)),
                ('from_entity', models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.CASCADE, to='memberaudit.EveEntity')),
                ('from_mailing_list', models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.CASCADE, to='memberaudit.MailingList')),
                ('owner', models.ForeignKey(help_text='character this mail belongs to', on_delete=django.db.models.deletion.CASCADE, to='memberaudit.Owner')),
            ],
            options={
                'unique_together': {('owner', 'mail_id')},
            },
        ),
        migrations.CreateModel(
            name='MailRecipient',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('mail', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='memberaudit.Mail')),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='memberaudit.EveEntity')),
            ],
            options={
                'unique_together': {('mail', 'recipient')},
            },
        ),
        migrations.CreateModel(
            name='MailLabels',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label_id', models.IntegerField(validators=[django.core.validators.MinValueValidator(0)])),
                ('mail', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='memberaudit.Mail')),
            ],
            options={
                'unique_together': {('mail', 'label_id')},
            },
        ),
    ]

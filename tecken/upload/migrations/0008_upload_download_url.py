# -*- coding: utf-8 -*-
# Generated by Django 1.11.4 on 2017-08-29 17:01
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('upload', '0007_upload_attempts'),
    ]

    operations = [
        migrations.AddField(
            model_name='upload',
            name='download_url',
            field=models.URLField(max_length=500, null=True),
        ),
    ]

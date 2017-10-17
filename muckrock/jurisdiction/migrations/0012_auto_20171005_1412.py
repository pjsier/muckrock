# -*- coding: utf-8 -*-
# Generated by Django 1.11.4 on 2017-10-05 14:12
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jurisdiction', '0011_auto_20170220_2153'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invokedexemption',
            name='properly_invoked',
            field=models.NullBooleanField(default=None, help_text=b'Did the agency properly invoke the exemption to the request?'),
        ),
    ]
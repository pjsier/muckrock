# -*- coding: utf-8 -*-
# Generated by Django 1.9.9 on 2017-03-01 20:35
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('foia', '0024_merge'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='foiamultirequest',
            options={'ordering': ['title'], 'permissions': (('file_multirequest', 'Can submit requests to multiple agencies'),), 'verbose_name': 'FOIA Multi-Request'},
        ),
        migrations.AlterModelOptions(
            name='foiarequest',
            options={'ordering': ['title'], 'permissions': (('view_foiarequest', 'Can view this request'), ('embargo_foiarequest', 'Can embargo request to make it private'), ('embargo_perm_foiarequest', 'Can embargo a request permananently'), ('crowdfund_foiarequest', 'Can start a crowdfund campaign for the request'), ('appeal_foiarequest', 'Can appeal the requests decision'), ('thank_foiarequest', 'Can thank the FOI officer for their help'), ('flag_foiarequest', 'Can flag the request for staff attention'), ('followup_foiarequest', 'Can send a manual follow up')), 'verbose_name': 'FOIA Request'},
        ),
        migrations.AlterModelOptions(
            name='rawemail',
            options={'permissions': (('view_rawemail', 'Can view the raw email for communications'),)},
        ),
    ]

"""Celery Tasks for the FOIA application"""

from celery.exceptions import SoftTimeLimitExceeded
from celery.schedules import crontab
from celery.task import periodic_task, task
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.core.urlresolvers import reverse
from django.template.defaultfilters import slugify
from django.template.loader import render_to_string, get_template

import dill as pickle
import dbsettings
import base64
import json
import logging
import numpy as np
import os
import os.path
import re
import requests
import sys
import urllib2
from boto.s3.connection import S3Connection
from datetime import date, datetime
from decimal import Decimal
from django_mailgun import MailgunAPIError
from phaxio import PhaxioApi
from phaxio.exceptions import PhaxioError
from random import randint
from raven import Client
from raven.contrib.celery import register_logger_signal, register_signal
from scipy.sparse import hstack
from urllib import quote_plus

from muckrock.foia.models import (
    FOIAFile,
    FOIARequest,
    FOIAMultiRequest,
    FOIACommunication,
    )
from muckrock.foia.codes import CODES
from muckrock.task.models import ResponseTask
from muckrock.utils import generate_status_action
from muckrock.vendor import MultipartPostHandler

foia_url = r'(?P<jurisdiction>[\w\d_-]+)-(?P<jidx>\d+)/(?P<slug>[\w\d_-]+)-(?P<idx>\d+)'

logger = logging.getLogger(__name__)

client = Client(os.environ.get('SENTRY_DSN'))
register_logger_signal(client)
register_signal(client)

class FOIAOptions(dbsettings.Group):
    """DB settings for the FOIA app"""
    enable_followup = dbsettings.BooleanValue(
            'whether to send automated followups or not')
    enable_weekend_followup = dbsettings.BooleanValue(
            'whether to send automated followups or not on the weekends')
foia_options = FOIAOptions()

class MLOptions(dbsettings.Group):
    """DB settings for the machine learning"""
    enable = dbsettings.BooleanValue(
            'automatically resolve response tasks by machine learning')
    confidence_min = dbsettings.PositiveIntegerValue(
            'minimum percent confidence level to automatically resolve')
ml_options = MLOptions()

def authenticate_documentcloud(request):
    """This is just standard username/password encoding"""
    username = settings.DOCUMENTCLOUD_USERNAME
    password = settings.DOCUMENTCLOUD_PASSWORD
    auth = base64.encodestring('%s:%s' % (username, password))[:-1]
    request.add_header('Authorization', 'Basic %s' % auth)
    return request

@task(ignore_result=True, max_retries=10, name='muckrock.foia.tasks.upload_document_cloud')
def upload_document_cloud(doc_pk, change, **kwargs):
    """Upload a document to Document Cloud"""

    try:
        doc = FOIAFile.objects.get(pk=doc_pk)
    except FOIAFile.DoesNotExist as exc:
        # give database time to sync
        upload_document_cloud.retry(countdown=300, args=[doc_pk, change], kwargs=kwargs, exc=exc)

    if not doc.is_doccloud():
        # not a file doc cloud supports, do not attempt to upload
        return

    if doc.doc_id and not change:
        # not change means we are uploading a new one - it should not have an id yet
        return

    if not doc.doc_id and change:
        # if we are changing it must have an id - this should never happen but it is!
        logger.warn('Upload Doc Cloud: Changing without a doc id: %s', doc.pk)
        return

    # these need to be encoded -> unicode to regular byte strings
    params = {
        'title': doc.title.encode('utf8'),
        'source': doc.source.encode('utf8'),
        'description': doc.description.encode('utf8'),
        'access': doc.access.encode('utf8'),
        'related_article': ('https://www.muckrock.com' +
                            doc.get_foia().get_absolute_url()).encode('utf8'),
        }
    if change:
        params['_method'] = str('put')
        url = '/documents/%s.json' % quote_plus(doc.doc_id.encode('utf-8'))
    else:
        params['file'] = doc.ffile.url.replace('https', 'http', 1)
        url = '/upload.json'

    opener = urllib2.build_opener(MultipartPostHandler.MultipartPostHandler)
    request = urllib2.Request('https://www.documentcloud.org/api/%s' % url, params)
    request = authenticate_documentcloud(request)

    try:
        ret = opener.open(request).read()
        if not change:
            info = json.loads(ret)
            doc.doc_id = info['id']
            doc.save()
            set_document_cloud_pages.apply_async(args=[doc.pk], countdown=1800)
    except (urllib2.URLError, urllib2.HTTPError) as exc:
        logger.warn('Upload Doc Cloud error: %s %s', url, doc.pk)
        countdown = ((2 ** upload_document_cloud.request.retries)
                * 300 + randint(0, 300))
        upload_document_cloud.retry(
                args=[doc.pk, change],
                kwargs=kwargs,
                exc=exc,
                countdown=countdown,
                )


@task(ignore_result=True, max_retries=10, name='muckrock.foia.tasks.set_document_cloud_pages')
def set_document_cloud_pages(doc_pk, **kwargs):
    """Get the number of pages from the document cloud server and save it locally"""

    try:
        doc = FOIAFile.objects.get(pk=doc_pk)
    except FOIAFile.DoesNotExist:
        return

    if doc.pages or not doc.is_doccloud() or not doc.doc_id:
        # already has pages set or not a doc cloud, just return
        return

    request = urllib2.Request(
            u'https://www.documentcloud.org/api/documents/%s.json' %
            quote_plus(doc.doc_id.encode('utf-8')))
    request = authenticate_documentcloud(request)

    try:
        ret = urllib2.urlopen(request).read()
        info = json.loads(ret)
        doc.pages = info['document']['pages']
        doc.save()
    except urllib2.HTTPError, exc:
        if exc.code == 404:
            # if 404, this doc id is not on document cloud
            # delete the doc_id which will cause it to get reuploaded by retry_stuck_documents
            doc.doc_id = ''
            doc.save()
        else:
            set_document_cloud_pages.retry(args=[doc.pk], countdown=600, kwargs=kwargs, exc=exc)
    except urllib2.URLError, exc:
        set_document_cloud_pages.retry(args=[doc.pk], countdown=600, kwargs=kwargs, exc=exc)


@task(ignore_result=True, max_retries=10, name='muckrock.foia.tasks.submit_multi_request')
def submit_multi_request(req_pk, **kwargs):
    """Submit a multi request to all agencies"""
    # pylint: disable=unused-argument
    req = FOIAMultiRequest.objects.get(pk=req_pk)

    # break the agencies into chunks of 50 to not timeout the database
    agencies = req.agencies.all()
    agency_chunks = [agencies[i*50:(i+1)*50] for i in xrange(agencies.count()/50 + 1)]

    for agency_chunk in agency_chunks:
        for agency in agency_chunk:
            # make a copy of the foia (and its communication) for each agency
            title = '%s (%s)' % (req.title, agency.name)
            template = get_template('text/foia/request.txt')
            context = {
                    'document_request': req.requested_docs,
                    'jurisdiction': agency.jurisdiction,
                    'user_name': req.user.get_full_name(),
                    }
            foia_request = template.render(context).split('\n', 1)[1].strip()

            new_foia = FOIARequest.objects.create(
                user=req.user,
                status='started',
                title=title,
                slug=slugify(title),
                jurisdiction=agency.jurisdiction,
                agency=agency,
                embargo=req.embargo,
                requested_docs=req.requested_docs,
                description=req.requested_docs,
                multirequest=req,
                )

            FOIACommunication.objects.create(
                foia=new_foia,
                from_who=new_foia.user.get_full_name(),
                to_who=new_foia.get_to_who(),
                date=datetime.now(),
                response=False,
                full_html=False,
                communication=foia_request,
                )

            new_foia.submit()
    req.status = 'filed'
    req.save()

@task(ignore_result=True, max_retries=3, name='muckrock.foia.tasks.classify_status')
def classify_status(task_pk, **kwargs):
    """Use a machine learning classifier to predict the communications status"""
    # pylint: disable=too-many-locals

    def get_text_ocr(doc_id):
        """Get the text OCR from document cloud"""
        doc_cloud_url = u'http://www.documentcloud.org/api/documents/%s.json'
        resp = requests.get(doc_cloud_url % quote_plus(doc_id.encode('utf-8')))
        try:
            doc_cloud_json = resp.json()
        except ValueError:
            logger.warn(u'Doc Cloud error for %s: %s', doc_id, resp.content)
            return ''
        if 'error' in doc_cloud_json:
            logger.warn(u'Doc Cloud error for %s: %s',
                    doc_id, doc_cloud_json['error'])
            return ''
        text_url = doc_cloud_json['document']['resources']['text']
        resp = requests.get(text_url)
        return resp.content.decode('utf-8')

    def get_classifier():
        """Load the pickled classifier"""
        with open('muckrock/foia/classifier.pkl', 'rb') as pkl_fp:
            return pickle.load(pkl_fp)

    def predict_status(vectorizer, selector, classifier, text, pages):
        """Run the prediction"""
        input_vect = vectorizer.transform([text])
        pages_vect = np.array([pages], dtype=np.float).transpose()
        input_vect = hstack([input_vect, pages_vect])
        input_vect = selector.transform(input_vect)
        probs = classifier.predict_proba(input_vect)[0]
        max_prob = max(probs)
        status = classifier.classes_[list(probs).index(max_prob)]
        return status, max_prob

    def resolve_if_possible(resp_task):
        """Resolve this response task if possible based off of ML setttings"""
        if (ml_options.enable and
                resp_task.status_probability >= ml_options.confidence_min):
            try:
                ml_robot = User.objects.get(username='mlrobot')
                resp_task.set_status(resp_task.predicted_status)
                resp_task.resolve(ml_robot)
            except User.DoesNotExist:
                logger.error('mlrobot account does not exist')

    try:
        resp_task = ResponseTask.objects.get(pk=task_pk)
    except ResponseTask.DoesNotExist, exc:
        classify_status.retry(
                countdown=60*30, args=[task_pk], kwargs=kwargs, exc=exc)

    file_text = []
    total_pages = 0
    for file_ in resp_task.communication.files.all():
        total_pages += file_.pages
        if file_.is_doccloud() and file_.doc_id:
            file_text.append(get_text_ocr(file_.doc_id))
        elif file_.is_doccloud() and not file_.doc_id:
            # wait longer for document cloud
            classify_status.retry(
                    countdown=60*30, args=[task_pk], kwargs=kwargs)

    full_text = resp_task.communication.communication + (' '.join(file_text))
    vectorizer, selector, classifier = get_classifier()

    status, prob = predict_status(
        vectorizer, selector, classifier, full_text, total_pages)

    resp_task.predicted_status = status
    resp_task.status_probability = int(100 * prob)

    resolve_if_possible(resp_task)

    resp_task.save()

@task(
    ignore_result=True,
    max_retries=5,
    name='muckrock.foia.tasks.send_fax',
    rate_limit='15/m',
    )
def send_fax(comm_id, subject, body, **kwargs):
    """Send a fax using the Phaxio API"""
    api = PhaxioApi(
            settings.PHAXIO_KEY,
            settings.PHAXIO_SECRET,
            raise_errors=True,
            )

    try:
        comm = FOIACommunication.objects.get(pk=comm_id)
    except FOIACommunication.DoesNotExist as exc:
        send_fax.retry(
                countdown=300,
                args=[comm_id, subject, body],
                kwargs=kwargs,
                exc=exc,
                )

    files = [f.ffile for f in comm.files.all()]
    callback_url = 'https://%s%s' % (
            settings.MUCKROCK_URL,
            reverse('phaxio-callback'),
            )
    try:
        results = api.send(
                to=comm.foia.email,
                header_text=subject[:45],
                string_data=body,
                string_data_type='text',
                files=files,
                batch=True,
                batch_delay=settings.PHAXIO_BATCH_DELAY,
                batch_collision_avoidance=True,
                callback_url=callback_url,
                **{'tag[comm_id]': comm.pk}
                )
    except PhaxioError as exc:
        logger.error(
                'Send fax error, will retry: %s',
                exc,
                exc_info=sys.exc_info(),
                )
        send_fax.retry(
                countdown=300,
                args=[comm_id, subject, body],
                kwargs=kwargs,
                exc=exc,
                )

    comm.delivered = 'fax'
    comm.fax_id = results['faxId']
    comm.save()

@periodic_task(
        run_every=crontab(hour=5, minute=0),
        time_limit=10 * 60,
        soft_time_limit=570,
        name='muckrock.foia.tasks.followup_requests')
def followup_requests():
    """Follow up on any requests that need following up on"""
    log = []
    # weekday returns 5 for sat and 6 for sun
    is_weekday = datetime.today().weekday() < 5
    if (foia_options.enable_followup and
            (foia_options.enable_weekend_followup or is_weekday)):
        try:
            num_requests = FOIARequest.objects.get_followup().count()
            for foia in FOIARequest.objects.get_followup():
                try:
                    foia.followup(automatic=True)
                    log.append('%s - %d - %s' % (foia.status, foia.pk, foia.title))
                except MailgunAPIError as exc:
                    logger.error('Mailgun error during followups: %s', exc, exc_info=sys.exc_info())
        except SoftTimeLimitExceeded:
            logger.warn('Follow ups did not complete in time. '
                    'Completed %d out of %d',
                    num_requests - FOIARequest.objects.get_followup().count(),
                    num_requests,
                    )

        logger.info('Follow Ups:\n%s', '\n'.join(log))


@periodic_task(run_every=crontab(hour=6, minute=0), name='muckrock.foia.tasks.embargo_warn')
def embargo_warn():
    """Warn users their requests are about to come off of embargo"""
    for foia in FOIARequest.objects.filter(embargo=True,
                                           permanent_embargo=False,
                                           date_embargo=date.today()):
        send_mail('[MuckRock] Embargo about to expire for FOI Request "%s"' % foia.title,
                  render_to_string('text/foia/embargo_will_expire.txt', {'request': foia}),
                  'info@muckrock.com',
                  [foia.user.email])

@periodic_task(run_every=crontab(hour=0, minute=0), name='muckrock.foia.tasks.embargo_expire')
def embargo_expire():
    """Expire requests that have a date_embargo before today"""
    for foia in FOIARequest.objects.filter(embargo=True,
                                           permanent_embargo=False,
                                           date_embargo__lt=date.today()):
        foia.embargo = False
        foia.save(comment='embargo expired')
        send_mail('[MuckRock] Embargo expired for FOI Request "%s"' % foia.title,
                  render_to_string('text/foia/embargo_did_expire.txt', {'request': foia}),
                  'info@muckrock.com',
                  [foia.user.email])

@periodic_task(run_every=crontab(hour=0, minute=0),
               name='muckrock.foia.tasks.set_all_document_cloud_pages')
def set_all_document_cloud_pages():
    """Try and set all document cloud documents that have no page count set"""
    docs = [doc for doc in FOIAFile.objects.filter(pages=0) if doc.is_doccloud()]
    logger.info('Setting document cloud pages, %d documents with 0 pages', len(docs))
    for doc in docs:
        set_document_cloud_pages.apply_async(args=[doc.pk])


@periodic_task(run_every=crontab(hour=0, minute=20),
               name='muckrock.foia.tasks.retry_stuck_documents')
def retry_stuck_documents():
    """Reupload all document cloud documents which are stuck"""
    docs = [doc for doc in FOIAFile.objects.filter(doc_id='')
            if doc.is_doccloud() and doc.get_foia()]
    logger.info('Reupload documents, %d documents are stuck', len(docs))
    for doc in docs:
        upload_document_cloud.apply_async(args=[doc.pk, False])

class SizeError(Exception):
    """Uploaded file is not the correct size"""

# Increase the time limit for autoimport to 1 hour, and a soft time limit to
# 5 minutes before that
@periodic_task(
        run_every=crontab(hour=2, minute=0),
        name='muckrock.foia.tasks.autoimport',
        time_limit=3600, soft_time_limit=3300)
def autoimport():
    """Auto import documents from S3"""
    # pylint: disable=broad-except
    # pylint: disable=too-many-locals
    # pylint: disable=too-many-branches
    # pylint: disable=too-many-statements
    p_name = re.compile(
            r'(?P<month>\d\d?)-(?P<day>\d\d?)-(?P<year>\d\d) '
            r'(?P<docs>(?:mr\d+ )+)(?P<code>[a-z-]+)(?:\$(?P<arg>\S+))?'
            r'(?: ID#(?P<id>\S+))?'
            r'(?: EST(?P<estm>\d\d?)-(?P<estd>\d\d?)-(?P<esty>\d\d))?'
            , re.I)

    def s3_copy(bucket, key_or_pre, dest_name):
        """Copy an s3 key or prefix"""

        if key_or_pre.name.endswith('/'):
            for key in bucket.list(prefix=key_or_pre.name, delimiter='/'):
                if key.name == key_or_pre.name:
                    key.copy(bucket, dest_name)
                    continue
                s3_copy(bucket, key, '%s/%s' % (
                    dest_name,
                    os.path.basename(os.path.normpath(key.name))
                ))
        else:
            key_or_pre.copy(bucket, dest_name)

    def s3_delete(bucket, key_or_pre):
        """Delete an s3 key or prefix"""

        if key_or_pre.name.endswith('/'):
            for key in bucket.list(prefix=key_or_pre.name, delimiter='/'):
                if key.name == key_or_pre.name:
                    key.delete()
                    continue
                s3_delete(bucket, key)
        else:
            key_or_pre.delete()

    def parse_name(name):
        """Parse a file name"""
        # strip off trailing / and file extension
        name = os.path.normpath(name)
        name = os.path.splitext(name)[0]

        m_name = p_name.match(name)
        if not m_name:
            raise ValueError('ERROR: %s does not match the file name format' % name)
        code = m_name.group('code').upper()
        if code not in CODES:
            raise ValueError('ERROR: %s uses an unknown code' % name)
        foia_pks = [pk[2:] for pk in m_name.group('docs').split()]
        file_date = datetime(int(m_name.group('year')) + 2000,
                             int(m_name.group('month')),
                             int(m_name.group('day')))
        title, status, body = CODES[code]
        arg = m_name.group('arg')
        id_ = m_name.group('id')
        if m_name.group('esty'):
            est_date = date(int(m_name.group('esty')) + 2000,
                            int(m_name.group('estm')),
                            int(m_name.group('estd')))
        else:
            est_date = None

        return (foia_pks, file_date, code, title,
                status, body, arg, id_, est_date)

    def import_key(key, storage_bucket, comm, log, title=None):
        """Import a key"""

        foia = comm.foia
        file_name = os.path.split(key.name)[1]

        title = title or file_name
        access = 'private' if foia.embargo else 'public'

        foia_file = FOIAFile(foia=foia, comm=comm, title=title, date=comm.date,
                             source=comm.from_who[:70], access=access)
        full_file_name = foia_file.ffile.field.generate_filename(
                foia_file.ffile.instance,
                file_name,
                )
        full_file_name = default_storage.get_available_name(full_file_name)
        new_key = key.copy(storage_bucket, full_file_name)
        new_key.set_acl('public-read')

        foia_file.ffile.name = full_file_name
        foia_file.save()
        if key.size != foia_file.ffile.size:
            raise SizeError(key.size, foia_file.ffile.size, foia_file)

        log.append('SUCCESS: %s uploaded to FOIA Request %s with a status of %s' %
                   (file_name, foia.pk, foia.status))

        upload_document_cloud.apply_async(args=[foia_file.pk, False], countdown=3)

    def import_prefix(prefix, bucket, storage_bucket, comm, log):
        """Import a prefix (folder) full of documents"""

        for key in bucket.list(prefix=prefix.name, delimiter='/'):
            if key.name == prefix.name:
                continue
            if key.name.endswith('/'):
                log.append('ERROR: nested directories not allowed: %s in %s' %
                        (key.name, prefix.name))
                continue
            try:
                import_key(key, storage_bucket, comm, log)
            except SizeError as exc:
                s3_copy(bucket, key, 'review/%s' % key.name[6:])
                exc.args[2].delete() # delete the foia file
                comm.delete()
                log.append('ERROR: %s was %s bytes and after uploaded was %s bytes - retry' %
                           (key.name[6:], exc.args[0], exc.args[1]))

    def process(log):
        """Process the files"""
        log.append('Start Time: %s' % datetime.now())
        conn = S3Connection(settings.AWS_ACCESS_KEY_ID, settings.AWS_SECRET_ACCESS_KEY)
        bucket = conn.get_bucket(settings.AWS_AUTOIMPORT_BUCKET_NAME)
        storage_bucket = conn.get_bucket(settings.AWS_STORAGE_BUCKET_NAME)
        for key in bucket.list(prefix='scans/', delimiter='/'):
            if key.name == 'scans/':
                continue
            # strip off 'scans/'
            file_name = key.name[6:]

            try:
                (foia_pks, file_date, code, title, status,
                        body, arg, id_, est_date) = parse_name(file_name)
            except ValueError as exc:
                s3_copy(bucket, key, 'review/%s' % file_name)
                s3_delete(bucket, key)
                log.append(unicode(exc))
                continue

            for foia_pk in foia_pks:
                try:
                    foia = FOIARequest.objects.get(pk=foia_pk)
                    source = foia.agency.name if foia.agency else ''

                    comm = FOIACommunication.objects.create(
                        foia=foia, from_who=source,
                        to_who=foia.user.get_full_name(), response=True,
                        date=file_date, full_html=False, delivered='mail',
                        communication=body, status=status)

                    foia.status = status or foia.status
                    if foia.status in ['partial', 'done', 'rejected', 'no_docs']:
                        foia.date_done = file_date.date()
                    if code == 'FEE' and arg:
                        foia.price = Decimal(arg)
                    if id_:
                        foia.tracking_id = id_
                    if est_date:
                        foia.date_estimate = est_date
                    if code == 'REJ-P':
                        foia.proxy_reject()

                    if key.name.endswith('/'):
                        import_prefix(key, bucket, storage_bucket, comm, log)
                    else:
                        import_key(key, storage_bucket, comm, log, title=title)

                    foia.save(comment='updated from autoimport files')
                    action = generate_status_action(foia)
                    foia.notify(action)
                    foia.update(comm.anchor())

                except FOIARequest.DoesNotExist:
                    s3_copy(bucket, key, 'review/%s' % file_name)
                    log.append('ERROR: %s references FOIA Request %s, but it does not exist' %
                               (file_name, foia_pk))
                except SoftTimeLimitExceeded:
                    # if we reach the soft time limit,
                    # re-raise so we can catch and clean up
                    raise
                except Exception as exc:
                    s3_copy(bucket, key, 'review/%s' % file_name)
                    log.append('ERROR: %s has caused an unknown error. %s' % (file_name, exc))
                    logger.error('Autoimport error: %s', exc, exc_info=sys.exc_info())
            # delete key after processing all requests for it
            s3_delete(bucket, key)
        log.append('End Time: %s' % datetime.now())

    try:
        log = []
        process(log)
    except SoftTimeLimitExceeded:
        log.append('ERROR: Time limit exceeded, please check folder for '
                'undeleted uploads.  How big of a file did you put in there?')
        log.append('End Time: %s' % datetime.now())
    finally:
        send_mail(
                '[AUTOIMPORT] %s Logs' % datetime.now(),
                '\n'.join(log),
                'info@muckrock.com',
                ['info@muckrock.com'],
                fail_silently=False)

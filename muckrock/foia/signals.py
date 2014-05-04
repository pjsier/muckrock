"""Model signal handlers for the FOIA applicaiton"""

from django.db.models.signals import pre_save, post_delete

from boto.s3.connection import S3Connection

from muckrock.foia.models import FOIARequest, FOIAFile
from muckrock.foia.tasks import upload_document_cloud
from muckrock.settings import AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_STORAGE_BUCKET_NAME,\
                              AWS_DEBUG, DEBUG

def foia_update_embargo(sender, **kwargs):
    """When embargo has possibly been switched, update the document cloud permissions"""
    # pylint: disable=E1101
    # pylint: disable=W0613

    request = kwargs['instance']
    old_request = request.get_saved()

    if not old_request:
        # if we are saving a new FOIA Request, there are no docs to update
        return

    if request.is_embargo(save=False) != old_request.is_embargo(save=False):
        access = 'private' if request.is_embargo(save=False) else 'public'
        for doc in request.files.all():
            if doc.is_doccloud() and doc.access != access:
                doc.access = access
                doc.save()
                upload_document_cloud.apply_async(args=[doc.pk, True], countdown=3)


def foia_file_delete_s3(sender, **kwargs):
    """Delete file from S3 after the model is deleted"""
    # pylint: disable=unused-argument

    if not DEBUG or AWS_DEBUG:
        # only delete if we are using s3
        foia_file = kwargs['instance']

        conn = S3Connection(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
        bucket = conn.get_bucket(AWS_STORAGE_BUCKET_NAME)
        key = bucket.get_key(foia_file.ffile.name)
        if key:
            key.delete()


pre_save.connect(foia_update_embargo, sender=FOIARequest,
                 dispatch_uid='muckrock.foia.signals.embargo')

post_delete.connect(foia_file_delete_s3, sender=FOIAFile,
                    dispatch_uid='muckrock.foia.signals.delete_s3')


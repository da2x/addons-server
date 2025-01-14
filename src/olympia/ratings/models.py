from django.core.cache import cache
from django.db import models
from django.db.models import Q
from django.dispatch import receiver
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

import six

import olympia.core.logger

from olympia import activity, amo
from olympia.amo.fields import PositiveAutoField
from olympia.amo.models import ManagerBase, ModelBase
from olympia.amo.templatetags import jinja_helpers
from olympia.amo.utils import send_mail_jinja
from olympia.translations.templatetags.jinja_helpers import truncate


log = olympia.core.logger.getLogger('z.ratings')


class RatingQuerySet(models.QuerySet):
    """
    A queryset modified for soft deletion.
    """
    def to_moderate(self):
        """Return ratings to moderate.

        Ratings attached lacking an addon or attached to an addon that is no
        longer nominated or public are ignored, as well as ratings attached to
        unlisted versions.
        """
        return self.exclude(
            Q(addon__isnull=True) |
            Q(version__channel=amo.RELEASE_CHANNEL_UNLISTED) |
            Q(ratingflag__isnull=True)).filter(
                editorreview=True, addon__status__in=amo.VALID_ADDON_STATUSES)

    def delete(self, user_responsible=None, hard_delete=False):
        if hard_delete:
            return super(RatingQuerySet, self).delete()
        else:
            for rating in self:
                rating.delete(user_responsible=user_responsible)


class RatingManager(ManagerBase):
    _queryset_class = RatingQuerySet

    def __init__(self, include_deleted=False):
        # DO NOT change the default value of include_deleted unless you've read
        # through the comment just above the Addon managers
        # declaration/instantiation and understand the consequences.
        super(RatingManager, self).__init__()
        self.include_deleted = include_deleted

    def get_queryset(self):
        qs = super(RatingManager, self).get_queryset()
        if not self.include_deleted:
            qs = qs.exclude(deleted=True).exclude(reply_to__deleted=True)
        return qs


class WithoutRepliesRatingManager(ManagerBase):
    """Manager to fetch ratings that aren't replies (and aren't deleted)."""
    _queryset_class = RatingQuerySet

    def get_queryset(self):
        qs = super(WithoutRepliesRatingManager, self).get_queryset()
        qs = qs.exclude(deleted=True)
        return qs.filter(reply_to__isnull=True)


@python_2_unicode_compatible
class Rating(ModelBase):
    RATING_CHOICES = (
        (None, _('None')),
        (0, '☆☆☆☆☆'),
        (1, '☆☆☆☆★'),
        (2, '☆☆☆★★'),
        (3, '☆☆★★★'),
        (4, '☆★★★★'),
        (5, '★★★★★'),
    )
    id = PositiveAutoField(primary_key=True)
    addon = models.ForeignKey(
        'addons.Addon', related_name='_ratings', on_delete=models.CASCADE)
    version = models.ForeignKey(
        'versions.Version', related_name='ratings', null=True,
        on_delete=models.CASCADE)
    user = models.ForeignKey(
        'users.UserProfile', related_name='_ratings_all',
        on_delete=models.CASCADE)
    reply_to = models.OneToOneField(
        'self', null=True, related_name='reply', db_column='reply_to',
        on_delete=models.CASCADE)

    rating = models.PositiveSmallIntegerField(
        null=True, choices=RATING_CHOICES)
    body = models.TextField(db_column='text_body', null=True)
    ip_address = models.CharField(max_length=255, default='0.0.0.0')

    editorreview = models.BooleanField(default=False)
    flag = models.BooleanField(default=False)

    deleted = models.BooleanField(default=False)

    # Denormalized fields for easy lookup queries.
    is_latest = models.BooleanField(
        default=True, editable=False,
        help_text="Is this the user's latest rating for the add-on?")
    previous_count = models.PositiveIntegerField(
        default=0, editable=False,
        help_text="How many previous ratings by the user for this add-on?")

    unfiltered = RatingManager(include_deleted=True)
    objects = RatingManager()
    without_replies = WithoutRepliesRatingManager()

    class Meta:
        db_table = 'reviews'
        # This is very important: please read the lengthy comment in Addon.Meta
        # description
        base_manager_name = 'unfiltered'
        ordering = ('-created',)

    def __str__(self):
        return truncate(six.text_type(self.body), 10)

    def __init__(self, *args, **kwargs):
        user_responsible = kwargs.pop('user_responsible', None)
        super(Rating, self).__init__(*args, **kwargs)
        if user_responsible is not None:
            self.user_responsible = user_responsible

    @property
    def user_responsible(self):
        """Return user responsible for the current changes being made on this
        model. Only set by the views when they are about to save a Review
        instance, to track if the original author or an admin was responsible
        for the change.

        Having this as a @property with a setter makes update_or_create() work,
        otherwise it rejects the property, causing an error."""
        return self._user_responsible

    @user_responsible.setter
    def user_responsible(self, value):
        self._user_responsible = value

    def get_url_path(self):
        return jinja_helpers.url(
            'addons.ratings.detail', self.addon.slug, self.id)

    def approve(self, user):
        from olympia.reviewers.models import ReviewerScore

        activity.log_create(
            amo.LOG.APPROVE_RATING, self.addon, self, user=user, details=dict(
                body=six.text_type(self.body),
                addon_id=self.addon.pk,
                addon_title=six.text_type(self.addon.name),
                is_flagged=self.ratingflag_set.exists()))
        for flag in self.ratingflag_set.all():
            flag.delete()
        self.editorreview = False
        # We've already logged what we want to log, no need to pass
        # user_responsible=user.
        self.save()
        ReviewerScore.award_moderation_points(user, self.addon, self.pk)

    def delete(self, user_responsible=None, send_post_save_signal=True):
        if user_responsible is None:
            user_responsible = self.user

        rating_was_moderated = False
        # Log deleting ratings to moderation log,
        # except if the author deletes it
        if user_responsible != self.user:
            # Remember moderation state
            rating_was_moderated = True
            from olympia.reviewers.models import ReviewerScore

            activity.log_create(
                amo.LOG.DELETE_RATING, self.addon, self, user=user_responsible,
                details={
                    'body': six.text_type(self.body),
                    'addon_id': self.addon.pk,
                    'addon_title': six.text_type(self.addon.name),
                    'is_flagged': self.ratingflag_set.exists()
                }
            )
            for flag in self.ratingflag_set.all():
                flag.delete()

        log.info(u'Rating deleted: %s deleted id:%s by %s ("%s")',
                 user_responsible.name, self.pk, self.user.name,
                 six.text_type(self.body))
        self.update(deleted=True, _signal=send_post_save_signal)
        # Force refreshing of denormalized data (it wouldn't happen otherwise
        # because we're not dealing with a creation).
        self.update_denormalized_fields()

        if rating_was_moderated:
            ReviewerScore.award_moderation_points(user_responsible,
                                                  self.addon,
                                                  self.pk)

    def undelete(self):
        self.update(deleted=False)
        # Force refreshing of denormalized data (it wouldn't happen otherwise
        # because we're not dealing with a creation).
        self.update_denormalized_fields()

    @classmethod
    def get_replies(cls, ratings):
        ratings = [r.id for r in ratings]
        qs = Rating.objects.filter(reply_to__in=ratings)
        return dict((r.reply_to_id, r) for r in qs)

    def send_notification_email(self):
        if self.reply_to:
            # It's a reply.
            reply_url = jinja_helpers.url(
                'addons.ratings.detail', self.addon.slug,
                self.reply_to.pk, add_prefix=False)
            data = {
                'name': self.addon.name,
                'reply': self.body,
                'rating_url': jinja_helpers.absolutify(reply_url)
            }
            recipients = [self.reply_to.user.email]
            subject = u'Mozilla Add-on Developer Reply: %s' % self.addon.name
            template = 'ratings/emails/reply_review.ltxt'
            perm_setting = 'reply'
        else:
            # It's a new rating.
            rating_url = jinja_helpers.url(
                'addons.ratings.detail', self.addon.slug, self.pk,
                add_prefix=False)
            data = {
                'name': self.addon.name,
                'rating': self,
                'rating_url': jinja_helpers.absolutify(rating_url)
            }
            recipients = [author.email for author in self.addon.authors.all()]
            subject = u'Mozilla Add-on User Rating: %s' % self.addon.name
            template = 'ratings/emails/new_rating.txt'
            perm_setting = 'new_review'
        send_mail_jinja(
            subject, template, data,
            recipient_list=recipients, perm_setting=perm_setting)

    def update_denormalized_fields(self):
        from . import tasks

        pair = self.addon_id, self.user_id
        tasks.update_denorm(pair)

    def post_save(sender, instance, created, **kwargs):
        from olympia.addons.models import update_search_index
        from . import tasks

        if kwargs.get('raw'):
            return

        if getattr(instance, 'user_responsible', None):
            # user_responsible is not a field on the model, so it's not
            # persistent: it's just something the views will set temporarily
            # when manipulating a Rating that indicates a real user made that
            # change.
            action = 'New' if created else 'Edited'
            if instance.reply_to:
                log.debug('%s reply to %s: %s' % (
                    action, instance.reply_to_id, instance.pk))
            else:
                log.debug('%s rating: %s' % (action, instance.pk))

            # For new ratings - not replies - and all edits (including replies
            # this time) by users we want to insert a new ActivityLog.
            new_rating_or_edit = not instance.reply_to or not created
            if new_rating_or_edit:
                action = amo.LOG.ADD_RATING if created else amo.LOG.EDIT_RATING
                activity.log_create(action, instance.addon, instance,
                                    user=instance.user_responsible)

            # For new ratings and new replies we want to send an email.
            if created:
                instance.send_notification_email()

        if created:
            # Do this immediately synchronously so is_latest is correct before
            # we fire the aggregates task.
            instance.update_denormalized_fields()

        # Rating counts have changed, so run the task and trigger a reindex.
        tasks.addon_rating_aggregates.delay(instance.addon_id)
        update_search_index(instance.addon.__class__, instance.addon)


@receiver(models.signals.post_save, sender=Rating,
          dispatch_uid='rating_post_save')
def rating_post_save(sender, instance, created, **kwargs):
    # The extra indirection is to make it easy to mock and deactivate on a case
    # by case basis in tests despite the fact that it's already been connected.
    Rating.post_save(sender, instance, created, **kwargs)


class RatingFlag(ModelBase):
    SPAM = 'review_flag_reason_spam'
    LANGUAGE = 'review_flag_reason_language'
    SUPPORT = 'review_flag_reason_bug_support'
    OTHER = 'review_flag_reason_other'
    FLAGS = (
        (SPAM, _(u'Spam or otherwise non-review content')),
        (LANGUAGE, _(u'Inappropriate language/dialog')),
        (SUPPORT, _(u'Misplaced bug report or support request')),
        (OTHER, _(u'Other (please specify)')),
    )

    rating = models.ForeignKey(
        Rating, db_column='review_id', on_delete=models.CASCADE)
    user = models.ForeignKey(
        'users.UserProfile', null=True, on_delete=models.CASCADE)
    flag = models.CharField(
        max_length=64, default=OTHER, choices=FLAGS, db_column='flag_name')
    note = models.CharField(
        max_length=100, db_column='flag_notes', blank=True, default='')

    class Meta:
        db_table = 'reviews_moderation_flags'
        unique_together = (('rating', 'user'),)


class GroupedRating(object):
    """
    Group an add-on's ratings so we can have a graph of rating counts.

    SELECT rating, COUNT(rating) FROM reviews where addon=:id
    """
    # Non-critical data, so we always leave it in memcache. Cache for a
    # particular add-on is cleared when a rating is added/modified and updated
    # when a request tries to retrieve them and the cache is empty.
    prefix = 'addons:grouped:rating'

    @classmethod
    def key(cls, addon_pk):
        return '%s:%s' % (cls.prefix, addon_pk)

    @classmethod
    def delete(cls, addon_pk):
        cache.delete(cls.key(addon_pk))

    @classmethod
    def get(cls, addon_pk, update_none=True):
        try:
            grouped_ratings = cache.get(cls.key(addon_pk))
            if update_none and grouped_ratings is None:
                return cls.set(addon_pk)
            return grouped_ratings
        except Exception:
            # Don't worry about failures, especially timeouts.
            return

    @classmethod
    def set(cls, addon_pk, using=None):
        qs = (Rating.without_replies.all().using(using)
              .filter(addon=addon_pk, is_latest=True)
              .values_list('rating')
              .annotate(models.Count('rating')).order_by())
        counts = dict(qs)
        ratings = [(rating, counts.get(rating, 0)) for rating in range(1, 6)]
        cache.set(cls.key(addon_pk), ratings)
        return ratings

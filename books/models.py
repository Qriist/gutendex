from django.db import models


class Book(models.Model):
    authors = models.ManyToManyField('Person')
    bookshelves = models.ManyToManyField('Bookshelf')
    copyright = models.BooleanField(null=True)
    download_count = models.PositiveIntegerField(blank=True, null=True)
    editors = models.ManyToManyField("Person", related_name="books_edited")
    gutenberg_id = models.PositiveIntegerField(unique=True)
    languages = models.ManyToManyField('Language')
    media_type = models.CharField(max_length=16)
    subjects = models.ManyToManyField('Subject')
    title = models.CharField(blank=True, max_length=1024, null=True)
    published_year = models.SmallIntegerField(null=True, blank=True)
    issued_date = models.DateField(null=True, blank=True)
    gt_modified = models.DateField(null=True, blank=True)
    wikipedia_url = models.URLField(max_length=512, blank=True, default='')
    word_count = models.PositiveIntegerField(null=True, blank=True)
    reading_time_minutes = models.PositiveIntegerField(null=True, blank=True)
    flesch_reading_ease = models.FloatField(null=True, blank=True)
    dale_chall_score = models.FloatField(null=True, blank=True)
    rare_word_ratio = models.FloatField(null=True, blank=True)
    stats_computed_at = models.DateTimeField(null=True, blank=True)
    stats_failed = models.BooleanField(default=False)
    stats_fail_reason = models.CharField(max_length=512, blank=True, default='')
    reading_score = models.CharField(max_length=256, blank=True, default='')
    reading_score_value = models.FloatField(null=True, blank=True)
    related_gt_books = models.TextField(blank=True, default='')
    se_match_id = models.CharField(max_length=256, blank=True, default='')
    translators = models.ManyToManyField(
        'Person', related_name='books_translated')

    def __str__(self):
        if self.title:
            return self.title
        else:
            return str(self.id)

    def get_formats(self):
        return Format.objects.filter(book_id=self.id)

    def get_summaries(self):
        return Summary.objects.filter(book_id=self.id)


class Bookshelf(models.Model):
    name = models.CharField(max_length=64)
    gutenberg_id = models.IntegerField(null=True, blank=True)
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='children'
    )

    def __str__(self):
        return self.name


class Format(models.Model):
    book = models.ForeignKey('Book', on_delete=models.CASCADE)
    mime_type = models.CharField(max_length=32)
    url = models.CharField(max_length=256)
    modified = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return "%s (%s)" % (
            self.mime_type,
            self.book.__str__()
        )


class Language(models.Model):
    code = models.CharField(max_length=4, unique=True)

    def __str__(self):
        return self.code


class Person(models.Model):
    birth_year = models.SmallIntegerField(blank=True, null=True)
    death_year = models.SmallIntegerField(blank=True, null=True)
    gutenberg_id = models.PositiveIntegerField(unique=True, null=True, blank=True)
    name = models.CharField(max_length=128)
    wikipedia_url = models.URLField(max_length=512, blank=True, default='')
    loc_url = models.URLField(max_length=512, blank=True, default='')
    viaf_url = models.URLField(max_length=512, blank=True, default='')
    birth_date = models.DateField(null=True, blank=True)
    death_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name


class Subject(models.Model):
    name = models.CharField(max_length=256)

    def __str__(self):
        return self.name


class Summary(models.Model):
    book = models.ForeignKey('Book', on_delete=models.CASCADE)
    text = models.TextField()

    def __str__(self):
        preview_len = 24
        return f'{self.text[:preview_len]}...' if len(self.text) > preview_len else self.text

import subprocess
from subprocess import call, Popen
import json
import os
import shutil
import threading
from time import sleep, strftime, time
import sys
import urllib.request
from datetime import datetime, timezone

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from books import utils
from books.models import *


TEMP_PATH = settings.CATALOG_TEMP_DIR

#URL = 'https://gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2'
#URL = 'https://od.lk/d/OV8yNjUxMzQxNjJfc2ZsOHc/catalog.tar.bz2' #tiny test file
URL = 'https://web.opendrive.com/api/v1/download/file.json/OV8yNjUxMzQxNjJfc2ZsOHc?inline=0'

DOWNLOAD_PATH = os.path.join(TEMP_PATH, 'catalog.tar.bz2')

MOVE_SOURCE_PATH = os.path.join(TEMP_PATH, 'cache/epub')
MOVE_TARGET_PATH = settings.CATALOG_RDF_DIR

LOG_DIRECTORY = settings.CATALOG_LOG_DIR
LOG_FILE_NAME = strftime('%Y-%m-%d_%H%M%S') + '.txt'
LOG_PATH = os.path.join(LOG_DIRECTORY, LOG_FILE_NAME)

CACHE_PATH = os.path.join(os.path.dirname(settings.CATALOG_RDF_DIR), 'rdf_stat_cache.json')
LAST_MODIFIED_PATH = os.path.join(os.path.dirname(settings.CATALOG_RDF_DIR), 'catalog_last_modified.txt')

_quiet_mode = False


# This gives a set of the names of the subdirectories in the given file path.
def get_directory_set(path):
    with os.scandir(path) as it:
        return {e.name for e in it if e.is_dir(follow_symlinks=False)}


def log(*args, force=False):
    now = strftime('%H:%M:%S')
    message = ' '.join(args)
    indent = '' if message.startswith("Starting '") else '    '
    text = now + '  ' + indent + message
    if not _quiet_mode or force:
        print(text)
    if not os.path.exists(LOG_DIRECTORY):
        os.makedirs(LOG_DIRECTORY)
    with open(LOG_PATH, 'a') as log_file:
        log_file.write(text + '\n')


def _get_remote_last_modified(url):
    """Return the Last-Modified header string for *url*, or None on failure."""
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.headers.get('Last-Modified')
    except Exception as e:
        log('  Warning: could not check remote file date (%s); will download.' % e)
        return None


def _read_local_last_modified():
    try:
        with open(LAST_MODIFIED_PATH) as f:
            return f.read().strip()
    except OSError:
        return None


def _save_local_last_modified(value):
    try:
        with open(LAST_MODIFIED_PATH, 'w') as f:
            f.write(value)
    except OSError as e:
        log('  Warning: could not save last-modified stamp (%s).' % e)


def load_stat_cache():
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('Cache root must be a JSON object')
        return data
    except Exception as e:
        log('  Warning: stat cache unreadable (%s); starting fresh.' % e)
        return {}


def save_stat_cache(cache, quiet=False):
    tmp = CACHE_PATH + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(cache, f, separators=(',', ':'))
        os.replace(tmp, CACHE_PATH)
        if not quiet:
            size_kb = os.path.getsize(CACHE_PATH) / 1024
            log('  Stat cache saved: %s (%.1f KB)' % (CACHE_PATH, size_kb))
    except Exception as e:
        log('  Warning: stat cache could not be saved (%s).' % e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return '%ds' % seconds
    return '%dm %ds' % (seconds // 60, seconds % 60)


def _set_m2m_if_changed(m2m_manager, new_objects, is_new):
    """Update an M2M relation only when the set of related objects has changed.

    For new books every relation is new — just add without comparing.
    For existing books, compare current PKs (from the prefetch cache) against
    the incoming PKs; skip the DELETE+INSERT entirely when they are equal.
    Handles the empty-list case: clears all relations if new_objects is empty
    and the current set is non-empty.
    """
    if is_new:
        if new_objects:
            m2m_manager.set(new_objects, clear=False)
        return
    new_pks = {obj.pk for obj in new_objects}
    current_pks = {obj.pk for obj in m2m_manager.all()}  # uses prefetch cache
    if new_pks != current_pks:
        m2m_manager.set(new_objects, clear=True)

def invalidate_format_time_cache(stat_cache):
    """Force books with missing format timestamps to be reprocessed."""
    book_ids = list(
        Book.objects.filter(
            format__modified__isnull=True
        ).values_list('gutenberg_id', flat=True).distinct()
    )

    if not book_ids:
        return stat_cache

    invalidated = 0

    for book_id in book_ids:
        directory = str(book_id)

        if directory in stat_cache:
            del stat_cache[directory]
            invalidated += 1

    log('Detected %d RDF stat-cache entries with missing format timestamps.' % invalidated)
    log('Those entries will be force-updated.')
    return stat_cache

def put_catalog_in_db(stat_cache, limit=None):
    book_ids = []
    with os.scandir(settings.CATALOG_RDF_DIR) as it:
        for entry in it:
            if entry.is_dir():
                try:
                    book_ids.append(int(entry.name))
                except ValueError:
                    pass
    book_ids.sort()
    if limit is not None:
        book_ids = book_ids[:limit]
    book_directories = [str(id) for id in book_ids]

    # DB diagnostics — logged before the loop so problems are visible up front.
    db_books      = Book.objects.count()
    db_persons    = Person.objects.count()
    db_bookshelves = Bookshelf.objects.count()
    db_languages  = Language.objects.count()
    db_subjects   = Subject.objects.count()
    db_formats    = Format.objects.count()
    db_summaries  = Summary.objects.count()
    log('  DB before run:  books=%d  persons=%d  bookshelves=%d  '
        'languages=%d  subjects=%d  formats=%d  summaries=%d' % (
            db_books, db_persons, db_bookshelves,
            db_languages, db_subjects, db_formats, db_summaries))
    log('  RDF files to scan: %d' % len(book_directories))

    # Defer WAL flushes for this session — commits remain atomic but Postgres
    # won't fsync on every commit, giving a significant write speedup.
    # Worst case on a crash: last ~200ms of commits are lost, but the stat
    # cache checkpoint handles recovery.
    with connection.cursor() as cur:
        cur.execute("SET synchronous_commit = off")

    # Pre-load lookup tables into memory to avoid per-book DB round-trips.
    bookshelf_cache = {b.name: b for b in Bookshelf.objects.all()}
    language_cache  = {l.code: l for l in Language.objects.all()}
    subject_cache   = {s.name: s for s in Subject.objects.all()}
    person_cache    = {p.gutenberg_id: p for p in Person.objects.filter(gutenberg_id__isnull=False)}
    log('  Caches loaded:  bookshelves=%d  languages=%d  subjects=%d  persons=%d' % (
        len(bookshelf_cache), len(language_cache), len(subject_cache), len(person_cache)))

    skipped = 0
    processed = 0
    total_start = time()
    batch_start = time()

    _BOOK_UPDATE_FIELDS = [
        'copyright', 'download_count', 'media_type', 'title',
        'published_year', 'issued_date', 'gt_modified', 'wikipedia_url',
        'reading_score', 'reading_score_value', 'related_gt_books',
    ]
    _BATCH_SIZE = 200

    # Collect books to process in batches so we can bulk-fetch existing Book
    # rows with one SELECT per batch instead of one per book.
    pending = []   # list of (directory, id, book_path, st)

    def flush_pending():
        nonlocal processed, batch_start

        if not pending:
            return

        # ── Batch SELECTs ─────────────────────────────────────────────────────
        # Load books with M2M prefetch so phase-2 PK comparisons use the cache
        # instead of hitting the DB per book.  Formats and summaries are loaded
        # separately (they are FK relations, not M2M through-tables).
        batch_ids = [p[1] for p in pending]

        books_in_db = {
            b.gutenberg_id: b
            for b in Book.objects.filter(gutenberg_id__in=batch_ids)
                .prefetch_related('authors', 'editors', 'translators',
                                  'bookshelves', 'languages', 'subjects')
        }
        existing_book_ids = list(books_in_db.keys())
        batch_formats = {}
        batch_summaries = {}
        if existing_book_ids:
            for f in Format.objects.filter(book__gutenberg_id__in=existing_book_ids):
                batch_formats.setdefault(f.book_id, []).append(f)
            for s in Summary.objects.filter(book__gutenberg_id__in=existing_book_ids):
                batch_summaries.setdefault(s.book_id, []).append(s)

        # ── Phase 1: parse RDF files; prepare bulk write lists ────────────────
        # Assign new field values to existing Book objects (for bulk_update) and
        # build unsaved Book objects for new entries (for bulk_create).
        # Parsing happens here — outside any transaction — to keep transactions
        # as short as possible.
        book_records    = []  # (directory, id, st, book_dict, is_new)
        books_to_update = []  # existing Book objects with updated field values
        books_to_create = []  # unsaved Book objects for new entries

        for directory, id, book_path, st in pending:
            book = utils.get_book(id, book_path)
            related_books_str = ','.join(str(i) for i in book['related_gt_books'])
            book_in_db = books_in_db.get(id)
            is_new = book_in_db is None

            if not is_new:
                book_in_db.copyright           = book['copyright']
                book_in_db.download_count       = book['downloads']
                book_in_db.media_type           = book['type']
                book_in_db.title                = book['title']
                book_in_db.published_year       = book['published_year']
                book_in_db.issued_date          = book['issued_date']
                book_in_db.gt_modified          = book['gt_modified']
                book_in_db.wikipedia_url        = book['wikipedia_url']
                book_in_db.reading_score        = book['reading_score']
                book_in_db.reading_score_value  = book['reading_score_value']
                book_in_db.related_gt_books     = related_books_str
                books_to_update.append(book_in_db)
            else:
                books_to_create.append(Book(
                    gutenberg_id=id,
                    copyright=book['copyright'],
                    download_count=book['downloads'],
                    media_type=book['type'],
                    title=book['title'],
                    published_year=book['published_year'],
                    issued_date=book['issued_date'],
                    gt_modified=book['gt_modified'],
                    wikipedia_url=book['wikipedia_url'],
                    reading_score=book['reading_score'],
                    reading_score_value=book['reading_score_value'],
                    related_gt_books=related_books_str,
                ))

            book_records.append((directory, id, st, book, is_new))

        # One UPDATE for all changed existing books; one INSERT for all new books.
        if books_to_update:
            Book.objects.bulk_update(books_to_update, _BOOK_UPDATE_FIELDS)
        if books_to_create:
            for b in Book.objects.bulk_create(books_to_create):
                books_in_db[b.gutenberg_id] = b

        # ── Phase 2: per-book M2M, formats, summaries ─────────────────────────
        for directory, id, st, book, is_new in book_records:
            book_in_db = books_in_db[id]

            try:
                with transaction.atomic():
                    ''' Make/update the authors. '''
                    _set_m2m_if_changed(
                        book_in_db.authors,
                        [get_or_create_person(a, person_cache) for a in book['authors']],
                        is_new,
                    )

                    ''' Make/update the editors. '''
                    _set_m2m_if_changed(
                        book_in_db.editors,
                        [get_or_create_person(e, person_cache) for e in book['editors']],
                        is_new,
                    )

                    ''' Make/update the translators. '''
                    _set_m2m_if_changed(
                        book_in_db.translators,
                        [get_or_create_person(t, person_cache) for t in book['translators']],
                        is_new,
                    )

                    ''' Make/update the book shelves. '''
                    bookshelves = []
                    for shelf in book['bookshelves']:
                        if shelf not in bookshelf_cache:
                            bookshelf_cache[shelf] = Bookshelf.objects.create(name=shelf)
                        bookshelves.append(bookshelf_cache[shelf])
                    _set_m2m_if_changed(book_in_db.bookshelves, bookshelves, is_new)

                    ''' Make/update the formats. '''
                    if is_new:
                        Format.objects.bulk_create([
                            Format(
                                book=book_in_db,
                                mime_type=mt,
                                url=url,
                                modified=(
                                    datetime.fromisoformat(book['format_times'][mt]).replace(tzinfo=timezone.utc)
                                    if book['format_times'].get(mt)
                                    else None
                                ),
                            ) 
                            for mt, url in book['formats'].items()
                        ])
                    else:
                        existing_formats = {
                            (f.mime_type, f.url): f
                            for f in batch_formats.get(book_in_db.pk, [])
                        }
                        keep_ids = set()
                        to_create = []
                        formats_to_update = []

                        for mime_type, url in book['formats'].items():
                            key = (mime_type, url)
                            format_time = book['format_times'].get(mime_type)

                            if key in existing_formats:
                                format_in_db = existing_formats[key]
                                format_in_db.modified = (
                                    datetime.fromisoformat(format_time).replace(tzinfo=timezone.utc)
                                    if format_time
                                    else None
                                )
                                formats_to_update.append(format_in_db)
                                keep_ids.add(format_in_db.id)
                            else:
                                to_create.append(
                                    Format(
                                        book=book_in_db,
                                        mime_type=mime_type,
                                        url=url,
                                        modified=(
                                            datetime.fromisoformat(format_time).replace(tzinfo=timezone.utc)
                                            if format_time
                                            else None
                                        ),
                                    )
                                )

                        if formats_to_update:
                            Format.objects.bulk_update(formats_to_update, ['modified'])

                        if to_create:
                            Format.objects.bulk_create(to_create)

                        stale_ids = {
                            f.id for f in existing_formats.values()
                        } - keep_ids

                        if stale_ids:
                            Format.objects.filter(id__in=stale_ids).delete()

                    ''' Make/update the languages. '''
                    languages = []
                    for language in book['languages']:
                        if language not in language_cache:
                            language_cache[language] = Language.objects.create(code=language)
                        languages.append(language_cache[language])
                    _set_m2m_if_changed(book_in_db.languages, languages, is_new)

                    ''' Make/update subjects. '''
                    subjects = []
                    for subject in book['subjects']:
                        if subject not in subject_cache:
                            subject_cache[subject] = Subject.objects.create(name=subject)
                        subjects.append(subject_cache[subject])
                    _set_m2m_if_changed(book_in_db.subjects, subjects, is_new)

                    ''' Make/update summaries. '''
                    if is_new:
                        Summary.objects.bulk_create([
                            Summary(book=book_in_db, text=text)
                            for text in book['summaries']
                        ])
                    else:
                        existing_summaries = {
                            s.text: s for s in batch_summaries.get(book_in_db.pk, [])
                        }
                        new_texts = set(book['summaries'])
                        to_create = [
                            Summary(book=book_in_db, text=text)
                            for text in new_texts
                            if text not in existing_summaries
                        ]
                        if to_create:
                            Summary.objects.bulk_create(to_create)
                        stale_ids = {
                            s.id for text, s in existing_summaries.items()
                            if text not in new_texts
                        }
                        if stale_ids:
                            Summary.objects.filter(id__in=stale_ids).delete()

            except Exception as error:
                book_json = json.dumps(book, indent=4)
                log(
                    '  Error while putting this book info in the database:\n',
                    book_json,
                    '\n'
                )
                raise error

            stat_cache[directory] = [st.st_mtime_ns, st.st_size]
            processed += 1

            if processed % 500 == 0:
                elapsed = time() - batch_start
                log('  [processed=%d  id=%d]  skipped=%d  (%.1fs)' % (
                    processed, id, skipped, elapsed))
                batch_start = time()
                save_stat_cache(stat_cache, quiet=True)

        pending.clear()

    for directory in book_directories:
        id = int(directory)

        book_path = os.path.join(
            settings.CATALOG_RDF_DIR,
            directory,
            'pg' + directory + '.rdf'
        )

        st = os.stat(book_path)
        cached = stat_cache.get(directory)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            skipped += 1
            continue

        pending.append((directory, id, book_path, st))

        if len(pending) >= _BATCH_SIZE:
            flush_pending()

    flush_pending()  # process any remaining books

    total_elapsed = _fmt_duration(time() - total_start)
    log('  Skipped (unchanged): %d  Processed: %d  Total: %s' % (
        skipped, processed, total_elapsed))

    # DB diagnostics after the run — compare with the before numbers to spot
    # unexpected growth or missing data.
    log('  DB after run:   books=%d  persons=%d  bookshelves=%d  '
        'languages=%d  subjects=%d  formats=%d  summaries=%d' % (
            Book.objects.count(), Person.objects.count(),
            Bookshelf.objects.count(), Language.objects.count(),
            Subject.objects.count(), Format.objects.count(),
            Summary.objects.count()))

    # Refresh PostgreSQL query planner statistics after a large import so that
    # subsequent API queries use accurate cost estimates.
    if processed > 0:
        log('  Running VACUUM ANALYZE on affected tables...')
        _tables = [
            'books_book', 'books_format', 'books_summary',
            'books_book_authors', 'books_book_editors', 'books_book_translators',
            'books_book_bookshelves', 'books_book_languages', 'books_book_subjects',
        ]
        prev_autocommit = connection.autocommit
        connection.set_autocommit(True)
        try:
            with connection.cursor() as cur:
                for table in _tables:
                    cur.execute('VACUUM ANALYZE %s' % table)
        finally:
            connection.set_autocommit(prev_autocommit)
        log('  VACUUM ANALYZE done.')

    return stat_cache, {str(id) for id in book_ids}, processed, skipped


def get_or_create_person(data, cache):
    gid = data.get('gutenberg_id')
    if gid:
        if gid in cache:
            return cache[gid]
        person = Person.objects.filter(gutenberg_id=gid).first()
        if person:
            # Only UPDATE if something actually changed — person data is stable
            # across catalog refreshes so this saves a query in most cases.
            if (person.name != data['name']
                    or person.birth_year != data['birth']
                    or person.death_year != data['death']):
                Person.objects.filter(pk=person.pk).update(
                    name=data['name'],
                    birth_year=data['birth'],
                    death_year=data['death'],
                )
        else:
            person = Person.objects.create(
                gutenberg_id=gid,
                name=data['name'],
                birth_year=data['birth'],
                death_year=data['death'],
            )
        cache[gid] = person
        return person
    # Fallback: no agent ID in RDF (rare) — not cached since there's no stable key
    person, _ = Person.objects.get_or_create(
        name=data['name'],
        birth_year=data['birth'],
        death_year=data['death'],
    )
    return person


def send_log_email():
    if not (settings.ADMIN_EMAILS or settings.EMAIL_HOST_ADDRESS):
        return

    log_text = ''
    with open(LOG_PATH, 'r') as log_file:
        log_text = log_file.read()

    email_html = '''
        <h1 style="color: #333;
                   font-family: 'Helvetica Neue', sans-serif;
                   font-size: 64px;
                   font-weight: 100;
                   text-align: center;">
            Gutendex
        </h1>

        <p style="color: #333;
                  font-family: 'Helvetica Neue', sans-serif;
                  font-size: 24px;
                  font-weight: 200;">
            Here is the log from your catalog retrieval:
        </p>

        <pre style="color:#333;
                    font-family: monospace;
                    font-size: 16px;
                    margin-left: 32px">''' + log_text + '</pre>'

    email_text = '''GUTENDEX

    Here is the log from your catalog retrieval:

    ''' + log_text

    send_mail(
        subject='Catalog retrieval',
        message=email_text,
        html_message=email_html,
        from_email=settings.EMAIL_HOST_ADDRESS,
        recipient_list=settings.ADMIN_EMAILS
    )


def prime_rdf_cache():
    if not os.path.exists(settings.CATALOG_RDF_DIR):
        log('  RDF directory does not exist; nothing to prime.')
        return
    cache = {}
    count = 0
    with os.scandir(settings.CATALOG_RDF_DIR) as it:
        for entry in it:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                int(entry.name)
            except ValueError:
                continue
            rdf_path = os.path.join(entry.path, 'pg' + entry.name + '.rdf')
            try:
                st = os.stat(rdf_path)
            except OSError:
                continue
            cache[entry.name] = [st.st_mtime_ns, st.st_size]
            count += 1
    save_stat_cache(cache)
    log('  Primed cache with %d files.' % count)


def _extracted_size_mb(path):
    """Return the total size of all files under *path* in MB."""
    try:
        import platform
        if platform.system() == 'Darwin':
            out = subprocess.check_output(['du', '-sk', path], stderr=subprocess.DEVNULL)
            return int(out.split()[0]) * 1024 / 1_048_576
        else:
            out = subprocess.check_output(['du', '-sb', path], stderr=subprocess.DEVNULL)
            return int(out.split()[0]) / 1_048_576
    except Exception:
        return sum(
            os.path.getsize(os.path.join(dirpath, fname))
            for dirpath, _, fnames in os.walk(path)
            for fname in fnames
        ) / 1_048_576


def _decompress_with_progress(src, dest_dir, quiet=False):
    """Extract *src* (a .tar.bz2 file) to *dest_dir*, showing an approximate
    progress bar by counting extracted subdirectories every 2 seconds.
    Returns (actual_dirs, size_mb)."""
    extract_target = MOVE_SOURCE_PATH  # cache/epub/ inside dest_dir

    with open(os.devnull, 'w') as null:
        proc = Popen(['tar', 'xjf', src, '-C', dest_dir], stdout=null, stderr=null)

    if not quiet:
        expected = Book.objects.count() or 70_000

        def _monitor():
            while proc.poll() is None:
                try:
                    count = len(os.listdir(extract_target))
                except FileNotFoundError:
                    count = 0
                pct = min(count / expected * 100, 100)
                filled = int(40 * pct / 100)
                bar = '█' * filled + '░' * (40 - filled)
                sys.stdout.write(
                    f'\r          [{bar}] {pct:4.0f}%  ~{count:,} / {expected:,} dirs'
                )
                sys.stdout.flush()
                sleep(2)

        t = threading.Thread(target=_monitor, daemon=True)
        t.start()

    proc.wait()

    if not quiet:
        t.join()

    try:
        actual = len(os.listdir(extract_target))
    except FileNotFoundError:
        actual = 0

    size_mb = _extracted_size_mb(extract_target)

    if not quiet:
        bar = '█' * 40
        sys.stdout.write(f'\r          [{bar}] 100%  {actual:,} dirs  {size_mb:,.0f} MB               \n')
        sys.stdout.flush()

    return actual, size_mb


def _download_with_progress(url, dest, quiet=False):
    """Download *url* to *dest*, printing a live progress bar to stdout."""
    if quiet:
        urllib.request.urlretrieve(url, dest)
        return

    def reporthook(block_count, block_size, total_size):
        downloaded = block_count * block_size
        if total_size > 0:
            pct = min(downloaded / total_size * 100, 100)
            filled = int(40 * pct / 100)
            bar = '█' * filled + '░' * (40 - filled)
            mb_done = downloaded / 1_048_576
            mb_total = total_size / 1_048_576
            sys.stdout.write(
                f'\r          [{bar}] {pct:5.1f}%  {mb_done:.0f}/{mb_total:.0f} MB'
            )
        else:
            sys.stdout.write(f'\r          {block_count * block_size / 1_048_576:.0f} MB downloaded...')
        sys.stdout.flush()

    urllib.request.urlretrieve(url, dest, reporthook=reporthook)
    sys.stdout.write('\n')
    sys.stdout.flush()


class Command(BaseCommand):
    help = 'This replaces the catalog files with the latest ones.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--prime-rdf-cache',
            action='store_true',
            help=(
                'Scan the existing RDF directory, record each file\'s mtime '
                'and size into the stat cache, then exit. Use this after '
                'upgrading from a previous version of gutendex to mark all '
                'current files as already imported, so the next updatecatalog '
                'run only processes new or changed files.'
            ),
        )
        parser.add_argument(
            '--force-download',
            action='store_true',
            help=(
                'Skip the Last-Modified check and always download and '
                'decompress the RDF archive, even if it appears unchanged.'
            ),
        )
        parser.add_argument(
            '--minimal-log',
            action='store_true',
            help=(
                'Suppress verbose output; print only the start line, '
                'a download/decompress summary, and a catalog update summary '
                'with total elapsed time. Errors are always shown.'
            ),
        )

    def handle(self, *args, **options):
        global _quiet_mode
        start_time = time()

        if options['prime_rdf_cache']:
            log('Priming RDF stat cache...')
            prime_rdf_cache()
            log('Done! (total %s)' % _fmt_duration(time() - start_time))
            return

        force = options['force_download']
        minimal_log = options['minimal_log']
        quiet_bars = options.get('quiet_bars', False)

        try:
            date_and_time = strftime('%H:%M:%S on %B %d, %Y')
            cmd_str = ' '.join(sys.argv[1:])
            log("Starting '%s' at %s" % (cmd_str, date_and_time))
            _quiet_mode = minimal_log

            log('Making temporary directory...')
            if os.path.exists(TEMP_PATH):
                raise CommandError(
                    'The temporary path, `' + TEMP_PATH + '`, already exists.'
                )
            else:
                os.makedirs(TEMP_PATH)

            log('Checking remote catalog date...')
            remote_lm = _get_remote_last_modified(URL)
            local_lm = _read_local_last_modified()
            if not force and remote_lm and remote_lm == local_lm:
                log('Catalog unchanged (Last-Modified: %s); skipping download.' % remote_lm)
                shutil.rmtree(TEMP_PATH)
                if minimal_log:
                    elapsed = _fmt_duration(time() - start_time)
                    log('Catalog unchanged (Last-Modified: %s) — total %s' % (
                        remote_lm, elapsed), force=True)
                else:
                    log('Done! (total %s)' % _fmt_duration(time() - start_time))
                send_log_email()
                return
            if remote_lm:
                log('  Remote Last-Modified: %s%s' % (remote_lm, ' (forced)' if force else ''))
            log('Downloading compressed catalog...')
            _download_with_progress(URL, DOWNLOAD_PATH, quiet=minimal_log or quiet_bars)
            if remote_lm:
                _save_local_last_modified(remote_lm)

            log('Decompressing catalog...')
            actual_dirs, size_mb = _decompress_with_progress(DOWNLOAD_PATH, TEMP_PATH, quiet=minimal_log or quiet_bars)
            if minimal_log:
                log('Archive downloaded and decompressed (%s dirs extracted, %s MB)' % (
                    f'{actual_dirs:,}', f'{size_mb:,.0f}'), force=True)

            log('Detecting stale directories...')
            if not os.path.exists(MOVE_TARGET_PATH):
                os.makedirs(MOVE_TARGET_PATH)
            new_directory_set = get_directory_set(MOVE_SOURCE_PATH)
            old_directory_set = get_directory_set(MOVE_TARGET_PATH)
            stale_directory_set = old_directory_set - new_directory_set

            log('Removing stale directories and books...')
            for directory in stale_directory_set:
                try:
                    book_id = int(directory)
                except ValueError:
                    # Ignore the directory if its name isn't a book ID number.
                    continue
                book = Book.objects.filter(gutenberg_id=book_id)
                book.delete()
                path = os.path.join(MOVE_TARGET_PATH, directory)
                shutil.rmtree(path)

            log('Replacing old catalog files...')
            with open(os.devnull, 'w') as null:
                with open(LOG_PATH, 'a') as log_file:
                    call(
                        [
                            'rsync',
                            '-va',
                            '--delete-after',
                            MOVE_SOURCE_PATH + '/',
                            MOVE_TARGET_PATH
                        ],
                        stdout=null,
                        stderr=log_file
                    )
            
            log('Putting the catalog in the database...')
            stat_cache = load_stat_cache()

            stat_cache = invalidate_format_time_cache(stat_cache)

            stat_cache, seen_ids, processed, skipped = put_catalog_in_db(stat_cache)
            save_stat_cache(stat_cache)

            log('Removing temporary files...')
            shutil.rmtree(TEMP_PATH)

            if minimal_log:
                elapsed = _fmt_duration(time() - start_time)
                log('Catalog updated: %s books processed, %s skipped — total %s' % (
                    f'{processed:,}', f'{skipped:,}', elapsed), force=True)
            else:
                log('Done! (total %s)\n' % _fmt_duration(time() - start_time))
        except Exception as error:
            error_message = str(error)
            log('Error:', error_message, force=True)
            log('')
            shutil.rmtree(TEMP_PATH)
        finally:
            _quiet_mode = False

        send_log_email()

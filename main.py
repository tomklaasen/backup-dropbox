"""Back up the contents of your Dropbox to a local folder.

The first run (and periodically afterwards) lists the whole Dropbox tree
recursively.  The cursor returned by that listing is stored, so later runs
only ask Dropbox for what changed since the previous run.

Local files are never thrown away: when a file changes or is deleted in
Dropbox, the previous local copy is moved to the archive directory.
"""

import argparse
import configparser
import contextlib
import datetime
import hashlib
import json
import logging
import logging.config
import os
import shutil
import sys
import time

import dropbox
import requests
import yaml


BLOCK_SIZE = 4 * 1024 * 1024  # 4 MB
MAX_RETRIES = 3
RETRY_BACKOFF = [5, 30, 120]  # seconds between retries
MTIME_TOLERANCE = 2  # seconds; FAT/exFAT drives store mtimes with 2 s resolution
PART_SUFFIX = '.dropbox-backup-part'
STATE_FILE = 'state.json'

TRANSIENT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    ConnectionError,
    TimeoutError,
    dropbox.exceptions.RateLimitError,
    dropbox.exceptions.InternalServerError,
)


def with_retry(func, *args, **kwargs):
    """Call func with retries on transient network and server errors."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except TRANSIENT_ERRORS as e:
            if attempt >= MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF[attempt]
            if isinstance(e, dropbox.exceptions.RateLimitError) and e.backoff:
                wait = max(wait, e.backoff)
            logging.warning("Transient error (attempt %d/%d), retrying in %ds: %s",
                            attempt + 1, MAX_RETRIES, wait, e)
            time.sleep(wait)


parser = argparse.ArgumentParser(description='Back up Dropbox content to a local folder')
parser.add_argument('folder', nargs='?', default='/',
                    help='Folder name in your Dropbox. Default is /')
parser.add_argument('--full', action='store_true',
                    help='List the whole Dropbox tree instead of only the changes since the last run')
parser.add_argument('--verify', action='store_true',
                    help='Full scan that also recomputes the content hash of every local file')


def compute_content_hash(path):
    """Compute the Dropbox content hash for a local file.

    Algorithm: split file into 4 MB blocks, SHA-256 each block,
    then SHA-256 the concatenation of all block hashes.
    """
    block_hashes = b''
    with open(path, 'rb') as f:
        while True:
            block = f.read(BLOCK_SIZE)
            if not block:
                break
            block_hashes += hashlib.sha256(block).digest()
    return hashlib.sha256(block_hashes).hexdigest()


def server_mtime(md):
    """Return the Dropbox server_modified time of md as a POSIX timestamp."""
    return md.server_modified.replace(tzinfo=datetime.timezone.utc).timestamp()


def find_entry(parent, name):
    """Return the local path of name in parent, matching case-insensitively, or None."""
    candidate = os.path.join(parent, name)
    if os.path.lexists(candidate):
        return candidate
    lower = name.lower()
    try:
        for entry in os.listdir(parent):
            if entry.lower() == lower:
                return os.path.join(parent, entry)
    except FileNotFoundError:
        pass
    return None


class Backup:
    """Apply a Dropbox listing (full or incremental) to the local backup."""

    def __init__(self, dbx, rootdir, archive_dir, verify=False):
        self.dbx = dbx
        self.rootdir = rootdir
        self.archive_dir = archive_dir
        self.verify = verify
        self.local_dirs = {'': rootdir}  # lowercased Dropbox folder path -> local directory
        self.stats = dict.fromkeys(['entries', 'files_unchanged', 'files_downloaded',
                                    'files_skipped', 'archived_replaced', 'archived_deleted'], 0)
        self.errors = []

    def run(self, path, cursor=None):
        """Process a recursive listing of path, or the changes since cursor.

        Returns the cursor to use for the next run.
        """
        with stopwatch('listing'):
            if cursor:
                res = with_retry(self.dbx.files_list_folder_continue, cursor)
            else:
                res = with_retry(self.dbx.files_list_folder, path, recursive=True)
            while True:
                for md in res.entries:
                    self.process(md)
                if not res.has_more:
                    return res.cursor
                res = with_retry(self.dbx.files_list_folder_continue, res.cursor)

    def process(self, md):
        self.stats['entries'] += 1
        logging.debug("Entry: %s", md.path_display)
        try:
            if isinstance(md, dropbox.files.FileMetadata):
                self.process_file(md)
            elif isinstance(md, dropbox.files.FolderMetadata):
                self.local_dir(md.path_lower, md.path_display)
            elif isinstance(md, dropbox.files.DeletedMetadata):
                self.process_deleted(md)
            else:
                logging.error("Didn't expect this Metadata class: %s", md)
                self.errors.append("%s: unexpected metadata class %s" % (md.path_display, type(md).__name__))
        except Exception:
            logging.exception("While handling %s :", md.path_display)
            self.errors.append(md.path_display)

    def local_dir(self, path_lower, path_display, create=True):
        """Return the local directory for a Dropbox folder.

        Dropbox paths are case-insensitive, and only the last component of
        path_display is guaranteed to be correctly cased, so an existing
        local directory is reused whatever its casing.  Returns None when
        the directory doesn't exist and create is False.
        """
        if path_lower in self.local_dirs:
            return self.local_dirs[path_lower]
        parent_lower = path_lower.rsplit('/', 1)[0]
        parent_display, name = path_display.rsplit('/', 1)
        parent = self.local_dir(parent_lower, parent_display, create)
        if parent is None:
            return None
        local = find_entry(parent, name)
        if local is not None and not os.path.isdir(local):
            if not create:
                return None
            self.archive(local, 'replaced')
            local = None
        if local is None:
            if not create:
                return None
            local = os.path.join(parent, name)
            os.mkdir(local)
        self.local_dirs[path_lower] = local
        return local

    def process_file(self, md):
        if md.symlink_info:
            logging.debug("This is a symlink; skipping")
            return
        if not md.is_downloadable:
            logging.info("Not downloadable (e.g. Paper or Google doc); skipping %s", md.path_display)
            self.stats['files_skipped'] += 1
            return
        parent = self.local_dir(md.path_lower.rsplit('/', 1)[0], md.path_display.rsplit('/', 1)[0])
        target = os.path.join(parent, md.name)
        if os.path.isdir(target):
            self.archive(target, 'replaced')
        elif os.path.exists(target) and self.is_current(target, md):
            self.stats['files_unchanged'] += 1
            return
        self.download(md, target)

    def is_current(self, target, md):
        """Check whether the local file at target matches md.

        Size and mtime are trusted unless verifying; the content hash is
        only computed when they disagree.  When the hash matches, the
        local mtime is aligned with Dropbox so the next run can skip it.
        """
        st = os.stat(target)
        if st.st_size != md.size:
            return False
        mtime = server_mtime(md)
        mtime_matches = abs(st.st_mtime - mtime) <= MTIME_TOLERANCE
        if mtime_matches and not self.verify:
            return True
        if compute_content_hash(target) != md.content_hash:
            if mtime_matches:
                logging.warning("Local copy of %s is corrupt (size and mtime match, content doesn't)",
                                md.path_display)
            return False
        if not mtime_matches:
            os.utime(target, (mtime, mtime))
        return True

    def download(self, md, target):
        """Download md next to target, verify it, then swap it into place."""
        part = target + PART_SUFFIX
        logging.debug("Downloading %s", md.path_display)
        try:
            with_retry(self.dbx.files_download_to_file, part, md.path_lower, rev=md.rev)
            if compute_content_hash(part) != md.content_hash:
                raise IOError("Content hash mismatch after downloading %s" % md.path_display)
            if os.path.exists(target):
                self.archive(target, 'replaced')
            os.replace(part, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(part)
        mtime = server_mtime(md)
        os.utime(target, (mtime, mtime))
        self.stats['files_downloaded'] += 1

    def process_deleted(self, md):
        parent = self.local_dir(md.path_lower.rsplit('/', 1)[0], md.path_display.rsplit('/', 1)[0],
                                create=False)
        if parent is None:
            return
        target = find_entry(parent, md.name)
        if target is None:
            return
        self.archive(target, 'deleted')
        for key in [k for k in self.local_dirs if k == md.path_lower or k.startswith(md.path_lower + '/')]:
            del self.local_dirs[key]

    def archive(self, local_path, reason):
        """Move a local file or directory out of the backup into the archive."""
        rel = os.path.relpath(local_path, self.rootdir)
        dest = os.path.join(self.archive_dir, reason, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        candidate, n = dest, 1
        while os.path.lexists(candidate):
            candidate = '%s.%d' % (dest, n)
            n += 1
        logging.info("Archiving %s (%s) to %s", rel, reason, candidate)
        shutil.move(local_path, candidate)
        self.stats['archived_' + reason] += 1


def is_cursor_reset(err):
    return isinstance(err.error, dropbox.files.ListFolderContinueError) and err.error.is_reset()


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(path, state):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def main():
    """Main program.

    Parse command line and configuration, decide between a full and an
    incremental run, apply the listing to the local backup and store the
    new cursor.  The cursor is only advanced when every entry succeeded,
    so failed entries are retried on the next run.
    """
    with open('logging.conf', 'r') as f:
        log_cfg = yaml.safe_load(f.read())
        logging.config.dictConfig(log_cfg)

    backup = None
    try:
        args = parser.parse_args()
        api_path = '/' + args.folder.strip('/')
        if api_path == '/':
            api_path = ''  # the API calls the root ''
        state_key = api_path or '/'

        config = configparser.ConfigParser()
        config.read("config.ini")
        app_key = config.get("dropbox", "app_key")
        secret = config.get("dropbox", "secret")
        refresh_token = config.get("dropbox", "refresh_token")
        rootdir = config.get("backup", "localdirectory")
        state_dir = config.get("backup", "statedirectory",
                               fallback=os.path.join(rootdir, '.dropbox-backup-state'))
        archive_root = config.get("backup", "archivedirectory",
                                  fallback=os.path.join(rootdir, '.dropbox-backup-archive'))
        full_scan_days = config.getint("backup", "fullscandays", fallback=7)

        logging.info('Dropbox folder name: %s', state_key)
        logging.info('Local directory: %s', rootdir)
        if not os.path.exists(rootdir):
            # Don't create it: most likely the backup drive isn't mounted, and we'd
            # start downloading the whole Dropbox onto the system disk.
            logging.error('%s does not exist on your filesystem (is the backup drive mounted?)', rootdir)
            sys.exit(1)
        elif not os.path.isdir(rootdir):
            logging.error('%s is not a folder on your filesystem', rootdir)
            sys.exit(1)
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, STATE_FILE)
        state = load_state(state_path)
        folder_state = state.get(state_key, {})

        cursor = folder_state.get('cursor')
        last_full_scan = folder_state.get('last_full_scan', 0)
        full = (args.full or args.verify or not cursor
                or time.time() - last_full_scan > full_scan_days * 86400)

        dbx = dropbox.Dropbox(app_key=app_key, app_secret=secret, oauth2_refresh_token=refresh_token)
        logging.info('Connected!')

        run_stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
        backup = Backup(dbx, rootdir, os.path.join(archive_root, run_stamp), verify=args.verify)

        started = time.time()
        if full:
            logging.info('Full scan%s', ' with verification' if args.verify else '')
            new_cursor = backup.run(api_path)
        else:
            logging.info('Incremental run since last cursor')
            try:
                new_cursor = backup.run(api_path, cursor)
            except dropbox.exceptions.ApiError as err:
                if not is_cursor_reset(err):
                    raise
                logging.warning('Cursor expired, falling back to a full scan')
                full = True
                new_cursor = backup.run(api_path)

        if backup.errors:
            logging.warning('Not advancing the cursor because of errors; these changes will be retried next run')
        else:
            state[state_key] = {
                'cursor': new_cursor,
                'last_full_scan': started if full else last_full_scan,
            }
            save_state(state_path, state)

    except Exception:
        logging.exception("Backup failed:")
        if backup:
            logging.error("Stats: %s", backup.stats)
        sys.exit(1)

    logging.info("Backup complete: %s", ', '.join('%s = %i' % kv for kv in backup.stats.items()))

    if backup.errors:
        logging.error("Failed entries (%d):", len(backup.errors))
        for err in backup.errors:
            logging.error("  %s", err)
        sys.exit(1)


@contextlib.contextmanager
def stopwatch(message):
    """Context manager to print how long a block of code took."""
    t0 = time.time()
    try:
        yield
    finally:
        t1 = time.time()
        logging.debug('Total elapsed time for %s: %.3f' % (message, t1 - t0))


if __name__ == '__main__':
    main()

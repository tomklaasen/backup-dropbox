"""Sync the contents of your Dropbox to a local folder."""

import logging
import logging.config
import configparser
import yaml
import argparse
import contextlib
import hashlib
import os
import sys
import time

import dropbox


BLOCK_SIZE = 4 * 1024 * 1024  # 4 MB


parser = argparse.ArgumentParser(description='Sync Dropbox content to a specified folder')
parser.add_argument('folder', nargs='?', default='/',
                    help='Folder name in your Dropbox. Default is /')


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


def main():
    """Main program.

    Parse command line, then iterate over files and directories under
    rootdir and download all files.  Skips symlinks and avoids duplicate
    downloads by comparing Dropbox content_hash with a locally computed
    hash.
    """
    with open('logging.conf', 'r') as f:
        log_cfg = yaml.safe_load(f.read())
        logging.config.dictConfig(log_cfg)

    current_folder = ''
    current_file = ''
    folders_checked = 0
    files_checked = 0
    files_downloaded = 0
    errors = []

    try:
        args = parser.parse_args()
        folder = args.folder

        config = configparser.ConfigParser()
        config.read("config.ini")
        app_key = config.get("dropbox", "app_key")
        secret = config.get("dropbox", "secret")
        refresh_token = config.get("dropbox", "refresh_token")
        rootdir = config.get("backup", "localdirectory")

        logging.info('Dropbox folder name: %s', folder)
        logging.info('Local directory: %s', rootdir)
        if not os.path.exists(rootdir):
            logging.error(rootdir, 'does not exist on your filesystem')
            os.makedirs(rootdir)
        elif not os.path.isdir(rootdir):
            logging.error(rootdir, 'is not a folder on your filesystem')
            sys.exit(1)

        dbx = dropbox.Dropbox(app_key=app_key, app_secret=secret, oauth2_refresh_token=refresh_token)

        logging.info('Connected!')

        folders = [folder]

        while folders:
            current_folder = folders.pop()

            logging.debug("Current folder: %s" % current_folder)

            folders_checked = folders_checked + 1

            listing = list_folder(dbx, current_folder, '')

            for file in listing:
                current_file = file
                logging.debug(file)
                md = listing[file]

                try:
                    if isinstance(md, dropbox.files.FileMetadata):
                        if md.symlink_info:
                            logging.debug("This is a symlink; skipping")
                        else:
                            logging.debug("We have a file!")
                            target = rootdir + os.path.sep + current_folder + os.path.sep + file
                            while '//' in target:
                                target = target.replace('//', '/')
                            source = current_folder + os.path.sep + file
                            while '//' in source:
                                source = source.replace('//', '/')

                            if os.path.exists(target):
                                local_hash = compute_content_hash(target)
                                if md.content_hash == local_hash:
                                    logging.debug("Content hash matches, keeping current version")
                                    files_checked = files_checked + 1
                                else:
                                    logging.debug("Content hash differs, downloading latest version")
                                    logging.debug("Downloading %s" % source)
                                    dbx.files_download_to_file(target, source)
                                    files_downloaded = files_downloaded + 1
                            else:
                                logging.debug("File doesn't exist locally, downloading")
                                logging.debug("Downloading %s" % source)
                                dbx.files_download_to_file(target, source)
                                files_downloaded = files_downloaded + 1

                    elif isinstance(md, dropbox.files.FolderMetadata):
                        logging.debug("We have a folder!")
                        target = rootdir + os.path.sep + current_folder + os.path.sep + file
                        while '//' in target:
                            target = target.replace('//', '/')
                        if not os.path.exists(target):
                            os.makedirs(target)

                        path = current_folder + '/' + file
                        while '//' in path:
                            path = path.replace('//', '/')

                        folders.append(path)
                    else:
                        logging.error("Didn't expect this Metadata class: %s" % md)
                        logging.error(" at %s/%s", current_folder, current_file)
                        errors.append("%s/%s: unexpected metadata class %s" % (current_folder, current_file, type(md).__name__))

                except Exception:
                    logging.exception("While handling %s/%s :", current_folder, current_file)
                    errors.append("%s/%s" % (current_folder, current_file))

    except Exception:
        logging.exception("While handling %s/%s :", current_folder, current_file)
        logging.error("folders_checked = %i; files_checked = %i; files_downloaded = %i", folders_checked, files_checked, files_downloaded)
        sys.exit(1)

    logging.info("Backup complete: folders_checked = %i; files_checked = %i; files_downloaded = %i", folders_checked, files_checked, files_downloaded)

    if errors:
        logging.error("Failed files (%d):", len(errors))
        for err in errors:
            logging.error("  %s", err)
        sys.exit(1)


def list_folder(dbx, folder, subfolder):
    """List a folder.

    Return a dict mapping unicode filenames to
    FileMetadata|FolderMetadata entries.
    """
    rv = {}

    path = '/%s/%s' % (folder, subfolder.replace(os.path.sep, '/'))
    while '//' in path:
        path = path.replace('//', '/')
    path = path.rstrip('/')
    try:
        with stopwatch('list_folder'):
            res = dbx.files_list_folder(path, False)
            for entry in res.entries:
                rv[entry.name] = entry

            while res.has_more:
                logging.info("More entries for folder %s" % path)
                cursor = res.cursor
                res = dbx.files_list_folder_continue(cursor)
                for entry in res.entries:
                    rv[entry.name] = entry

    except dropbox.exceptions.ApiError as err:
        logging.warning('Folder listing failed for %s -- assumed empty: %s', path, err)
        return {}
    else:
        return rv


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

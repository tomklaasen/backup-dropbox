"""Upload the contents of your Downloads folder to Dropbox.

This is an example app for API v2.
"""

from __future__ import print_function

import logging
import logging.config
import ConfigParser
import yaml
import argparse
import contextlib
import datetime
import os
import six
import sys
import time
import unicodedata

if sys.version.startswith('2'):
    input = raw_input  # noqa: E501,F821; pylint: disable=redefined-builtin,undefined-variable,useless-suppression

import dropbox


parser = argparse.ArgumentParser(description='Sync Dropbox content to a specified folder')
parser.add_argument('folder', nargs='?', default='/',
                    help='Folder name in your Dropbox. Default is /')
parser.add_argument('--yes', '-y', action='store_true',
                    help='Answer yes to all questions')
parser.add_argument('--no', '-n', action='store_true',
                    help='Answer no to all questions')
parser.add_argument('--default', '-d', action='store_true',
                    help='Take default answer on all questions')

def main():
    """Main program.

    Parse command line, then iterate over files and directories under
    rootdir and upload all files.  Skips some temporary files and
    directories, and avoids duplicate uploads by comparing size and
    mtime with the server.
    """
    with open('logging.conf', 'r') as f:
        log_cfg = yaml.safe_load(f.read())
        logging.config.dictConfig(log_cfg)

    current_folder = ''
    current_file = ''
    folders_checked = 0
    files_checked = 0
    files_downloaded = 0

    try:
        args = parser.parse_args()
        if sum([bool(b) for b in (args.yes, args.no, args.default)]) > 1:
            logging.error('At most one of --yes, --no, --default is allowed')
            sys.exit(2)

        folder = args.folder

        config = ConfigParser.ConfigParser()
        config.read("config.ini")
        token = config.get("dropbox", "token")
        rootdir = config.get("backup", "localdirectory")

        logging.info('Dropbox folder name: %s', folder)
        logging.info('Local directory: %s', rootdir)
        if not os.path.exists(rootdir):
            logging.error(rootdir, 'does not exist on your filesystem')
            os.makedirs(rootdir)    
        elif not os.path.isdir(rootdir):
            logging.error(rootdir, 'is not a folder on your filesystem')
            sys.exit(1)

        dbx = dropbox.Dropbox(token)

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
                if isinstance(md, dropbox.files.FileMetadata):
                    if md.symlink_info:
                        logging.debug("This is a symlink; skipping")
                    else:
                        logging.debug("We have a file!")
                        target = rootdir + os.path.sep + current_folder + os.path.sep + file
                        while '//' in target:
                            target = target.replace('//', '/')
                        if os.path.exists(target):
                            logging.debug("Server version modified on %s" % md.server_modified)
                            local_modified = datetime.datetime.fromtimestamp(os.path.getmtime(target))
                            logging.debug("Local version modified on %s" % local_modified)
                            local_size = os.stat(target).st_size
                            if (md.server_modified > local_modified) or (md.size != local_size):
                                logging.debug("Dowloading latest version")
                                files_downloaded = files_downloaded + 1
                                source = current_folder + os.path.sep + file
                                while '//' in source:
                                    source = source.replace('//', '/')
                                logging.debug("Downloading %s" % source)
                                dbx.files_download_to_file(target, source)
                                modTime = time.mktime(md.server_modified.timetuple())
                                os.utime(target, (modTime, modTime))
                            else:
                                logging.debug("Keeping current version")
                                if (md.server_modified != local_modified):
                                    logging.debug("updating modification time")
                                    modTime = time.mktime(md.server_modified.timetuple())
                                    os.utime(target, (modTime, modTime))
                                files_checked = files_checked + 1

                        else:
                            logging.debug("File doesn't exist locally, downloading")
                            files_downloaded = files_downloaded + 1
                            source = current_folder + os.path.sep + file
                            while '//' in source:
                                source = source.replace('//', '/')
                            logging.debug("Downloading %s" % source)
                            try: 
                                dbx.files_download_to_file(target, source)
                                modTime = time.mktime(md.server_modified.timetuple())
                                os.utime(target, (modTime, modTime))
                            except Exception:
                                logging.exception("While handling %s/%s :", current_folder, current_file)
                                logging.error("folders_checked = %i; files_checked = %i; files_downloaded = %i", folders_checked, files_checked, files_downloaded)
    
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
                    sys.exit(1)
    except Exception:
        logging.exception("While handling %s/%s :", current_folder, current_file)
        logging.error("folders_checked = %i; files_checked = %i; files_downloaded = %i", folders_checked, files_checked, files_downloaded)


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
        logging.warn('Folder listing failed for %s -- assumed empty: %s', path, err)
        return {}
    else:
        return rv

def download(dbx, folder, subfolder, name):
    """Download a file.

    Return the bytes of the file, or None if it doesn't exist.
    """
    path = '/%s/%s/%s' % (folder, subfolder.replace(os.path.sep, '/'), name)
    while '//' in path:
        path = path.replace('//', '/')
    with stopwatch('download'):
        try:
            md, res = dbx.files_download(path)
        except dropbox.exceptions.HttpError as err:
            logging.error('*** HTTP error', err)
            return None
    data = res.content
    logging.debug(len(data), 'bytes; md:', md)
    return data

def upload(dbx, fullname, folder, subfolder, name, overwrite=False):
    """Upload a file.

    Return the request response, or None in case of error.
    """
    path = '/%s/%s/%s' % (folder, subfolder.replace(os.path.sep, '/'), name)
    while '//' in path:
        path = path.replace('//', '/')
    mode = (dropbox.files.WriteMode.overwrite
            if overwrite
            else dropbox.files.WriteMode.add)
    mtime = os.path.getmtime(fullname)
    with open(fullname, 'rb') as f:
        data = f.read()
    with stopwatch('upload %d bytes' % len(data)):
        try:
            res = dbx.files_upload(
                data, path, mode,
                client_modified=datetime.datetime(*time.gmtime(mtime)[:6]),
                mute=True)
        except dropbox.exceptions.ApiError as err:
            logging.error('*** API error', err)
            return None
    logging.info('uploaded as', res.name.encode('utf8'))
    return res

def yesno(message, default, args):
    """Handy helper function to ask a yes/no question.

    Command line arguments --yes or --no force the answer;
    --default to force the default answer.

    Otherwise a blank line returns the default, and answering
    y/yes or n/no returns True or False.

    Retry on unrecognized answer.

    Special answers:
    - q or quit exits the program
    - p or pdb invokes the debugger
    """
    if args.default:
        print(message + '? [auto]', 'Y' if default else 'N')
        return default
    if args.yes:
        print(message + '? [auto] YES')
        return True
    if args.no:
        print(message + '? [auto] NO')
        return False
    if default:
        message += '? [Y/n] '
    else:
        message += '? [N/y] '
    while True:
        answer = input(message).strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        if answer in ('q', 'quit'):
            print('Exit')
            raise SystemExit(0)
        if answer in ('p', 'pdb'):
            import pdb
            pdb.set_trace()
        print('Please answer YES or NO.')

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

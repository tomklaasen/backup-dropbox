"""Scenario tests for main.py against an in-memory fake Dropbox.

Run with: .venv/bin/python3 test_main.py
"""

import datetime, hashlib, json, os, shutil, sys, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import dropbox
from dropbox.files import (FileMetadata, FolderMetadata, DeletedMetadata, ListFolderResult,
                           ListFolderContinueError)
import main as m

m.RETRY_BACKOFF = [0, 0, 0]


class FakeDbx:
    def __init__(self):
        self.files = {}    # path_lower -> (display, bytes, rev, mtime)
        self.folders = {}  # path_lower -> display
        self.log = []      # list of metadata changes
        self.rev = 0
        self.fail_download = set()
        self.reset = False
        self.downloads = 0

    def md_file(self, low):
        d, data, rev, mt = self.files[low]
        return FileMetadata(name=d.rsplit('/', 1)[1], id='id:' + low, client_modified=mt, server_modified=mt,
                            rev=rev, size=len(data), path_lower=low, path_display=d,
                            content_hash=hash_bytes(data), is_downloadable=True)

    def put(self, display, data, mt=datetime.datetime(2024, 1, 1, 12, 0, 0)):
        parts = display.split('/')
        for i in range(2, len(parts)):
            fd = '/'.join(parts[:i])
            if fd.lower() not in self.folders:
                self.folders[fd.lower()] = fd
                self.log.append(FolderMetadata(name=parts[i - 1], id='id', path_lower=fd.lower(), path_display=fd))
        self.rev += 1
        self.files[display.lower()] = (display, data, '%09x' % self.rev, mt)
        self.log.append(self.md_file(display.lower()))

    def delete(self, display):
        low = display.lower()
        for k in [k for k in self.files if k == low or k.startswith(low + '/')]:
            del self.files[k]
        for k in [k for k in self.folders if k == low or k.startswith(low + '/')]:
            del self.folders[k]
        self.log.append(DeletedMetadata(name=display.rsplit('/', 1)[1], path_lower=low, path_display=display))

    def files_list_folder(self, path, recursive=False):
        assert recursive
        entries = [FolderMetadata(name=d.rsplit('/', 1)[1], id='id', path_lower=k, path_display=d)
                   for k, d in sorted(self.folders.items())]
        entries += [self.md_file(k) for k in sorted(self.files)]
        # paginate in pages of 2 to exercise has_more
        self._pages = [entries[i:i + 2] for i in range(0, len(entries), 2)] or [[]]
        return self._page(0, final=len(self.log))

    def _page(self, i, final):
        has_more = i + 1 < len(self._pages)
        cur = 'p:%d:%d' % (i + 1, final) if has_more else 'c:%d' % final
        return ListFolderResult(entries=self._pages[i], cursor=cur, has_more=has_more)

    def files_list_folder_continue(self, cursor):
        kind, *rest = cursor.split(':')
        if kind == 'p':
            return self._page(int(rest[0]), int(rest[1]))
        if self.reset:
            self.reset = False
            raise dropbox.exceptions.ApiError('r', ListFolderContinueError.reset, None, None)
        pos = int(rest[0])
        return ListFolderResult(entries=self.log[pos:], cursor='c:%d' % len(self.log), has_more=False)

    def files_download_to_file(self, download_path, path, rev=None):
        self.downloads += 1
        if path in self.fail_download:
            with open(download_path, 'wb') as f:
                f.write(b'partial')
            raise ConnectionError('boom')
        d, data, r, mt = self.files[path]
        assert rev == r
        with open(download_path, 'wb') as f:
            f.write(data)


def hash_bytes(data):
    bh = b''.join(hashlib.sha256(data[i:i + m.BLOCK_SIZE]).digest() for i in range(0, len(data), m.BLOCK_SIZE))
    return hashlib.sha256(bh).hexdigest()

work = tempfile.mkdtemp()
root = os.path.join(work, 'backup')
shutil.copy(os.path.join(REPO, 'logging.conf'), work)
with open(os.path.join(work, 'config.ini'), 'w') as f:
    f.write('[dropbox]\napp_key=a\nsecret=b\nrefresh_token=c\n[backup]\nlocaldirectory=%s\n' % root)
os.chdir(work)
fake = FakeDbx()
m.dropbox.Dropbox = lambda **kw: fake


def run(*argv):
    sys.argv = ['main.py', *argv]
    try:
        m.main()
        return 0
    except SystemExit as e:
        return e.code


def read(p):
    with open(os.path.join(root, p), 'rb') as f:
        return f.read()


def archived(reason):
    base = os.path.join(root, '.dropbox-backup-archive')
    out = []
    for dp, dn, fn in os.walk(base):
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), base).split(os.sep, 1)[1]
            if rel.startswith(reason + os.sep):
                out.append(rel[len(reason) + 1:])
    return sorted(out)


def state():
    with open(os.path.join(root, '.dropbox-backup-state', 'state.json')) as f:
        return json.load(f)['/']


# Pre-existing local data from the old script: same content, wrong mtime; and a case-differing folder
assert run() == 1 and not os.path.exists(root)  # missing root: refuse
os.makedirs(os.path.join(root, 'Photos'))
with open(os.path.join(root, 'Photos', 'a.jpg'), 'wb') as f:
    f.write(b'AAA')
fake.put('/photos/a.jpg', b'AAA')  # display casing differs from local
fake.put('/Docs/sub/b.txt', b'BBB')
fake.put('/c.txt', b'CCC')

# 1. first run = full scan
assert run() == 0
assert fake.downloads == 2, fake.downloads            # a.jpg not re-downloaded
assert 'photos' not in os.listdir(root) and 'Photos' in os.listdir(root)
assert read('Docs/sub/b.txt') == b'BBB'
mt = datetime.datetime(2024, 1, 1, 12, tzinfo=datetime.timezone.utc).timestamp()
assert os.stat(os.path.join(root, 'Photos/a.jpg')).st_mtime == mt  # mtime adopted
assert os.stat(os.path.join(root, 'c.txt')).st_mtime == mt
assert state()['cursor'].startswith('c:')
print('1 full scan ok')

# 2. incremental, nothing changed
d0 = fake.downloads
assert run() == 0 and fake.downloads == d0
print('2 incremental no-op ok')

# 3. modification -> archived old version
fake.put('/c.txt', b'CCC v2', datetime.datetime(2024, 2, 1))
assert run() == 0
assert read('c.txt') == b'CCC v2' and archived('replaced') == ['c.txt']
print('3 modification ok')

# 4. deletion of file and folder -> archived
fake.delete('/Docs')
assert run() == 0
assert not os.path.exists(os.path.join(root, 'Docs'))
assert archived('deleted') == ['Docs/sub/b.txt']
print('4 deletion ok')

# 5. failed download -> cursor not advanced, no .part left, retried next run
fake.put('/new.bin', b'NEW')
fake.fail_download.add('/new.bin')
cur = state()['cursor']
assert run() == 1
assert state()['cursor'] == cur
assert not any(f.endswith(m.PART_SUFFIX) for f in os.listdir(root))
assert not os.path.exists(os.path.join(root, 'new.bin'))
fake.fail_download.clear()
assert run() == 0 and read('new.bin') == b'NEW'
print('5 failure/retry ok')

# 6. cursor reset -> falls back to full scan
fake.reset = True
d0 = fake.downloads
assert run() == 0 and fake.downloads == d0
print('6 cursor reset ok')

# 7. --verify detects silent local corruption (size and mtime unchanged)
p = os.path.join(root, 'new.bin')
st = os.stat(p)
with open(p, 'r+b') as f:
    f.write(b'XYZ')
os.utime(p, (st.st_mtime, st.st_mtime))
assert run() == 0 and read('new.bin') == b'XYZ'  # normal run trusts size+mtime
assert run('--verify') == 0 and read('new.bin') == b'NEW'
print('7 verify ok')

# 8. folder replaced by file of same name
fake.put('/thing/inner.txt', b'I')
assert run() == 0
fake.delete('/thing')
fake.put('/thing', b'now a file')
assert run() == 0 and read('thing') == b'now a file'
print('8 folder->file ok')

os.chdir(REPO)
shutil.rmtree(work)
print('ALL OK')

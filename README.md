# backup-dropbox

Since Dropbox introduced smart synchronisation, it's become harder to keep a local backup of all your Dropbox content.
This script does exactly that.

*backup-dropbox* can easily be installed on a Linux machine (e.g. Ubuntu or a Raspberry Pi) with a connected USB drive.

*backup-dropbox* only downloads files from Dropbox; it never deletes files.
When a file changes or is deleted in Dropbox, the previous local copy is moved to an archive folder
(`<archivedirectory>/<run timestamp>/replaced|deleted/...`), so a bad change in Dropbox (accidental
overwrite, ransomware) does not destroy your backup. Clean up the archive yourself when you want to reclaim space.

## How it works

- The first run lists your whole Dropbox and stores a cursor. Later runs ask Dropbox only for the changes
  since that cursor, so a nightly run with few changes takes seconds.
- Every `fullscandays` days (default 7) the whole tree is listed again, to catch files that went missing locally.
- A local file is considered up to date when its size and modification time match Dropbox. Only when they
  differ is its content hash computed. (The first run after upgrading hashes existing files once and adopts
  Dropbox's timestamps.)
- Downloads go to a temporary file, are checked against Dropbox's content hash and only then replace the local file.
- If any file fails, the cursor isn't advanced, so the changes are retried on the next run.

## Installation

Clone this git repository. The first run of `backup.sh` will automatically create a virtual environment and install dependencies.

## Configuration

Copy `config.ini.example` to `config.ini`, and change the values in that file to your preferences. Don't forget to add your Dropbox authentication token.
`localdirectory` must already exist: the script refuses to run when it's missing, so an unmounted USB drive doesn't cause a full download onto your system disk.
Optional settings: `statedirectory`, `archivedirectory` and `fullscandays` (see the example file).

Copy `backup.conf.example` to `backup.conf` and optionally configure:
- `LOG_FILE` — path to the cron log file (enables log rotation)
- `LOG_MAX_SIZE_KB` — rotate when the log exceeds this size (default 10 MB)
- `LOG_KEEP` — number of rotated log files to keep (default 5)
- `HC_PING_URL` — [Healthchecks.io](https://healthchecks.io) ping URL for uptime monitoring

## Running

```
./backup.sh
```

Or directly via the venv (after the first `backup.sh` run):

```
.venv/bin/python3 main.py
```

Options:
- `--full` — list the whole Dropbox tree instead of only the changes since the last run
- `--verify` — full scan that also recomputes the content hash of every local file (slow; detects silent disk corruption)

### Cron

To run daily as a cron job:

```
10 2 * * * /path/to/backup.sh
```

`backup.sh` handles log rotation and sends start/success/fail pings to Healthchecks.io.

## Tests

`test_main.py` runs the backup against an in-memory fake Dropbox (full scan, incremental runs, modifications,
deletions, failed downloads, cursor reset, `--verify`, missing backup drive):

```
.venv/bin/python3 test_main.py
```

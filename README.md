# backup-dropbox

Since Dropbox introduced smart synchronisation, it's become harder to keep a local backup of all your Dropbox content.
This script does exactly that.

*backup-dropbox* can easily be installed on a Raspberry Pi with a connected USB drive.

*backup-dropbox* only downloads files from Dropbox; it never deletes files.

## Installation

Clone this git repository. The first run of `backup.sh` will automatically create a virtual environment and install dependencies.

## Configuration

Copy `config.ini.example` to `config.ini`, and change the values in that file to your preferences. Don't forget to add your Dropbox authentication token.

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

### Cron

To run daily as a cron job:

```
10 2 * * * /path/to/backup.sh
```

`backup.sh` handles log rotation and sends start/success/fail pings to Healthchecks.io.

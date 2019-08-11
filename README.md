# backup-dropbox

Since Dropbox introduced smart synchronisation, it's become harder to keep a local backup of all your Dropbox content.
This script does exactly that.

*backup-dropbox* can easily be installed on a Raspberry Pi with a connected USB drive.

*backup-dropbox* only downloads files from Dropbox; it never deletes files.

## Installation

Clone this git repository.

## Configuration

Copy `config.ini.example` to `config.ini`, and change the values in that file to your preferences. Don't forget to add your Dropbox authentication token.

## Running

`python main.py`


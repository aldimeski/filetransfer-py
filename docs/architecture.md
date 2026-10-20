# Architecture

```
 cron
  |
  v
 file-transfer.py job.param
  |
  +-- load_params ----> Config.from_params --> expand date tokens, compile FileMask,
  |                                            build DateFilter
  +-- acquire_lock (one running instance per .param file)
  +-- setup_logging (file + stderr + error collector for e-mail)
  |
  +-- Source  (LocalSource | SftpSource/paramiko)
  |     list_files, stat, materialize (download), remove
  |
  +-- select_files:  mask -> date filter -> size check -> control file
  |
  +-- for each selected file:
  |     log creation/modification date   <-- always, before the transfer
  |     deliver to every OutputDir  (tmp_<name> then atomic rename, optional gzip/gunzip)
  |     deliver original to ArchiveDir  (optional)
  |     delete source (+control file) only if KeepInput:N and everything above succeeded
  |
  +-- on any ERROR: e-mail notification (Notification:Y), exit code 1
```

## Behaviour that differs from the Perl tool

- A file is deleted from the input only after **every** target (and the archive) was written.
- Invalid `Y/N` values, dates, or missing parameters stop the job instead of being ignored.
- SFTP password files readable by others and multi-line password files are reported.
- SFTP host keys are verified (`SftpStrictHostKey:N` disables this).
- Control files (`CtlFileExtension`) are never transferred as data files.
- Files are downloaded once from SFTP even with several output directories.

## Optional parameters added

| Parameter | Default | Meaning |
|---|---|---|
| `SftpPort` | 22 | SFTP port |
| `SftpStrictHostKey` | Y | verify the server key against known_hosts |
| `SizeCheckDelay` | 20 | seconds to wait for the size check |
| `NotificationPort` | 25 | SMTP port |

Command line: `--check` (print expanded values and exit), `--now "YYYY-MM-DD HH:MM:SS"` (fake the current time).

## Exit codes

`0` success, `1` configuration error, another instance running, or at least one error logged.

## convert-param.py

```
 folder (argument or current dir)
  |
  +-- for each *.param: find FileMask / FileCreationDate lines that look like Perl
  |
  +-- mini Perl interpreter (convert_perl): localtime, sprintf, $var+-N, concatenation
  |     values: Raw (localtime) -> Cal (calendar number + shift) -> Part (zero padded)
  |
  +-- render: parts printed together form one block <FORMAT-Nu>
  +-- validate with expand_date_tokens / build_date_filter from file-transfer.py
  |
  +-- ask per file -> copy to <name>.param_perl -> rewrite original via temp file + rename
```

Corrections compared with the Perl semantics: a day shift such as `$mday-1` no longer produces
day `00` on the 1st; month and year follow. Equivalence with real Perl is tested on mid-month dates
where both behave the same.

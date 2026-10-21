"""Tests for file-transfer.py. Run from the project root:

    python3 tests/test-file-transfer.py -v
"""
import contextlib
import gzip
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("file_transfer_tool", os.path.join(ROOT, "file-transfer.py"))
ft = importlib.util.module_from_spec(_spec)
sys.modules["file_transfer_tool"] = ft
_spec.loader.exec_module(ft)


def epoch(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


class DateTokenTests(unittest.TestCase):
    def expand(self, text, now):
        return ft.expand_date_tokens(text, datetime.strptime(now, "%Y-%m-%d %H:%M:%S"))

    def test_today_parts(self):
        self.assertEqual(self.expand("<YYYY><MM><DD>", "2026-05-07 13:45:09"), "20260507")
        self.assertEqual(self.expand("<YY>-<HH>:<MI>:<SS>", "2026-05-07 13:45:09"), "26-13:45:09")

    def test_day_minus_five_crosses_into_previous_month(self):
        # May 1 minus 5 days -> April 26 (April has 30 days)
        self.assertEqual(self.expand("<DD-5>", "2026-05-01 00:00:00"), "26")
        self.assertEqual(self.expand("<MM-0>", "2026-05-01 00:00:00"), "05")

    def test_day_arithmetic_respects_month_length_and_leap_year(self):
        self.assertEqual(self.expand("<DD-1>", "2026-03-01 00:00:00"), "28")
        self.assertEqual(self.expand("<DD-1>", "2028-03-01 00:00:00"), "29")
        self.assertEqual(self.expand("<DD+1>", "2026-12-31 00:00:00"), "01")

    def test_month_and_year_shift(self):
        self.assertEqual(self.expand("<MM-1>", "2026-01-15 00:00:00"), "12")
        self.assertEqual(self.expand("<YYYY-1>", "2026-01-15 00:00:00"), "2025")
        self.assertEqual(self.expand("<MM-1>", "2026-03-31 00:00:00"), "02")  # day clamped to Feb 28
        self.assertEqual(self.expand("<MM+13>", "2026-01-15 00:00:00"), "02")

    def test_case_and_whitespace_are_tolerated(self):
        self.assertEqual(self.expand("<dd - 5>", "2026-05-01 00:00:00"), "26")

    def test_block_shifts_the_whole_date_across_month_and_year(self):
        self.assertEqual(self.expand("<YYYYMMDD-5d>", "2026-01-03 00:00:00"), "20251229")
        self.assertEqual(self.expand("<YYYY-MM-DD-1d>", "2026-03-01 00:00:00"), "2026-02-28")
        self.assertEqual(self.expand("<YYYYMMDD+1d>", "2026-12-31 00:00:00"), "20270101")
        self.assertEqual(self.expand("<YYYYMM-1mo>", "2026-01-15 00:00:00"), "202512")
        self.assertEqual(self.expand("<YYYY-1y>", "2026-05-01 00:00:00"), "2025")
        self.assertEqual(self.expand("<YYYYMMDD HH-2h>", "2026-05-01 01:00:00"), "20260430 23")
        self.assertEqual(self.expand("<YYYY-MM-DD HH-MI-SS-90mi>", "2026-05-01 00:30:00"), "2026-04-30 23-00-00")

    def test_block_without_shift_formats_today(self):
        self.assertEqual(self.expand("<YYYY-MM-DD>", "2026-05-07 13:45:09"), "2026-05-07")
        self.assertEqual(self.expand("<YYYYMMDDHHMISS>", "2026-05-07 13:45:09"), "20260507134509")

    def test_block_is_case_insensitive_and_mixes_with_text(self):
        self.assertEqual(self.expand("A_<yyyymmdd - 5D>.zip <DD-5>", "2026-05-01 00:00:00"), "A_20260426.zip 26")

    def test_invalid_blocks_are_rejected(self):
        for bad in ("<YYYYMMDD-5>", "<YYYYMMDD-5m>", "<YYYYMMDD-5w>", "<YYYYMMDD-d>"):
            with self.assertRaises(ft.ConfigError, msg=bad):
                self.expand(bad, "2026-05-01 00:00:00")

    def test_regex_without_tokens_is_untouched(self):
        mask = r"^DR_P11_\d{8}.*\.(zip|gz)$"
        self.assertEqual(self.expand(mask, "2026-05-01 00:00:00"), mask)

    def test_invalid_token_is_rejected(self):
        for bad in ("<DD*5>", "<DD-x>", "<YYYY--1>"):
            with self.assertRaises(ft.ConfigError):
                self.expand(bad, "2026-05-01 00:00:00")


class DateFilterTests(unittest.TestCase):
    def moment(self, text):
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")

    def test_single_day(self):
        flt = ft.build_date_filter("2026-04-22")
        self.assertTrue(flt.matches(self.moment("2026-04-22 23:59:59")))
        self.assertFalse(flt.matches(self.moment("2026-04-23 00:00:00")))

    def test_day_range_includes_both_days(self):
        flt = ft.build_date_filter("2026-04-22&date2026-04-23")
        self.assertTrue(flt.matches(self.moment("2026-04-22 00:00:01")))
        self.assertTrue(flt.matches(self.moment("2026-04-23 23:59:59")))
        self.assertFalse(flt.matches(self.moment("2026-04-21 23:59:59")))
        self.assertFalse(flt.matches(self.moment("2026-04-24 00:00:00")))

    def test_other_precisions(self):
        self.assertTrue(ft.build_date_filter("2026").matches(self.moment("2026-11-30 10:00:00")))
        self.assertTrue(ft.build_date_filter("2026-04").matches(self.moment("2026-04-30 10:00:00")))
        self.assertTrue(ft.build_date_filter("2026-04-22 10").matches(self.moment("2026-04-22 10:59:00")))
        self.assertFalse(ft.build_date_filter("2026-04-22 10-15").matches(self.moment("2026-04-22 10:16:00")))
        self.assertTrue(ft.build_date_filter("2019-03-10 15-10-30").matches(self.moment("2019-03-10 15:10:30")))

    def test_invalid_dates_are_rejected_not_ignored(self):
        for bad in ("", "2026-13-01", "26-04-22", "2026-04-22&date2026-04-23&date2026-04-24", "2026-04-23&date2026-04-22"):
            with self.assertRaises(ft.ConfigError):
                ft.build_date_filter(bad)


class ParamTests(unittest.TestCase):
    def test_values_keep_colons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.param")
            with open(path, "w") as handle:
                handle.write("# comment\n\nFileMask:^a(?:b|c)$\nSftpHost: host:22 \n")
            self.assertEqual(ft.load_params(path), {"FileMask": "^a(?:b|c)$", "SftpHost": "host:22"})


class JobTestCase(unittest.TestCase):
    """Builds a temp workspace with in/ out/ log/ and runs main() on a .param file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ft-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("in", "out", "out2", "arch", "log"):
            os.mkdir(os.path.join(self.tmp, name))

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def put(self, relative, content=b"data\n", mtime="2026-05-01 09:15:30"):
        full = self.path("in", relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(content)
        stamp = epoch(mtime)
        os.utime(full, (stamp, stamp))
        return full

    def run_job(self, now="2026-05-10 08:00:00", extra_args=(), **overrides):
        params = {
            "InputDir": self.path("in") + "/", "OutputDir": self.path("out") + "/", "KeepInput": "Y",
            "SftpCheck": "N", "FileMask": r"^DR_P11.*", "SubDirCheck": "N", "SizeCheck": "N",
            "CtlFileCheck": "N", "GzipOutput": "N", "GzipInput": "N", "ArchiveCheck": "N",
            "ArchiveDir": self.path("arch") + "/", "LogMode": "Full", "LogDir": self.path("log") + "/",
            "LogFile": "job.log", "Notification": "N", "FileDateCheck": "N",
        }
        params.update(overrides)
        param_file = self.path("job.param")
        with open(param_file, "w") as handle:
            handle.write("".join(f"{key}:{value}\n" for key, value in params.items()))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = ft.main([param_file, "--now", now, *extra_args])
        self.err = err.getvalue()
        return code

    def log_text(self):
        with open(self.path("log", "job.log")) as handle:
            return handle.read()

    def listing(self, folder):
        return sorted(os.listdir(self.path(folder)))


class LocalTransferTests(JobTestCase):
    def test_copy_logs_creation_date_before_transfer(self):
        self.put("DR_P11_a.dat", mtime="2026-05-01 09:15:30")
        self.assertEqual(self.run_job(), 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_a.dat"])
        self.assertEqual(self.listing("in"), ["DR_P11_a.dat"])  # KeepInput=Y
        log = self.log_text()
        date_line = log.index("creation/modification date (before transfer): 2026-05-01 09:15:30")
        self.assertLess(date_line, log.index("copied to"))
        self.assertRegex(log, r"\d+: \d\d/\d\d/\d\d \d\d:\d\d:\d\d \[ ")

    def test_move_multiple_targets_and_awkward_names(self):
        self.put("DR_P11 100%.dat")
        self.assertEqual(self.run_job(KeepInput="N", OutputDir=f"{self.path('out')}/,{self.path('out2')}/"), 0, self.err)
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(self.listing("out"), ["DR_P11 100%.dat"])
        self.assertEqual(self.listing("out2"), ["DR_P11 100%.dat"])

    def test_mask_with_date_tokens(self):
        self.put("DR_P11_20260505.dat")
        self.put("DR_P11_20260510.dat")
        self.assertEqual(self.run_job(FileMask=r"^DR_P11_<YYYY><MM><DD-5>\.dat$"), 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_20260505.dat"])
        self.assertIn("expanded to", self.log_text())

    def test_file_date_range_with_tokens(self):
        self.put("DR_P11_old.dat", mtime="2026-05-07 10:00:00")
        self.put("DR_P11_yesterday.dat", mtime="2026-05-09 10:00:00")
        self.put("DR_P11_today.dat", mtime="2026-05-10 07:00:00")
        code = self.run_job(FileDateCheck="Y", FileCreationDate="<YYYY>-<MM>-<DD-1>&date<YYYY>-<MM>-<DD>")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_today.dat", "DR_P11_yesterday.dat"])

    def test_yesterday_and_today_range_works_on_the_first_of_the_month(self):
        self.put("DR_P11_prev.dat", mtime="2026-04-30 23:00:00")
        self.put("DR_P11_today.dat", mtime="2026-05-01 07:00:00")
        self.put("DR_P11_older.dat", mtime="2026-04-29 12:00:00")
        code = self.run_job(now="2026-05-01 08:00:00", FileDateCheck="Y",
                            FileCreationDate="<YYYY-MM-DD-1d>&date<YYYY-MM-DD>")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_prev.dat", "DR_P11_today.dat"])

    def test_single_day_with_tokens_at_month_start(self):
        self.put("DR_P11_a.dat", mtime="2026-04-26 10:00:00")
        self.put("DR_P11_b.dat", mtime="2026-04-27 10:00:00")
        code = self.run_job(now="2026-05-01 08:00:00", FileDateCheck="Y", FileCreationDate="<YYYY>-<MM-1>-<DD-5>")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_a.dat"])

    def test_bad_date_config_stops_instead_of_transferring_everything(self):
        self.put("DR_P11_a.dat")
        self.assertEqual(self.run_job(FileDateCheck="Y", FileCreationDate="garbage"), 1)
        self.assertEqual(self.listing("out"), [])

    def test_subdirectories(self):
        self.put("DR_P11_top.dat")
        self.put("sub/deeper/DR_P11_deep.dat")
        self.assertEqual(self.run_job(SubDirCheck="N"), 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_top.dat"])
        self.assertEqual(self.run_job(SubDirCheck="Y"), 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_deep.dat", "DR_P11_top.dat"])

    def test_archive_keeps_original(self):
        self.put("DR_P11_a.dat", b"original")
        self.assertEqual(self.run_job(ArchiveCheck="Y", KeepInput="N", GzipInput="Y"), 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_a.dat.gz"])
        self.assertEqual(self.listing("arch"), ["DR_P11_a.dat"])
        with open(self.path("arch", "DR_P11_a.dat"), "rb") as handle:
            self.assertEqual(handle.read(), b"original")

    def test_gzip_round_trip(self):
        self.put("DR_P11_a.dat", b"payload " * 100)
        self.assertEqual(self.run_job(GzipInput="Y"), 0, self.err)
        packed = self.path("out", "DR_P11_a.dat.gz")
        with gzip.open(packed, "rb") as handle:
            self.assertEqual(handle.read(), b"payload " * 100)
        # now treat the packed file as input that must be unpacked into the target
        shutil.rmtree(self.path("in")); os.mkdir(self.path("in"))
        shutil.move(packed, self.path("in", "DR_P11_a.dat.gz"))
        self.assertEqual(self.run_job(GzipOutput="Y"), 0, self.err)
        with open(self.path("out", "DR_P11_a.dat"), "rb") as handle:
            self.assertEqual(handle.read(), b"payload " * 100)

    def test_control_file_required_and_removed_on_move(self):
        self.put("DR_P11_ready.dat")
        self.put("DR_P11_ready.FIN", b"")
        self.put("DR_P11_notready.dat")
        self.assertEqual(self.run_job(CtlFileCheck="Y", CtlFileExtension="FIN", KeepInput="N"), 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_ready.dat"])
        self.assertEqual(self.listing("in"), ["DR_P11_notready.dat"])

    def test_size_check_skips_growing_file(self):
        path = self.put("DR_P11_a.dat", b"12345")
        original_sleep = ft.time.sleep
        ft.time.sleep = lambda seconds: open(path, "ab").write(b"more")  # file grows during the wait
        self.addCleanup(setattr, ft.time, "sleep", original_sleep)
        self.assertEqual(self.run_job(SizeCheck="Y", SizeCheckDelay="1"), 0, self.err)
        self.assertEqual(self.listing("out"), [])
        self.assertIn("size is still changing", self.log_text())

    def test_failed_delivery_keeps_source_and_reports_error(self):
        self.put("DR_P11_a.dat")
        # a non-empty directory with the target's name makes the final rename fail
        os.makedirs(self.path("out2", "DR_P11_a.dat", "blocker"))
        code = self.run_job(KeepInput="N", OutputDir=f"{self.path('out')}/,{self.path('out2')}/")
        self.assertEqual(code, 1)
        self.assertEqual(self.listing("in"), ["DR_P11_a.dat"])  # must not be deleted
        self.assertEqual([n for n in self.listing("out2") if n.startswith("tmp_")], [])  # no leftovers
        self.assertIn("ERROR:", self.log_text())
        self.assertIn("not fully processed", self.err)

    def test_unwritable_target_is_reported_before_anything_is_touched(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.put("DR_P11_a.dat")
        os.chmod(self.path("out2"), 0o500)
        self.addCleanup(os.chmod, self.path("out2"), 0o700)
        code = self.run_job(KeepInput="N", OutputDir=f"{self.path('out')}/,{self.path('out2')}/")
        self.assertEqual(code, 1)
        self.assertEqual(self.listing("out"), [])
        self.assertEqual(self.listing("in"), ["DR_P11_a.dat"])

    def test_log_mode_none_and_basic(self):
        self.put("DR_P11_a.dat")
        self.assertEqual(self.run_job(LogMode="None"), 0, self.err)
        self.assertEqual(self.listing("log"), [])
        self.assertEqual(self.run_job(LogMode="Basic"), 0, self.err)
        self.assertNotIn("matches", self.log_text())
        self.assertIn("before transfer", self.log_text())

    def test_missing_input_dir_is_an_error(self):
        self.assertEqual(self.run_job(InputDir=self.path("nope") + "/"), 1)
        self.assertIn("InputDir does not exist", self.log_text())

    def test_invalid_flag_is_rejected(self):
        self.assertEqual(self.run_job(KeepInput="maybe"), 1)
        self.assertIn("KeepInput must be Y or N", self.err)

    def test_check_mode_prints_expansion_and_transfers_nothing(self):
        self.put("DR_P11_a.dat")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.run_job(extra_args=("--check",), FileMask="^X_<YYYY><MM><DD-5>$")
        self.assertEqual(code, 0)
        self.assertIn("X_20260505", out.getvalue())
        self.assertEqual(self.listing("out"), [])

    def test_second_instance_is_refused(self):
        lock = ft.acquire_lock(self.path("job.param"))
        self.addCleanup(lock.close)
        self.put("DR_P11_a.dat")
        self.assertEqual(self.run_job(), 1)
        self.assertIn("instance of the same script", self.err)

    def test_notification_is_sent_on_error(self):
        sent = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None): sent.append((host, port))
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def send_message(self, message, to_addrs=None): sent.append((message["Subject"], to_addrs, message.as_string()))

        original = ft.smtplib.SMTP
        ft.smtplib.SMTP = FakeSMTP
        self.addCleanup(setattr, ft.smtplib, "SMTP", original)
        code = self.run_job(
            InputDir=self.path("nope") + "/", Notification="Y", NotificationSender="a@x.at",
            NotificationRe="b@x.at,c@x.at", NotificationCc="d@x.at", NotificationServer="smtp.x", NotificationMethod="smtp",
        )
        self.assertEqual(code, 1)
        self.assertEqual(sent[0], ("smtp.x", 25))
        self.assertEqual(sent[1][1], ["b@x.at", "c@x.at", "d@x.at"])
        host = ft.socket.getfqdn() or ft.socket.gethostname()
        self.assertIn(f"on {host}", sent[1][0])  # subject names the machine
        text = self.decoded_mail(sent[1][2])
        self.assertIn(f"Script ran on server: {host}", text)
        self.assertIn("Transfer: local transfer", text)
        self.assertIn(self.path("job.param"), text)
        self.assertIn("InputDir does not exist", text)

    def decoded_mail(self, raw):
        import email
        message = email.message_from_string(raw)
        return "".join(part.get_payload(decode=True).decode("utf-8") for part in message.walk()
                       if part.get_content_type() == "text/html")


class FakeEntry:
    """Like paramiko's SFTPAttributes from listdir_attr: describes a link itself, not its target."""

    def __init__(self, name, info):
        self.filename, self.st_mode, self.st_size, self.st_mtime = name, info.st_mode, info.st_size, info.st_mtime


class FakeSftpChannel:
    """Maps 'remote' paths straight onto the local file system."""

    def listdir_attr(self, path):
        # some servers also list "." and ".."
        entries = [FakeEntry(name, os.lstat(os.path.join(path, name))) for name in (".", "..")]
        entries += [FakeEntry(entry.name, entry.stat(follow_symlinks=False)) for entry in os.scandir(path)]
        return entries

    def stat(self, path):  # follows links, like the real thing
        try:
            return FakeEntry(os.path.basename(path), os.stat(path))
        except OSError:
            raise IOError("no such file")

    def get(self, remote, local):
        shutil.copyfile(remote, local)

    def remove(self, path):
        os.unlink(path)

    def close(self):
        pass


class FakeSSHClient:
    connected_with = None

    def load_system_host_keys(self): pass
    def set_missing_host_key_policy(self, policy): FakeSSHClient.policy = policy
    def connect(self, **options): FakeSSHClient.connected_with = options
    def open_sftp(self): return FakeSftpChannel()
    def close(self): pass


class SftpTransferTests(JobTestCase):
    def setUp(self):
        super().setUp()
        fake = types.ModuleType("paramiko")
        fake.SSHClient = FakeSSHClient
        fake.RejectPolicy = type("RejectPolicy", (), {})
        fake.AutoAddPolicy = type("AutoAddPolicy", (), {})
        sys.modules["paramiko"] = fake
        self.addCleanup(sys.modules.pop, "paramiko", None)

    def sftp_job(self, **overrides):
        key = self.path("id_key")
        open(key, "w").close()
        options = dict(SftpCheck="Y", SftpType="Key", SftpKey=key, SftpHost="example.test", SftpUser="svc")
        options.update(overrides)
        return self.run_job(**options)

    def test_key_login_download_move_and_cleanup(self):
        self.put("DR_P11_a.dat", b"remote")
        self.assertEqual(self.sftp_job(KeepInput="N", ArchiveCheck="Y"), 0, self.err)
        self.assertEqual(self.listing("out"), ["DR_P11_a.dat"])
        self.assertEqual(self.listing("arch"), ["DR_P11_a.dat"])
        self.assertEqual(self.listing("in"), [])
        self.assertEqual(FakeSSHClient.connected_with["username"], "svc")
        self.assertEqual(FakeSSHClient.connected_with["key_filename"], self.path("id_key"))
        self.assertIn("creation/modification date (before transfer): 2026-05-01 09:15:30", self.log_text())

    def test_password_file_and_recursion_and_date_filter(self):
        self.put("DR_P11_a.dat", mtime="2026-05-09 10:00:00")
        self.put("sub/DR_P11_b.dat", mtime="2026-05-09 11:00:00")
        self.put("sub/DR_P11_old.dat", mtime="2026-01-01 11:00:00")
        passfile = self.path("pw")
        with open(passfile, "w") as handle:
            handle.write("s3cret\n")
        os.chmod(passfile, 0o600)
        code = self.sftp_job(SftpType="Clear", SftpPassFile=passfile, SubDirCheck="Y",
                             FileDateCheck="Y", FileCreationDate="<YYYY>-<MM>-<DD-1>")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(FakeSSHClient.connected_with["password"], "s3cret")
        self.assertEqual(self.listing("out"), ["DR_P11_a.dat", "DR_P11_b.dat"])

    def test_dots_directories_and_links_are_never_downloaded(self):
        self.put("DR_P11_a.dat", b"real")
        os.makedirs(self.path("in", "dwh_in"))
        os.makedirs(self.path("in", "out"))
        with open(self.path("elsewhere.dat"), "w") as handle:
            handle.write("target outside the input folder")
        os.symlink(self.path("elsewhere.dat"), self.path("in", "DR_P11_link.dat"))  # link to a file: allowed
        os.symlink("out", self.path("in", "DR_P11_dirlink"))            # link to a directory: skipped
        os.symlink("/nonexistent", self.path("in", "DR_P11_broken"))    # broken link: skipped
        code = self.sftp_job(KeepInput="N", FileMask=".*")  # a mask that also matches ".", ".." and folder names
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.err.count("ERROR"), 0)
        self.assertEqual(self.listing("out"), ["DR_P11_a.dat", "DR_P11_link.dat"])
        self.assertEqual(self.listing("in"), ["DR_P11_broken", "DR_P11_dirlink", "dwh_in", "out"])  # folders untouched
        self.assertNotIn("/..", self.log_text())

    def test_listing_has_only_regular_files(self):
        self.put("a.dat")
        self.put("sub/b.dat")
        os.makedirs(self.path("in", "emptydir"))
        source = ft.SftpSource.__new__(ft.SftpSource)  # skip the connection, only the listing is tested
        source._sftp = FakeSftpChannel()
        names = lambda recursive: sorted(f.name for f in source.list_files(self.path("in") + "/", recursive))
        self.assertEqual(names(False), ["a.dat"])
        self.assertEqual(names(True), ["a.dat", "b.dat"])

    def test_error_mail_names_the_sftp_server(self):
        sent = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def send_message(self, message, to_addrs=None): sent.append(message)

        original = ft.smtplib.SMTP
        ft.smtplib.SMTP = FakeSMTP
        self.addCleanup(setattr, ft.smtplib, "SMTP", original)
        self.put("DR_P11_a.dat")
        code = self.sftp_job(SftpKey=self.path("missing"), SftpHost="sftp.example.test", SftpUser="svc", Notification="Y",
                             NotificationSender="a@x.at", NotificationRe="b@x.at", NotificationServer="smtp.x")
        self.assertEqual(code, 1)
        text = sent[0].get_payload()[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("Transfer: SFTP from server sftp.example.test:22 (user svc)", text)
        self.assertIn("Script ran on server:", text)

    def test_missing_key_file_reports_error(self):
        self.put("DR_P11_a.dat")
        self.assertEqual(self.sftp_job(SftpKey=self.path("missing")), 1)
        self.assertIn("SFTP key file not found", self.log_text())


if __name__ == "__main__":
    unittest.main()

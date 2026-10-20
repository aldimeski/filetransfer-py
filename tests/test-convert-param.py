"""Tests for convert-param.py. Run from the project root:

    python3 tests/test-convert-param.py -v
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tool = load("file_transfer_tool", "file-transfer.py")
conv = load("convert_param_tool", "convert-param.py")

# The two examples from the PerlFileTransfer document
PDF_MASK = ('my ($sec,$min,$hour,$mday,$mon,$year) = localtime(); $year = sprintf("%02d", $year+1900); '
            '$mon = sprintf("%02d", $mon+1); $mday = sprintf("%02d", $mday);my $filemask = '
            '"OK_Export_File_"."$year"."$mon"."$mday".".*zip\\$";')
PDF_DATE = ('my ($sec,$min,$hour,$mday,$mon,$year) = localtime(); $year = sprintf("%02d", $year+1900); '
            '$mon = sprintf("%02d", $mon+1); $mday1 = sprintf("%02d", $mday-1); $mday2 = sprintf("%02d", $mday); '
            'my $filecreationdate = "$year-$mon-$mday1&date$year-$mon-$mday2";')
HEADER = 'my ($sec,$min,$hour,$mday,$mon,$year) = localtime(); '
FIX = ('$year = sprintf("%02d", $year+1900); $mon = sprintf("%02d", $mon+1); '
       '$mday = sprintf("%02d", $mday); $hour = sprintf("%02d", $hour); $min = sprintf("%02d", $min); ')


def run_perl(snippet, moment):
    """Evaluate the snippet with a real Perl whose clock is frozen at `moment`."""
    script = ('BEGIN { my $f = $ENV{FROZEN}; *CORE::GLOBAL::time = sub () { $f }; '
              '*CORE::GLOBAL::localtime = sub { CORE::localtime(@_ ? $_[0] : $f) }; } '
              'my $v = eval($ARGV[0]); die $@ if $@; print $v;')
    out = subprocess.run(["perl", "-e", script, snippet], capture_output=True, text=True,
                         env=dict(os.environ, FROZEN=str(int(moment.timestamp()))))
    if out.returncode:
        raise RuntimeError(out.stderr)
    return out.stdout


HAS_PERL = shutil.which("perl") is not None


class ConvertValueTests(unittest.TestCase):
    def test_pdf_filemask(self):
        new, notes = conv.convert_perl(PDF_MASK)
        self.assertEqual(new, "OK_Export_File_<YYYYMMDD>.*zip$")

    def test_pdf_filecreationdate_becomes_a_correct_whole_date_shift(self):
        new, notes = conv.convert_perl(PDF_DATE)
        self.assertEqual(new, "<YYYY-MM-DD-1d>&date<YYYY-MM-DD>")

    def test_localtime_with_time_offset(self):
        code = HEADER + FIX + 'my $m = "F_".$year.$mon.$mday;'
        self.assertEqual(conv.convert_perl(code.replace("localtime()", "localtime(time - 86400*5)"))[0], "F_<YYYYMMDD-5d>")
        self.assertEqual(conv.convert_perl(code.replace("localtime()", "localtime(time-3600*2)"))[0], "F_<YYYYMMDD-2h>")
        self.assertEqual(conv.convert_perl(code.replace("localtime()", "localtime(time - 90)"))[0], "F_<YYYYMMDD-90s>")

    def test_month_and_year_arithmetic(self):
        code = HEADER + '$year = sprintf("%04d", $year+1900); my $pm = sprintf("%02d", $mon); my $x = "R_$year$pm.csv";'
        # $mon is the raw 0-11 value, so sprintf of it alone is "previous month" (a classic Perl off-by-one)
        self.assertEqual(conv.convert_perl(code)[0], "R_<YYYYMM-1mo>.csv")
        code = HEADER + '$year = sprintf("%04d", $year+1900-1); my $r = "Y$year";'
        self.assertEqual(conv.convert_perl(code)[0], "Y<YYYY-1y>")
        code = HEADER + 'my $yy = sprintf("%02d", $year-100); my $r = "Y$yy";'
        self.assertEqual(conv.convert_perl(code)[0], "Y<YY>")

    def test_separate_runs_keep_their_own_shift(self):
        code = HEADER + FIX + '$mday1 = sprintf("%02d", $mday-1); my $r = "A_$year$mon$mday_B_$mday1";'
        # "$mday_B_" is a different (undefined) variable in Perl, exactly as the converter reports
        with self.assertRaises(conv.Unsupported):
            conv.convert_perl(code)
        code = HEADER + FIX + '$mday1 = sprintf("%02d", $mday-1); my $r = "A_${year}${mon}${mday}_B_$mday1";'
        self.assertEqual(conv.convert_perl(code)[0], "A_<YYYYMMDD>_B_<DD-1d>")

    def test_time_parts_and_separators(self):
        code = HEADER + FIX + 'my $r = "$year-$mon-$mday $hour-$min";'
        self.assertEqual(conv.convert_perl(code)[0], "<YYYY-MM-DD HH-MI>")

    def test_unsupported_code_is_reported_not_guessed(self):
        for code in (
            'if (1) { my $x = "a"; }',
            'my ($s,$m,$h,$d,$mo,$y,$wday) = localtime(); my $r = "x$wday";',
            HEADER + 'my $r = "F_$mday";',  # unpadded day cannot be a date token
            HEADER + 'my $r = "F_$year";',  # raw year is 126, not 2026
            HEADER + '$mday = sprintf("%d", $mday); my $r = "F_$mday";',
            'my $r = "F_$unknown";',
            HEADER + '$a = sprintf("%02d", $mday-1); $b = sprintf("%02d", $mon+2); my $r = "$a$b";' + ' $a = $a + 1;',
        ):
            with self.assertRaises(conv.Unsupported, msg=code):
                conv.convert_perl(code)

    def test_mixed_shifts_are_split_with_a_note(self):
        code = HEADER + FIX + '$d1 = sprintf("%02d", $mday-1); $m1 = sprintf("%02d", $mon-1); my $r = "$year$m1$d1";'
        value, notes = conv.convert_perl(code)
        self.assertEqual(value, "<YYYY><MM-1mo><DD-1d>")
        self.assertTrue(notes)

    def test_looks_like_perl(self):
        self.assertTrue(conv.looks_like_perl(PDF_MASK))
        self.assertTrue(conv.looks_like_perl('my $x = "a";'))
        for plain in (r"^DR_P11.*", r"^OK_<YYYYMMDD>.*zip$", "2019-03-10&date2019-03-12", "<YYYY-MM-DD-1d>"):
            self.assertFalse(conv.looks_like_perl(plain), plain)


@unittest.skipUnless(HAS_PERL, "perl is not installed")
class PerlEquivalenceTests(unittest.TestCase):
    """The converted value must expand to exactly what the original Perl code returns."""

    MOMENTS = [datetime(2026, 5, 15, 12, 34, 56), datetime(2026, 11, 20, 3, 4, 5), datetime(2028, 2, 20, 23, 59, 59)]

    def assert_same(self, code):
        converted, _ = conv.convert_perl(code)
        for moment in self.MOMENTS:
            self.assertEqual(tool.expand_date_tokens(converted, moment), run_perl(code, moment),
                             f"{converted!r} at {moment}")

    def test_pdf_examples(self):
        self.assert_same(PDF_MASK)
        self.assert_same(PDF_DATE)

    def test_offsets(self):
        body = FIX + 'my $r = "F_$year-$mon-$mday $hour:$min";'
        self.assert_same(HEADER + body)
        self.assert_same((HEADER + body).replace("localtime()", "localtime(time - 86400*3)"))
        self.assert_same((HEADER + body).replace("localtime()", "localtime(time + 3600*2)"))

    def test_individual_parts(self):
        self.assert_same(HEADER + FIX + '$d = sprintf("%02d", $mday-4); my $r = "P_$d.zip";')
        self.assert_same(HEADER + FIX + '$h = sprintf("%02d", $hour-1); my $r = "P_${mday}_$h";')
        self.assert_same(HEADER + '$pm = sprintf("%02d", $mon); my $r = "R_$pm";')
        self.assert_same(HEADER + '$y = sprintf("%04d", $year+1900-1); my $r = "Y$y";')
        self.assert_same(HEADER + '$yy = sprintf("%02d", $year-100); my $r = "Y$yy";')


class ParamFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ft-conv-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", newline="") as handle:
            handle.write(text)
        return path

    def read(self, name):
        with open(os.path.join(self.dir, name), newline="") as handle:
            return handle.read()

    def run_main(self, *args, answers=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("builtins.input", side_effect=list(answers)) as fake_input:
            code = conv.main([*args])
        self.asked = fake_input.call_count
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    PERL_JOB = ("# job\nInputDir:/in/\nFileMask:" + PDF_MASK + "\nFileDateCheck:Y\n"
                "FileCreationDate:" + PDF_DATE + "\nLogFile:a.log\n")

    def test_yes_converts_and_keeps_a_perl_backup(self):
        self.write("job.param", self.PERL_JOB)
        self.assertEqual(self.run_main(self.dir, answers=["y"]), 0, self.err)
        self.assertEqual(self.asked, 1)
        self.assertEqual(self.read("job.param_perl"), self.PERL_JOB)  # byte-for-byte original
        self.assertEqual(self.read("job.param"), (
            "# job\nInputDir:/in/\nFileMask:OK_Export_File_<YYYYMMDD>.*zip$\nFileDateCheck:Y\n"
            "FileCreationDate:<YYYY-MM-DD-1d>&date<YYYY-MM-DD>\nLogFile:a.log\n"))

    def test_no_leaves_everything_untouched(self):
        self.write("job.param", self.PERL_JOB)
        self.assertEqual(self.run_main(self.dir, answers=["n"]), 0)
        self.assertEqual(self.read("job.param"), self.PERL_JOB)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "job.param_perl")))

    def test_each_file_is_asked_separately(self):
        for name in ("a.param", "b.param", "c.param"):
            self.write(name, self.PERL_JOB)
        self.assertEqual(self.run_main(self.dir, answers=["y", "n", "y"]), 0)
        self.assertEqual(self.asked, 3)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "a.param_perl")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "b.param_perl")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "c.param_perl")))

    def test_all_and_quit(self):
        for name in ("a.param", "b.param", "c.param"):
            self.write(name, self.PERL_JOB)
        self.run_main(self.dir, answers=["a"])
        self.assertEqual(self.asked, 1)
        self.assertEqual(sorted(n for n in os.listdir(self.dir) if n.endswith("_perl")), ["a.param_perl", "b.param_perl", "c.param_perl"])
        shutil.rmtree(self.dir); os.mkdir(self.dir)
        for name in ("a.param", "b.param"):
            self.write(name, self.PERL_JOB)
        self.run_main(self.dir, answers=["q"])
        self.assertEqual(self.read("a.param"), self.PERL_JOB)
        self.assertEqual(self.read("b.param"), self.PERL_JOB)

    def test_files_without_perl_are_not_touched_or_asked(self):
        plain = "InputDir:/in/\nFileMask:^DR_P11.*\nFileCreationDate:<YYYY-MM-DD-1d>\n"
        self.write("plain.param", plain)
        self.write("notes.txt", PDF_MASK)
        self.assertEqual(self.run_main(self.dir), 0)
        self.assertEqual(self.asked, 0)
        self.assertEqual(self.read("plain.param"), plain)
        self.assertEqual(sorted(os.listdir(self.dir)), ["notes.txt", "plain.param"])

    def test_unconvertible_file_is_reported_and_unchanged(self):
        text = "FileMask:if (1) { my $x = 1; }\n"
        self.write("odd.param", text)
        self.assertEqual(self.run_main(self.dir), 1)
        self.assertEqual(self.asked, 0)
        self.assertIn("CANNOT convert", self.out)
        self.assertEqual(self.read("odd.param"), text)

    def test_half_convertible_file_is_not_partly_converted(self):
        text = "FileMask:" + PDF_MASK + "\nFileCreationDate:if (1) { my $x = 1; }\n"
        self.write("half.param", text)
        self.assertEqual(self.run_main(self.dir, "--yes"), 1)
        self.assertEqual(self.read("half.param"), text)

    def test_existing_backup_is_never_overwritten(self):
        self.write("job.param", self.PERL_JOB)
        self.write("job.param_perl", "older backup\n")
        self.assertEqual(self.run_main(self.dir, answers=["y"]), 1)
        self.assertEqual(self.read("job.param_perl"), "older backup\n")
        self.assertEqual(self.read("job.param"), self.PERL_JOB)

    def test_dry_run_and_yes(self):
        self.write("job.param", self.PERL_JOB)
        self.assertEqual(self.run_main(self.dir, "--dry-run"), 0)
        self.assertEqual(self.asked, 0)
        self.assertEqual(self.read("job.param"), self.PERL_JOB)
        self.assertEqual(self.run_main(self.dir, "--yes"), 0)
        self.assertEqual(self.asked, 0)
        self.assertIn("<YYYYMMDD>", self.read("job.param"))

    def test_windows_line_endings_and_permissions_survive(self):
        text = "InputDir:/in/\r\nFileMask:" + PDF_MASK + "\r\nLogFile:a.log\r\n"
        path = self.write("win.param", text)
        os.chmod(path, 0o640)
        self.assertEqual(self.run_main(self.dir, "--yes"), 0, self.err)
        self.assertEqual(self.read("win.param"), "InputDir:/in/\r\nFileMask:OK_Export_File_<YYYYMMDD>.*zip$\r\nLogFile:a.log\r\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertEqual(sorted(os.listdir(self.dir)), ["win.param", "win.param_perl"])  # no temp files left

    def test_default_folder_is_the_current_one(self):
        self.write("job.param", self.PERL_JOB)
        previous = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(self.run_main("--yes"), 0, self.err)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "job.param_perl")))

    def test_missing_folder(self):
        self.assertEqual(self.run_main(os.path.join(self.dir, "nope")), 1)

    def test_converted_job_is_accepted_by_the_transfer_tool(self):
        self.write("job.param", self.PERL_JOB + "OutputDir:/out/\nLogMode:None\n")
        self.run_main(self.dir, "--yes")
        cfg = tool.Config.from_params("job.param", tool.load_params(os.path.join(self.dir, "job.param")),
                                      datetime(2026, 5, 1, 8, 0, 0))
        self.assertEqual(cfg.file_mask_expanded, "OK_Export_File_20260501.*zip$")
        self.assertEqual(cfg.date_spec_expanded, "2026-04-30&date2026-05-01")


if __name__ == "__main__":
    unittest.main()

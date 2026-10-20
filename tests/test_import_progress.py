"""Small import fixtures and mocked UI: no audio, visible GUI or NVDA."""
import threading
import unittest
from unittest.mock import Mock, patch

import test_library
import test_plugin


class ImportWorkProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_library.LibraryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.library = self.fixture.library

    def test_zip_and_duplicate_finish_only_after_verified_library_entry(self):
        source = self.fixture.make_zip([('1.mp3', b'audio'), ('2.mp3', b'more audio')])
        events = []
        book = self.library.import_book(source, stage_progress=lambda *args: events.append(args))
        self.assertEqual(events[-1], ('complete', 5, 5))
        self.assertEqual(self.library.books(), [book])
        events.clear()
        duplicate = self.library.import_book(source, stage_progress=lambda *args: events.append(args))
        self.assertEqual(duplicate, book)
        self.assertEqual(self.library.books(), [book])
        self.assertEqual(events[-1], ('complete', 1, 1))
        self.assertEqual(self.library.track_path(book['id'], 0).read_bytes(), b'audio')
        self.assertTrue(source.is_file())

    def test_probe_save_failure_and_cancellation_never_report_complete(self):
        original = self.library.import_book(self.fixture.make_source())
        before = self.library.json_path.read_bytes()
        source = self.fixture.make_zip([('1.mp3', b'first'), ('2.mp3', b'second')])
        for failure in ('probe', 'save', 'cancel'):
            with self.subTest(failure=failure):
                events, cancel = [], threading.Event()
                def probe(path):
                    if failure == 'probe':
                        raise ValueError('Broken audio')
                    if failure == 'cancel':
                        cancel.set()
                    return 30.0
                self.library._duration_probe = probe
                original_write = self.library._write
                def write(books):
                    if failure == 'save':
                        raise OSError('Disk full')
                    return original_write(books)
                with patch.object(self.library, '_write', write):
                    with self.assertRaises((self.fixture.module.LibraryError,
                                            self.fixture.module.ImportCancelled)):
                        self.library.import_book(source, cancel=cancel,
                            stage_progress=lambda *args: events.append(args))
                self.assertTrue(all(done < total for _, done, total in events))
                self.assertEqual(self.library.books(), [original])
                self.assertEqual(self.library.json_path.read_bytes(), before)
                self.assertEqual([p.name for p in self.library.media_dir.iterdir()], [original['id']])
                self.assertTrue(source.is_file())

    def test_folder_progress_covers_copy_probe_and_commit(self):
        source = self.fixture.make_source()
        events, byte_events = [], []
        def report(stage, done, total):
            events.append((stage, done, total))
            if stage == 'complete':
                self.assertEqual(len(self.library.books()), 1)
                self.assertEqual(len(self.fixture.module.Library(self.fixture.addons).books()), 1)
        def probe(path):
            self.assertEqual(events[-1][0], 'probe')
            self.assertLess(events[-1][1], events[-1][2])
            return 60.0
        self.library._duration_probe = probe
        original_write = self.library._write
        def write(books):
            self.assertEqual(events[-1][0], 'save')
            self.assertLess(events[-1][1], events[-1][2])
            original_write(books)
        self.library._write = write
        book = self.library.import_book(source, stage_progress=report,
            progress=lambda *value: byte_events.append(value))
        fractions = [done / total for _, done, total in events]
        self.assertEqual(sorted(fractions), fractions)
        self.assertTrue(all(value < 1 for value in fractions[:-1]))
        self.assertEqual(fractions[-1], 1)
        self.assertEqual(events[-1], ('complete', 7, 7))
        self.assertEqual(byte_events[-1], (9, 9))  # old public callback unchanged
        self.assertEqual(self.library.books(), [book])
        self.assertEqual((source / '1.M4B').read_bytes(), b'one')


class ImportUiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_plugin.PluginTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.p = self.fixture.plugin
        self.p._executor.shutdown(wait=True)
        self.p._executor = Mock()
        self.jobs, self.callbacks = [], []
        self.p._executor.submit.side_effect = self.jobs.append
        self.wx = self.fixture.modules['wx']
        self.wx.CallAfter = lambda fn, *args: self.callbacks.append(lambda: fn(*args))
        self.wx.CallLater = self.fixture.later
        frame = self.fixture.modules['gui'].mainFrame
        frame.prePopup, frame.postPopup = Mock(), Mock()
        self.factory = self.fixture.modules['plugin_test.dialogs'].ImportProgressDialog
        self.window = self.factory.return_value
        self.p._controller = Mock()
        self.library = self.p._controller.library
        self.book = {'id': 'new', 'title': 'Nowa książka', 'tracks': [{}]}
        self.books = [{'id': 'old', 'title': 'Stara', 'tracks': [{}]}, self.book]
        self.library.import_book.return_value = self.book
        self.library.books.return_value = self.books
        self.p._deliver_message = Mock()

    def pump(self):
        while self.callbacks:
            self.callbacks.pop(0)()

    def test_closed_hidden_library_is_not_reused_on_completion(self):
        old = self.p._dialog = Mock()
        self.p.import_source('book.zip')
        self.p.dialog_closed(old)
        old.reset_mock()
        self.jobs.pop(0)()
        self.pump()
        old.Show.assert_not_called()
        new = self.fixture.modules['plugin_test.dialogs'].LibraryDialog.return_value
        new.Show.assert_called_once()
        new.bookList.SetSelection.assert_called_once_with(1)
        new.bookList.SetFocus.assert_called()
        self.assertIs(self.p._dialog, new)

    def test_fast_duplicate_selects_existing_book_without_restarting_it(self):
        self.library.import_book.return_value = self.books[0]
        self.p.import_source('duplicate.zip')
        self.jobs.pop(0)()
        self.pump()
        self.assertFalse(self.p._busy)
        self.assertIsNone(self.p._import_progress)
        self.p._dialog.bookList.SetSelection.assert_called_with(0)
        self.p._dialog.bookList.SetFocus.assert_called()
        self.p._controller.start.assert_not_called()
        self.window.Destroy.assert_called_once()

    def test_worker_progress_coalesces_without_gui_calls_or_per_chunk_callbacks(self):
        main_thread = threading.get_ident()
        def update(*args):
            self.assertEqual(threading.get_ident(), main_thread)
        self.window.update_progress.side_effect = update
        released = threading.Event()
        copying = threading.Event()
        def import_book(source, *, stage_progress, cancel):
            self.assertNotEqual(threading.get_ident(), main_thread)
            for index in range(10000):
                stage_progress('copy', index, 20001)
            copying.set()
            if not released.wait(3):
                raise RuntimeError('Test release timeout')
            return self.book
        self.library.import_book.side_effect = import_book
        self.p.import_source('folder')
        worker = threading.Thread(target=self.jobs.pop(0))
        worker.start()
        try:
            self.assertTrue(copying.wait(3))
            self.assertEqual(self.callbacks, [])
            self.window.update_progress.assert_called_once_with('prepare', 0, 1)
            timer = self.fixture.timers[-1]
            timer.fire()
            self.window.update_progress.assert_called_with('copy', 9999, 20001)
            self.assertEqual(len(self.fixture.timers), 3)  # save + old tick + next tick
        finally:
            released.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.callbacks), 1)  # only worker completion
        self.pump()
        self.assertIsNone(self.p._import_progress)

    def test_busy_closed_and_second_import_do_not_open_or_queue_work(self):
        self.p._busy = True
        self.p.import_source('busy.zip')
        self.factory.assert_not_called()
        self.assertEqual(self.jobs, [])
        self.p._busy = False
        self.p.import_source('first.zip')
        self.p.import_source('second.zip')
        self.factory.assert_called_once()
        self.assertEqual(len(self.jobs), 1)
        self.p.terminate()
        self.p.import_source('closed.zip')
        self.factory.assert_called_once()
        self.jobs.pop(0)()  # queued import was cancelled before worker entry
        self.library.import_book.assert_not_called()

    def test_failure_without_previous_library_does_not_create_library(self):
        self.library.import_book.side_effect = ValueError('Invalid source')
        self.p.import_source('bad.zip')
        self.jobs.pop(0)()
        self.pump()
        self.window.Destroy.assert_called_once()
        self.fixture.modules['plugin_test.dialogs'].LibraryDialog.assert_not_called()
        self.assertFalse(self.p._busy)
        self.assertIsNone(self.p._import_progress)

    def test_stale_timer_from_prior_import_cannot_touch_new_window(self):
        self.p.import_source('first.zip')
        stale = self.fixture.timers[-1]
        self.jobs.pop(0)()
        self.pump()
        new = Mock()
        self.factory.return_value = new
        self.p.import_source('second.zip')
        new.reset_mock()
        stale.callback(*stale.args)
        new.update_progress.assert_not_called()
        new.Destroy.assert_not_called()
        self.assertIs(self.p._import_progress.window, new)

    def test_reentrant_import_while_showing_progress_cannot_create_second_window(self):
        entered = []
        def show():
            if not entered:
                entered.append(True)
                self.p.import_source('second.zip')
        self.window.Show.side_effect = show
        self.p.import_source('first.zip')
        self.factory.assert_called_once()
        self.assertEqual(len(self.jobs), 1)
        self.jobs.pop(0)()
        self.pump()
        self.assertEqual(self.library.import_book.call_args.args, ('first.zip',))

    def test_start_failure_cleans_window_restores_library_and_allows_retry(self):
        old = self.p._dialog = Mock()
        for where in ('show', 'submit'):
            with self.subTest(where=where):
                old.reset_mock()
                self.window.reset_mock()
                if where == 'show':
                    self.window.Show.side_effect = RuntimeError('Cannot show')
                else:
                    self.p._executor.submit.side_effect = RuntimeError('Cannot submit')
                try:
                    self.p.import_source('book.zip')
                    self.assertFalse(self.p._busy)
                    self.assertIsNone(self.p._import_progress)
                    self.window.Destroy.assert_called_once()
                    old.Show.assert_called_once()
                    self.assertTrue(all(t.stopped for t in self.fixture.timers[1:]))
                finally:
                    self.window.Show.side_effect = None
                    self.p._executor.submit.side_effect = self.jobs.append
        self.p.import_source('retry.zip')
        self.assertTrue(self.p._busy)
        self.assertEqual(len(self.jobs), 1)

    def test_open_library_during_import_raises_progress_not_hidden_library(self):
        old = self.p._dialog = Mock()
        self.p.import_source('book.zip')
        self.window.reset_mock()
        self.fixture.module.GlobalPlugin.show_library(self.p)
        self.window.Show.assert_called_once()
        self.window.Raise.assert_called_once()
        old.Raise.assert_not_called()
        old.bookList.SetSelection.assert_not_called()
        self.assertEqual(len(self.jobs), 1)

    def test_terminate_cleans_progress_and_rejects_queued_completion_and_timer(self):
        self.p.import_source('book.zip')
        state = self.p._import_progress
        timer = self.fixture.timers[-1]
        self.jobs.pop(0)()  # completed worker, undelivered UI result
        self.p.terminate()
        self.assertTrue(self.p._cancel.is_set())
        self.assertIsNone(self.p._import_progress)
        self.window.Destroy.assert_called_once()
        self.assertTrue(timer.stopped)
        self.pump()
        timer.callback(*timer.args)
        state.report('copy', 1, 3)
        self.fixture.modules['plugin_test.dialogs'].LibraryDialog.assert_not_called()
        self.p._deliver_message.assert_not_called()
        self.window.Destroy.assert_called_once()

    def test_failure_restores_previous_library_and_reports_error(self):
        old = self.p._dialog = Mock()
        self.library.import_book.side_effect = ValueError('Uszkodzony ZIP')
        self.p.import_source('bad.zip')
        timer = self.fixture.timers[-1]
        self.jobs.pop(0)()
        self.pump()
        self.assertFalse(self.p._busy)
        self.window.Destroy.assert_called_once()
        self.assertIsNone(self.p._import_progress)
        self.assertTrue(timer.stopped)
        old.Show.assert_called_once()
        old.bookList.SetFocus.assert_called_once()
        old.refresh.assert_not_called()
        self.assertIn('Uszkodzony ZIP', self.p._deliver_message.call_args.args[0])
        timer.callback(*timer.args)  # already-queued timer must be inert
        self.window.Destroy.assert_called_once()

    def test_import_shows_modeless_progress_then_selects_library_without_play(self):
        old = self.p._dialog = Mock()
        self.p.import_source('book.zip')
        self.factory.assert_called_once()
        self.window.Show.assert_called_once()
        self.window.ShowModal.assert_not_called()
        old.Hide.assert_called_once()
        self.library.import_book.assert_not_called()
        self.assertEqual(len(self.jobs), 1)
        self.jobs.pop(0)()
        self.assertTrue(self.p._busy)  # worker never changes UI-owned state
        self.pump()
        self.window.Destroy.assert_called_once()
        self.assertFalse(self.p._busy)
        old.refresh.assert_called_once_with(self.books)
        old.Show.assert_called_once()
        old.bookList.SetSelection.assert_called_once_with(1)
        old.bookList.SetFocus.assert_called()
        self.p._controller.start.assert_not_called()
        self.assertTrue(all(timer.stopped for timer in self.fixture.timers[1:]))


if __name__ == '__main__':
    unittest.main()

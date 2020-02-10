# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import List, Union

import aqt
from anki import hooks
from anki.lang import _
from anki.rsbackend import (
    DBError,
    Interrupted,
    MediaSyncProgress,
    NetworkError,
    NetworkErrorKind,
    Progress,
    ProgressKind,
    SyncError,
    SyncErrorKind,
)
from anki.types import assert_impossible
from anki.utils import intTime
from aqt import gui_hooks
from aqt.qt import QDialog, QDialogButtonBox, QPushButton
from aqt.utils import showWarning

LogEntry = Union[MediaSyncProgress, str]


@dataclass
class LogEntryWithTime:
    time: int
    entry: LogEntry


class MediaSyncer:
    def __init__(self, mw: aqt.main.AnkiQt):
        self.mw = mw
        self._syncing: bool = False
        self._log: List[LogEntryWithTime] = []
        self._want_stop = False
        hooks.rust_progress_callback.append(self._on_rust_progress)
        gui_hooks.media_sync_did_start_or_stop.append(self._on_start_stop)

    def _on_rust_progress(self, proceed: bool, progress: Progress) -> bool:
        if progress.kind != ProgressKind.MediaSyncProgress:
            return proceed

        self._log_and_notify(progress.val)

        if self._want_stop:
            return False
        else:
            return proceed

    def start(self) -> None:
        "Start media syncing in the background, if it's not already running."
        if self._syncing:
            return

        hkey = self.mw.pm.sync_key()
        if hkey is None:
            return

        if not self.mw.pm.media_syncing_enabled():
            self._log_and_notify(_("Media syncing disabled."))
            return

        self._log_and_notify(_("Media sync starting..."))
        self._syncing = True
        self._want_stop = False
        gui_hooks.media_sync_did_start_or_stop(True)

        def run() -> None:
            self.mw.col.backend.sync_media(hkey, self._endpoint())

        self.mw.taskman.run_in_background(run, self._on_finished)

    def _endpoint(self) -> str:
        shard = self.mw.pm.sync_shard()
        if shard is not None:
            shard_str = str(shard)
        else:
            shard_str = ""
        return f"https://sync{shard_str}.ankiweb.net/msync/"

    def _log_and_notify(self, entry: LogEntry) -> None:
        entry_with_time = LogEntryWithTime(time=intTime(), entry=entry)
        self._log.append(entry_with_time)
        self.mw.taskman.run_on_main(
            lambda: gui_hooks.media_sync_did_progress(entry_with_time)
        )

    def _on_finished(self, future: Future) -> None:
        self._syncing = False
        gui_hooks.media_sync_did_start_or_stop(False)

        exc = future.exception()
        if exc is not None:
            self._handle_sync_error(exc)
        else:
            self._log_and_notify(_("Media sync complete."))

    def _handle_sync_error(self, exc: BaseException):
        if isinstance(exc, Interrupted):
            self._log_and_notify(_("Media sync aborted."))
            return

        self._log_and_notify(_("Media sync failed."))
        if isinstance(exc, SyncError):
            kind = exc.kind()
            if kind == SyncErrorKind.AUTH_FAILED:
                self.mw.pm.set_sync_key(None)
                showWarning(
                    _("AnkiWeb ID or password was incorrect; please try again.")
                )
            elif kind == SyncErrorKind.SERVER_ERROR:
                showWarning(
                    _(
                        "AnkiWeb encountered a problem. Please try again in a few minutes."
                    )
                )
            elif kind == SyncErrorKind.MEDIA_CHECK_REQUIRED:
                showWarning(_("Please use the Tools>Check Media menu option."))
            elif kind == SyncErrorKind.RESYNC_REQUIRED:
                showWarning(
                    _(
                        "Please sync again, and post on the support forum if this message keeps appearing."
                    )
                )
            else:
                showWarning(_("Unexpected error: {}").format(str(exc)))
        elif isinstance(exc, NetworkError):
            nkind = exc.kind()
            if nkind in (NetworkErrorKind.OFFLINE, NetworkErrorKind.TIMEOUT):
                showWarning(
                    _("Syncing failed; please check your internet connection.")
                    + "\n\n"
                    + _("Detailed error: {}").format(str(exc))
                )
            else:
                showWarning(_("Unexpected error: {}").format(str(exc)))
        elif isinstance(exc, DBError):
            showWarning(_("Problem accessing the media database: {}").format(str(exc)))
        else:
            raise exc

    def entries(self) -> List[LogEntryWithTime]:
        return self._log

    def abort(self) -> None:
        if not self.is_syncing():
            return
        self._log_and_notify(_("Media sync aborting..."))
        self._want_stop = True

    def is_syncing(self) -> bool:
        return self._syncing

    def _on_start_stop(self, running: bool):
        self.mw.toolbar.set_sync_active(running)  # type: ignore

    def show_sync_log(self):
        aqt.dialogs.open("sync_log", self.mw, self)

    def show_diag_until_finished(self):
        # nothing to do if not syncing
        if not self.is_syncing():
            return

        diag: MediaSyncDialog = aqt.dialogs.open("sync_log", self.mw, self, True)
        diag.exec_()

    def seconds_since_last_sync(self) -> int:
        if self.is_syncing():
            return 0

        if self._log:
            last = self._log[-1].time
        else:
            last = 0
        return intTime() - last


class MediaSyncDialog(QDialog):
    silentlyClose = True

    def __init__(
        self, mw: aqt.main.AnkiQt, syncer: MediaSyncer, close_when_done: bool = False
    ) -> None:
        super().__init__(mw)
        self.mw = mw
        self._syncer = syncer
        self._close_when_done = close_when_done
        self.form = aqt.forms.synclog.Ui_Dialog()
        self.form.setupUi(self)
        self.abort_button = QPushButton(_("Abort"))
        self.abort_button.clicked.connect(self._on_abort)  # type: ignore
        self.abort_button.setAutoDefault(False)
        self.form.buttonBox.addButton(self.abort_button, QDialogButtonBox.ActionRole)

        gui_hooks.media_sync_did_progress.append(self._on_log_entry)
        gui_hooks.media_sync_did_start_or_stop.append(self._on_start_stop)

        self.form.plainTextEdit.setPlainText(
            "\n".join(self._entry_to_text(x) for x in syncer.entries())
        )
        self.show()

    def reject(self) -> None:
        if self._close_when_done and self._syncer.is_syncing():
            # closing while syncing on close starts an abort
            self._on_abort()
            return

        aqt.dialogs.markClosed("sync_log")
        QDialog.reject(self)

    def reopen(self, mw, syncer, close_when_done: bool = False) -> None:
        self._close_when_done = close_when_done
        self.show()

    def _on_abort(self, *args) -> None:
        self._syncer.abort()
        self.abort_button.setHidden(True)

    def _time_and_text(self, stamp: int, text: str) -> str:
        asctime = time.asctime(time.localtime(stamp))
        return f"{asctime}: {text}"

    def _entry_to_text(self, entry: LogEntryWithTime):
        if isinstance(entry.entry, str):
            txt = entry.entry
        elif isinstance(entry.entry, MediaSyncProgress):
            txt = self._logentry_to_text(entry.entry)
        else:
            assert_impossible(entry.entry)
        return self._time_and_text(entry.time, txt)

    def _logentry_to_text(self, e: MediaSyncProgress) -> str:
        return _(
            "Added: %(a_up)s↑ %(a_dwn)s↓, Removed: %(r_up)s↑ %(r_dwn)s↓, Checked: %(chk)s"
        ) % dict(
            a_up=e.uploaded_files,
            a_dwn=e.downloaded_files,
            r_up=e.uploaded_deletions,
            r_dwn=e.downloaded_deletions,
            chk=e.checked,
        )

    def _on_log_entry(self, entry: LogEntryWithTime):
        self.form.plainTextEdit.appendPlainText(self._entry_to_text(entry))
        if not self._syncer.is_syncing():
            self.abort_button.setHidden(True)

    def _on_start_stop(self, running: bool) -> None:
        if not running and self._close_when_done:
            aqt.dialogs.markClosed("sync_log")
            self._close_when_done = False
            self.close()
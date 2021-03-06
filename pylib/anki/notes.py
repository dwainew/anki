# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

import anki  # pylint: disable=unused-import
from anki import hooks
from anki.models import NoteType
from anki.rsbackend import BackendNote
from anki.utils import fieldChecksum, joinFields, splitFields, stripHTMLMedia


class Note:
    # not currently exposed
    flags = 0
    data = ""

    def __init__(
        self,
        col: anki.collection.Collection,
        model: Optional[NoteType] = None,
        id: Optional[int] = None,
    ) -> None:
        assert not (model and id)
        self.col = col.weakref()
        # self.newlyAdded = False

        if id:
            # existing note
            self.id = id
            self.load()
        else:
            # new note for provided notetype
            self._load_from_backend_note(self.col.backend.new_note(model["id"]))

    def load(self) -> None:
        n = self.col.backend.get_note(self.id)
        assert n
        self._load_from_backend_note(n)

    def _load_from_backend_note(self, n: BackendNote) -> None:
        self.id = n.id
        self.guid = n.guid
        self.mid = n.ntid
        self.mod = n.mtime_secs
        self.usn = n.usn
        self.tags = list(n.tags)
        self.fields = list(n.fields)
        self._fmap = self.col.models.fieldMap(self.model())

    def to_backend_note(self) -> BackendNote:
        hooks.note_will_flush(self)
        return BackendNote(
            id=self.id,
            guid=self.guid,
            ntid=self.mid,
            mtime_secs=self.mod,
            usn=self.usn,
            tags=self.tags,
            fields=self.fields,
        )

    def flush(self) -> None:
        assert self.id != 0
        self.col.backend.update_note(self.to_backend_note())

    def joinedFields(self) -> str:
        return joinFields(self.fields)

    def cards(self) -> List[anki.cards.Card]:
        return [
            self.col.getCard(id)
            for id in self.col.db.list(
                "select id from cards where nid = ? order by ord", self.id
            )
        ]

    def model(self) -> Optional[NoteType]:
        return self.col.models.get(self.mid)

    _model = property(model)

    def cloze_numbers_in_fields(self) -> Sequence[int]:
        return self.col.backend.cloze_numbers_in_note(self.to_backend_note())

    # Dict interface
    ##################################################

    def keys(self) -> List[str]:
        return list(self._fmap.keys())

    def values(self) -> List[str]:
        return self.fields

    def items(self) -> List[Tuple[Any, Any]]:
        return [(f["name"], self.fields[ord]) for ord, f in sorted(self._fmap.values())]

    def _fieldOrd(self, key: str) -> Any:
        try:
            return self._fmap[key][0]
        except:
            raise KeyError(key)

    def __getitem__(self, key: str) -> str:
        return self.fields[self._fieldOrd(key)]

    def __setitem__(self, key: str, value: str) -> None:
        self.fields[self._fieldOrd(key)] = value

    def __contains__(self, key) -> bool:
        return key in self._fmap

    # Tags
    ##################################################

    def hasTag(self, tag: str) -> Any:
        return self.col.tags.inList(tag, self.tags)

    def stringTags(self) -> Any:
        return self.col.tags.join(self.col.tags.canonify(self.tags))

    def setTagsFromStr(self, tags: str) -> None:
        self.tags = self.col.tags.split(tags)

    def delTag(self, tag: str) -> None:
        rem = []
        for t in self.tags:
            if t.lower() == tag.lower():
                rem.append(t)
        for r in rem:
            self.tags.remove(r)

    def addTag(self, tag: str) -> None:
        # duplicates will be stripped on save
        self.tags.append(tag)

    # Unique/duplicate check
    ##################################################

    def dupeOrEmpty(self) -> int:
        "1 if first is empty; 2 if first is a duplicate, False otherwise."
        val = self.fields[0]
        if not val.strip():
            return 1
        csum = fieldChecksum(val)
        # find any matching csums and compare
        for flds in self.col.db.list(
            "select flds from notes where csum = ? and id != ? and mid = ?",
            csum,
            self.id or 0,
            self.mid,
        ):
            if stripHTMLMedia(splitFields(flds)[0]) == stripHTMLMedia(self.fields[0]):
                return 2
        return False

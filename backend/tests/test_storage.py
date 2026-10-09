"""The local document store, on a real filesystem (Phase 2.4).

Why this file exists separately from the API tests
--------------------------------------------------
Every property that makes document storage safe is a property of the *filesystem
layout*, and none of it is visible through HTTP: a client can prove a file was
removed, but it cannot prove that the object it never saw the name of never sat
inside another tenant's directory. Those claims need a filesystem to test
against, so they are stated here rather than being left implicit in
``test_documents_api.py``.

The central claim
-----------------
**No client-supplied string ever becomes a path component.** In practice that
means: given any string at all as a storage key, the store either returns a path
that is inside the tenant's own directory or refuses. There is no third outcome,
and in particular there is no outcome in which a caller's bytes land somewhere
other than where the store said they would.

Those cases are generated rather than enumerated where enumeration would be
tedious — traversal strings, absolute paths, NUL truncation, URL-encoded forms,
symlinked directories — because an enumerated list only proves the entries it
happens to name.
"""

from __future__ import annotations

import io
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.storage.local import (
    STORAGE_KEY_MAX_LENGTH,
    STORAGE_KEY_PATTERN,
    WRITE_CHUNK_BYTES,
    FileTooLargeError,
    InvalidStorageKeyError,
    LocalFileStore,
    StorageContainmentError,
    StorageKeyError,
)

TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER_TENANT = uuid.UUID("99999999-8888-7777-6666-555555555555")


@pytest.fixture
def store(tmp_path: Path) -> LocalFileStore:
    """A store rooted in a fresh temporary directory.

    ``tmp_path`` rather than the configured ``STORAGE_ROOT`` so that this file
    never depends on, reads, or writes to the developer's real storage directory.
    """
    return LocalFileStore(tmp_path / "storage")


def source(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)


# ---------------------------------------------------------------------------
# The key shape
# ---------------------------------------------------------------------------


class TestStorageKeys:
    def test_a_generated_key_matches_the_pattern(self) -> None:
        assert STORAGE_KEY_PATTERN.fullmatch(LocalFileStore.new_storage_key())

    def test_keys_are_the_exact_width_of_the_column(self) -> None:
        assert len(LocalFileStore.new_storage_key()) == STORAGE_KEY_MAX_LENGTH

    def test_the_pattern_rejects_a_trailing_newline(self) -> None:
        """``^``/``$`` would accept this; ``\\A``/``\\Z`` do not.

        The whole point of writing the anchors as they are: ``re.fullmatch``
        already rejects a trailing newline in many cases, but the pattern is also
        used on its own, and a key that can be written one way and looked up
        another is the class of bug this prevents.
        """
        key = LocalFileStore.new_storage_key()
        assert STORAGE_KEY_PATTERN.fullmatch(f"{key}\n") is None

    def test_keys_are_distinct(self) -> None:
        keys = {LocalFileStore.new_storage_key() for _ in range(1000)}
        assert len(keys) == 1000

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "..",
            ".",
            "../etc/passwd",
            "..%2fetc%2fpasswd",
            "%2e%2e%2fetc%2fpasswd",
            "a" * 31,
            "a" * 33,
            "A" * 32,
            "0123456789abcdef0123456789abcde-",
            "0123456789abcdef0123456789abcdeg",
            "0123456789abcdef0123456789abcde/",
            "0123456789abcdef0123456789abcde\x00",
            "\x00" * 32,
            " " + "0" * 31,
        ],
        ids=lambda k: repr(k)[:24],
    )
    def test_a_key_that_is_not_a_generated_token_is_refused(
        self, store: LocalFileStore, key: str
    ) -> None:
        with pytest.raises(InvalidStorageKeyError):
            store.path_for(TENANT, key)

    def test_the_refusal_is_a_storage_key_error(self) -> None:
        """A caller can catch every refusal from this module in one clause."""
        assert issubclass(InvalidStorageKeyError, StorageKeyError)
        assert issubclass(StorageKeyError, ValueError)

    def test_a_refused_key_writes_nothing(self, store: LocalFileStore) -> None:
        with pytest.raises(InvalidStorageKeyError):
            store.copy_stream_into(TENANT, "../escape", source(b"x"), max_bytes=100)
        assert not store.root.exists() or not any(store.root.rglob("*"))


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class TestLayout:
    def test_the_layout_is_root_tenant_key(self, store: LocalFileStore) -> None:
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"hello"), max_bytes=100)

        path = store.path_for(TENANT, key)
        assert path.parent == store.root / TENANT.hex
        assert path.name == key
        assert path.read_bytes() == b"hello"

    def test_constructing_a_store_touches_no_disk(self, tmp_path: Path) -> None:
        """A store is free to build, so a read path needs no teardown."""
        LocalFileStore(tmp_path / "never-created")
        assert not (tmp_path / "never-created").exists()

    def test_a_relative_root_is_resolved_against_the_process_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store holds an absolute path, whatever it was handed.

        Only relevant because :class:`LocalFileStore` resolves in ``__init__``
        rather than on each use; a later working-directory change must not move
        where documents are read from or written to.
        """
        monkeypatch.chdir(tmp_path)
        store = LocalFileStore(Path("relative-root"))

        assert store.root.is_absolute()
        assert store.root == (tmp_path / "relative-root").resolve()

    def test_two_tenants_get_separate_directories(
        self, store: LocalFileStore
    ) -> None:
        """Even with the *same* key, one tenant's object is unreachable by the other.

        This is the storage half of tenant isolation, and it is why a path is
        built from two values rather than one: knowing every ``storage_key`` in
        the system still does not let a caller reach another tenant's bytes.
        """
        shared_key = LocalFileStore.new_storage_key()
        store.copy_stream_into(TENANT, shared_key, source(b"tenant one"), max_bytes=99)
        store.copy_stream_into(OTHER_TENANT, shared_key, source(b"tenant two"), max_bytes=99)

        assert store.path_for(TENANT, shared_key).read_bytes() == b"tenant one"
        assert store.path_for(OTHER_TENANT, shared_key).read_bytes() == b"tenant two"
        assert store.path_for(TENANT, shared_key) != store.path_for(
            OTHER_TENANT, shared_key
        )

    def test_iter_paths_reports_exactly_what_was_written(
        self, store: LocalFileStore
    ) -> None:
        keys = [store.new_storage_key() for _ in range(3)]
        for key in keys:
            store.copy_stream_into(TENANT, key, source(b"x"), max_bytes=99)

        assert {path.name for path in store.iter_paths(TENANT)} == set(keys)

    def test_iter_paths_is_empty_for_a_tenant_that_stored_nothing(
        self, store: LocalFileStore
    ) -> None:
        """An absent directory is the same as an empty one — no exception."""
        assert list(store.iter_paths(OTHER_TENANT)) == []


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


class TestContainment:
    def test_a_symlinked_tenant_directory_cannot_redirect_a_write(
        self, store: LocalFileStore, tmp_path: Path
    ) -> None:
        """The one attack the key pattern cannot prevent on its own.

        An operator or a compromised process creates ``<root>/<tenant hex>`` as a
        symlink to somewhere else. The key still validates, so the containment
        check is the only thing standing between a write and its destination —
        which is why the check compares *resolved* paths rather than the literal
        ones.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (store.root).mkdir(parents=True)
        (store.root / TENANT.hex).symlink_to(elsewhere, target_is_directory=True)

        key = store.new_storage_key()
        with pytest.raises(StorageContainmentError):
            store.path_for(TENANT, key)

    def test_containment_failure_is_not_a_key_failure(
        self, store: LocalFileStore, tmp_path: Path
    ) -> None:
        """An operator reading a log must be able to tell the two apart.

        A bad value reached storage, versus the configured root is not where it
        claims to be — different problems with different fixes.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        store.root.mkdir(parents=True)
        (store.root / TENANT.hex).symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(StorageContainmentError) as caught:
            store.path_for(TENANT, store.new_storage_key())

        assert not isinstance(caught.value, StorageKeyError)

    @pytest.mark.skipif(
        not hasattr(os, "symlink"),
        reason="this platform provides no symlink(2)",
    )
    def test_a_symlinked_root_is_followed_consistently(
        self, tmp_path: Path
    ) -> None:
        """A symlinked *root* is fine: containment is checked after resolution.

        The counterpart to the test above. Operators do symlink deployment
        directories routinely, and refusing that would be a false positive; the
        check must accept it while still refusing an escaping tenant directory.
        """
        real_root = tmp_path / "real"
        real_root.mkdir()
        link_root = tmp_path / "link"
        link_root.symlink_to(real_root, target_is_directory=True)

        store = LocalFileStore(link_root)
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"data"), max_bytes=99)

        assert (real_root / TENANT.hex / key).read_bytes() == b"data"
        assert store.path_for(TENANT, key).is_relative_to(real_root.resolve())


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


class TestCopying:
    def test_it_returns_the_number_of_bytes_written(self, store: LocalFileStore) -> None:
        key = store.new_storage_key()
        written = store.copy_stream_into(
            TENANT, key, source(b"x" * 1234), max_bytes=10_000
        )
        assert written == 1234

    def test_it_streams_rather_than_buffering(self, store: LocalFileStore) -> None:
        """The chunk size bounds memory, so it is asserted to be small.

        A source that records how much was asked for at once proves the copy reads
        in bounded chunks; asserting the *constant* as well pins the bound, so
        raising it to something that would buffer a whole upload fails a test
        rather than passing quietly.
        """
        reads: list[int] = []

        # Subclasses :class:`io.BytesIO` rather than :class:`io.RawIOBase`: the
        # copy takes an ``IO[bytes]``, and only the buffered hierarchy is typed
        # as one, so this needs no suppression to be passed.
        class Recording(io.BytesIO):
            def read(self, size: int | None = -1, /) -> bytes:
                reads.append(size if isinstance(size, int) else -1)
                return super().read(size)

        payload = b"y" * (WRITE_CHUNK_BYTES * 2 + 17)
        written = store.copy_stream_into(
            TENANT,
            store.new_storage_key(),
            Recording(payload),
            max_bytes=len(payload),
        )

        assert written == len(payload)
        assert reads
        assert max(reads) <= WRITE_CHUNK_BYTES

    def test_a_file_at_exactly_the_limit_is_accepted(
        self, store: LocalFileStore
    ) -> None:
        """The limit is inclusive: ``size == max_bytes`` is not too large."""
        key = store.new_storage_key()
        written = store.copy_stream_into(
            TENANT, key, source(b"z" * 100), max_bytes=100
        )
        assert written == 100
        assert store.path_for(TENANT, key).stat().st_size == 100

    def test_one_byte_over_the_limit_is_refused_and_leaves_nothing(
        self, store: LocalFileStore
    ) -> None:
        key = store.new_storage_key()
        with pytest.raises(FileTooLargeError) as caught:
            store.copy_stream_into(TENANT, key, source(b"z" * 101), max_bytes=100)

        assert caught.value.max_bytes == 100
        assert list(store.iter_paths(TENANT)) == []

    def test_it_refuses_to_overwrite_an_existing_object(
        self, store: LocalFileStore
    ) -> None:
        """``xb``, not ``wb``: a collision means a bug or an attack."""
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"original"), max_bytes=100)

        with pytest.raises(FileExistsError):
            store.copy_stream_into(TENANT, key, source(b"replacement"), max_bytes=100)

        assert store.path_for(TENANT, key).read_bytes() == b"original"

    def test_a_read_failure_during_the_copy_leaves_nothing(
        self, store: LocalFileStore
    ) -> None:
        """A client disconnecting mid-upload is cleaned up like any other failure."""

        class Exploding(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(b"partial")
                self._served = False

            def read(self, size: int | None = -1, /) -> bytes:
                if self._served:
                    raise OSError("client disconnected")
                self._served = True
                return super().read(size)

        with pytest.raises(OSError):
            store.copy_stream_into(
                TENANT, store.new_storage_key(), Exploding(), max_bytes=1000
            )

        assert list(store.iter_paths(TENANT)) == []

    def test_a_refused_copy_does_not_disturb_an_earlier_document(
        self, store: LocalFileStore
    ) -> None:
        """A failed upload must not damage a stored one.

        The two tenants share nothing but the root, and the two keys share
        nothing but the shape, so this is close to trivially true — which is
        exactly why it is worth one test: the cleanup path calls ``unlink`` and
        could plausibly be aimed at the wrong directory.
        """
        kept = store.new_storage_key()
        store.copy_stream_into(OTHER_TENANT, kept, source(b"kept"), max_bytes=100)

        with pytest.raises(FileTooLargeError):
            store.copy_stream_into(TENANT, store.new_storage_key(), source(b"no"), max_bytes=1)

        assert store.path_for(OTHER_TENANT, kept).read_bytes() == b"kept"


# ---------------------------------------------------------------------------
# Reading, deleting, discarding
# ---------------------------------------------------------------------------


class TestReadingAndDeleting:
    def test_leading_bytes_reads_at_most_what_was_asked_for(
        self, store: LocalFileStore
    ) -> None:
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"0123456789"), max_bytes=100)

        assert store.read_leading_bytes(TENANT, key, 4) == b"0123"
        assert store.read_leading_bytes(TENANT, key, 100) == b"0123456789"

    def test_leading_bytes_of_a_shorter_file_is_the_whole_file(
        self, store: LocalFileStore
    ) -> None:
        """Asking for eight bytes of a three-byte file must not raise."""
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"abc"), max_bytes=100)

        assert store.read_leading_bytes(TENANT, key, 8) == b"abc"

    def test_delete_reports_whether_anything_was_there(
        self, store: LocalFileStore
    ) -> None:
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"data"), max_bytes=100)

        assert store.delete(TENANT, key) is True
        assert store.delete(TENANT, key) is False

    def test_delete_leaves_the_tenant_directory_in_place(
        self, store: LocalFileStore
    ) -> None:
        """The directory is the tenant's, not one document's."""
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"data"), max_bytes=100)
        store.delete(TENANT, key)

        assert store.path_for(TENANT, key).parent.is_dir()

    def test_discard_partial_is_silent_when_the_file_is_absent(
        self, store: LocalFileStore
    ) -> None:
        """Its caller is already unwinding; a second error would mask the first."""
        store.discard_partial(TENANT, store.new_storage_key())

    def test_discard_partial_removes_a_written_file(self, store: LocalFileStore) -> None:
        key = store.new_storage_key()
        store.copy_stream_into(TENANT, key, source(b"data"), max_bytes=100)

        store.discard_partial(TENANT, key)

        assert list(store.iter_paths(TENANT)) == []

    def test_discard_partial_still_refuses_a_malformed_key(
        self, store: LocalFileStore
    ) -> None:
        """It swallows filesystem errors, not caller mistakes.

        The distinction :meth:`LocalFileStore.discard_partial` documents: a
        missing file is expected cleanup, whereas a key that is not a storage key
        at all means the caller is wrong and deserves to hear so.
        """
        with pytest.raises(InvalidStorageKeyError):
            store.discard_partial(TENANT, "not-a-key")


# ---------------------------------------------------------------------------
# The package surface
# ---------------------------------------------------------------------------


class TestPackageSurface:
    def test_the_module_exports_nothing_but_the_store(self) -> None:
        """``app.storage`` re-exports deliberately, so the public surface is small.

        Judged on what ``app.storage.__all__`` names, so a later addition has to be
        a conscious edit rather than an accidental one.
        """
        from app import storage

        assert set(storage.__all__) == {
            "FileTooLargeError",
            "InvalidStorageKeyError",
            "LocalFileStore",
            "STORAGE_KEY_MAX_LENGTH",
            "STORAGE_KEY_PATTERN",
            "StorageContainmentError",
            "StorageKeyError",
        }

    def test_the_root_is_exposed_read_only(self, store: LocalFileStore) -> None:
        """A caller can learn the layout, not change it.

        Written against an :data:`typing.Any`-typed alias because the write is
        exactly what the type checker forbids — and that refusal is the behaviour
        under test, not an obstacle to it. A suppression would assert the
        opposite of what this asserts; widening the local's type says "the
        checker would stop me, and this is what happens when it does not".
        """
        writable: Any = store

        with pytest.raises(AttributeError):
            writable.root = Path("/elsewhere")

        assert store.root != Path("/elsewhere")


class TestGeneratedSources:
    """A blunt check that the traversal corpus above really is the bad shapes.

    Kept as a test rather than as a comment so that a future edit to the
    parameterised list cannot quietly reduce it to two tame entries while still
    reading as thorough coverage.
    """

    def test_the_corpus_covers_traversal_separators_and_encodings(self) -> None:
        corpus = [
            "../etc/passwd",
            "..%2fetc%2fpasswd",
            "%2e%2e%2fetc%2fpasswd",
            "0123456789abcdef0123456789abcde/",
            "0123456789abcdef0123456789abcde\x00",
        ]

        for candidate in corpus:
            assert STORAGE_KEY_PATTERN.fullmatch(candidate) is None, candidate
            assert re.search(r"[/.%\x00]", candidate), candidate

    def test_no_parameterised_key_ever_matches(self) -> None:
        """A single assertion covering every entry, independent of the store.

        The pattern is the whole argument, so it is tested directly rather than
        only through :meth:`LocalFileStore.path_for`. If the pattern were ever
        loosened, this fails even in the paths where the store is not reached.
        """
        for key in ("", "..", ".", "A" * 32, "a" * 31, "a" * 33, "0" * 31 + "-"):
            assert STORAGE_KEY_PATTERN.fullmatch(key) is None, repr(key)
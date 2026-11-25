from __future__ import annotations

import multiprocessing
import sqlite3
from dataclasses import replace

import pytest

from auto_coder.merge_operation_state import (
    MAX_THROTTLED_RETRIES_AFTER_FIRST,
    MIN_LOCAL_RETRY_INTERVAL_SECONDS,
    BlockReason,
    ConfirmationSource,
    EffectName,
    EffectReceipt,
    EffectSenderBinding,
    EffectState,
    MergeOperationIdentity,
    MergeOperationPersistenceError,
    MergeOperationStore,
    OperationStatus,
    ReservationRefusal,
)


def _identity(pr: int = 42) -> MergeOperationIdentity:
    return MergeOperationIdentity("https://api.github.com", "acme/widgets", pr)


def _binding(*, app_id: int = 11, installation_id: int = 22, app_role: str = "controller") -> EffectSenderBinding:
    return EffectSenderBinding(app_role, "https://api.github.com", "acme/widgets", app_id, installation_id, "auto-coder[bot]")


def _paused_bound_reservation(path, ready, finish, settled):
    """Expose a committed reservation before allowing its owner to settle it."""
    store = MergeOperationStore(path)
    identity = _identity()
    reservation = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=3)
    ready.put(reservation)
    if not finish.wait(timeout=15):
        raise AssertionError("The parent did not release the reserved attempt")
    receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, target_head_sha="H", merge_commit_sha="merged")
    settled.put(store.record_confirmed_complete(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, receipt, now=4))


def _paused_same_head_refresh(path, loaded, finish, completed):
    """Pause the real refresh between its read and write in another process."""

    class PausedRefreshStore(MergeOperationStore):
        def _load_operation(self, connection, identity):
            operation = super()._load_operation(connection, identity)
            loaded.put(operation)
            if not finish.wait(timeout=15):
                raise AssertionError("The parent did not release the metadata refresh")
            return operation

    refreshed = PausedRefreshStore(path).get_or_create(_identity(), expected_head_sha="H", merge_method="merge", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=2)
    completed.put(refreshed)


def test_app_reservation_atomically_binds_sender_and_refuses_changed_principal(tmp_path, monkeypatch):
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)

    missing = store.reserve_app_attempt(identity, EffectName.MERGE, None, now=2)
    assert not missing.granted
    assert missing.refusal is ReservationRefusal.BINDING_REQUIRED
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED

    original_save_attempt = store._save_attempt
    monkeypatch.setattr(store, "_save_attempt", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("write failed")))
    with pytest.raises(MergeOperationPersistenceError):
        store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=3)
    monkeypatch.setattr(store, "_save_attempt", original_save_attempt)
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED
    assert store.get(identity).effect(EffectName.MERGE).sender_binding is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM merge_operation_effect_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM merge_operation_attempt_history").fetchone()[0] == 0

    reserved = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=4)
    assert reserved.granted
    reopened = MergeOperationStore(path)
    assert reopened.get(identity).effect(EffectName.MERGE).sender_binding == _binding()
    refused = reopened.reserve_app_attempt(identity, EffectName.MERGE, _binding(installation_id=23), now=5)
    assert refused.refusal is ReservationRefusal.PRINCIPAL_MISMATCH
    assert reopened.get(identity).effect(EffectName.MERGE).attempt_id == reserved.attempt_id


@pytest.mark.parametrize("contender", [_binding(), _binding(installation_id=23)])
def test_concurrent_app_reservations_have_one_durable_owner(tmp_path, contender):
    """AS-001: B observes A's committed binding while A is explicitly paused."""
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    prior = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=1)
    store.defer_local(identity, EffectName.MERGE, prior.attempt_id, prior.generation, is_real_throttle=True, throttle_attempt_id="throttle-1", now=2)
    context = multiprocessing.get_context("spawn")
    ready, settled = context.Queue(), context.Queue()
    finish = context.Event()
    owner = context.Process(target=_paused_bound_reservation, args=(path, ready, finish, settled))
    owner.start()
    try:
        winner = ready.get(timeout=15)
        assert winner.granted
        durable = MergeOperationStore(path).get(identity)
        assert durable == winner.operation
        exact = store.get_attempt(identity, EffectName.MERGE, winner.generation, winner.attempt_id)
        assert exact.sender_binding == _binding()

        refused = MergeOperationStore(path).reserve_app_attempt(identity, EffectName.MERGE, contender, now=3)
        assert not refused.granted
        assert refused.refusal is (ReservationRefusal.INELIGIBLE if contender == _binding() else ReservationRefusal.PRINCIPAL_MISMATCH)
        assert store.get(identity) == durable
        assert store.get_attempt(identity, EffectName.MERGE, winner.generation, winner.attempt_id) == exact

        finish.set()
        assert settled.get(timeout=15) is True
    finally:
        finish.set()
        owner.join(timeout=15)
        if owner.is_alive():
            owner.terminate()
            owner.join(timeout=5)
    assert owner.exitcode == 0
    completed = MergeOperationStore(path).get(identity)
    effect = completed.effect(EffectName.MERGE)
    assert completed.status is OperationStatus.MERGE_CONFIRMED
    assert completed.not_before == durable.not_before
    assert effect.attempt_id == winner.attempt_id
    assert effect.generation == winner.generation
    assert effect.sender_binding == _binding()
    assert effect.throttle_attempts == 1
    assert effect.throttled_attempt_ids == ("throttle-1",)
    assert store.get_attempt(identity, EffectName.MERGE, winner.generation, winner.attempt_id).receipt == effect.receipt


def test_bound_unsent_attempt_continues_only_with_same_principal_after_restart(tmp_path):
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    first = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=1)
    assert store.record_confirmed_unsent(identity, EffectName.MERGE, first.attempt_id, first.generation, now=2)

    restarted = MergeOperationStore(path)
    changed = restarted.reserve_app_attempt(identity, EffectName.MERGE, _binding(app_id=99), now=3)
    assert changed.refusal is ReservationRefusal.PRINCIPAL_MISMATCH
    continued = restarted.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=3)
    assert continued.granted
    assert continued.operation.effect(EffectName.MERGE).sender_binding == _binding()


def _create_legacy_database(path, *, merge_state=EffectState.NOT_ATTEMPTED, approval_state=EffectState.CONFIRMED_COMPLETE):
    """Create the exact pre-binding schema, including retained older history."""
    identity = _identity()
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE merge_operations (op_key TEXT PRIMARY KEY, api_origin TEXT NOT NULL, repository TEXT NOT NULL,
        pr_number INTEGER NOT NULL, expected_head_sha TEXT NOT NULL, merge_method TEXT NOT NULL,
        approval_credential_role TEXT NOT NULL, reviewer_identity TEXT NOT NULL, generation INTEGER NOT NULL,
        status TEXT NOT NULL, resume_reason TEXT NOT NULL, not_before REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE merge_operation_effects (op_key TEXT NOT NULL, effect_name TEXT NOT NULL, state TEXT NOT NULL,
        attempt_id TEXT NOT NULL, generation INTEGER NOT NULL, receipt_json TEXT NOT NULL, throttle_attempts INTEGER NOT NULL,
        throttled_attempt_ids TEXT NOT NULL, last_error TEXT NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY (op_key, effect_name));
        CREATE TABLE merge_operation_attempt_history (history_id INTEGER PRIMARY KEY AUTOINCREMENT, op_key TEXT NOT NULL,
        effect_name TEXT NOT NULL, generation INTEGER NOT NULL, attempt_id TEXT NOT NULL,
        outcome TEXT NOT NULL, detail TEXT NOT NULL, recorded_at REAL NOT NULL);
    """)
    connection.execute("INSERT INTO merge_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (identity.key(), identity.api_origin, identity.repository, identity.pr_number, "H2", "squash", "bot", "r", 2, "waiting", "legacy", 10, 1))
    approval_receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, review_id="R", reviewer_identity="legacy-reviewer", target_head_sha="H1") if approval_state is EffectState.CONFIRMED_COMPLETE else None
    connection.execute("INSERT INTO merge_operation_effects VALUES (?,?,?,?,?,?,?,?,?,?)", (identity.key(), "approval", approval_state.value, "old-a" if approval_receipt else "", 1, approval_receipt.to_json() if approval_receipt else "", 0, "[]", "", 1))
    attempt_id = "" if merge_state is EffectState.NOT_ATTEMPTED else "old-m"
    merge_receipt = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, target_head_sha="H2", merge_commit_sha="legacy-merged") if merge_state is EffectState.CONFIRMED_COMPLETE else None
    connection.execute("INSERT INTO merge_operation_effects VALUES (?,?,?,?,?,?,?,?,?,?)", (identity.key(), "merge", merge_state.value, attempt_id, 2, merge_receipt.to_json() if merge_receipt else "", 1, '["legacy-throttle"]', "legacy detail", 1))
    connection.execute("INSERT INTO merge_operation_attempt_history VALUES (?,?,?,?,?,?,?,?)", (1, identity.key(), "merge", 1, "prior-m", "confirmed_unsent", "retained original detail", 0.5))
    if attempt_id:
        connection.execute("INSERT INTO merge_operation_attempt_history VALUES (?,?,?,?,?,?,?,?)", (2, identity.key(), "merge", 2, attempt_id, merge_state.value, "retained current detail", 1))
    connection.commit()
    connection.close()
    return identity


def test_legacy_schema_migrates_as_explicitly_unbound_without_relabeling(tmp_path):
    path = tmp_path / "merge.db"
    identity = _create_legacy_database(path)

    migrated = MergeOperationStore(path).get(identity)
    assert migrated.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE
    assert migrated.effect(EffectName.APPROVAL).sender_binding is None
    assert migrated.effect(EffectName.MERGE).sender_binding is None


def test_late_bound_receipt_is_retained_only_on_original_attempt(tmp_path):
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)
    store.get_or_create(identity, expected_head_sha="H1", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    old = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=1)
    store.get_or_create(identity, expected_head_sha="H2", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=2)
    current = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(installation_id=23), now=2)
    store.defer_local(identity, EffectName.MERGE, current.attempt_id, current.generation, is_real_throttle=True, throttle_attempt_id="new-head-throttle", retry_after_seconds=20, now=2)
    current = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(installation_id=23), now=22)
    restarted = MergeOperationStore(path)
    restarted.recover_after_restart(now=23, repository=identity.repository)
    current_before_late_result = restarted.get(identity)

    wrong_head = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, target_head_sha="H2", merge_commit_sha="unrelated")
    assert not restarted.record_confirmed_complete(identity, EffectName.MERGE, old.attempt_id, old.generation, wrong_head, now=24)
    assert restarted.get_attempt(identity, EffectName.MERGE, old.generation, old.attempt_id).receipt is None

    late_receipt = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, reviewer_identity="observer", target_head_sha="H1", merge_commit_sha="old")
    assert not restarted.record_confirmed_complete(identity, EffectName.MERGE, old.attempt_id, old.generation, late_receipt, now=25)
    old_record = restarted.get_attempt(identity, EffectName.MERGE, old.generation, old.attempt_id)
    assert old_record.sender_binding == _binding()
    assert old_record.receipt == late_receipt
    assert old_record.expected_head_sha == "H1"
    assert not restarted.record_delivery_unknown(identity, EffectName.MERGE, old.attempt_id, old.generation, now=26)
    assert not restarted.record_confirmed_unsent(identity, EffectName.MERGE, old.attempt_id, old.generation, now=27)
    assert restarted.get_attempt(identity, EffectName.MERGE, old.generation, old.attempt_id).receipt == late_receipt
    assert restarted.get(identity) == current_before_late_result
    active = restarted.get(identity).effect(EffectName.MERGE)
    assert active.attempt_id == current.attempt_id
    assert active.receipt is None
    assert active.sender_binding == _binding(installation_id=23)
    current_receipt = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, target_head_sha="H2", merge_commit_sha="new")
    assert restarted.record_confirmed_complete(identity, EffectName.MERGE, current.attempt_id, current.generation, current_receipt, now=28)
    settled = MergeOperationStore(path).get(identity)
    assert settled.status is OperationStatus.MERGE_CONFIRMED
    assert settled.effect(EffectName.MERGE).receipt == current_receipt
    assert settled.effect(EffectName.MERGE).throttle_attempts == 1
    assert settled.effect(EffectName.MERGE).throttled_attempt_ids == ("new-head-throttle",)
    assert settled.not_before == current_before_late_result.not_before
    assert restarted.get_attempt(identity, EffectName.MERGE, old.generation, old.attempt_id).receipt == late_receipt


def test_as001_approved_merge_unsent_survives_restart(tmp_path):
    """AS-001: approval receipt is retained while merge alone stays waiting,
    and a scheduler retiring its own queue row must not un-execute approval."""
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)

    operation = store.get_or_create(
        identity,
        expected_head_sha="H",
        merge_method="squash",
        approval_credential_role="bot",
        reviewer_identity="reviewer-a",
        needs_approval=True,
        now=100,
    )
    assert operation.generation == 1

    reservation = store.reserve_attempt(identity, EffectName.APPROVAL, now=100)
    assert reservation.granted
    receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, review_id="R", reviewer_identity="reviewer-a", target_head_sha="H", recorded_at=100)
    assert store.record_confirmed_complete(identity, EffectName.APPROVAL, reservation.attempt_id, reservation.generation, receipt, now=100)

    merge_reservation = store.reserve_attempt(identity, EffectName.MERGE, now=101)
    assert merge_reservation.granted
    deferred = store.defer_local(identity, EffectName.MERGE, merge_reservation.attempt_id, merge_reservation.generation, is_real_throttle=False, now=101)
    assert deferred is not None
    assert deferred.status is OperationStatus.WAITING

    restarted = MergeOperationStore(path)
    reopened = restarted.get(identity)
    assert reopened is not None
    assert reopened.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE
    assert reopened.effect(EffectName.APPROVAL).receipt.review_id == "R"
    assert reopened.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED
    assert reopened.status is OperationStatus.WAITING

    # Same-target re-entry (e.g. a scheduler row being recreated) must not
    # revert the confirmed approval back to not-attempted.
    reentered = restarted.get_or_create(
        identity,
        expected_head_sha="H",
        merge_method="squash",
        approval_credential_role="bot",
        reviewer_identity="reviewer-a",
        needs_approval=True,
        now=200,
    )
    assert reentered.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE


def test_as002_running_effect_after_reopen_is_delivery_unknown(tmp_path):
    """AS-002: a crash between reservation and outcome recording must not be
    assumed unsent on reopen; only an explicitly confirmed-unsent effect is
    treated as eligible to resume once its deadline passes."""
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    assert reservation.granted
    # No terminal outcome recorded: simulate a controller stopping here.

    restarted = MergeOperationStore(path)
    recovered = restarted.recover_after_restart(now=2)
    assert len(recovered) == 1
    operation = restarted.get(identity)
    assert operation.effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # It must not be reservable again until explicitly reconciled.
    stuck = restarted.reserve_attempt(identity, EffectName.MERGE, now=3)
    assert stuck.granted is False

    # A cancellation alone (recover_after_restart again) must not convert
    # delivery-unknown into anything else.
    restarted.recover_after_restart(now=4)
    assert restarted.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # By contrast, a correlated confirmed-unsent result for a *different*
    # attempt is retried once its deadline passes.
    store2 = MergeOperationStore(tmp_path / "merge2.db")
    identity2 = _identity(7)
    store2.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation2 = store2.reserve_attempt(identity2, EffectName.MERGE, now=1)
    store2.record_confirmed_unsent(identity2, EffectName.MERGE, reservation2.attempt_id, reservation2.generation, now=1)
    retried = store2.reserve_attempt(identity2, EffectName.MERGE, now=2)
    assert retried.granted


def test_as003_stale_attempt_result_does_not_overwrite_new_head(tmp_path):
    """AS-003: a duplicate reservation is refused, and a stale attempt's late
    result (from before a head change) never mutates the new generation's
    effect or deadline, though its own evidence is kept in history."""
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H1", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)

    owner_a = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    assert owner_a.granted
    # A second entry point trying the same effect while A owns it is refused.
    owner_b = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    assert owner_b.granted is False

    # A new head arrives before A's result does.
    updated = store.get_or_create(identity, expected_head_sha="H2", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=2)
    assert updated.generation == 2
    assert updated.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED

    owner_c = store.reserve_attempt(identity, EffectName.MERGE, now=2)
    assert owner_c.granted
    assert owner_c.generation == 2

    # A's late result (old generation/attempt) arrives now.
    receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="stale-sha", target_head_sha="H1")
    applied = store.record_confirmed_complete(identity, EffectName.MERGE, owner_a.attempt_id, owner_a.generation, receipt, now=3)
    assert applied is False

    # H2's effect/state must be unaffected by A's stale result.
    current = store.get(identity)
    assert current.effect(EffectName.MERGE).state is EffectState.RUNNING
    assert current.effect(EffectName.MERGE).attempt_id == owner_c.attempt_id
    assert current.generation == 2

    # C's own result completes normally.
    receipt_c = EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="real-sha", target_head_sha="H2")
    assert store.record_confirmed_complete(identity, EffectName.MERGE, owner_c.attempt_id, owner_c.generation, receipt_c, now=4)
    assert store.get(identity).effect(EffectName.MERGE).receipt.merge_commit_sha == "real-sha"


def test_as004_deadline_never_shortened_and_throttle_counted_once_per_id(tmp_path):
    """AS-004: wakes, duplicate notifications, and metadata-only saves never
    shorten or arbitrarily extend a retained deadline; a real throttle
    redelivered under the same attempt id only spends one retry."""
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=100)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=100)

    deferred = store.defer_local(
        identity,
        EffectName.MERGE,
        reservation.attempt_id,
        reservation.generation,
        is_real_throttle=True,
        throttle_attempt_id="attempt-1",
        retry_after_seconds=0.0,
        governor_deadline=130,
        now=100,
    )
    # Prior retained deadline of 150 (simulated as already-stored not_before)
    # must not be undercut; apply a second defer using a fresh reservation
    # representing a later, already-scheduled retry that raises not_before
    # to 150 first.
    assert deferred.not_before == 130

    # A metadata-only save (merge method change) must not touch the deadline.
    refreshed = store.get_or_create(identity, expected_head_sha="H", merge_method="rebase", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=110)
    assert refreshed.not_before == 130
    assert refreshed.merge_method == "rebase"

    # Duplicate redelivery of the same real-throttle attempt id costs nothing extra.
    reservation2 = store.reserve_attempt(identity, EffectName.MERGE, now=131)
    deferred2 = store.defer_local(
        identity,
        EffectName.MERGE,
        reservation2.attempt_id,
        reservation2.generation,
        is_real_throttle=True,
        throttle_attempt_id="attempt-1",
        now=131,
    )
    assert deferred2.effect(EffectName.MERGE).throttle_attempts == 1

    # Local (pre-send) deferrals never spend a throttle attempt regardless of id reuse.
    reservation3 = store.reserve_attempt(identity, EffectName.MERGE, now=132)
    deferred3 = store.defer_local(identity, EffectName.MERGE, reservation3.attempt_id, reservation3.generation, is_real_throttle=False, now=132)
    assert deferred3.effect(EffectName.MERGE).throttle_attempts == 1
    assert deferred3.not_before == 132 + MIN_LOCAL_RETRY_INTERVAL_SECONDS

    # No deadline at all: falls back to now + floor.
    store2 = MergeOperationStore(tmp_path / "merge2.db")
    identity2 = _identity(9)
    store2.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=100)
    r = store2.reserve_attempt(identity2, EffectName.MERGE, now=100)
    d = store2.defer_local(identity2, EffectName.MERGE, r.attempt_id, r.generation, is_real_throttle=False, now=100)
    assert d.not_before == 100 + MIN_LOCAL_RETRY_INTERVAL_SECONDS

    # Real throttles: first attempt + MAX_THROTTLED_RETRIES_AFTER_FIRST more are tolerated, then blocked.
    store3 = MergeOperationStore(tmp_path / "merge3.db")
    identity3 = _identity(11)
    store3.get_or_create(identity3, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=0)
    result = None
    for i in range(MAX_THROTTLED_RETRIES_AFTER_FIRST + 2):
        res = store3.reserve_attempt(identity3, EffectName.MERGE, now=i)
        result = store3.defer_local(identity3, EffectName.MERGE, res.attempt_id, res.generation, is_real_throttle=True, throttle_attempt_id=f"a{i}", now=i)
        if result.status is OperationStatus.OPERATIONALLY_BLOCKED:
            break
    assert result.status is OperationStatus.OPERATIONALLY_BLOCKED
    assert result.resume_reason == BlockReason.RETRIES_EXHAUSTED.value


def test_as005_queue_completion_is_not_receipt_deletion(tmp_path):
    """AS-005: re-entry after a scheduler retires its own row must not create
    unexecuted operations for confirmed effects, and delivery-unknown is not
    completed merely by that re-entry either."""
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=True, now=1)
    approval_reservation = store.reserve_attempt(identity, EffectName.APPROVAL, now=1)
    store.record_confirmed_complete(identity, EffectName.APPROVAL, approval_reservation.attempt_id, approval_reservation.generation, EffectReceipt(ConfirmationSource.OWN_RESPONSE, review_id="R"), now=1)
    merge_reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    store.record_confirmed_complete(identity, EffectName.MERGE, merge_reservation.attempt_id, merge_reservation.generation, EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="sha"), now=1)

    # Re-injecting an old notification is just a get_or_create re-entry.
    reentered = store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=True, now=99)
    assert reentered.status is OperationStatus.MERGE_CONFIRMED
    assert reentered.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE
    assert reentered.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE
    assert store.reserve_attempt(identity, EffectName.APPROVAL, now=99).granted is False
    assert store.reserve_attempt(identity, EffectName.MERGE, now=99).granted is False

    # Delivery-unknown is a separate case: it is not completed by re-entry.
    identity2 = _identity(8)
    store.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    r2 = store.reserve_attempt(identity2, EffectName.MERGE, now=1)
    store.recover_after_restart(now=2)
    reentered2 = store.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=3)
    assert reentered2.effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN


def test_as006_persistence_failure_blocks_dependent_effects(tmp_path, monkeypatch):
    """AS-006: injected failures at reservation/receipt-write/reopen raise
    rather than report success, and leave existing records untouched."""
    path = tmp_path / "merge.db"
    store = MergeOperationStore(path)
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)

    original_connect = store._connect
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    try:
        raised = False
        try:
            store.reserve_attempt(identity, EffectName.MERGE, now=2)
        except MergeOperationPersistenceError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(store, "_connect", original_connect)

    # Existing record survived the injected failure untouched.
    intact = store.get(identity)
    assert intact is not None
    assert intact.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED

    # A receipt-write failure after a genuine reservation also raises rather
    # than silently completing.
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=3)
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    try:
        raised = False
        try:
            store.record_confirmed_complete(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, EffectReceipt(ConfirmationSource.OWN_RESPONSE), now=3)
        except MergeOperationPersistenceError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(store, "_connect", original_connect)
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.RUNNING


def test_reservation_refused_while_operationally_blocked(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    store.operationally_block(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, BlockReason.AUTHENTICATION, now=1)
    assert store.get(identity).status is OperationStatus.OPERATIONALLY_BLOCKED
    refused = store.reserve_attempt(identity, EffectName.MERGE, now=2)
    assert refused.granted is False


def test_manual_reset_effect_clears_only_selected_effect(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    store.operationally_block(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, BlockReason.FORBIDDEN, now=1)

    reset = store.manual_reset_effect(identity, EffectName.MERGE, now=5)
    assert reset.status is OperationStatus.WAITING
    assert reset.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED
    assert store.reserve_attempt(identity, EffectName.MERGE, now=6).granted


def test_due_lists_only_waiting_operations_past_deadline(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=100)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=100)
    store.defer_local(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, is_real_throttle=False, now=100)
    assert store.due(now=100) == []
    assert len(store.due(now=101.5)) == 1


@pytest.mark.parametrize(
    "binding",
    [
        _binding(app_role="reviewer"),
        _binding(app_role="unknown"),
        _binding(app_role=""),
        _binding(app_id=0),
        _binding(installation_id=True),
        replace(_binding(), repository="acme/other"),
        replace(_binding(), api_origin="https://github.example.test/api/v3"),
        replace(_binding(), bot_login="human"),
    ],
)
def test_invalid_effect_sender_binding_cannot_change_durable_state(tmp_path, binding):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    before = store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=True, now=1)
    refused = store.reserve_app_attempt(identity, EffectName.MERGE, binding, now=2)
    assert not refused.granted
    assert refused.refusal is ReservationRefusal.INVALID_BINDING
    assert store.get(identity) == before
    with sqlite3.connect(tmp_path / "merge.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM merge_operation_effect_attempts").fetchone()[0] == 0


def test_approval_reservation_requires_reviewer_role(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=True, now=1)
    refused = store.reserve_app_attempt(identity, EffectName.APPROVAL, _binding(), now=2)
    assert not refused.granted
    assert refused.refusal is ReservationRefusal.INVALID_BINDING
    reviewer = _binding(app_role="reviewer")
    granted = store.reserve_app_attempt(identity, EffectName.APPROVAL, reviewer, now=3)
    assert granted.granted
    assert store.get_attempt(identity, EffectName.APPROVAL, granted.generation, granted.attempt_id).sender_binding == reviewer


def test_same_principal_normalization_survives_refresh_and_restart(tmp_path):
    path = tmp_path / "merge.db"
    store = MergeOperationStore(path)
    identity = _identity()
    original = replace(_binding(), api_origin="https://api.github.com/", repository="ACME/Widgets")
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    first = store.reserve_app_attempt(identity, EffectName.MERGE, original, now=1)
    assert first.granted
    store.defer_local(identity, EffectName.MERGE, first.attempt_id, first.generation, is_real_throttle=True, throttle_attempt_id="renewal-throttle", retry_after_seconds=10, now=2)
    before = store.get(identity)
    reopened = MergeOperationStore(path)
    refreshed = reopened.get_or_create(identity, expected_head_sha="H", merge_method="merge", approval_credential_role="changed-metadata", reviewer_identity="changed-reviewer", needs_approval=True, now=3)
    assert refreshed.effects == before.effects
    assert refreshed.not_before == before.not_before
    assert refreshed.generation == before.generation
    for changed in (_binding(app_id=99), _binding(installation_id=99)):
        refused = reopened.reserve_app_attempt(identity, EffectName.MERGE, changed, now=12)
        assert refused.refusal is ReservationRefusal.PRINCIPAL_MISMATCH
        assert reopened.get(identity) == refreshed
    continued = reopened.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=12)
    assert continued.granted
    assert continued.operation.effect(EffectName.MERGE).sender_binding == before.effect(EffectName.MERGE).sender_binding
    assert continued.operation.effect(EffectName.MERGE).throttle_attempts == 1
    assert continued.operation.not_before == before.not_before


def test_bound_effect_cannot_fall_back_to_legacy_personal_reservation(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    first = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=1)
    assert store.record_confirmed_unsent(identity, EffectName.MERGE, first.attempt_id, first.generation, now=2)
    before = store.get(identity)
    fallback = store.reserve_attempt(identity, EffectName.MERGE, now=3)
    assert not fallback.granted
    assert fallback.refusal in (ReservationRefusal.BINDING_REQUIRED, ReservationRefusal.PRINCIPAL_MISMATCH)
    assert store.get(identity) == before
    personal = _identity(pr=43)
    store.get_or_create(personal, expected_head_sha="H", merge_method="squash", approval_credential_role="personal", reviewer_identity="human", needs_approval=False, now=1)
    compatible = store.reserve_attempt(personal, EffectName.MERGE, now=3)
    assert compatible.granted
    assert compatible.operation.effect(EffectName.MERGE).sender_binding is None


def test_migration_failure_rolls_back_schema_and_preserves_legacy_evidence(tmp_path, monkeypatch):
    path = tmp_path / "merge.db"
    identity = _create_legacy_database(path, merge_state=EffectState.DELIVERY_UNKNOWN)
    original_connect = sqlite3.connect
    denied = []

    def reject_attempt_backfill(*args, **kwargs):
        connection = original_connect(*args, **kwargs)

        def authorize(action, table, _column, _database, _source):
            if action == sqlite3.SQLITE_INSERT and table == "merge_operation_effect_attempts":
                denied.append(table)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    with monkeypatch.context() as scoped:
        scoped.setattr(sqlite3, "connect", reject_attempt_backfill)
        with pytest.raises(MergeOperationPersistenceError):
            MergeOperationStore(path).get(identity)
    assert denied
    with original_connect(path) as connection:
        assert "sender_binding_json" not in {row[1] for row in connection.execute("PRAGMA table_info(merge_operation_effects)")}
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='merge_operation_effect_attempts'").fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM merge_operation_attempt_history").fetchone()[0] == 2
        assert connection.execute("SELECT state FROM merge_operation_effects WHERE effect_name='merge'").fetchone()[0] == "delivery_unknown"
    resumed = MergeOperationStore(path)
    assert resumed.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN
    assert resumed.get_attempt(identity, EffectName.MERGE, 2, "old-m").sender_binding is None


@pytest.mark.parametrize("approval_state", [EffectState.CONFIRMED_COMPLETE, EffectState.NOT_NEEDED])
@pytest.mark.parametrize("merge_state", [EffectState.RUNNING, EffectState.DELIVERY_UNKNOWN])
def test_migrated_uncertain_delivery_requires_exact_positive_evidence(tmp_path, approval_state, merge_state):
    path = tmp_path / "merge.db"
    identity = _create_legacy_database(path, merge_state=merge_state, approval_state=approval_state)
    store = MergeOperationStore(path)
    migrated = store.get(identity)
    approval_before = migrated.effect(EffectName.APPROVAL)
    historical = store.get_attempt(identity, EffectName.MERGE, 1, "prior-m")
    assert historical is not None
    assert historical.sender_binding is None
    assert historical.receipt is None
    assert historical.outcome == "confirmed_unsent"
    reopened = MergeOperationStore(path)
    reopened.recover_after_restart(now=11, repository=identity.repository)
    unresolved = reopened.get(identity)
    assert unresolved.effect(EffectName.APPROVAL) == approval_before
    assert unresolved.effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN
    assert unresolved.effect(EffectName.MERGE).sender_binding is None
    assert unresolved.effect(EffectName.MERGE).throttle_attempts == 1
    assert unresolved.effect(EffectName.MERGE).throttled_attempt_ids == ("legacy-throttle",)
    refused = reopened.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=12)
    assert not refused.granted
    assert refused.refusal is ReservationRefusal.INELIGIBLE
    assert reopened.get(identity) == unresolved
    invalid = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, target_head_sha="other-head", merge_commit_sha="unrelated")
    assert not reopened.record_confirmed_complete(identity, EffectName.MERGE, "old-m", 2, invalid, now=13)
    assert reopened.get(identity) == unresolved
    observation = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, reviewer_identity="external-observer", target_head_sha="H2", merge_commit_sha="observed-merge")
    assert reopened.record_confirmed_complete(identity, EffectName.MERGE, "old-m", 2, observation, now=14)
    settled = MergeOperationStore(path).get(identity)
    assert settled.status is OperationStatus.MERGE_CONFIRMED
    assert settled.effect(EffectName.APPROVAL) == approval_before
    assert settled.effect(EffectName.MERGE).sender_binding is None
    assert settled.effect(EffectName.MERGE).receipt == observation
    assert reopened.get_attempt(identity, EffectName.MERGE, 2, "old-m").sender_binding is None
    assert reopened.get_attempt(identity, EffectName.MERGE, 2, "old-m").receipt == observation
    if approval_state is EffectState.CONFIRMED_COMPLETE:
        assert reopened.get_attempt(identity, EffectName.APPROVAL, 1, "old-a").receipt == approval_before.receipt
        assert reopened.get_attempt(identity, EffectName.APPROVAL, 1, "old-a").expected_head_sha == "H1"


def test_migrated_unsent_attempt_can_bind_without_relabeling_older_attempts(tmp_path):
    path = tmp_path / "merge.db"
    identity = _create_legacy_database(path, merge_state=EffectState.CONFIRMED_UNSENT)
    store = MergeOperationStore(path)
    before = store.get(identity)
    legacy_attempt = store.get_attempt(identity, EffectName.MERGE, 2, "old-m")
    granted = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=11)
    assert granted.granted
    assert granted.operation.effect(EffectName.MERGE).sender_binding == _binding()
    assert granted.operation.effect(EffectName.APPROVAL) == before.effect(EffectName.APPROVAL)
    assert store.get_attempt(identity, EffectName.MERGE, 2, "old-m") == legacy_attempt
    assert legacy_attempt.sender_binding is None
    store.get_or_create(identity, expected_head_sha="H3", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=12)
    restarted = MergeOperationStore(path)
    assert restarted.get_attempt(identity, EffectName.MERGE, 2, "old-m") == legacy_attempt
    assert restarted.get_attempt(identity, EffectName.MERGE, granted.generation, granted.attempt_id).sender_binding == _binding()
    assert restarted.get_attempt(identity, EffectName.APPROVAL, 1, "old-a").receipt == before.effect(EffectName.APPROVAL).receipt
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT detail FROM merge_operation_attempt_history WHERE history_id=1").fetchone()[0] == "retained original detail"


@pytest.mark.parametrize("effect_name", [EffectName.APPROVAL, EffectName.MERGE])
def test_state_observation_needs_exact_head_and_effect_evidence(tmp_path, effect_name):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=True, now=1)
    binding = _binding(app_role="reviewer" if effect_name is EffectName.APPROVAL else "controller")
    reserved = store.reserve_app_attempt(identity, effect_name, binding, now=2)
    store.record_delivery_unknown(identity, effect_name, reserved.attempt_id, reserved.generation, now=3)
    before = store.get(identity)
    before_attempt = store.get_attempt(identity, effect_name, reserved.generation, reserved.attempt_id)
    valid = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, review_id="review" if effect_name is EffectName.APPROVAL else "", reviewer_identity="observed-actor", target_head_sha="H", merge_commit_sha="merge" if effect_name is EffectName.MERGE else "")
    invalid_receipts = [replace(valid, target_head_sha=""), replace(valid, target_head_sha="another-head")]
    invalid_receipts.append(replace(valid, review_id="") if effect_name is EffectName.APPROVAL else replace(valid, merge_commit_sha=""))
    if effect_name is EffectName.APPROVAL:
        invalid_receipts.append(replace(valid, reviewer_identity=""))
    for receipt in invalid_receipts:
        assert not store.record_confirmed_complete(identity, effect_name, reserved.attempt_id, reserved.generation, receipt, now=4)
        assert store.get(identity) == before
        assert store.get_attempt(identity, effect_name, reserved.generation, reserved.attempt_id) == before_attempt
    assert store.record_confirmed_complete(identity, effect_name, reserved.attempt_id, reserved.generation, valid, now=5)
    settled = store.get(identity).effect(effect_name)
    assert settled.sender_binding == binding
    assert settled.receipt == valid
    assert settled.receipt.reviewer_identity != binding.bot_login


def test_confirmed_bound_effect_cannot_regress_to_uncertain_or_unsent(tmp_path):
    path = tmp_path / "merge.db"
    store = MergeOperationStore(path)
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reserved = store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=1)
    receipt = EffectReceipt(ConfirmationSource.STATE_OBSERVATION, target_head_sha="H", merge_commit_sha="merged")
    assert store.record_confirmed_complete(identity, EffectName.MERGE, reserved.attempt_id, reserved.generation, receipt, now=2)
    completed = store.get(identity)
    exact = store.get_attempt(identity, EffectName.MERGE, reserved.generation, reserved.attempt_id)
    assert not store.record_delivery_unknown(identity, EffectName.MERGE, reserved.attempt_id, reserved.generation, now=3)
    assert not store.record_confirmed_unsent(identity, EffectName.MERGE, reserved.attempt_id, reserved.generation, now=4)
    assert store.defer_local(identity, EffectName.MERGE, reserved.attempt_id, reserved.generation, is_real_throttle=True, throttle_attempt_id="delayed-throttle", retry_after_seconds=10, now=5) is None
    assert not store.operationally_block(identity, EffectName.MERGE, reserved.attempt_id, reserved.generation, BlockReason.FORBIDDEN, now=6)
    assert MergeOperationStore(path).get(identity) == completed
    assert store.get_attempt(identity, EffectName.MERGE, reserved.generation, reserved.attempt_id) == exact
    assert not store.reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=20).granted


def test_same_head_refresh_serializes_read_with_concurrent_reservation(tmp_path, monkeypatch):
    """A paused refresh cannot overwrite a reservation based on an older read."""
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)
    before = store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    context = multiprocessing.get_context("spawn")
    loaded, completed = context.Queue(), context.Queue()
    finish = context.Event()
    worker = context.Process(target=_paused_same_head_refresh, args=(path, loaded, finish, completed))
    worker.start()
    original_connect = sqlite3.connect

    def nonwaiting_connect(*args, **kwargs):
        # A zero SQLite busy timeout observes the lock without a timing oracle.
        kwargs["timeout"] = 0
        return original_connect(*args, **kwargs)

    try:
        assert loaded.get(timeout=15) == before
        with monkeypatch.context() as scoped:
            scoped.setattr(sqlite3, "connect", nonwaiting_connect)
            with pytest.raises(MergeOperationPersistenceError):
                MergeOperationStore(path).reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=3)
        finish.set()
        refreshed = completed.get(timeout=15)
    finally:
        finish.set()
        worker.join(timeout=15)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
    assert worker.exitcode == 0
    assert refreshed.effects == before.effects
    assert refreshed.merge_method == "merge"
    reserved = MergeOperationStore(path).reserve_app_attempt(identity, EffectName.MERGE, _binding(), now=3)
    assert reserved.granted
    durable = MergeOperationStore(path).get(identity)
    assert durable == reserved.operation
    assert durable.effect(EffectName.MERGE).state is EffectState.RUNNING
    assert durable.effect(EffectName.MERGE).sender_binding == _binding()
    assert durable.effect(EffectName.MERGE).attempt_id == reserved.attempt_id

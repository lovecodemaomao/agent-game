"""Maintain / Preempt / Assign. No business scoring belongs here."""
import logging
from dataclasses import replace
from math import isfinite
from .model import JobRecord, Lifecycle, Priority, Signal, Tombstone, ReservationRequest, Reservation
from .rules import ROUNDS_PER_DAY

LOG = logging.getLogger(__name__)


class ReservationManager:
    def available_gold(self, state, world):
        return max(0, world.gold - sum(r.gold for r in state.reservations.values()))

    def can_commit(self, state, world, request, excluding=()):
        held = [r for owner, r in state.reservations.items() if owner not in excluding]
        if not isfinite(request.gold) or request.gold < 0 or len(set(request.keys)) != len(request.keys):
            return False
        return (sum(r.gold for r in held) + request.gold <= world.gold
                and not set(request.keys).intersection(k for r in held for k in r.keys))

    def commit(self, state, world, owner, request):
        if not self.can_commit(state, world, request, (owner,)):
            return False
        state.reservations[owner] = Reservation(owner, request.gold, request.keys)
        return True

    def release(self, state, owner):
        state.reservations.pop(owner, None)

    def reduce(self, state, owner, request):
        current = state.reservations[owner]
        if request.keys != current.keys or not 0 <= request.gold <= current.gold:
            raise ValueError('invalid reservation reduction')
        state.reservations[owner] = Reservation(owner, request.gold, request.keys)


class Scheduler:
    def cancel_all(self, state, world, reason):
        for record in tuple(state.jobs.values()):
            if record.lifecycle == Lifecycle.ACTIVE:
                self.finish(state, record, Lifecycle.CANCELLED, world, reason)
        self.assert_invariants(state)

    def __init__(self, factories=None):
        self.factories = dict(factories or {})
        self.reservations = ReservationManager()

    @staticmethod
    def request(proposal, binding):
        # Binding claims are separate from semantic deduplication. A rebind
        # replaces only the binding claim, preserving all explicit claims.
        keys = proposal.reservation.keys
        if binding is not None:
            keys += (('binding', binding),)
        return ReservationRequest(proposal.reservation.gold, keys)

    def finish(self, state, record, lifecycle, world, reason=''):
        assert lifecycle not in (Lifecycle.ACTIVE, Lifecycle.CREATED)
        if record.lifecycle != Lifecycle.ACTIVE:
            return
        state.jobs[record.id] = replace(record, lifecycle=lifecycle)
        state.assignments.pop(record.role_id, None)
        self.reservations.release(state, record.id)
        if lifecycle == Lifecycle.PREEMPTED:
            state.tombstones.append(Tombstone(record.proposal.semantic_key, record.role_id,
                                              reason, world.round_no,
                                              world.day_no * ROUNDS_PER_DAY))
        LOG.info('%s job=%s role=%s reason=%s', lifecycle.value, record.id, record.role_id, reason)

    def cancel(self, state, job_id, world, reason='cancelled'):
        self.finish(state, state.jobs[job_id], Lifecycle.CANCELLED, world, reason)

    def feasible(self, world, proposal, role_id, binding, navigation, behavior, initial=False):
        role = world.role(role_id)
        if role is None or role.kind not in ('worker', 'pioneer'):
            return False
        if proposal.estimated_duration < 0:
            return False
        remaining = behavior.remaining_duration(world, role_id, binding, navigation)
        if initial:
            remaining = max(remaining, proposal.estimated_duration)
        return isfinite(remaining) and remaining >= 0 and (proposal.deadline is None or
                                      world.round_no + remaining - 1 <= proposal.deadline)

    def rebind(self, state, record, world, navigation):
        for target in record.proposal.fallback_targets:
            if target == record.binding:
                continue
            current = state.reservations[record.id]
            keys = tuple(k for k in current.keys if k != ('binding', record.binding)) + (('binding', target),)
            request = ReservationRequest(current.gold, keys)
            if (self.feasible(world, record.proposal, record.role_id, target, navigation, record.behavior)
                    and self.reservations.commit(state, world, record.id, request)):
                state.jobs[record.id] = replace(record, binding=target)
                LOG.info('REBIND job=%s target=%s', record.id, target)
                return True
        return False

    def maintain(self, state, world, signals, navigation):
        self.assert_invariants(state)
        state.tombstones[:] = [t for t in state.tombstones if t.cooldown_until >= world.round_no]
        rebinds = []
        for record in sorted(tuple(state.jobs.values()), key=lambda r: (r.proposal.priority, r.id)):
            if record.lifecycle != Lifecycle.ACTIVE:
                continue
            signal = signals.get(record.id, Signal.CONTINUE)
            if signal == Signal.SUCCESS:
                self.finish(state, record, Lifecycle.COMPLETED, world)
                continue
            if signal == Signal.FAIL or world.role(record.role_id) is None:
                self.finish(state, record, Lifecycle.FAILED, world, 'feedback or role lost')
                continue
            if signal != Signal.NEEDS_REBIND:
                signal = record.behavior.check(world, record.role_id, record.binding)
            if signal == Signal.SUCCESS:
                self.finish(state, record, Lifecycle.COMPLETED, world)
            elif signal == Signal.FAIL:
                self.finish(state, record, Lifecycle.FAILED, world, 'job check')
            elif signal == Signal.NEEDS_REBIND:
                rebinds.append(record.id)
            elif not self.feasible(world, record.proposal, record.role_id, record.binding, navigation, record.behavior):
                self.finish(state, record, Lifecycle.FAILED, world, 'deadline or unreachable binding')
        # Apply all budget reductions first: a purchase by one job must not
        # invalidate another job before the spent reservation is reconciled.
        requests = {}
        for record in tuple(state.jobs.values()):
            if record.lifecycle == Lifecycle.ACTIVE:
                current = state.reservations[record.id]
                request = record.behavior.reservation_request(world, record.role_id, record.binding, current)
                if not isfinite(request.gold) or request.gold < 0 or request.keys != current.keys:
                    self.finish(state, record, Lifecycle.FAILED, world, 'invalid budget update')
                elif request.gold <= current.gold:
                    self.reservations.reduce(state, record.id, request)
                else:
                    requests[record.id] = request
        # If real funds shrink unexpectedly, shed the lowest priority jobs.
        for record in sorted(tuple(state.jobs.values()), key=lambda r: (r.proposal.priority, r.id), reverse=True):
            if sum(r.gold for r in state.reservations.values()) <= world.gold:
                break
            if record.lifecycle == Lifecycle.ACTIVE:
                self.finish(state, record, Lifecycle.FAILED, world, 'reservation no longer affordable')
        for job_id, request in requests.items():
            record = state.jobs[job_id]
            if record.lifecycle == Lifecycle.ACTIVE and not self.reservations.commit(state, world, job_id, request):
                self.finish(state, record, Lifecycle.FAILED, world, 'budget increase rejected')
        for job_id in rebinds:
            record = state.jobs[job_id]
            if record.lifecycle == Lifecycle.ACTIVE and not self.rebind(state, record, world, navigation):
                self.finish(state, record, Lifecycle.FAILED, world, 'no feasible fallback')
        self.assert_invariants(state)

    @staticmethod
    def may_preempt(proposal, record):
        if not record.proposal.interruptible:
            return False
        if proposal.priority == Priority.EMERGENCY:
            return record.proposal.priority > Priority.EMERGENCY
        return (proposal.priority in (Priority.NIGHT_PREP, Priority.CRITICAL_DEFENSE)
                and record.proposal.priority in (Priority.NORMAL, Priority.OPTIONAL))

    def assign(self, state, world, proposals, navigation):
        """Preemption and replacement form one transaction within the turn copy."""
        for proposal in sorted(proposals, key=lambda p: (p.priority, -p.utility, p.semantic_key)):
            if proposal.kind not in self.factories:
                raise ValueError('unregistered job kind: ' + proposal.kind)
            if any(r.lifecycle == Lifecycle.ACTIVE and r.proposal.semantic_key == proposal.semantic_key
                   for r in state.jobs.values()):
                continue
            roles = sorted(proposal.eligible_roles, key=lambda rid: (rid in state.assignments, rid))
            for role_id in roles:
                if any(t.role_id == role_id and t.semantic_key == proposal.semantic_key
                       and t.cooldown_until >= world.round_no for t in state.tombstones):
                    continue
                old = state.jobs.get(state.assignments.get(role_id))
                if old and not self.may_preempt(proposal, old):
                    continue
                behavior = self.factories[proposal.kind](proposal)
                if not self.feasible(world, proposal, role_id, proposal.target, navigation, behavior, initial=True):
                    continue
                request = self.request(proposal, proposal.target)
                if not self.reservations.can_commit(state, world, request, (old.id,) if old else ()):
                    continue
                if old:
                    self.finish(state, old, Lifecycle.PREEMPTED, world, proposal.source)
                state.sequence += 1
                job_id = f'job-{state.sequence}'
                if not self.reservations.commit(state, world, job_id, request):
                    raise AssertionError('reservation changed during assignment')
                state.jobs[job_id] = JobRecord(job_id, role_id, proposal, behavior, proposal.target)
                state.assignments[role_id] = job_id
                LOG.info('ASSIGN job=%s role=%s proposal=%s source=%s', job_id, role_id,
                         proposal.semantic_key, proposal.source)
                break
        self.assert_invariants(state)

    @staticmethod
    def assert_invariants(state):
        active = {k: r for k, r in state.jobs.items() if r.lifecycle == Lifecycle.ACTIVE}
        assert len({r.role_id for r in active.values()}) == len(active), 'multiple ACTIVE jobs per role'
        assert state.assignments == {r.role_id: k for k, r in active.items()}, 'assignment mismatch'
        assert set(state.reservations) == set(active), 'orphan or missing reservation'
        assert all(owner == reservation.owner_job_id for owner, reservation in state.reservations.items()), 'owner mismatch'
        keys = [key for req in state.reservations.values() for key in req.keys]
        assert len(keys) == len(set(keys)), 'conflicting reservations'

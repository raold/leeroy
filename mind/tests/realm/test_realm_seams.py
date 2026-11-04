"""Wave 4's seams: where two Realm parts, built apart against fakes, meet. Every test here runs
both parts for real (with the real service, Store and DirectLedger where a send is involved);
only the native side, the model and the clock are test boundaries."""
import uuid

from leeroy_mind.protocol import Ack
from leeroy_mind.realm import bank, canon
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.realm.protocol import BankStock, GrantAchievement
from test_realm_direct import LIVE, PROCESS


def _refused(ledger, cmd, owner, ref, error, *, expects_job=False):
    assert ledger.prepare(cmd, owner=owner, ref=ref, context=LIVE, expects_job=expects_job) == cmd.id
    ledger.before_send(cmd, context=LIVE)
    assert ledger.on_ack(cmd, Ack(1, cmd.id, False, error))
    return ledger.state(owner, ref)


# -- item 8: native's busy:<owner> survives the direct ledger --------------------------------------

def test_busy_reason_survives_the_ledger_for_canon_and_bank(store):
    ledger = DirectLedger(store, process_id=PROCESS, clock=store.clock)
    grant = GrantAchievement(uuid.uuid4().hex, 101, 2186)
    state = _refused(ledger, grant, "canon", "canon:2186:101:0", "busy:training_owner")
    # The column holds [a-z0-9_] only, so native's ':' is kept as '_'; the owner is still there.
    assert state == ("refused", "busy_training_owner", None)
    assert canon._outcome(*state[:2]) == "deferred"  # Canon defers it as it does gm_mode.
    stock = BankStock(uuid.uuid4().hex, 104, 3, 47586, 1)
    state = _refused(ledger, stock, "step", "step:bank_stock:" + "a" * 64 + ":0", "busy:operation_identity",
                     expects_job=True)
    assert state == ("refused", "busy_operation_identity", None)
    assert bank._verdict("bank_stock", state) == "send"  # Nothing moved: the bank asks again.
    # A reason native never gives (spaces, capitals) still reads as the ledger's own fallback.
    odd = GrantAchievement(uuid.uuid4().hex, 102, 2186)
    assert _refused(ledger, odd, "canon", "canon:2186:102:0", "Busy: Raid")[1] == "native_refused"

# GPU admission and requested photos

The arbiter keeps live leases in event order and serializes model administration under one
lock. Ordinary nightly image stages still require no live leases. Requested-photo borrowing
is attached only with `[photos] requests = true`, `send = true`, `[selfies] enabled = true`,
`snaps = true`, and `[server] enabled = true`; restart the mind after changing these switches.
See [the photo operator notes](../photos/README.md#requested-photos) for what the owner sees.

`GpuArbiter.borrow(holder)` allows the `server` and `discord` presence leases. It refuses
human, offline-raid, voice-GPU and other job leases, paused GPU use, or any night/booth pass
still running or cleaning up. Its temporary request lease prevents another image pass from
starting. The request service additionally requires confirmed offline game presence and
enforces its durable hourly/day limits at admission.

Borrow admission closes before draining active non-embedding model exchanges. The client
registers each exchange atomically with the open check, and cancellation joins its HTTP
thread before releasing admission bookkeeping. No later mind chat call can autoload a model
beside the image worker. Embeddings bypass this borrow gate and stay resident. Free GPU's
separate pause gate still rejects/cancels all model work, including embeddings.
Chat jobs waiting for borrow admission lend their scheduler slots, so embeddings still run
when every chat slot was occupied. Those jobs rejoin the normal priority queue before
starting an exchange; concurrent children share one slot admission. Voice pin/unpin also
fences its residency check and joins its HTTP thread before borrowing can unload models.

The caller registers each worker with `room.own_worker(worker)` before startup and uses
`room.make_room()` for room-making retries. A hard lease arriving mid-borrow requests
cancellation immediately, then waits behind worker teardown. The borrow owns stop and live
reload even when startup fails, rendering times out, or cancellation repeats. It never
releases that ownership merely because the render's 180-second work budget expired.

Live reload uses the current model revision and context size, pins it, then reopens chat.
A transient reload error gets one retry. If worker shutdown or reload remains unproved,
admission stays closed with an explicit failure; another image pass cannot start. The next
live admission attempts recovery before waiting on the gate, so it cannot deadlock waiting
for its own repair. Free GPU pause interrupts borrowing and suppresses live reload until
resume; shutdown drains any registered worker before closing shared resources.

`DeadlineCredit` counts overlapping closed-admission time once from a turn's enrollment.
Pipeline expiry checks and the scheduler share that token rather than independently adding
time to copied deadlines. The credit covers safe cleanup/reload as well as the bounded
image work, and does not extend unrelated queue waits. The off path attaches no borrow gate
or credit tokens and keeps the ordinary deadline behavior.

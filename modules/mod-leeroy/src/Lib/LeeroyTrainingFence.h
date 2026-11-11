// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_FENCE_H
#define MOD_LEEROY_TRAINING_FENCE_H
#include <cstdint>
#include <string>
#include <string_view>

namespace Leeroy::Training
{
// How long an unproved training cleanup fences every body. After that only the per-body
// fences (job owners, journals, DC runs, pending bot commands) hold; training stays refused.
inline constexpr std::uint64_t kTrainingFenceReleaseMs = 10ULL * 60 * 1000;
inline constexpr std::uint64_t kTrainingReportMs = 60ULL * 1000;

// Why new training work is refused. Training that is on but waiting for its cleanup is
// cleanup_pending, never training_off.
inline std::string_view TrainingRefusal(bool guildOn, bool trainingOn, bool offPending, bool cleanupPending)
{
    if (!guildOn) return "guild_off";
    if (!trainingOn || offPending) return "training_off";
    if (cleanupPending) return "cleanup_pending";
    return {};
}

// The global cleanup fence of one pending episode. Pending lasts until a cleanup pass proves
// every adapter clean; the fence over unrelated bodies lasts at most kTrainingFenceReleaseMs.
class TrainingFence
{
public:
    // A later failure inside the same episode does not renew the bound: the cleanup pass
    // itself raises it again every second while a journal stays unrestored.
    void Raise(std::uint64_t now)
    {
        if (_pending) return;
        _pending = true;
        _released = false;
        _since = now;
    }
    void Settle(bool pending, std::uint64_t now)
    {
        if (pending) Raise(now);
        else _pending = _released = false;
    }
    bool Pending() const { return _pending; }
    bool Fencing(std::uint64_t now) const { return _pending && Elapsed(now) < kTrainingFenceReleaseMs; }
    // True once per episode, on the first observation past the bound.
    bool TakeRelease(std::uint64_t now)
    {
        if (!_pending || _released || Elapsed(now) < kTrainingFenceReleaseMs) return false;
        _released = true;
        return true;
    }
    std::uint64_t Elapsed(std::uint64_t now) const { return _pending && now >= _since ? now - _since : 0; }
private:
    bool _pending = false, _released = false;
    std::uint64_t _since = 0;
};

// Which adapters keep the cleanup pending, logged on change and at most once a minute.
// The all-clear is logged at once: no later pass runs to report a deferred one.
class CleanupReport
{
public:
    bool Due(std::string const& adapters, std::uint64_t now)
    {
        if (adapters == _logged) return false;
        bool const clear = adapters.empty();
        if (!clear && _loggedOnce && now >= _at && now - _at < kTrainingReportMs) return false;
        _logged = adapters;
        _at = now;
        _loggedOnce = true;
        return true;
    }
private:
    std::string _logged;
    std::uint64_t _at = 0;
    bool _loggedOnce = false;
};
}
#endif

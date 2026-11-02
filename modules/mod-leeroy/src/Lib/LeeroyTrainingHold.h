// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_HOLD_H
#define MOD_LEEROY_TRAINING_HOLD_H
#include "LeeroyTrainingProtocol.h"

namespace Leeroy::Training
{
struct BossHold
{
    bool paused = false, atBoss = false;
    std::uint64_t boss = 0;
};
// A hold already present before our WaitAtBoss override is not ours to resume.
class TrainingHold
{
public:
    void Enable(BossHold current)
    {
        _blocked = current.paused && current.atBoss ? current.boss : 0;
        _boss = 0;
        _state = {};
    }
    void Observe(BossHold current, std::uint64_t now)
    {
        if (!current.paused || !current.atBoss || !current.boss)
        {
            _boss = 0;
            _state = {};
            if (!current.paused) _blocked = 0;
            return;
        }
        if (current.boss == _blocked) return;
        if (_boss != current.boss)
        {
            _boss = current.boss;
            _state = {true, true, now, false};
        }
    }
    bool ShouldResume(BossHold current, std::uint64_t now, bool force = false) const
    {
        return _boss && current.boss == _boss && current.paused && current.atBoss && !_state.resumed
            && (force || HoldShouldResume(_state, now));
    }
    void Resumed() { _state.resumed = true; }
private:
    std::uint64_t _boss = 0, _blocked = 0;
    HoldState _state;
};
}
#endif

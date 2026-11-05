// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_OVERRIDES_H
#define MOD_LEEROY_TRAINING_OVERRIDES_H

#include <cmath>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace Leeroy::Training
{
struct OverrideState
{
    bool present = false;
    double value = 0; // Effective value even when the explicit override is absent.
};
struct OwnedOverride { OverrideState prior, applied; };
enum class OverrideUndo { Unowned, PreserveExternal, Remove, Restore };
struct OverrideUndoPlan { OverrideUndo action = OverrideUndo::Unowned; double value = 0; };

// One run's journal, owned by the world thread. The core has no override ownership token: a later actor
// restoring the exact same normalized value is indistinguishable (ABA), not proof of exclusive ownership.
class TrainingOverrides
{
public:
    bool CanChange(std::string_view key, OverrideState current) const
    {
        if (key.empty() || !std::isfinite(current.value)) return false;
        auto found = _entries.find(key);
        return found == _entries.end() || Same(current, found->second.applied);
    }

    std::optional<double> ScaleBase(std::string_view key, OverrideState current) const
    {
        if (!CanChange(key, current)) return std::nullopt;
        auto found = _entries.find(key);
        return found == _entries.end() ? current.value : found->second.prior.value;
    }

    // Call only after a successful SetOverride and an actual normalized readback. Rejected or failed writes
    // never enter the journal. A preflight snapshot must still match this run's previously applied value.
    bool NoteApplied(std::string_view key, OverrideState before, OverrideState after)
    {
        if (!CanChange(key, before) || !after.present || !std::isfinite(after.value)) return false;
        auto [found, inserted] = _entries.try_emplace(std::string(key), OwnedOverride{before, after});
        if (!inserted) found->second.applied = after;
        return true;
    }

    OverrideUndoPlan Undo(std::string_view key, OverrideState current) const
    {
        auto found = _entries.find(key);
        if (found == _entries.end()) return {};
        if (!std::isfinite(current.value) || !Same(current, found->second.applied))
            return {OverrideUndo::PreserveExternal, 0};
        return found->second.prior.present ? OverrideUndoPlan{OverrideUndo::Restore, found->second.prior.value}
            : OverrideUndoPlan{OverrideUndo::Remove, 0};
    }

    // Consume only after the runtime verifies restoration, or deliberately preserves a detected external change.
    void Forget(std::string_view key)
    {
        if (auto found = _entries.find(key); found != _entries.end()) _entries.erase(found);
    }
    std::vector<std::string> Keys() const
    {
        std::vector<std::string> keys;
        for (auto const& [key, entry] : _entries) keys.push_back(key);
        return keys;
    }
    bool Empty() const { return _entries.empty(); }

private:
    static bool Same(OverrideState a, OverrideState b)
    {
        return a.present == b.present && (!a.present || a.value == b.value);
    }
    std::map<std::string, OwnedOverride, std::less<>> _entries;
};
}
#endif

// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_CRAFT_PROOF_H
#define MOD_LEEROY_REALM_CRAFT_PROOF_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <utility>

namespace Leeroy::Realm
{
struct CraftOutput
{
    std::uint32_t entry = 0, count = 0;
    bool operator==(CraftOutput const&) const = default;
};
struct CraftItemFact
{
    std::uint64_t guid = 0;
    CraftOutput output;
};
struct CraftOutputShape
{
    std::array<CraftOutput, 3> outputs{};
    std::size_t size = 0;
    bool operator==(CraftOutputShape const&) const = default;
};
enum class CraftEffectKind { Empty, CreateItem, Other };
struct CraftEffectFacts
{
    CraftEffectKind kind = CraftEffectKind::Empty;
    bool selfOnly = false, triggered = false, conditional = false, chained = false, outputModifier = false;
    std::int32_t basePoints = 0, dieSides = 0;
    float perLevel = 0, perComboPoint = 0;
    std::uint32_t entry = 0, maxStack = 0;
};
struct CraftRecipeFacts
{
    bool missile = false, channel = false, loot = false, scripted = false, linked = false, specialization = false;
    std::array<CraftEffectFacts, 3> effects{};
};
// Uses the same fixed-count clamp as installed DoCreateItem. Everything that
// can replace, delay, trigger or randomize the output is refused before cast.
inline std::optional<CraftOutputShape> PlanCraftOutputs(CraftRecipeFacts const& recipe)
{
    if (recipe.missile || recipe.channel || recipe.loot || recipe.scripted || recipe.linked || recipe.specialization)
        return std::nullopt;
    CraftOutputShape shape;
    for (auto const& effect : recipe.effects)
    {
        if (effect.kind == CraftEffectKind::Empty) continue;
        auto const amount = static_cast<std::int64_t>(effect.basePoints) + effect.dieSides;
        if (effect.kind != CraftEffectKind::CreateItem || !effect.selfOnly || effect.triggered || effect.conditional
            || effect.chained || effect.outputModifier || effect.dieSides < 0 || effect.dieSides > 1
            || effect.perLevel != 0 || effect.perComboPoint != 0 || !effect.entry || !effect.maxStack
            || amount < 0 || amount > std::numeric_limits<std::int32_t>::max()) return std::nullopt;
        auto const count = amount < 1 ? 1u : static_cast<std::uint32_t>(amount);
        shape.outputs[shape.size++] = {effect.entry, count > effect.maxStack ? effect.maxStack : count};
    }
    return shape.size ? std::optional<CraftOutputShape>(shape) : std::nullopt;
}
enum class CraftCastState { Unprepared, Prepared, Casting, Complete, Cancelled, Rejected, Unknown };

// A single native invocation. The native adapter checks the immutable body and
// owner before calling this proof, and never dereferences the saved spell key.
// Each CREATE_ITEM effect must provide exactly one complete, copied receipt.
class CraftCastProof
{
public:
    static constexpr std::size_t MaxOutputs = 3;
    CraftCastProof(std::uint64_t dispatch, std::array<CraftOutput, MaxOutputs> outputs, std::size_t size)
        : _dispatch(dispatch), _expected(outputs), _size(size)
    {
        if (!dispatch || !size || size > MaxOutputs) Poison("craft_output_shape");
        else for (std::size_t i = 0; i < size; ++i)
            if (!outputs[i].entry || !outputs[i].count) Poison("craft_output_shape");
    }
    bool Prepare(std::uintptr_t spell)
    {
        if (!spell || _state != CraftCastState::Unprepared) return Poison("craft_preparation_changed");
        _spell = spell; _state = CraftCastState::Prepared; return true;
    }
    bool PreCast(std::uintptr_t spell, std::uintptr_t thread)
    {
        if (!Exact(spell) || !thread || _state != CraftCastState::Prepared)
            return Poison("craft_precast_changed");
        _thread = thread; _state = CraftCastState::Casting; return true;
    }
    bool Created(std::uintptr_t thread, CraftItemFact fact)
    {
        if (_state != CraftCastState::Casting || thread != _thread || !fact.guid
            || _received >= _size || fact.output != _expected[_received])
            return Poison("craft_output_unproved");
        _items[_received++] = fact; _total += fact.output.count; return true;
    }
    bool PostCast(std::uintptr_t spell, std::uintptr_t thread)
    {
        if (!Exact(spell) || _state != CraftCastState::Casting || thread != _thread || _received != _size)
            return Poison("craft_postcast_unproved");
        _state = CraftCastState::Complete; return true;
    }
    bool Cancel(std::uintptr_t spell, bool nativePreparing)
    {
        // The installed cancel hook runs after native current-container removal
        // and before finish(false). PREPARING plus no pre-cast means no effects
        // have run for this precisely supported, immediate CREATE_ITEM subset.
        if (!Exact(spell) || !nativePreparing || _state != CraftCastState::Prepared || _received)
            return Poison("craft_cancel_unproved");
        _state = CraftCastState::Cancelled; return true;
    }
    void NativeReturned(bool accepted)
    {
        if (!accepted && _state == CraftCastState::Prepared && !_received)
            _state = CraftCastState::Rejected;
        else if (_state == CraftCastState::Unprepared || (!accepted && _state != CraftCastState::Unknown))
            Poison("craft_prepare_return_unproved");
    }
    bool Poison(std::string reason)
    {
        _state = CraftCastState::Unknown;
        if (_reason.empty()) _reason = std::move(reason);
        return false;
    }
    bool Exact(std::uintptr_t spell) const { return spell && spell == _spell; }
    CraftCastState State() const { return _state; }
    std::string const& Reason() const { return _reason; }
    std::uint64_t Dispatch() const { return _dispatch; }
    std::uintptr_t SpellKey() const { return _spell; }
    std::uint64_t Total() const { return _total; }
    std::size_t Received() const { return _received; }
    std::array<CraftItemFact, MaxOutputs> const& Items() const { return _items; }
private:
    std::uint64_t _dispatch = 0, _total = 0;
    std::uintptr_t _spell = 0, _thread = 0;
    std::array<CraftOutput, MaxOutputs> _expected{};
    std::array<CraftItemFact, MaxOutputs> _items{};
    std::size_t _size = 0, _received = 0;
    CraftCastState _state = CraftCastState::Unprepared;
    std::string _reason;
};

// Constant storage on each native thread. A missing posthook cannot accumulate
// candidates: the next preparation or cast boundary extracts and poisons it.
class CraftCandidate
{
public:
    bool Open(std::uint64_t dispatch)
    {
        if (_dispatch || !dispatch) return false;
        _dispatch = dispatch; return true;
    }
    std::uint64_t Boundary() { return std::exchange(_dispatch, 0); }
    std::uint64_t Dispatch() const { return _dispatch; }
private:
    std::uint64_t _dispatch = 0;
};

class CraftProgress
{
public:
    explicit CraftProgress(std::uint32_t requested, std::uint32_t completed = 0,
        std::uint64_t produced = 0, std::uint64_t lastDispatch = 0)
        : _requested(requested), _completed(completed), _produced(produced), _lastDispatch(lastDispatch)
    {
        _valid = requested >= 1 && requested <= 5 && completed <= requested
            && ((completed == 0 && produced == 0 && lastDispatch == 0)
                || (completed != 0 && produced != 0 && lastDispatch != 0));
    }
    bool Record(CraftCastProof const& cast)
    {
        if (!_valid || _completed >= _requested || cast.State() != CraftCastState::Complete
            || cast.Dispatch() <= _lastDispatch || !cast.Total()
            || cast.Total() > std::numeric_limits<std::uint64_t>::max() - _produced)
        { _valid = false; return false; }
        ++_completed; _produced += cast.Total(); _lastDispatch = cast.Dispatch(); return true;
    }
    bool Complete() const { return _valid && _completed == _requested; }
    std::uint32_t Casts() const { return _completed; }
    std::uint64_t Items() const { return _produced; }
    std::uint64_t LastDispatch() const { return _lastDispatch; }
private:
    std::uint32_t _requested, _completed;
    std::uint64_t _produced, _lastDispatch;
    bool _valid = false;
};
}
#endif

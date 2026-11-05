// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmCraft.h"
#include "RealmPorts.h"
#include "LeeroyRealmCraftProof.h"
#include "AllSpellScript.h"
#include "PlayerScript.h"
#include "SkillExtraItems.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "WorldSession.h"
#include <algorithm>
#include <array>
#include <limits>
#include <map>
#include <mutex>

namespace Leeroy::Realm
{
namespace
{
using OutputShape = CraftOutputShape;

// Fixed CREATE_ITEM output only. Random-count and perfection specialization
// recipes need a different native capacity/quantity proof before enrollment.
std::string Outputs(Player* player, SpellInfo const* info, OutputShape& shape)
{
    if (!player || !info) return "not_known";
    if (info->Speed != 0.0f || info->IsChanneled() || info->IsLootCrafting()
        || info->SpellFamilyName != SPELLFAMILY_GENERIC) return "craft_effect_unsupported";
    auto const scripts = sObjectMgr->GetSpellScriptsBounds(info->Id);
    if (scripts.first != scripts.second || sSpellMgr->GetSpellLinked(static_cast<int32>(info->Id))
        || sSpellMgr->GetSpellLinked(static_cast<int32>(info->Id + SPELL_LINK_HIT)))
        return "craft_script_unsupported";
    float chance = 0.0f;
    uint32 perfect = 0;
    int32 extra = 0;
    if (CanCreatePerfectItem(player, info->Id, chance, perfect)
        || canCreateExtraItems(player, info->Id, chance, extra)) return "craft_random_unsupported";
    CraftRecipeFacts recipe;
    constexpr std::array<SpellModOp, MAX_SPELL_EFFECTS> effectMods{SPELLMOD_EFFECT1, SPELLMOD_EFFECT2, SPELLMOD_EFFECT3};
    for (std::size_t i = 0; i < MAX_SPELL_EFFECTS; ++i)
    {
        auto const& effect = info->Effects[i];
        if (!effect.IsEffect()) continue;
        auto& facts = recipe.effects[i];
        facts.kind = effect.Effect == SPELL_EFFECT_CREATE_ITEM ? CraftEffectKind::CreateItem : CraftEffectKind::Other;
        facts.selfOnly = effect.TargetA.GetTarget() == TARGET_UNIT_CASTER && effect.TargetB.GetTarget() == 0;
        facts.triggered = effect.TriggerSpell != 0; facts.conditional = effect.ImplicitTargetConditions != nullptr;
        facts.chained = effect.ChainTarget != 0;
        facts.dieSides = effect.DieSides; facts.basePoints = effect.BasePoints;
        facts.perLevel = effect.RealPointsPerLevel; facts.perComboPoint = effect.PointsPerComboPoint;
        // Output modifiers could make a partial bag-store indistinguishable
        // from a complete fixed-count result. Do not evaluate/consume them.
        // Conservatively refuse these modifier lists even if a modifier's
        // mask appears unrelated. Checking applicability invokes external
        // hooks; that cannot run inside the receipt inbox critical section.
        if (info->IsAffectedBySpellMods())
            for (auto op : {SPELLMOD_ALL_EFFECTS, effectMods[i]})
                if (!player->GetSpellModList(op).empty()) return "craft_output_modifier";
        auto const* item = sObjectMgr->GetItemTemplate(effect.ItemType);
        facts.entry = effect.ItemType; facts.maxStack = item ? item->GetMaxStackSize() : 0;
    }
    auto const planned = PlanCraftOutputs(recipe);
    if (!planned) return "craft_effect_unsupported";
    shape = *planned;
    return "";
}

BodyIdentity NativeBody(Player* player)
{
    BodyIdentity body;
    if (!player) return body;
    body.player = player;
    body.ai = GET_PLAYERBOT_AI(player);
    body.session = player->GetSession();
    body.account = body.session ? body.session->GetAccountId() : 0;
    body.guild = player->GetGuildId();
    body.classId = player->getClass(); body.race = player->getRace(); body.sex = player->getGender();
    body.map = player->GetMapId(); body.instance = player->GetInstanceId();
    body.group = player->GetGroup() ? player->GetGroup()->GetGUID().GetRawValue() : 0;
    return body;
}
struct Attempt
{
    std::string const owner;
    std::uint32_t const guid, spellId;
    std::uint64_t const epoch;
    BodyIdentity const body;
    OutputShape const shape;
    CraftCastProof proof;
    Attempt(std::string source, std::uint32_t member, std::uint32_t spell, std::uint64_t generation,
        BodyIdentity identity, OutputShape outputs, std::uint64_t serial)
        : owner(std::move(source)), guid(member), spellId(spell), epoch(generation), body(identity), shape(outputs),
          proof(serial, outputs.outputs, outputs.size) { }
};
struct Inbox
{
    std::mutex mutex;
    std::uint64_t serial = 0;
    std::map<std::uint64_t, Attempt> attempts;
    std::map<std::uint32_t, std::uint64_t> bodies;
    std::map<std::uintptr_t, std::uint64_t> spells;
} inbox;
thread_local CraftCandidate candidate;
std::uintptr_t ThreadKey() { return reinterpret_cast<std::uintptr_t>(&candidate); }
struct Capability { std::uint64_t serial; bool consumed = false; };
thread_local Capability* capability = nullptr;
struct NativeCall
{
    Capability value;
    Capability* previous;
    explicit NativeCall(std::uint64_t serial) : value{serial}, previous(capability) { capability = &value; }
    ~NativeCall() { capability = previous; }
};
Attempt* FindLocked(std::uint64_t serial)
{
    auto found = inbox.attempts.find(serial);
    return found == inbox.attempts.end() ? nullptr : &found->second;
}
Attempt* BodyLocked(Player* player)
{
    if (!player) return nullptr;
    auto found = inbox.bodies.find(player->GetGUID().GetCounter());
    return found == inbox.bodies.end() ? nullptr : FindLocked(found->second);
}
Attempt* SpellLocked(Spell* spell)
{
    auto found = inbox.spells.find(reinterpret_cast<std::uintptr_t>(spell));
    return found == inbox.spells.end() ? nullptr : FindLocked(found->second);
}
void BoundaryLocked(char const* reason)
{
    if (auto* previous = FindLocked(candidate.Boundary())) previous->proof.Poison(reason);
}
bool ExactBody(Attempt const& attempt, Player* player)
{
    return player && player->GetGUID().GetCounter() == attempt.guid && player->IsInWorld()
        && !player->IsDuringRemoveFromWorld() && !player->IsBeingTeleported()
        && NativeBody(player) == attempt.body && RealmModule::Instance().On()
        && RealmModule::Instance().Epoch() == attempt.epoch;
}
bool ExactShape(Attempt const& attempt, Player* player, SpellInfo const* info)
{
    OutputShape now;
    return info && info->Id == attempt.spellId && Outputs(player, info, now).empty() && now == attempt.shape;
}
bool ExactSpell(Attempt const& attempt, Player* player, Spell* spell)
{
    if (!spell || !spell->GetSpellInfo() || !spell->GetSpellValue()) return false;
    for (std::size_t i = 0; i < MAX_SPELL_EFFECTS; ++i)
        if (spell->GetSpellInfo()->Effects[i].Effect == SPELL_EFFECT_CREATE_ITEM
            && spell->GetSpellValue()->EffectBasePoints[i] != spell->GetSpellInfo()->Effects[i].BasePoints)
            return false;
    // InitExplicitTargets removes a redundant explicit self target for a
    // caster-targeted recipe. Later SelectSpellTargets can set UNIT without
    // restoring that explicit pointer; both are native self-only forms.
    return ExactBody(attempt, player) && spell && spell->GetCaster() == player
        && spell->GetTriggeredCastFlags() == TRIGGERED_NONE && !spell->m_CastItem && !spell->m_preCastSpell
        && (!spell->m_targets.GetObjectTarget() || spell->m_targets.GetObjectTarget() == player)
        && (spell->m_targets.GetTargetMask() & ~TARGET_FLAG_UNIT) == 0
        && ExactShape(attempt, player, spell->GetSpellInfo());
}
void EraseLocked(std::uint64_t serial)
{
    if (auto* attempt = FindLocked(serial))
    {
        inbox.spells.erase(attempt->proof.SpellKey());
        inbox.bodies.erase(attempt->guid);
        inbox.attempts.erase(serial);
    }
}

class CraftSpellScripts final : public AllSpellScript
{
public:
    CraftSpellScripts() : AllSpellScript("LeeroyRealmCraftSpellScripts", {
        ALLSPELLHOOK_CAN_PREPARE, ALLSPELLHOOK_ON_CAST, ALLSPELLHOOK_ON_CAST_CANCEL}) { }
    bool CanPrepare(Spell* spell, SpellCastTargets const* targets, AuraEffect const* triggeredByAura) override
    {
        std::lock_guard lock(inbox.mutex);
        // This observes preparations from every caster, including nonplayers
        // whose triggered effects could otherwise create an item on our body.
        BoundaryLocked("craft_nested_preparation");
        if (!spell) return true;
        if (auto* reused = SpellLocked(spell)) reused->proof.Poison("craft_spell_reprepared");
        auto* player = spell->GetCaster() ? spell->GetCaster()->ToPlayer() : nullptr;
        auto* expected = capability ? FindLocked(capability->serial) : nullptr;
        if (expected && !capability->consumed && player && expected->guid == player->GetGUID().GetCounter()
            && spell->GetSpellInfo()->Id == expected->spellId)
        {
            capability->consumed = true;
            if (!expected->proof.Prepare(reinterpret_cast<std::uintptr_t>(spell))) return false;
            if (!inbox.spells.emplace(reinterpret_cast<std::uintptr_t>(spell), expected->proof.Dispatch()).second)
                return expected->proof.Poison("craft_spell_key_reused");
            if (!targets || targets->GetUnitTarget() != player || targets->GetTargetMask() != TARGET_FLAG_UNIT
                || triggeredByAura || !ExactSpell(*expected, player, spell))
                return expected->proof.Poison("craft_prepare_identity");
            return true;
        }
        if (auto* sameBody = BodyLocked(player)) sameBody->proof.Poison("craft_foreign_preparation");
        return true;
    }
    void OnSpellCast(Spell* spell, Unit* caster, SpellInfo const*, bool) override
    {
        std::lock_guard lock(inbox.mutex);
        auto const serial = candidate.Boundary();
        auto* frame = FindLocked(serial);
        auto* bound = SpellLocked(spell);
        auto* player = caster ? caster->ToPlayer() : nullptr;
        if (frame && frame == bound && ExactSpell(*frame, player, spell))
            frame->proof.PostCast(reinterpret_cast<std::uintptr_t>(spell), ThreadKey());
        else
        {
            if (frame) frame->proof.Poison("craft_postcast_interference");
            if (bound) bound->proof.Poison("craft_postcast_identity");
        }
    }
    void OnSpellCastCancel(Spell* spell, Unit* caster, SpellInfo const*, bool) override
    {
        std::lock_guard lock(inbox.mutex);
        BoundaryLocked("craft_cancel_in_production");
        auto* bound = SpellLocked(spell);
        if (!bound) return;
        auto* player = caster ? caster->ToPlayer() : nullptr;
        if (!ExactSpell(*bound, player, spell)) bound->proof.Poison("craft_cancel_identity");
        else bound->proof.Cancel(reinterpret_cast<std::uintptr_t>(spell), spell->getState() == SPELL_STATE_PREPARING);
    }
};
class CraftPlayerScripts final : public PlayerScript
{
public:
    CraftPlayerScripts() : PlayerScript("LeeroyRealmCraftPlayerScripts", {
        PLAYERHOOK_ON_SPELL_CAST, PLAYERHOOK_ON_CREATE_ITEM, PLAYERHOOK_ON_LOGIN,
        PLAYERHOOK_ON_LOGOUT, PLAYERHOOK_ON_MAP_CHANGED, PLAYERHOOK_ON_BEFORE_TELEPORT}) { }
    void OnPlayerSpellCast(Player* player, Spell* spell, bool) override
    {
        std::lock_guard lock(inbox.mutex);
        BoundaryLocked("craft_precast_interference");
        auto* bound = SpellLocked(spell);
        if (!bound)
        {
            if (auto* sameBody = BodyLocked(player)) sameBody->proof.Poison("craft_foreign_precast");
            return;
        }
        if (!ExactSpell(*bound, player, spell)) { bound->proof.Poison("craft_precast_identity"); return; }
        if (!bound->proof.PreCast(reinterpret_cast<std::uintptr_t>(spell), ThreadKey())
            || !candidate.Open(bound->proof.Dispatch())) bound->proof.Poison("craft_candidate_unavailable");
    }
    void OnPlayerCreateItem(Player* player, Item* item, uint32 count) override
    {
        std::lock_guard lock(inbox.mutex);
        auto* frame = FindLocked(candidate.Dispatch());
        auto* recipient = BodyLocked(player);
        if (!frame || frame != recipient || !item || item->GetOwnerGUID() != player->GetGUID()
            || !ExactBody(*frame, player) || !ExactShape(*frame, player, sSpellMgr->GetSpellInfo(frame->spellId)))
        {
            if (frame) frame->proof.Poison("craft_item_identity");
            if (recipient) recipient->proof.Poison("craft_unbound_output");
            return;
        }
        frame->proof.Created(ThreadKey(), {item->GetGUID().GetRawValue(), {item->GetEntry(), count}});
    }
    static void Changed(Player* player)
    {
        std::lock_guard lock(inbox.mutex);
        if (auto* attempt = BodyLocked(player)) attempt->proof.Poison("craft_body_lifetime_changed");
    }
    void OnPlayerLogin(Player* player) override { Changed(player); }
    void OnPlayerLogout(Player* player) override { Changed(player); }
    void OnPlayerMapChanged(Player* player) override { Changed(player); }
    bool OnPlayerBeforeTeleport(Player* player, uint32, float, float, float, float, uint32, Unit*) override
        { Changed(player); return true; }
};

RealmStep ReadResult(Queued& entry, std::uint32_t guid, bool invocationReturned)
{
    std::lock_guard lock(inbox.mutex);
    auto const serial = entry.payload.at("craft_dispatch").get<std::uint64_t>();
    auto* attempt = FindLocked(serial);
    if (!attempt || attempt->owner != entry.id || attempt->guid != guid)
        return {StepState::Unknown, "craft_operation_identity"};
    auto* player = FindOnline(guid);
    if (!ExactBody(*attempt, player)) attempt->proof.Poison("craft_body_identity");
    auto const state = attempt->proof.State();
    if (state == CraftCastState::Complete)
    {
        CraftProgress progress(entry.payload.at("count").get<std::uint32_t>(),
            entry.payload.value("craft_casts_done", 0u), entry.payload.value("craft_items_done", std::uint64_t{0}),
            entry.payload.value("craft_last_dispatch", std::uint64_t{0}));
        if (!progress.Record(attempt->proof))
        {
            attempt->proof.Poison("craft_progress_unproved");
            return {StepState::Unknown, "craft_progress_unproved"};
        }
        EraseLocked(serial); entry.payload.erase("craft_dispatch");
        entry.payload["craft_items_done"] = progress.Items(); entry.payload["craft_casts_done"] = progress.Casts();
        entry.payload["craft_last_dispatch"] = progress.LastDispatch();
        return {progress.Complete() ? StepState::Complete : StepState::Pending, "", progress.Items()};
    }
    if (state == CraftCastState::Cancelled || state == CraftCastState::Rejected)
    {
        // A native preparing cancellation/failed prepare proves no effects for
        // this invocation. Previously committed casts do not get recast.
        EraseLocked(serial); entry.payload.erase("craft_dispatch");
        return {StepState::Refused, state == CraftCastState::Cancelled ? "craft_cancelled" : "craft_cast_refused"};
    }
    if (state == CraftCastState::Casting && invocationReturned)
        attempt->proof.Poison("craft_postcast_missing");
    if (attempt->proof.State() == CraftCastState::Unknown)
        return {StepState::Unknown, attempt->proof.Reason()};
    return {StepState::Pending, ""};
}
RealmStep Craft(Queued& entry, std::uint32_t guid)
{
    auto& module = RealmModule::Instance();
    if (entry.payload.contains("craft_dispatch")) return ReadResult(entry, guid, true);
    auto refuse = [&](std::string reason) -> RealmStep
    {
        // Any incomplete request after production stays excluded until an
        // exact native terminal proof exists; no timer invents a handback.
        return {entry.payload.value("craft_casts_done", 0u) ? StepState::Unknown : StepState::Refused, std::move(reason)};
    };
    if (!IsCast(guid)) return refuse("not_cast");
    if (auto why = AdmissionReason(guid, Purpose::Craft, entry.id); !why.empty()) return refuse(why);
    auto* bot = FindOnline(guid);
    if (!bot || bot->IsBeingTeleported() || bot->duel) return refuse("busy:transition");
    if (bot->IsNonMeleeSpellCast(false)) return refuse("busy:casting");
    auto const spellId = entry.payload.at("spell_id").get<std::uint32_t>();
    auto const* info = sSpellMgr->GetSpellInfo(spellId);
    if (!info || !bot->HasSpell(spellId)) return refuse("not_known");
    bool recipe = false;
    for (auto skill : kProfessionSkills)
        for (auto const* ability : GetSkillLineAbilitiesBySkillLine(skill))
            if (ability->Spell == spellId && bot->HasSkill(static_cast<uint16>(skill))) recipe = true;
    if (!recipe) return refuse("not_recipe");
    OutputShape shape;
    if (auto why = Outputs(bot, info, shape); !why.empty()) return refuse(why);
    for (std::size_t i = 0; i < info->Reagent.size(); ++i)
        if (info->Reagent[i] > 0 && bot->GetItemCount(static_cast<uint32>(info->Reagent[i]), false) < info->ReagentCount[i])
            return refuse("no_mats");
    auto const identity = Identity(guid);
    if (!identity || *identity != NativeBody(bot)) return refuse("craft_body_unavailable");
    std::uint64_t serial = 0;
    {
        std::lock_guard lock(inbox.mutex);
        if (inbox.bodies.contains(guid)) return {StepState::Unknown, "craft_receipt_held"};
        if (inbox.attempts.size() >= ActionQueue::Capacity || inbox.serial == std::numeric_limits<std::uint64_t>::max())
            return refuse("craft_receipt_capacity");
        serial = ++inbox.serial;
        inbox.attempts.try_emplace(serial, entry.id, guid, spellId, module.Epoch(), *identity, shape, serial);
        inbox.bodies.emplace(guid, serial);
        entry.payload["craft_dispatch"] = serial;
    }
    SpellCastResult result;
    {
        NativeCall call(serial);
        // Ordinary nontriggered casting preserves cast time, reagent, power,
        // cooldown and native failure semantics. No current-spell adoption.
        result = bot->CastSpell(bot, info, CastSpellExtraArgs{});
    }
    {
        std::lock_guard lock(inbox.mutex);
        if (auto* attempt = FindLocked(serial)) attempt->proof.NativeReturned(result == SPELL_CAST_OK);
    }
    return ReadResult(entry, guid, true);
}
}

void RegisterRealmCraftHandlers()
{
    RealmModule::Instance().RegisterExecutor("craft", Craft);
    RegisterCommand("craft", [](nlohmann::json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseCraft(message, error);
        if (!command) return {false, error};
        Queued entry;
        entry.id = message.at("id").get<std::string>(); entry.kind = "craft"; entry.guids = {command->guid};
        entry.payload = {{"spell_id", command->spellId}, {"count", command->count}};
        if (!RealmModule::Instance().Accept(std::move(entry), error)) return {false, error};
        return {true, ""};
    });
}
void AddRealmCraftScripts() { new CraftSpellScripts(); new CraftPlayerScripts(); }
}

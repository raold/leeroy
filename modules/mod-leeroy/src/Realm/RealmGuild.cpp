// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmGuild.h"
#include "RealmPorts.h"
#include "LeeroyRealmGuild.h"
#include "LeeroyRealmReadmit.h"
#include "LeeroyEvents.h"

namespace Leeroy::Realm
{
namespace
{
// A protected pointer-to-member may be formed by the derived view and applied
// to the actual base object. This copies the count without downcasting a Guild,
// exposing its vector, or modifying core access control.
class GuildRankView : public ::Guild
{
public:
    static std::size_t Count(::Guild const& guild)
    { return (guild.*(&GuildRankView::m_ranks)).size(); }
};

std::string RecruitReason(std::uint32_t guid, ::Guild const& guild)
{
    auto& module = RealmModule::Instance();
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
    auto* online = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
    auto const* leader = sCharacterCache->GetCharacterCacheByGuid(guild.GetLeaderGUID());
    GuildRecruitFacts facts;
    facts.human = guid == HumanGuid(); facts.cast = IsCast(guid);
    facts.configuredMember = IsConfiguredGuildMember(guid);
    facts.protectedFormer = CastRecruitmentProtected(guid);
    facts.actor = module.IsActor(guid); facts.randomAccount = IsOnRandomBotAccount(guid);
    facts.exists = character != nullptr;
    // Both views must be guildless; a conflicting cache cannot authorize an add.
    facts.guild = character ? character->GuildId : 0;
    if (online && online->GetGuildId()) facts.guild = online->GetGuildId();
    facts.level = online ? online->GetLevel() : (character ? character->Level : 0);
    facts.factionMatches = character && leader
        && Player::TeamIdForRace(character->Race) == Player::TeamIdForRace(leader->Race);
    return GuildRecruitReason(facts);
}

std::string GrowAdmission(Queued const& entry, std::uint32_t guid)
{
    auto& module = RealmModule::Instance();
    auto const destination = entry.payload.at("guild_id").get<std::uint32_t>();
    auto* guild = sGuildMgr->GetGuildById(destination);
    if (destination == GuildId() || !module.RealmGuilds().contains(destination) || IsRealGuild(destination))
        return "refused_guild";
    if (!guild) return "guild not found";
    if (auto why = RecruitReason(guid, *guild); !why.empty()) return why + ":" + std::to_string(guid);
    return {};
}

LeeroyCommandResult Rename(nlohmann::json const& message)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return {false, "realm_off"};
    std::string why;
    auto const command = ParseGuildRename(message, why);
    if (!command) return {false, why};
    if (command->guildId == GuildId()) return {false, "refused_guild"};
    auto* guild = sGuildMgr->GetGuildById(command->guildId);
    if (!guild) return {false, "guild not found"};
    if (IsRealGuild(command->guildId)) return {false, "refused_guild"};
    if (!ObjectMgr::IsValidCharterName(command->name)) return {false, "bad_name"};
    auto* held = sGuildMgr->GetGuildByName(command->name);
    if (held && held != guild) return {false, "name_taken"};
    auto const epoch = module.Epoch();
    if (!guild->SetName(command->name)) return {false, "bad_name"};
    return module.On() && module.Epoch() == epoch && sGuildMgr->GetGuildById(command->guildId) == guild
        && guild->GetName() == command->name ? LeeroyCommandResult{true, ""}
        : LeeroyCommandResult{false, "guild_rename_unproved"};
}

LeeroyCommandResult Admit(nlohmann::json const& message)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return {false, "realm_off"};
    std::string why;
    auto const command = ParseGuildAdmit(message, why);
    if (!command) return {false, why};
    auto const configuredGuild = GuildId(), human = HumanGuid();
    auto* guild = sGuildMgr->GetGuildById(configuredGuild);
    if (!guild) return {false, "guild not found"};
    auto const ranks = RealmGuildRankCount(*guild);
    if (command->rank >= ranks || ranks > GUILD_RANKS_MAX_COUNT) return {false, "no_rank"};
    auto const* candidate = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(command->guid));
    auto* connected = ObjectAccessor::FindConnectedPlayer(PlayerGuid(command->guid));
    if ((candidate && candidate->GuildId) || (connected && connected->GetGuildId()))
        return {false, "not_guildless"};
    if (!(why = RecruitReason(command->guid, *guild)).empty()) return {false, why};
    if (!(why = module.MutationReason(command->guid)).empty()) return {false, why};
    auto const generation = module.BodyGeneration(command->guid);
    auto const before = Identity(command->guid);
    if (!before || before->guild != 0) return {false, "not_guildless"};
    auto const epoch = module.Epoch();
    bool const added = guild->AddMember(PlayerGuid(command->guid), static_cast<uint8>(command->rank));
    if (!module.On() || module.Epoch() != epoch || GuildId() != configuredGuild || HumanGuid() != human
        || module.BodyGeneration(command->guid) != generation
        || sGuildMgr->GetGuildById(configuredGuild) != guild || !module.Holder(command->guid).empty()
        || !module.MutationReason(command->guid).empty()) return {false, "guild_join_unproved"};
    auto const current = Identity(command->guid);
    auto const* member = guild->GetMember(PlayerGuid(command->guid));
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(command->guid));
    auto expected = *before; expected.guild = configuredGuild;
    if (!added)
        return !member && character && character->GuildId == 0 && current == before
            ? LeeroyCommandResult{false, "refused"} : LeeroyCommandResult{false, "guild_join_unproved"};
    if (!member || member->GetGUID() != PlayerGuid(command->guid) || member->GetRankId() != command->rank
        || member->GetAccountId() != before->account || member->GetClass() != before->classId
        || member->GetGender() != before->sex || member->GetLevel() != 80
        || !character || character->GuildId != configuredGuild || character->AccountId != before->account
        || !current || *current != expected) return {false, "guild_join_unproved"};
    // A generic bridge command deliberately does not call GuardState::AllowRejoin.
    return {true, ""};
}

// Consented re-admission of exactly one member he kicked, bound to the guard's
// receipt of that kick. guild_admit keeps refusing every rejoin.
LeeroyCommandResult Readmit(nlohmann::json const& message)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return {false, "realm_off"};
    std::string why;
    auto const command = ParseGuildReadmit(message, why);
    if (!command) return {false, why};
    auto const configuredGuild = GuildId(), human = HumanGuid();
    auto* guild = sGuildMgr->GetGuildById(configuredGuild);
    if (!guild) return {false, "guild not found"};
    auto* guard = module.Guard();
    auto const ranks = RealmGuildRankCount(*guild);
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(command->guid));
    auto* connected = ObjectAccessor::FindConnectedPlayer(PlayerGuid(command->guid));
    auto const* leader = sCharacterCache->GetCharacterCacheByGuid(guild->GetLeaderGUID());
    ReadmitFacts facts;
    facts.guardAvailable = guard && !CastGuardFactsUnknown();
    facts.kickPending = guard && (guard->KickPending(command->guid) || CastGuardKickFactPending(command->guid));
    facts.human = command->guid == human;
    facts.actor = module.IsActor(command->guid);
    facts.exists = character != nullptr;
    facts.randomAccount = IsOnRandomBotAccount(command->guid);
    facts.member = IsConfiguredGuildMember(command->guid);
    facts.guild = character ? character->GuildId : 0;
    if (connected && connected->GetGuildId()) facts.guild = connected->GetGuildId();
    facts.factionMatches = character && leader
        && Player::TeamIdForRace(character->Race) == Player::TeamIdForRace(leader->Race);
    facts.rankExists = command->rank < ranks && ranks <= GUILD_RANKS_MAX_COUNT;
    auto const unixNow = NowUnixMs();
    facts.nowUnixMs = unixNow > 0 ? static_cast<std::uint64_t>(unixNow) : 0;
    auto const decision = DecideReadmit(guard ? guard->Receipt(command->guid) : nullptr, *command, facts);
    if (decision.verdict == ReadmitVerdict::AlreadyDone) return {true, ""}; // Idempotent: no second add.
    if (decision.verdict != ReadmitVerdict::Admit) return {false, decision.reason};
    if (!(why = module.MutationReason(command->guid)).empty()) return {false, why};
    auto const generation = module.BodyGeneration(command->guid);
    auto const before = Identity(command->guid);
    if (!before || before->guild != 0) return {false, "not_guildless"};
    auto const epoch = module.Epoch();
    bool const added = guild->AddMember(PlayerGuid(command->guid), static_cast<uint8>(command->rank));
    auto const unproved = [&]
    {
        guard->ReadmitUnknown(command->guid, command->kickTs); // This receipt never authorizes another add.
        return LeeroyCommandResult{false, "guild_join_unproved"};
    };
    if (!module.On() || module.Epoch() != epoch || GuildId() != configuredGuild || HumanGuid() != human
        || module.Guard() != guard || module.BodyGeneration(command->guid) != generation
        || sGuildMgr->GetGuildById(configuredGuild) != guild || !module.Holder(command->guid).empty()
        || !module.MutationReason(command->guid).empty()) return unproved();
    auto const current = Identity(command->guid);
    auto const* member = guild->GetMember(PlayerGuid(command->guid));
    auto const* after = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(command->guid));
    auto expected = *before; expected.guild = configuredGuild;
    if (!added)
        return !member && after && after->GuildId == 0 && current == before
            ? LeeroyCommandResult{false, "refused"} : unproved();
    if (!member || member->GetGUID() != PlayerGuid(command->guid) || member->GetRankId() != command->rank
        || member->GetAccountId() != before->account || member->GetClass() != before->classId
        || member->GetGender() != before->sex
        || !after || after->GuildId != configuredGuild || after->AccountId != before->account
        || !current || *current != expected) return unproved();
    // Only this proved add lifts his exclusion, and only for this exact kick.
    if (!guard->CompleteReadmit(command->guid, command->kickTs)) return unproved();
    return {true, ""};
}

LeeroyCommandResult Note(nlohmann::json const& message)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return {false, "realm_off"};
    std::string why;
    auto const command = ParseGuildNote(message, why);
    if (!command) return {false, why};
    if (!IsCast(command->guid)) return {false, "not_member"};
    auto const configuredGuild = GuildId(), human = HumanGuid();
    auto* guild = sGuildMgr->GetGuildById(configuredGuild);
    auto* member = guild ? guild->GetMember(PlayerGuid(command->guid)) : nullptr;
    if (!member) return {false, "not_member"};
    if (!(why = module.MutationReason(command->guid)).empty()) return {false, why};
    auto const generation = module.BodyGeneration(command->guid);
    auto const before = Identity(command->guid);
    if (!before || before->guild != configuredGuild) return {false, "not_member"};
    auto const epoch = module.Epoch();
    member->SetOfficerNote(command->officerNote);
    if (!module.On() || module.Epoch() != epoch || GuildId() != configuredGuild || HumanGuid() != human
        || module.BodyGeneration(command->guid) != generation
        || sGuildMgr->GetGuildById(configuredGuild) != guild || Identity(command->guid) != before
        || !module.MutationReason(command->guid).empty()) return {false, "guild_note_unproved"};
    auto const* after = guild->GetMember(PlayerGuid(command->guid));
    return after && after->GetOfficerNote() == command->officerNote
        ? LeeroyCommandResult{true, ""} : LeeroyCommandResult{false, "guild_note_unproved"};
}

std::string AchievementAuthority(std::uint32_t guid)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return "realm_off";
    if (!guid || (guid != HumanGuid() && !IsCast(guid))) return "not_member";
    if (!module.Holder(guid).empty()) return "busy:realm_owner";
    return guid == HumanGuid() ? NativeOwnershipReason(guid) : module.MutationReason(guid);
}

LeeroyCommandResult Grant(nlohmann::json const& message)
{
    auto& module = RealmModule::Instance();
    std::string why;
    auto const command = ParseGrantAchievement(message, why);
    if (!command) return {false, why};
    if (!(why = AchievementAuthority(command->guid)).empty()) return {false, why};
    auto* player = FindOnline(command->guid);
    if (!player) return {false, "offline"};
    auto const* achievement = sAchievementStore.LookupEntry(command->achievementId);
    if (!achievement) return {false, "refused"};
    auto const generation = module.BodyGeneration(command->guid);
    auto const before = Identity(command->guid);
    if (!before || before->player != player) return {false, "offline"};
    auto const epoch = module.Epoch();
    auto const configuredGuild = GuildId(), human = HumanGuid();
    why = ApplyGrantAchievement(command->achievementId, player->IsGameMaster(),
        player->HasAchieved(command->achievementId), [&]
        {
            LeeroyQuietScope quiet;
            player->CompletedAchievement(achievement);
        }, [&]
        {
            auto* current = FindOnline(command->guid);
            return module.Epoch() == epoch && module.BodyGeneration(command->guid) == generation
                && GuildId() == configuredGuild && HumanGuid() == human
                && AchievementAuthority(command->guid).empty() && Identity(command->guid) == before
                && current == player && current->HasAchieved(command->achievementId);
        });
    return {why.empty(), why};
}
}

std::size_t RealmGuildRankCount(::Guild const& guild) { return GuildRankView::Count(guild); }

void RegisterRealmGuildHandlers()
{
    auto& module = RealmModule::Instance();
    module.RegisterExecutor("guild_grow", [](Queued& entry, std::uint32_t guid)
    {
        return RealmModule::Instance().PerformGuildGrowJoin(entry.id, guid,
            entry.payload.at("guild_id").get<std::uint32_t>());
    }, GrowAdmission);
    RegisterCommand("guild_grow", [](nlohmann::json const& message) -> LeeroyCommandResult
    {
        std::string why;
        auto const command = ParseGuildGrow(message, why);
        if (!command) return {false, why};
        Queued entry;
        entry.id = message.at("id").get<std::string>(); entry.kind = "guild_grow";
        entry.guids = command->guids; entry.payload = {{"guild_id", command->guildId}};
        return RealmModule::Instance().Accept(std::move(entry), why) ? LeeroyCommandResult{true, ""}
            : LeeroyCommandResult{false, why};
    });
    RegisterCommand("guild_rename", Rename);
    RegisterCommand("guild_admit", Admit);
    RegisterCommand("guild_readmit", Readmit);
    RegisterCommand("guild_note", Note);
    RegisterCommand("grant_achievement", Grant);
}
}

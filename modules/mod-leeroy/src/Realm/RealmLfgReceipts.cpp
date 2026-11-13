// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmLfgReceipts.h"
#include "RealmPorts.h"
#include "GroupMgr.h"
#include "Opcodes.h"
#include "ScriptMgr.h"
#include "WorldPacket.h"
#include "WorldSession.h"
#include <mutex>

namespace Leeroy::Realm
{
namespace
{
struct Inbox { std::mutex mutex; LfgReceiptBook book; } inbox;
thread_local std::uint64_t ownedAbort = 0;
void Epoch() { inbox.book.SetEpoch(RealmModule::Instance().Epoch()); }
LfgObservedLife Life(Player const* player)
{
    return player ? LfgObservedLife{player->GetGUID().GetCounter(), reinterpret_cast<std::uintptr_t>(player),
        reinterpret_cast<std::uintptr_t>(player->GetSession())} : LfgObservedLife{};
}
std::uint64_t GroupId(Group const* group) { return group ? group->GetGUID().GetRawValue() : 0; }
std::vector<std::uint32_t> Roster(Group const* group)
{
    std::vector<std::uint32_t> result;
    if (!group || group->GetMembersCount() > 40) return result;
    for (auto const& slot : group->GetMemberSlots())
    {
        if (!slot.guid.IsPlayer()) return {};
        result.push_back(slot.guid.GetCounter());
    }
    std::sort(result.begin(), result.end()); return result;
}
LfgObservedLife AddedLife(Group const* group, ObjectGuid guid)
{
    if (!group || group->GetMembersCount() > LfgReceiptBook::MemberCapacity) return {};
    for (auto* ref = group->GetFirstMember(); ref; ref = ref->next())
        if (auto* player = ref->GetSource(); player && player->GetGUID() == guid) return Life(player);
    return {};
}
bool Ordinary(Group const* group)
{
    return group && !group->isLFGGroup() && !group->isBGGroup() && !group->isBFGroup() && !group->isRaidGroup();
}
// The packet hook copies bounded packet bytes and its direct recipient only.
// It never resolves another Player, Group, LFG manager row or scheduler lease.
std::optional<LfgRoleReceipt> Decode(WorldPacket const& packet)
{
    auto const size = packet.size();
    if (size < 7 || size > 7 + 255 * 4 + LfgReceiptBook::MemberCapacity * 14) return std::nullopt;
    auto const* data = packet.contents();
    std::size_t cursor = 0;
    auto read = [&](unsigned count) -> std::uint64_t
    {
        std::uint64_t result = 0;
        for (unsigned i = 0; i < count; ++i) result |= std::uint64_t(data[cursor++]) << (8 * i);
        return result;
    };
    auto const state = read(4); read(1);
    auto const dungeons = read(1);
    if (cursor + dungeons * 4 + 1 > size) return std::nullopt;
    cursor += static_cast<std::size_t>(dungeons * 4);
    auto const count = read(1);
    if (!count || count > LfgReceiptBook::MemberCapacity || cursor + count * 14 != size) return std::nullopt;
    LfgRoleReceipt result;
    result.state = state == lfg::LFG_ROLECHECK_INITIALITING ? LfgRoleReceipt::State::Initial
        : state == lfg::LFG_ROLECHECK_FINISHED ? LfgRoleReceipt::State::Finished
        : state == lfg::LFG_ROLECHECK_ABORTED ? LfgRoleReceipt::State::Aborted : LfgRoleReceipt::State::Other;
    for (std::size_t i = 0; i < count; ++i)
    {
        ObjectGuid const guid(read(8));
        if (!guid.IsPlayer() || !guid.GetCounter()) return std::nullopt;
        if (!i) result.leader = guid.GetCounter();
        result.members.push_back(guid.GetCounter());
        read(1); read(4); read(1);
    }
    return result;
}
class LfgReceiptPlayers final : public PlayerScript
{
public:
    LfgReceiptPlayers() : PlayerScript("LeeroyRealmLfgReceiptPlayers", {PLAYERHOOK_CAN_JOIN_LFG,
        PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT}) { }
    bool OnPlayerCanJoinLfg(Player* player, uint8, std::set<uint32>&, std::string const&) override
    {
        if (!player) return true;
        auto* group = player->GetGroup();
        auto members = group ? Roster(group) : std::vector<std::uint32_t>{player->GetGUID().GetCounter()};
        std::lock_guard lock(inbox.mutex); Epoch();
        if (inbox.book.Fenced(GroupId(group), members)) return false;
        if (!Ordinary(group)) { inbox.book.Changed(GroupId(group)); return true; }
        return inbox.book.Join(GroupId(group), reinterpret_cast<std::uintptr_t>(group), Life(player));
    }
    void Changed(Player* player)
    {
        if (!player) return;
        std::lock_guard lock(inbox.mutex); Epoch(); inbox.book.Lifecycle(player->GetGUID().GetCounter());
    }
    void OnPlayerLogin(Player* player) override { Changed(player); }
    void OnPlayerLogout(Player* player) override { Changed(player); }
};
class LfgReceiptGroups final : public GroupScript
{
public:
    LfgReceiptGroups() : GroupScript("LeeroyRealmLfgReceiptGroups", {GROUPHOOK_ON_CREATE, GROUPHOOK_ON_ADD_MEMBER,
        GROUPHOOK_ON_REMOVE_MEMBER, GROUPHOOK_ON_CHANGE_LEADER, GROUPHOOK_ON_DISBAND, GROUPHOOK_ON_INVITE_MEMBER}) { }
    void OnCreate(Group* group, Player* leader) override
    {
        auto const members = Roster(group);
        std::lock_guard lock(inbox.mutex); Epoch();
        inbox.book.ObserveGroup(GroupId(group), members);
        if (Ordinary(group) && leader && members == std::vector<std::uint32_t>{leader->GetGUID().GetCounter()})
            inbox.book.Born(GroupId(group), reinterpret_cast<std::uintptr_t>(group), Life(leader));
    }
    void OnAddMember(Group* group, ObjectGuid guid) override
    {
        auto const life = AddedLife(group, guid);
        auto const members = Roster(group);
        std::lock_guard lock(inbox.mutex); Epoch();
        inbox.book.ObserveGroup(GroupId(group), members);
        if (Ordinary(group)) inbox.book.Added(GroupId(group), reinterpret_cast<std::uintptr_t>(group), life);
        else inbox.book.Changed(GroupId(group));
    }
    void Changed(Group* group)
    {
        auto const members = Roster(group);
        std::lock_guard lock(inbox.mutex); Epoch();
        inbox.book.ObserveGroup(GroupId(group), members); inbox.book.Changed(GroupId(group));
    }
    void OnRemoveMember(Group* group, ObjectGuid, RemoveMethod, ObjectGuid, char const*) override { Changed(group); }
    void OnChangeLeader(Group* group, ObjectGuid, ObjectGuid) override { Changed(group); }
    void OnDisband(Group* group) override { Changed(group); }
    // An invitation does not add to the private LFG set, but invalidates an
    // existing applicant receipt. Before first join it must preserve birth.
    void OnInviteMember(Group* group, ObjectGuid) override
    {
        std::lock_guard lock(inbox.mutex); Epoch();
        inbox.book.Invited(GroupId(group));
    }
};
class LfgReceiptPackets final : public ServerScript
{
public:
    LfgReceiptPackets() : ServerScript("LeeroyRealmLfgReceiptPackets", {SERVERHOOK_ON_PACKET_SENT}) { }
    void OnPacketSent(WorldSession* session, WorldPacket const& packet) override
    {
        if (!session || packet.GetOpcode() != SMSG_LFG_ROLE_CHECK_UPDATE) return;
        auto* player = session->GetPlayer();
        if (!player) return;
        auto const life = Life(player);
        auto const decoded = Decode(packet);
        std::lock_guard lock(inbox.mutex); Epoch();
        if (!decoded || reinterpret_cast<std::uintptr_t>(session) != life.session) inbox.book.Lifecycle(life.guid);
        else inbox.book.Packet(life, *decoded, ownedAbort);
    }
};
class LfgReceiptProposals final : public GlobalScript
{
public:
    LfgReceiptProposals() : GlobalScript("LeeroyRealmLfgReceiptProposals", {GLOBALHOOK_CAN_CREATE_LFG_PROPOSAL}) { }
    bool CanCreateLfgProposal(lfg::Lfg5Guids const& guids) override
    {
        std::vector<std::uint64_t> groups;
        std::vector<std::uint32_t> players;
        for (uint8 i = 0; i < 5 && guids.guids[i]; ++i)
            if (guids.guids[i].IsGroup()) groups.push_back(guids.guids[i].GetRawValue());
            else if (guids.guids[i].IsPlayer()) players.push_back(guids.guids[i].GetCounter());
        std::lock_guard lock(inbox.mutex); Epoch(); return inbox.book.Proposal(groups, players);
    }
};
class LfgReceiptWorld final : public WorldScript
{
public:
    LfgReceiptWorld() : WorldScript("LeeroyRealmLfgReceiptWorld", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32) override
    {
        std::lock_guard lock(inbox.mutex); Epoch();
        std::map<std::string, bool> alive;
        for (auto const& [operation, members] : inbox.book.Owners())
            for (auto guid : members)
                alive[operation] = alive[operation] || RealmModule::Instance().Holder(guid) == operation;
        for (auto const& [operation, held] : alive) if (!held) inbox.book.Retire(operation);
    }
};
// All global manager/body lookups are world-thread only. Group/current count
// merely cross-check the witnessed fresh-birth/add producer chain and complete
// actual roles packet. They cannot manufacture a missing producer receipt.
bool NativeExact(LfgApplicantReceipt const& receipt, bool cancelled, std::uint32_t* failedGuid = nullptr)
{
    auto const guid = ObjectGuid(receipt.group);
    auto* group = sGroupMgr->GetGroupByGUID(guid.GetCounter());
    if (!Ordinary(group) || group->GetGUID() != guid || reinterpret_cast<std::uintptr_t>(group) != receipt.object
        || group->GetLeaderGUID() != PlayerGuid(receipt.leader) || group->GetInviteeCount()
        || Roster(group) != LfgReceiptGuids(receipt) || sLFGMgr->GetPlayerCount(guid) != receipt.members.size()
        || sLFGMgr->GetOldState(guid) != lfg::LFG_STATE_NONE) return false;
    auto const state = cancelled ? lfg::LFG_STATE_NONE : receipt.queued ? lfg::LFG_STATE_QUEUED : lfg::LFG_STATE_ROLECHECK;
    if (sLFGMgr->GetState(guid) != state) return false;
    std::vector<std::uint32_t> linked;
    for (auto* ref = group->GetFirstMember(); ref; ref = ref->next())
        if (auto* player = ref->GetSource()) linked.push_back(player->GetGUID().GetCounter());
    std::sort(linked.begin(), linked.end());
    if (linked != LfgReceiptGuids(receipt)) return false;
    for (auto const& life : receipt.members)
    {
        auto* player = FindOnline(life.guid);
        if (!player || Life(player) != life || player->GetSession()->PlayerLogout() || player->GetGroup() != group
            || player->GetOriginalGroup() || player->InBattleground() || player->IsBeingTeleported()
            || sLFGMgr->GetState(player->GetGUID()) != state || sLFGMgr->GetGroup(player->GetGUID()) != guid
            || sLFGMgr->GetOldState(player->GetGUID()) != lfg::LFG_STATE_NONE)
        {
            if (failedGuid) *failedGuid = life.guid;
            return false;
        }
    }
    return true;
}
}
std::optional<LfgApplicantReceipt> ReadLfgApplicants(std::uint64_t group, bool queued)
{
    std::optional<LfgApplicantReceipt> receipt;
    { std::lock_guard lock(inbox.mutex); Epoch(); receipt = inbox.book.Proof(group, queued); }
    if (!receipt || !NativeExact(*receipt, false)) return std::nullopt;
    std::lock_guard lock(inbox.mutex); Epoch();
    return inbox.book.Proof(group, queued) == receipt ? receipt : std::nullopt;
}
std::string BeginLfgApplicantCleanup(std::string const& operation, LfgApplicantReceipt const& receipt)
{
    if (!NativeExact(receipt, false)) return "lfg_applicant_lineage_changed";
    auto& module = RealmModule::Instance();
    for (auto const& life : receipt.members)
        if (module.Holder(life.guid) != operation || !module.ValidateOwnedBody(life.guid, operation).empty())
            return "lfg_applicant_owner_changed";
    std::lock_guard lock(inbox.mutex); Epoch();
    return inbox.book.Fence(operation, receipt) ? "" : "lfg_applicant_fence_unavailable";
}
void CancelLfgApplicants(LfgApplicantReceipt const& receipt)
{
    struct AbortScope
    {
        std::uint64_t previous;
        explicit AbortScope(std::uint64_t serial) : previous(ownedAbort) { ownedAbort = serial; }
        ~AbortScope() { ownedAbort = previous; }
    } scope(receipt.queued ? 0 : receipt.join);
    sLFGMgr->LeaveLfg(ObjectGuid(receipt.group));
}
bool VerifyLfgApplicantCleanup(std::string const& operation, std::uint64_t group, std::uint64_t join, std::uint32_t* failedGuid)
{
    if (failedGuid) *failedGuid = 0;
    std::optional<LfgApplicantReceipt> receipt;
    { std::lock_guard lock(inbox.mutex); Epoch(); receipt = inbox.book.Held(operation, group, join); }
    if (!receipt || !NativeExact(*receipt, true, failedGuid)) return false;
    for (auto const& life : receipt->members)
        if (RealmModule::Instance().Holder(life.guid) != operation
            || !RealmModule::Instance().ValidateOwnedBody(life.guid, operation).empty())
        {
            if (failedGuid) *failedGuid = life.guid;
            return false;
        }
    std::lock_guard lock(inbox.mutex); Epoch(); return inbox.book.Verify(operation, *receipt);
}
void AddRealmLfgReceiptScripts()
{
    new LfgReceiptPlayers(); new LfgReceiptGroups(); new LfgReceiptPackets(); new LfgReceiptProposals(); new LfgReceiptWorld();
}
}

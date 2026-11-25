// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoHuman.h"
#if DOJO_TRAINING_BUILD
#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "CharacterCache.h"
#include "DatabaseEnv.h"
#include "MotionMaster.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "Playerbots.h"
#include "World.h"
#include "WorldScript.h"
#include "WorldSession.h"
#include <memory>

namespace
{
constexpr char const* Account = "DOJORAID";
constexpr char const* Name = "Dojomaster";
WorldSession* session = nullptr;
ObjectGuid identity;
std::string ownerToken, failure;

void Close()
{
    if (session)
    {
        session->LogoutPlayer(true);
        delete session;
        session = nullptr;
    }
    identity.Clear();
    ownerToken.clear();
}

void Update()
{
    if (!session) return;
    session->ProcessQueryCallbacks();
    Player* player = session->GetPlayer();
    if (!player) return;
    if (player->GetGUID() != identity || GET_PLAYERBOT_AI(player))
    {
        failure = "human_fixture_identity_or_ai_changed";
        return;
    }
    // A scripted client acknowledges relocation through the ordinary handlers.
    // It never supplies movement or combat choices for a measured bot.
    if (player->IsBeingTeleportedFar()) session->HandleMoveWorldportAck();
    else if (player->IsBeingTeleportedNear() && player->IsInWorld())
    {
        WorldPacket ack(MSG_MOVE_TELEPORT_ACK, 20);
        ack << player->GetPackGUID() << uint32(0) << uint32(0);
        session->HandleMoveTeleportAck(ack);
    }
}
class Ticks : public WorldScript
{
public:
    Ticks() : WorldScript("DojoHumanTicks", {WORLDHOOK_ON_UPDATE, WORLDHOOK_ON_SHUTDOWN}) { }
    void OnUpdate(uint32) override { Update(); }
    void OnShutdown() override { Close(); }
};
}

namespace Dojo
{
std::string BeginHumanFixture(std::string const& token)
{
    if (State().plan.state != RuntimeState::Armed || State().manifest) return "human_fixture_not_armed";
    if (token.empty() || token.size() > 64 ||
        token.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
        return "human_fixture_invalid_token";
    if (session) return ownerToken == token ? failure : "human_fixture_busy";
    uint32 const account = AccountMgr::GetId(Account);
    if (!account) return "human_fixture_account_missing";
    ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(Name);
    if (!guid)
    {
        auto* creating = new WorldSession(account, std::string(), 0, nullptr, SEC_PLAYER,
            EXPANSION_WRATH_OF_THE_LICH_KING, time_t(0), sWorld->GetDefaultDbcLocale(), 0, false, false, 0);
        auto* player = new Player(creating);
        player->GetMotionMaster()->Initialize();
        CharacterCreateInfo info(Name, RACE_HUMAN, CLASS_WARRIOR, GENDER_MALE, 0, 0, 0, 0, 0);
        if (player->Create(sObjectMgr->GetGenerator<HighGuid::Player>().Generate(), &info))
        {
            player->setCinematic(2);
            player->SetAtLoginFlag(AT_LOGIN_NONE);
            player->SaveToDB(true, false);
            guid = player->GetGUID();
            sCharacterCache->AddCharacterCacheEntry(guid, account, player->GetName(), player->getGender(),
                player->getRace(), player->getClass(), player->GetLevel());
        }
        player->CleanupsBeforeDelete();
        delete player;
        delete creating;
    }
    if (!guid || sCharacterCache->GetCharacterAccountIdByGuid(guid) != account)
        return "human_fixture_account_mismatch";
    if (ObjectAccessor::FindConnectedPlayer(guid)) return "human_fixture_already_online";
    auto holder = std::make_shared<LoginQueryHolder>(account, guid);
    if (!holder->Initialize()) return "human_fixture_query_failed";
    session = new WorldSession(account, std::string(), 0, nullptr, SEC_PLAYER,
        EXPANSION_WRATH_OF_THE_LICH_KING, time_t(0), sWorld->GetDefaultDbcLocale(), 0, false, false, 0);
    identity = guid;
    ownerToken = token;
    failure.clear();
    session->AddQueryHolderCallback(CharacterDatabase.DelayQueryHolder(holder)).AfterComplete(
        [](SQLQueryHolderBase const& query)
        {
            session->HandlePlayerLoginFromDB(static_cast<LoginQueryHolder const&>(query));
            if (!session->GetPlayer()) failure = "human_fixture_login_failed";
        });
    return {};
}

Player* HumanFixture(std::string const& token)
{
    if (!session || token != ownerToken || !failure.empty()) return nullptr;
    Player* player = session->GetPlayer();
    return player && player->GetGUID() == identity && player->IsInWorld() && !player->IsDuringRemoveFromWorld()
        && !GET_PLAYERBOT_AI(player) ? player : nullptr;
}
bool EndHumanFixture(std::string const& token)
{
    if (!session || token != ownerToken) return false;
    ObjectGuid const guid = identity;
    Close();
    return ObjectAccessor::FindConnectedPlayer(guid) == nullptr;
}
std::string HumanFixtureError(std::string const& token)
{
    return session && token == ownerToken ? failure : "human_fixture_not_owned";
}
}
void AddSC_dojo_human() { new Ticks(); }
#else
void AddSC_dojo_human() { }
#endif
#else
void AddSC_dojo_human() { }
#endif

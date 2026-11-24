// Compile the production ExecuteSay with fake game objects and packet delivery.
#include "LeeroyProtocol.h"
#include "TestHarness.h"

namespace
{
int delivered = 0;
int published = 0;
constexpr int LANG_UNIVERSAL = 0;
constexpr std::string_view kBadChannel = "bad field: channel";
struct Player
{
    bool combat = false;
    bool IsInCombat() const { return combat; }
    int GetGUID() const { return 101; }
    int GetSession() const { return 0; }
    void Say(std::string const&, int) { ++delivered; }
    void Yell(std::string const&, int) { ++delivered; }
    void Whisper(std::string const&, int, Player*) { ++delivered; }
};
struct PlayerbotAI
{
    bool SayToParty(std::string const&) { ++delivered; return true; }
    bool SayToRaid(std::string const&) { ++delivered; return true; }
};
struct Guild
{
    bool GetMember(int) const { return true; }
    void BroadcastToGuild(int, bool, std::string const&, int) { ++delivered; }
};
struct GuildMgr
{
    Guild guild;
    Guild* GetGuildById(int) { return &guild; }
} guildMgr;
auto* sGuildMgr = &guildMgr;
struct LeeroyModule
{
    static LeeroyModule& Instance() { static LeeroyModule module; return module; }
    int GuildId() const { return 21; }
    unsigned HumanGuid() const { return 1; }
};
Player bot, human;
PlayerbotAI ai;
bool humanOnline = true;
struct Speaker { Player* bot; PlayerbotAI* botAI; std::string_view error; };
Speaker FindSpeaker(unsigned) { return {&bot, &ai, {}}; }
Player* FindHuman() { return humanOnline ? &human : nullptr; }
unsigned PlayerGuid(unsigned guid) { return guid; }
struct ObjectAccessor
{
    static Player* FindConnectedPlayer(unsigned) { return FindHuman(); }
};
std::string LeeroyBotChatText(Player*, std::string const& text) { return text; }
void LeeroyPublishChat(Player*, Leeroy::Channel, std::string const&, unsigned) { ++published; }

#include "ExecuteSay.inc"
}

LEEROY_TEST(CombatQuiet_RechecksBothPlayersAtDeliveryOnEveryGameChannel)
{
    for (auto channel : {Leeroy::Channel::Guild, Leeroy::Channel::Officer, Leeroy::Channel::Party,
        Leeroy::Channel::Raid, Leeroy::Channel::Say, Leeroy::Channel::Yell, Leeroy::Channel::Whisper})
    {
        auto decoded = Leeroy::DecodeCommand(R"({"v":1,"id":"s","type":"say","guid":101,
            "channel":"whisper","to_guid":1,"text":"Ready.","quiet_in_combat":true})");
        CHECK(decoded.command);
        auto cmd = *decoded.command;
        cmd.channel = channel;
        humanOnline = true;
        bot.combat = human.combat = false;
        delivered = published = 0;
        CHECK(ExecuteSay(cmd).empty());
        CHECK_EQ(delivered, 1);
        int const before = published;
        human.combat = true;  // combat starts after the command was decoded/queued
        CHECK_EQ(ExecuteSay(cmd), Leeroy::Err::CombatQuiet);
        human.combat = false;
        bot.combat = true;
        CHECK_EQ(ExecuteSay(cmd), Leeroy::Err::CombatQuiet);
        CHECK_EQ(delivered, 1);
        CHECK_EQ(published, before);  // no echo for an undelivered line
        cmd.quietInCombat = false;   // explicit answers/tactics and old clients
        CHECK(ExecuteSay(cmd).empty());
        CHECK_EQ(delivered, 2);
        bot.combat = false;
        cmd.quietInCombat = true;
        CHECK(ExecuteSay(cmd).empty());  // combat end needs no stale telemetry reset
        CHECK_EQ(delivered, 3);
    }
}

LEEROY_TEST(CombatQuiet_OfflineHumanDoesNotBlockIdleBot)
{
    Leeroy::Command cmd;
    cmd.guid = 101;
    cmd.channel = Leeroy::Channel::Guild;
    cmd.text = "Ready.";
    cmd.quietInCombat = true;
    humanOnline = false;
    human.combat = true;
    bot.combat = false;
    delivered = published = 0;
    CHECK(ExecuteSay(cmd).empty());
    bot.combat = true;
    CHECK_EQ(ExecuteSay(cmd), Leeroy::Err::CombatQuiet);
    CHECK_EQ(delivered, 1);
    humanOnline = true;
    human.combat = bot.combat = false;
}

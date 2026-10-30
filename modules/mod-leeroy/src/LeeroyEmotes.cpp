// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyEmotes.h"

#include "SharedDefines.h"

#include <cctype>
#include <string>
#include <unordered_map>

namespace
{
struct TextEmoteName
{
    std::string_view name;
    uint32 id;
};

// Generated from enum TextEmotes in SharedDefines.h:
// grep -oE 'TEXT_EMOTE_[A-Z0-9_]+' src/server/shared/SharedDefines.h | sort -u |
//     awk '{printf "    {\"%s\", %s},\n", tolower(substr($0, 12)), $0}'
constexpr TextEmoteName kTextEmotes[] =
{
    {"absent", TEXT_EMOTE_ABSENT},
    {"agree", TEXT_EMOTE_AGREE},
    {"amaze", TEXT_EMOTE_AMAZE},
    {"angry", TEXT_EMOTE_ANGRY},
    {"apologize", TEXT_EMOTE_APOLOGIZE},
    {"applaud", TEXT_EMOTE_APPLAUD},
    {"arm", TEXT_EMOTE_ARM},
    {"attackmytarget", TEXT_EMOTE_ATTACKMYTARGET},
    {"awe", TEXT_EMOTE_AWE},
    {"backpack", TEXT_EMOTE_BACKPACK},
    {"badfeeling", TEXT_EMOTE_BADFEELING},
    {"bark", TEXT_EMOTE_BARK},
    {"bashful", TEXT_EMOTE_BASHFUL},
    {"beckon", TEXT_EMOTE_BECKON},
    {"beg", TEXT_EMOTE_BEG},
    {"bite", TEXT_EMOTE_BITE},
    {"blame", TEXT_EMOTE_BLAME},
    {"blank", TEXT_EMOTE_BLANK},
    {"bleed", TEXT_EMOTE_BLEED},
    {"blink", TEXT_EMOTE_BLINK},
    {"blush", TEXT_EMOTE_BLUSH},
    {"boggle", TEXT_EMOTE_BOGGLE},
    {"bonk", TEXT_EMOTE_BONK},
    {"bored", TEXT_EMOTE_BORED},
    {"bounce", TEXT_EMOTE_BOUNCE},
    {"bow", TEXT_EMOTE_BOW},
    {"brandish", TEXT_EMOTE_BRANDISH},
    {"brb", TEXT_EMOTE_BRB},
    {"breath", TEXT_EMOTE_BREATH},
    {"burp", TEXT_EMOTE_BURP},
    {"bye", TEXT_EMOTE_BYE},
    {"cackle", TEXT_EMOTE_CACKLE},
    {"calm", TEXT_EMOTE_CALM},
    {"challenge", TEXT_EMOTE_CHALLENGE},
    {"charge", TEXT_EMOTE_CHARGE},
    {"charm", TEXT_EMOTE_CHARM},
    {"cheer", TEXT_EMOTE_CHEER},
    {"chicken", TEXT_EMOTE_CHICKEN},
    {"chuckle", TEXT_EMOTE_CHUCKLE},
    {"chug", TEXT_EMOTE_CHUG},
    {"clap", TEXT_EMOTE_CLAP},
    {"cold", TEXT_EMOTE_COLD},
    {"comfort", TEXT_EMOTE_COMFORT},
    {"commend", TEXT_EMOTE_COMMEND},
    {"confused", TEXT_EMOTE_CONFUSED},
    {"congratulate", TEXT_EMOTE_CONGRATULATE},
    {"cough", TEXT_EMOTE_COUGH},
    {"coverears", TEXT_EMOTE_COVEREARS},
    {"cower", TEXT_EMOTE_COWER},
    {"crack", TEXT_EMOTE_CRACK},
    {"cringe", TEXT_EMOTE_CRINGE},
    {"crossarms", TEXT_EMOTE_CROSSARMS},
    {"cry", TEXT_EMOTE_CRY},
    {"cuddle", TEXT_EMOTE_CUDDLE},
    {"curious", TEXT_EMOTE_CURIOUS},
    {"curtsey", TEXT_EMOTE_CURTSEY},
    {"dance", TEXT_EMOTE_DANCE},
    {"ding", TEXT_EMOTE_DING},
    {"disagree", TEXT_EMOTE_DISAGREE},
    {"doubt", TEXT_EMOTE_DOUBT},
    {"drink", TEXT_EMOTE_DRINK},
    {"drool", TEXT_EMOTE_DROOL},
    {"duck", TEXT_EMOTE_DUCK},
    {"eat", TEXT_EMOTE_EAT},
    {"embarrass", TEXT_EMOTE_EMBARRASS},
    {"encourage", TEXT_EMOTE_ENCOURAGE},
    {"enemy", TEXT_EMOTE_ENEMY},
    {"eye", TEXT_EMOTE_EYE},
    {"eyebrow", TEXT_EMOTE_EYEBROW},
    {"facepalm", TEXT_EMOTE_FACEPALM},
    {"fail", TEXT_EMOTE_FAIL},
    {"faint", TEXT_EMOTE_FAINT},
    {"fart", TEXT_EMOTE_FART},
    {"fidget", TEXT_EMOTE_FIDGET},
    {"flee", TEXT_EMOTE_FLEE},
    {"flex", TEXT_EMOTE_FLEX},
    {"flirt", TEXT_EMOTE_FLIRT},
    {"flop", TEXT_EMOTE_FLOP},
    {"follow", TEXT_EMOTE_FOLLOW},
    {"frown", TEXT_EMOTE_FROWN},
    {"gasp", TEXT_EMOTE_GASP},
    {"gaze", TEXT_EMOTE_GAZE},
    {"giggle", TEXT_EMOTE_GIGGLE},
    {"glare", TEXT_EMOTE_GLARE},
    {"gloat", TEXT_EMOTE_GLOAT},
    {"glower", TEXT_EMOTE_GLOWER},
    {"go", TEXT_EMOTE_GO},
    {"going", TEXT_EMOTE_GOING},
    {"golfclap", TEXT_EMOTE_GOLFCLAP},
    {"goodluck", TEXT_EMOTE_GOODLUCK},
    {"greet", TEXT_EMOTE_GREET},
    {"grin", TEXT_EMOTE_GRIN},
    {"groan", TEXT_EMOTE_GROAN},
    {"grovel", TEXT_EMOTE_GROVEL},
    {"growl", TEXT_EMOTE_GROWL},
    {"guffaw", TEXT_EMOTE_GUFFAW},
    {"hail", TEXT_EMOTE_HAIL},
    {"happy", TEXT_EMOTE_HAPPY},
    {"headache", TEXT_EMOTE_HEADACHE},
    {"healme", TEXT_EMOTE_HEALME},
    {"hello", TEXT_EMOTE_HELLO},
    {"helpme", TEXT_EMOTE_HELPME},
    {"hiccup", TEXT_EMOTE_HICCUP},
    {"highfive", TEXT_EMOTE_HIGHFIVE},
    {"hiss", TEXT_EMOTE_HISS},
    {"holdhand", TEXT_EMOTE_HOLDHAND},
    {"hug", TEXT_EMOTE_HUG},
    {"hungry", TEXT_EMOTE_HUNGRY},
    {"hurry", TEXT_EMOTE_HURRY},
    {"idea", TEXT_EMOTE_IDEA},
    {"incoming", TEXT_EMOTE_INCOMING},
    {"insult", TEXT_EMOTE_INSULT},
    {"introduce", TEXT_EMOTE_INTRODUCE},
    {"jealous", TEXT_EMOTE_JEALOUS},
    {"jk", TEXT_EMOTE_JK},
    {"joke", TEXT_EMOTE_JOKE},
    {"kiss", TEXT_EMOTE_KISS},
    {"kneel", TEXT_EMOTE_KNEEL},
    {"laugh", TEXT_EMOTE_LAUGH},
    {"laydown", TEXT_EMOTE_LAYDOWN},
    {"lick", TEXT_EMOTE_LICK},
    {"listen", TEXT_EMOTE_LISTEN},
    {"look", TEXT_EMOTE_LOOK},
    {"lost", TEXT_EMOTE_LOST},
    {"love", TEXT_EMOTE_LOVE},
    {"luck", TEXT_EMOTE_LUCK},
    {"map", TEXT_EMOTE_MAP},
    {"mercy", TEXT_EMOTE_MERCY},
    {"message", TEXT_EMOTE_MESSAGE},
    {"moan", TEXT_EMOTE_MOAN},
    {"mock", TEXT_EMOTE_MOCK},
    {"moo", TEXT_EMOTE_MOO},
    {"moon", TEXT_EMOTE_MOON},
    {"mount_special", TEXT_EMOTE_MOUNT_SPECIAL},
    {"mourn", TEXT_EMOTE_MOURN},
    {"mutter", TEXT_EMOTE_MUTTER},
    {"nervous", TEXT_EMOTE_NERVOUS},
    {"no", TEXT_EMOTE_NO},
    {"nod", TEXT_EMOTE_NOD},
    {"nosepick", TEXT_EMOTE_NOSEPICK},
    {"object", TEXT_EMOTE_OBJECT},
    {"offer", TEXT_EMOTE_OFFER},
    {"oom", TEXT_EMOTE_OOM},
    {"openfire", TEXT_EMOTE_OPENFIRE},
    {"panic", TEXT_EMOTE_PANIC},
    {"pat", TEXT_EMOTE_PAT},
    {"peer", TEXT_EMOTE_PEER},
    {"pet", TEXT_EMOTE_PET},
    {"pinch", TEXT_EMOTE_PINCH},
    {"pity", TEXT_EMOTE_PITY},
    {"plead", TEXT_EMOTE_PLEAD},
    {"point", TEXT_EMOTE_POINT},
    {"poke", TEXT_EMOTE_POKE},
    {"ponder", TEXT_EMOTE_PONDER},
    {"pounce", TEXT_EMOTE_POUNCE},
    {"pout", TEXT_EMOTE_POUT},
    {"praise", TEXT_EMOTE_PRAISE},
    {"pray", TEXT_EMOTE_PRAY},
    {"promise", TEXT_EMOTE_PROMISE},
    {"proud", TEXT_EMOTE_PROUD},
    {"pulse", TEXT_EMOTE_PULSE},
    {"punch", TEXT_EMOTE_PUNCH},
    {"purr", TEXT_EMOTE_PURR},
    {"puzzle", TEXT_EMOTE_PUZZLE},
    {"raise", TEXT_EMOTE_RAISE},
    {"rasp", TEXT_EMOTE_RASP},
    {"ready", TEXT_EMOTE_READY},
    {"regret", TEXT_EMOTE_REGRET},
    {"revenge", TEXT_EMOTE_REVENGE},
    {"roar", TEXT_EMOTE_ROAR},
    {"rofl", TEXT_EMOTE_ROFL},
    {"rolleyes", TEXT_EMOTE_ROLLEYES},
    {"rude", TEXT_EMOTE_RUDE},
    {"ruffle", TEXT_EMOTE_RUFFLE},
    {"sad", TEXT_EMOTE_SAD},
    {"salute", TEXT_EMOTE_SALUTE},
    {"scared", TEXT_EMOTE_SCARED},
    {"scoff", TEXT_EMOTE_SCOFF},
    {"scold", TEXT_EMOTE_SCOLD},
    {"scowl", TEXT_EMOTE_SCOWL},
    {"scratch", TEXT_EMOTE_SCRATCH},
    {"search", TEXT_EMOTE_SEARCH},
    {"serious", TEXT_EMOTE_SERIOUS},
    {"sexy", TEXT_EMOTE_SEXY},
    {"shake", TEXT_EMOTE_SHAKE},
    {"shakefist", TEXT_EMOTE_SHAKEFIST},
    {"shifty", TEXT_EMOTE_SHIFTY},
    {"shimmy", TEXT_EMOTE_SHIMMY},
    {"shiver", TEXT_EMOTE_SHIVER},
    {"shoo", TEXT_EMOTE_SHOO},
    {"shout", TEXT_EMOTE_SHOUT},
    {"shrug", TEXT_EMOTE_SHRUG},
    {"shudder", TEXT_EMOTE_SHUDDER},
    {"shy", TEXT_EMOTE_SHY},
    {"sigh", TEXT_EMOTE_SIGH},
    {"signal", TEXT_EMOTE_SIGNAL},
    {"silence", TEXT_EMOTE_SILENCE},
    {"sing", TEXT_EMOTE_SING},
    {"sit", TEXT_EMOTE_SIT},
    {"slap", TEXT_EMOTE_SLAP},
    {"sleep", TEXT_EMOTE_SLEEP},
    {"smack", TEXT_EMOTE_SMACK},
    {"smile", TEXT_EMOTE_SMILE},
    {"smirk", TEXT_EMOTE_SMIRK},
    {"snap", TEXT_EMOTE_SNAP},
    {"snarl", TEXT_EMOTE_SNARL},
    {"sneak", TEXT_EMOTE_SNEAK},
    {"sneeze", TEXT_EMOTE_SNEEZE},
    {"snicker", TEXT_EMOTE_SNICKER},
    {"sniff", TEXT_EMOTE_SNIFF},
    {"snort", TEXT_EMOTE_SNORT},
    {"snub", TEXT_EMOTE_SNUB},
    {"soothe", TEXT_EMOTE_SOOTHE},
    {"spit", TEXT_EMOTE_SPIT},
    {"squeal", TEXT_EMOTE_SQUEAL},
    {"stand", TEXT_EMOTE_STAND},
    {"stare", TEXT_EMOTE_STARE},
    {"stink", TEXT_EMOTE_STINK},
    {"stopattack", TEXT_EMOTE_STOPATTACK},
    {"surprised", TEXT_EMOTE_SURPRISED},
    {"surrender", TEXT_EMOTE_SURRENDER},
    {"suspicious", TEXT_EMOTE_SUSPICIOUS},
    {"sweat", TEXT_EMOTE_SWEAT},
    {"talk", TEXT_EMOTE_TALK},
    {"talkex", TEXT_EMOTE_TALKEX},
    {"talkq", TEXT_EMOTE_TALKQ},
    {"tap", TEXT_EMOTE_TAP},
    {"taunt", TEXT_EMOTE_TAUNT},
    {"tease", TEXT_EMOTE_TEASE},
    {"thank", TEXT_EMOTE_THANK},
    {"think", TEXT_EMOTE_THINK},
    {"thirsty", TEXT_EMOTE_THIRSTY},
    {"threaten", TEXT_EMOTE_THREATEN},
    {"tickle", TEXT_EMOTE_TICKLE},
    {"tired", TEXT_EMOTE_TIRED},
    {"toast", TEXT_EMOTE_TOAST},
    {"train", TEXT_EMOTE_TRAIN},
    {"truce", TEXT_EMOTE_TRUCE},
    {"twiddle", TEXT_EMOTE_TWIDDLE},
    {"veto", TEXT_EMOTE_VETO},
    {"victory", TEXT_EMOTE_VICTORY},
    {"violin", TEXT_EMOTE_VIOLIN},
    {"wait", TEXT_EMOTE_WAIT},
    {"warn", TEXT_EMOTE_WARN},
    {"wave", TEXT_EMOTE_WAVE},
    {"welcome", TEXT_EMOTE_WELCOME},
    {"whine", TEXT_EMOTE_WHINE},
    {"whistle", TEXT_EMOTE_WHISTLE},
    {"wink", TEXT_EMOTE_WINK},
    {"work", TEXT_EMOTE_WORK},
    {"yawn", TEXT_EMOTE_YAWN},
    {"yw", TEXT_EMOTE_YW},
};

std::string Normalize(std::string_view name)
{
    std::string out;
    out.reserve(name.size());
    for (char c : name)
        if (c != '_')
            out.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
    return out;
}

std::unordered_map<std::string, uint32> const& ByName()
{
    static std::unordered_map<std::string, uint32> const byName = []
    {
        std::unordered_map<std::string, uint32> map;
        for (TextEmoteName const& entry : kTextEmotes)
            map.emplace(Normalize(entry.name), entry.id);
        return map;
    }();
    return byName;
}

std::unordered_map<uint32, std::string_view> const& ById()
{
    static std::unordered_map<uint32, std::string_view> const byId = []
    {
        std::unordered_map<uint32, std::string_view> map;
        for (TextEmoteName const& entry : kTextEmotes)
            map.emplace(entry.id, entry.name);
        return map;
    }();
    return byId;
}
}

std::optional<uint32> LeeroyTextEmoteId(std::string_view name)
{
    auto const& byName = ByName();
    auto const it = byName.find(Normalize(name));
    if (it == byName.end())
        return std::nullopt;
    return it->second;
}

std::string_view LeeroyTextEmoteName(uint32 id)
{
    auto const& byId = ById();
    auto const it = byId.find(id);
    return it == byId.end() ? std::string_view() : it->second;
}

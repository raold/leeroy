// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_VOCABULARY_H
#define MOD_LEEROY_TRAINING_VOCABULARY_H
#include "LeeroyTrainingProtocol.h"
#include "LeeroyProtocol.h"

namespace Leeroy::Training
{
struct VocabSetting
{
    std::string key;
    std::int64_t min = 0, max = 0, value = 0, raid = -1, heroic = -1;
};
struct Vocabulary
{
    std::string id;
    std::uint32_t cls = 0, probe = 0;
    std::vector<std::string> strategies, actions, spells, triggers, whitelist;
    bool spellsChecked = false;
    std::vector<VocabSetting> dc;
};
// Encoder-thread helper: an oversize result is an explicit failure, never a partial vocabulary.
inline std::optional<std::string> EncodeVocabulary(Vocabulary const& data, std::uint64_t timestamp)
{
    auto dc = nlohmann::json::array();
    for (auto const& row : data.dc)
        dc.push_back({{"key", row.key}, {"min", row.min}, {"max", row.max}, {"value", row.value},
            {"raid", row.raid}, {"heroic", row.heroic}});
    auto line = EncodeEvent(timestamp, {{"type", "vocab"}, {"id", data.id}, {"class", data.cls},
        {"probe", data.probe}, {"strategies", data.strategies}, {"actions", data.actions}, {"spells", data.spells},
        {"spells_checked", data.spellsChecked}, {"triggers", data.triggers}, {"dc", std::move(dc)},
        {"whitelist", data.whitelist}});
    if (line.size() > kMaxLineBytes) return {};
    return line;
}
}
#endif

// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

// The joint action catalog and its masks (the shared cases in tests/fixtures/action_cases.json, which
// dojo/tests/test_actions.py also runs) and the pending-command queue.

#include "DojoActionCatalog.h"
#include "DojoActionQueue.h"
#include "TestHarness.h"

#include <cstdint>
#include <fstream>
#include <numeric>
#include <sstream>
#include <string>

using namespace Dojo;
using nlohmann::json;

namespace
{
json const& Cases()
{
    static json const cases = []
    {
        std::ifstream in(std::string(DOJO_TEST_FIXTURES) + "/action_cases.json", std::ios::binary);
        std::ostringstream text;
        text << in.rdbuf();
        return json::parse(text.str());
    }();
    return cases;
}

TargetSlot Target(std::string const& name)
{
    return name == "enemy" ? TargetSlot::Enemy : name == "self" ? TargetSlot::Self : TargetSlot::None;
}

std::vector<KitAbility> Kit(json const& doc)
{
    std::vector<KitAbility> kit;
    for (json const& k : doc)
        kit.push_back({k["spell_id"].get<std::uint32_t>(), k["name"].get<std::string>(), Target(k["target"].get<std::string>()),
            k["facing"].get<bool>()});
    return kit;
}

ActorKnowledge Knowledge(json const& doc)
{
    ActorKnowledge k;
    k.alive = doc["alive"].get<bool>();
    k.mana = doc["mana"].get<std::uint32_t>();
    k.manaRegenBound = doc["mana_regen_bound"].get<std::uint32_t>();
    k.casting = doc["casting"].get<bool>();
    k.castRemainingMs = doc["cast_remaining_ms"].get<std::uint32_t>();
    k.castQueued = doc["cast_queued"].get<bool>();
    for (json const& s : doc["kit"])
        k.kit.push_back({s["known"].get<bool>(), s["cooldown_ms"].get<std::uint32_t>(), s["gcd_ms"].get<std::uint32_t>(),
            s["cost_known"].get<bool>(), s["cost"].get<std::uint32_t>(), s["range_known"].get<bool>(),
            s["max_range"].get<float>()});
    k.enemyKnown = doc["enemy_known"].get<bool>();
    k.enemyAlive = doc["enemy_alive"].get<bool>();
    k.enemyDistance = doc["enemy_distance"].get<float>();
    return k;
}

ActionCatalog Frost()
{
    std::string error;
    std::optional<ActionCatalog> catalog = BuildCatalog(Kit(Cases()["kit"]), error);
    CHECK_MSG(catalog.has_value(), error);
    return catalog ? *catalog : ActionCatalog{};
}
}

DOJO_TEST(catalog_size_hash_and_names)
{
    ActionCatalog const catalog = Frost();
    json const& want = Cases()["catalog"];
    CHECK_EQ(catalog.entries.size(), want["size"].get<std::size_t>());
    CHECK_EQ(catalog.sha256, want["sha256"].get<std::string>());
    CHECK_EQ(CatalogToJson(catalog)["size"].get<std::size_t>(), catalog.entries.size());
    CHECK_EQ(catalog.abilities.size(), want["abilities"].size());
    for (std::size_t i = 0; i < catalog.abilities.size() && i < want["abilities"].size(); ++i)
        CHECK_EQ(catalog.abilities[i].name, want["abilities"][i].get<std::string>());
    for (auto const& [name, index] : want["names"].items())
    {
        std::optional<std::size_t> found = FindAction(catalog, name);
        CHECK_MSG(found.has_value() && *found == index.get<std::size_t>(), name);
    }
    CHECK(catalog.entries[0].noop);
    CHECK(!FindAction(catalog, "frostbolt/hold/sideways"));
}

DOJO_TEST(catalog_refuses_bad_kits)
{
    for (json const& c : Cases()["bad_kits"])
    {
        std::string error;
        CHECK(!BuildCatalog(Kit(c["kit"]), error).has_value());
        CHECK_EQ(error, c["error"].get<std::string>());
    }
}

DOJO_TEST(catalog_names_and_directions)
{
    CHECK_EQ(AbilityName("Frost Nova"), std::string("frost_nova"));
    CHECK_EQ(AbilityName("  Ice--Lance! "), std::string("ice_lance"));
    CHECK(IsBackward(Movement::BackLeft) && IsBackward(Movement::Back) && IsBackward(Movement::BackRight));
    CHECK(!IsBackward(Movement::Left) && !IsBackward(Movement::Hold));
    CHECK(MovementAngle(Movement::Left) > 1.57f && MovementAngle(Movement::Left) < 1.58f);
    CHECK(MovementAngle(Movement::Right) < -1.57f && MovementAngle(Movement::Right) > -1.58f);
}

DOJO_TEST(masks_follow_the_shared_cases)
{
    ActionCatalog const catalog = Frost();
    for (json const& c : Cases()["masks"])
    {
        MaskRules rules;
        if (c.contains("rules"))
        {
            rules.commandDelayMs = c["rules"]["command_delay_ms"].get<std::uint32_t>();
            rules.queueWindowMs = c["rules"]["queue_window_ms"].get<std::uint32_t>();
            rules.rangeSlackYards = c["rules"]["range_slack_yards"].get<float>();
        }
        MaskResult const r = ComputeMask(catalog, Knowledge(c["knowledge"]), rules);
        std::string const name = c["name"].get<std::string>();
        for (std::size_t a = 0; a < catalog.abilities.size(); ++a)
            CHECK_MSG(r.abilityReasons[a] == c["abilities"][catalog.abilities[a].name].get<std::string>(),
                name + ": " + catalog.abilities[a].name + " gave '" + r.abilityReasons[a] + "'");
        for (std::size_t f = 0; f < kFacings; ++f)
            CHECK_MSG(r.facingReasons[f] == c["facings"][std::string(FacingName(Facing(f)))].get<std::string>(), name);
        CHECK_MSG(std::accumulate(r.mask.begin(), r.mask.end(), 0u) == c["open"].get<unsigned>(), name);
        CHECK_MSG(r.mask[0] == 1, name);
        for (auto const& [entry, bit] : c["check"].items())
            CHECK_MSG(r.mask[*FindAction(catalog, entry)] == bit.get<int>(), name + ": " + entry);
    }
}

DOJO_TEST(queue_delays_supersedes_and_keeps)
{
    ActionQueue q(2, 150);
    std::vector<QueueEvent> e = q.Submit(0, 29, 10, "console", "c1", 1000);
    CHECK_EQ(e.size(), std::size_t(1));
    CHECK(e[0].outcome == QueueOutcome::Queued && e[0].command.dueMs == 1150);
    CHECK(q.TakeDue(1149).empty());

    e = q.Submit(0, 0, 11, "console", "c2", 1050);   // a noop keeps the pending command
    CHECK(e.size() == 1 && e[0].outcome == QueueOutcome::Kept && e[0].command.actionId == 10 && e[0].byActionId == 11);
    CHECK(q.Pending(0) && q.Pending(0)->actionId == 10);

    e = q.Submit(0, 82, 12, "console", "c3", 1100);   // a new command replaces it before it executes
    CHECK(e.size() == 2 && e[0].outcome == QueueOutcome::Superseded && e[0].command.actionId == 10 && e[0].byActionId == 12);
    CHECK(e[1].outcome == QueueOutcome::Queued && e[1].command.dueMs == 1250);

    q.Submit(1, 4, 13, "console", "c4", 1090);
    std::vector<QueuedCommand> due = q.TakeDue(1260);
    CHECK_EQ(due.size(), std::size_t(2));
    CHECK(due[0].actionId == 13 && due[1].actionId == 12);   // oldest due first
    CHECK(!q.Pending(0) && !q.Pending(1));

    e = q.Submit(1, 0, 14, "console", "c5", 1300);
    CHECK(e.size() == 1 && e[0].outcome == QueueOutcome::Idle);
    CHECK(q.Submit(2, 4, 15, "console", "c6", 1300)[0].outcome == QueueOutcome::Dropped);
}

DOJO_TEST(queue_drop_removes_only_pending)
{
    ActionQueue q(2, 150);
    CHECK(q.Drop(0, "released", 0).empty());
    q.Submit(0, 5, 1, "console", "c", 0);
    std::vector<QueueEvent> e = q.Drop(0, "released", 10);
    CHECK(e.size() == 1 && e[0].outcome == QueueOutcome::Dropped && e[0].reason == "released" && e[0].command.actionId == 1);
    CHECK(!q.Pending(0));
    CHECK(q.TakeDue(1000).empty());
    CHECK_EQ(std::string(QueueOutcomeName(QueueOutcome::Superseded)), std::string("superseded"));
}

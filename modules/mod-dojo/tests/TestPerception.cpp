// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

// The v1 actor observation (the shared schema case in tests/fixtures/observation_cases.json, which
// dojo/tests/test_observation.py also runs), its perceptual delay, visibility, last-seen memory, inference from seen
// uses, events, episode boundaries and the masks it feeds.

#include "DojoActionCatalog.h"
#include "DojoPerception.h"
#include "TestHarness.h"

#include <cmath>
#include <fstream>
#include <set>
#include <sstream>
#include <string>

using namespace Dojo;
using nlohmann::json;

namespace
{
json Load(char const* name)
{
    std::ifstream in(std::string(DOJO_TEST_FIXTURES) + "/" + name, std::ios::binary);
    std::ostringstream text;
    text << in.rdbuf();
    return json::parse(text.str());
}

ActionCatalog const& Frost()
{
    static ActionCatalog const catalog = []
    {
        std::vector<KitAbility> kit;
        json const cases = Load("action_cases.json");   // kept alive: a range-for over a temporary's member dangles
        for (json const& k : cases["kit"])
            kit.push_back({k["spell_id"].get<std::uint32_t>(), k["name"].get<std::string>(),
                k["target"].get<std::string>() == "enemy" ? TargetSlot::Enemy : TargetSlot::Self, k["facing"].get<bool>()});
        std::string error;
        return *BuildCatalog(kit, error);
    }();
    return catalog;
}

ObservationSchema const& Schema()
{
    static ObservationSchema const schema = BuildObservationSchema(Frost());
    return schema;
}

PerceptionConfig Config(std::uint32_t delay = 100)
{
    PerceptionConfig c;
    c.perceptualDelayMs = delay;
    c.matchMs = 180000;
    c.arenaX = 4055.5;
    c.arenaY = 2919.66;
    c.arenaRadius = 70.0;
    c.arenaZMin = 0.0;
    c.arenaZMax = 40.0;
    c.classId = 8;
    c.level = 80;
    c.treePoints = {0, 0, 71};
    c.kitCooldownMs = {0, 0, 25000};
    return c;
}

ActorTruth Truth(std::uint64_t guid, float x, float y)
{
    ActorTruth t;
    t.guid = guid;
    SelfView& s = t.self;
    s.alive = true;
    s.health = s.maxHealth = 16523;
    s.mana = s.maxMana = 11658;
    s.manaRegenBound = 30;
    s.kit = {{true, 0, 0, true, 312, true, 36.0f}, {true, 0, 0, true, 170, true, 36.0f}, {true, 0, 0, true, 198, false, 0.0f}};
    s.x = x;
    s.y = y;
    s.z = 12.5f;
    s.runSpeed = 7.0f;
    return t;
}

double Value(ActorObservation const& o, std::string const& name)
{
    std::optional<std::size_t> const i = Schema().Index(name);
    if (!i)
    {
        DojoTest::Fail(__FILE__, __LINE__, "no field " + name);
        return -99.0;
    }
    return o.values[*i];
}

bool Near(double a, double b, double tolerance = 1e-6)
{
    return std::fabs(a - b) <= tolerance;
}

// Percepts every 10 ms from `from` to `to` inclusive; self and (when visible) the enemy from the given truths.
void Feed(Perception& p, std::uint64_t from, std::uint64_t to, ActorTruth const& self, ActorTruth const* enemy)
{
    for (std::uint64_t t = from; t <= to; t += 10)
        CHECK_EQ(p.Push(MakePercept(t, self, enemy, 20.0f, {})), std::string());
}

std::size_t Bit(std::string const& action)
{
    return *FindAction(Frost(), action);
}

MaskRules Rules()
{
    MaskRules r;
    r.perceptualDelayMs = 100;
    return r;
}
}

DOJO_TEST(observation_schema_follows_the_shared_case)
{
    json const cases = Load("observation_cases.json");
    ObservationSchema const& s = Schema();
    CHECK_EQ(s.fields.size(), cases["size"].get<std::size_t>());
    CHECK_EQ(s.sha256, cases["sha256"].get<std::string>());
    std::vector<std::string> names;
    for (ObservationField const& f : s.fields)
        names.push_back(f.name);
    CHECK(names == cases["names"].get<std::vector<std::string>>());
    CHECK_EQ(std::set<std::string>(names.begin(), names.end()).size(), names.size());
    for (ObservationField const& f : s.fields)
        CHECK_MSG(f.low <= f.missing && f.missing <= f.high && f.scale > 0, f.name);
    CHECK(s.fields.size() <= 4096);   // kMaxObservationValues
}

DOJO_TEST(a_change_reaches_the_observation_only_after_the_delay)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    Feed(p, 0, 490, self, nullptr);
    self.self.health = self.self.maxHealth / 2;
    Feed(p, 500, 590, self, nullptr);
    std::string why;
    std::optional<ActorObservation> before = p.Observe(599, why);
    CHECK(before && before->perceptMs == 490);
    CHECK(before && Near(Value(*before, "self.health"), 1.0));
    Feed(p, 600, 600, self, nullptr);
    std::optional<ActorObservation> after = p.Observe(600, why);
    CHECK(after && after->perceptMs == 500);
    CHECK(after && Near(Value(*after, "self.health"), double(self.self.health) / self.self.maxHealth));
    CHECK(after && after->decisionMs - after->perceptMs == 100);
}

DOJO_TEST(masks_use_only_the_delayed_knowledge)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    ActorTruth enemy = Truth(2, 4060.0f, 2919.0f);
    self.self.kit[2].cooldownMs = 20000;   // Frost Nova on cooldown
    Feed(p, 0, 990, self, &enemy);
    self.self.kit[2].cooldownMs = 0;       // cleared at t = 1000
    Feed(p, 1000, 1090, self, &enemy);
    std::string why;
    std::optional<ActorObservation> o = p.Observe(1099, why);
    CHECK(o.has_value());
    MaskResult m = ComputeMask(Frost(), o->knowledge, Rules());
    CHECK_EQ(m.mask[Bit("frost_nova/hold/keep")], 0);
    CHECK_EQ(m.abilityReasons[3], std::string("cooldown"));
    Feed(p, 1100, 1100, self, &enemy);
    o = p.Observe(1100, why);
    m = ComputeMask(Frost(), o->knowledge, Rules());
    CHECK_EQ(m.mask[Bit("frost_nova/hold/keep")], 1);
    CHECK(Near(Value(*o, "self.kit.frost_nova.cooldown"), 0.0));
}

DOJO_TEST(hidden_enemy_state_never_reaches_a_percept)
{
    ActorTruth const self = Truth(1, 4040.0f, 2919.0f);
    ActorTruth a = Truth(2, 4060.0f, 2919.0f);
    ActorTruth b = a;
    // What no client shows of another unit: its cooldowns, global cooldown, spell queue, regeneration, pending command.
    b.self.kit[0].cooldownMs = 1234;
    b.self.kit[2].cooldownMs = 25000;
    b.self.kit[1].gcdMs = 1500;
    b.self.queued = true;
    b.self.manaRegenBound = 999;
    b.self.pending = PendingView{2, true, 4242};
    b.self.inCombat = true;
    b.self.silenced = true;
    b.self.fingersOfFrost = 2;
    CHECK_EQ(PerceptToJson(MakePercept(10, self, &a, 20.0f, {})).dump(), PerceptToJson(MakePercept(10, self, &b, 20.0f, {})).dump());
    // Out of sight, not even where it is reaches the observer.
    ActorTruth c = a;
    c.self.x = 4000.0f;
    c.self.health = 1;
    CHECK_EQ(PerceptToJson(MakePercept(10, self, nullptr, 20.0f, {})).dump(), PerceptToJson(MakePercept(10, self, nullptr, 0.0f, {})).dump());

    Perception pa(Schema(), Config());
    Perception pb(Schema(), Config());
    pa.Reset(1, 0);
    pb.Reset(1, 0);
    Feed(pa, 0, 300, self, &a);
    Feed(pb, 0, 300, self, &b);
    Feed(pa, 310, 2000, self, nullptr);   // the enemy moves out of sight
    Feed(pb, 310, 2000, self, nullptr);
    (void)c;
    std::string why;
    std::optional<ActorObservation> oa = pa.Observe(2000, why);
    std::optional<ActorObservation> ob = pb.Observe(2000, why);
    CHECK(oa && ob && oa->values == ob->values);
    CHECK(oa && ob && ComputeMask(Frost(), oa->knowledge, Rules()).mask == ComputeMask(Frost(), ob->knowledge, Rules()).mask);
}

DOJO_TEST(the_enemy_last_seen_stays_where_it_was)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    ActorTruth enemy = Truth(2, 4060.0f, 2919.0f);
    enemy.self.health = enemy.self.maxHealth / 4;
    enemy.self.vx = 7.0f;
    Feed(p, 0, 300, self, &enemy);
    Feed(p, 310, 1000, self, nullptr);
    std::string why;
    std::optional<ActorObservation> o = p.Observe(1000, why);
    CHECK(o.has_value());
    CHECK(Near(Value(*o, "enemy.present"), 1.0));
    CHECK(Near(Value(*o, "enemy.visible"), 0.0));
    CHECK(Near(Value(*o, "enemy.age"), (900.0 - 300.0) / 10000.0));
    CHECK(Near(Value(*o, "enemy.forward"), 20.0 / 40.0, 1e-5));       // facing +x, the enemy 20 yd ahead when last seen
    CHECK(Near(Value(*o, "enemy.health"), double(enemy.self.health) / enemy.self.maxHealth));
    CHECK(Near(Value(*o, "enemy.velocity.forward"), 0.0));            // motion is shown only while visible
    MaskResult const m = ComputeMask(Frost(), o->knowledge, Rules());
    CHECK_EQ(m.abilityReasons[1], std::string("no visible enemy"));
    CHECK_EQ(m.abilityReasons[5], std::string("no visible enemy"));   // target_enemy
    CHECK_EQ(m.mask[Bit("frost_nova/hold/keep")], 1);
    // It comes back into sight at 1010: visible again only once that percept has matured.
    Feed(p, 1010, 1100, self, &enemy);
    o = p.Observe(1109, why);
    CHECK(o && Near(Value(*o, "enemy.visible"), 0.0));
    Feed(p, 1110, 1110, self, &enemy);
    o = p.Observe(1110, why);
    CHECK(o && Near(Value(*o, "enemy.visible"), 1.0));
    CHECK(o && Near(Value(*o, "enemy.age"), 0.0));
}

DOJO_TEST(the_enemy_never_seen_is_missing)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    Feed(p, 0, 200, Truth(1, 4040.0f, 2919.0f), nullptr);
    std::string why;
    std::optional<ActorObservation> o = p.Observe(200, why);
    CHECK(o.has_value());
    CHECK(Near(Value(*o, "enemy.present"), 0.0));
    CHECK(Near(Value(*o, "enemy.age"), 1.0));
    for (std::string const& k : Schema().kit)
        CHECK(Near(Value(*o, "enemy.kit." + k + ".unknown"), 1.0));
    for (std::size_t i = 0; i < kEventSlots; ++i)
        CHECK(Near(Value(*o, "event." + std::to_string(i) + ".age"), 1.0));
    CHECK(Near(Value(*o, "context.class.mage"), 1.0));
    CHECK(Near(Value(*o, "context.tree.2"), 1.0));
    CHECK(Near(Value(*o, "match.remaining"), (180000.0 - 100.0) / 180000.0));
    for (std::size_t i = 0; i < o->values.size(); ++i)
        CHECK_MSG(o->values[i] >= double(Schema().fields[i].low) && o->values[i] <= double(Schema().fields[i].high),
            Schema().fields[i].name);
}

DOJO_TEST(enemy_cooldowns_come_only_from_uses_seen)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    ActorTruth enemy = Truth(2, 4050.0f, 2919.0f);
    enemy.self.kit[2].cooldownMs = 25000;   // the enemy's own state, which the observer never gets
    Feed(p, 0, 190, self, &enemy);
    CHECK_EQ(p.Push(MakePercept(200, self, &enemy, 10.0f, {{200, false, EventKind::CastCompleted, 2}})), std::string());
    Feed(p, 210, 5300, self, &enemy);
    std::string why;
    // A fresh perception fed the same stream, observed early, has not seen the use yet.
    Perception early(Schema(), Config());
    early.Reset(1, 0);
    Feed(early, 0, 190, self, &enemy);
    CHECK_EQ(early.Push(MakePercept(200, self, &enemy, 10.0f, {{200, false, EventKind::CastCompleted, 2}})), std::string());
    Feed(early, 210, 290, self, &enemy);
    std::optional<ActorObservation> o = early.Observe(299, why);
    CHECK(o && Near(Value(*o, "enemy.kit.frost_nova.unknown"), 1.0));
    Feed(early, 300, 300, self, &enemy);
    o = early.Observe(300, why);
    CHECK(o && Near(Value(*o, "enemy.kit.frost_nova.unknown"), 0.0));
    CHECK(o && Near(Value(*o, "enemy.kit.frost_nova.cooldown"), 25000.0 / 30000.0));
    CHECK(o && Near(Value(*o, "event.0.kind.cast_completed"), 1.0) && Near(Value(*o, "event.0.kit.frost_nova"), 1.0));
    CHECK(o && Near(Value(*o, "event.0.by_self"), 0.0));
    o = p.Observe(5300, why);
    CHECK(o && Near(Value(*o, "enemy.kit.frost_nova.cooldown"), (25000.0 - 5000.0) / 30000.0));
    CHECK(o && Near(Value(*o, "enemy.kit.frostbolt.unknown"), 1.0));
}

DOJO_TEST(events_are_the_newest_seen_first)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    for (std::uint64_t i = 0; i < 6; ++i)
        CHECK_EQ(p.Push(MakePercept(i * 100, self, nullptr, 0.0f, {{i * 100, true, EventKind::CastStarted, int(i % 3)}})),
            std::string());
    Feed(p, 600, 700, self, nullptr);
    std::string why;
    std::optional<ActorObservation> o = p.Observe(700, why);
    CHECK(o.has_value());
    // Matured up to 600: events at 500, 400, 300, 200 fill the four slots.
    CHECK(Near(Value(*o, "event.0.age"), (600.0 - 500.0) / 2000.0));
    CHECK(Near(Value(*o, "event.3.age"), (600.0 - 200.0) / 2000.0));
    CHECK(Near(Value(*o, "event.0.kit.frost_nova"), 1.0));   // 500 % 3 == 2
    CHECK(Near(Value(*o, "event.0.by_self"), 1.0));
    CHECK(Near(Value(*o, "event.3.present"), 1.0));
}

DOJO_TEST(reset_clears_the_actor_history)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    ActorTruth enemy = Truth(2, 4050.0f, 2919.0f);
    CHECK_EQ(p.Push(MakePercept(0, self, &enemy, 10.0f, {{0, false, EventKind::CastCompleted, 2}})), std::string());
    Feed(p, 10, 1000, self, &enemy);
    std::string why;
    CHECK(p.Observe(1000, why).has_value());
    p.Reset(2, 5000);
    CHECK(!p.Observe(5000, why).has_value());
    CHECK(p.Push(MakePercept(4990, self, nullptr, 0.0f, {})) != std::string());   // from before the boundary
    Feed(p, 5000, 5100, self, nullptr);
    std::optional<ActorObservation> o = p.Observe(5100, why);
    CHECK(o && o->generation == 2);
    CHECK(o && Near(Value(*o, "enemy.present"), 0.0));
    CHECK(o && Near(Value(*o, "enemy.kit.frost_nova.unknown"), 1.0));
    CHECK(o && Near(Value(*o, "event.0.present"), 0.0));
    CHECK(o && Near(Value(*o, "match.remaining"), 1.0));
}

DOJO_TEST(percepts_and_decisions_keep_time_order_and_bounds)
{
    PerceptionConfig c = Config(100);
    c.maxPending = 16;   // a 100 ms delay at 10 ms ticks keeps about 11 waiting
    Perception p(Schema(), c);
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    Feed(p, 0, 50, self, nullptr);
    CHECK(p.Push(MakePercept(40, self, nullptr, 0.0f, {})) != std::string());
    std::string why;
    CHECK(!p.Observe(40, why).has_value());   // before the newest percept
    CHECK(!p.Observe(60, why).has_value());   // nothing matured yet
    Feed(p, 60, 200, self, nullptr);
    CHECK(p.Observe(200, why).has_value());
    CHECK(!p.Observe(199, why).has_value());
    CHECK(why == "decision time went back" || why == "a decision before the newest percept");

    PerceptionConfig slow = Config(1000000);
    slow.maxPending = 4;
    Perception q(Schema(), slow);
    q.Reset(1, 0);
    for (std::uint64_t t = 0; t < 4; ++t)
        CHECK_EQ(q.Push(MakePercept(t, self, nullptr, 0.0f, {})), std::string());
    CHECK(q.Push(MakePercept(4, self, nullptr, 0.0f, {})) != std::string());
}

DOJO_TEST(the_own_pending_command_is_self_state)
{
    Perception p(Schema(), Config());
    p.Reset(1, 0);
    ActorTruth self = Truth(1, 4040.0f, 2919.0f);
    Feed(p, 0, 90, self, nullptr);
    self.self.pending = PendingView{1, true, 250};   // frostbolt with a move, due at 250
    Feed(p, 100, 200, self, nullptr);
    std::string why;
    std::optional<ActorObservation> o = p.Observe(200, why);
    CHECK(o && Near(Value(*o, "self.pending"), 1.0));
    CHECK(o && Near(Value(*o, "self.pending.frostbolt"), 1.0));
    CHECK(o && Near(Value(*o, "self.pending.moves"), 1.0));
    CHECK(o && Near(Value(*o, "self.pending.due"), (250.0 - 100.0) / 200.0));
}

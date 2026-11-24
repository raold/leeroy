// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMechanicPolicy.h"
#include "TestHarness.h"

#include <cmath>
#include <limits>
#include <string>
#include <vector>

using namespace Leeroy::Guild;
using nlohmann::json;

namespace
{
constexpr float kReach = 1.0f;
constexpr float kPi = 3.14159265358979f;

json Rule(std::string const& id = "icc.deathwhisper.death_and_decay", std::string const& kind = "avoid_ground",
    std::string const& area = "dest")
{
    return json{{"id", id}, {"kind", kind}, {"area", area},
        {"roles", json::array({"tank", "healer", "melee", "ranged"})},
        {"spells", json::array({72110})}, {"radius", 8}, {"range", 0}, {"angle", 0}, {"duration", 0}};
}

json Table(json rules)
{
    return json{{"v", 1}, {"type", "raid_mechanics"}, {"id", "m1"}, {"map", 631}, {"difficulty", 3},
        {"mechanics", std::move(rules)}};
}

std::string Refused(json message)
{
    std::string error;
    auto table = ParseRaidMechanics(message, error);
    return table ? std::string("accepted") : error;
}

MechanicRule Make(MechanicKind kind, MechanicArea area, float radius, float range = 0, float angle = 0,
    std::uint8_t roles = kMechanicTank | kMechanicHealer | kMechanicMelee | kMechanicRanged)
{
    MechanicRule rule;
    rule.id = "test.boss.mechanic";
    rule.kind = kind;
    rule.area = area;
    rule.radius = radius;
    rule.range = range;
    rule.angle = angle;
    rule.roles = roles;
    rule.spells = {100};
    return rule;
}

MechanicCast CastAt(HazardPoint caster, std::optional<HazardPoint> target = std::nullopt,
    std::optional<HazardPoint> dest = std::nullopt, float facing = 0)
{
    MechanicCast cast;
    cast.caster = caster;
    cast.target = target;
    cast.dest = dest;
    cast.facing = facing;
    return cast;
}

float Dist(HazardPoint a, HazardPoint b) { return std::hypot(a.x - b.x, a.y - b.y); }

MechanicDuty Leave(MechanicShape shape)
{
    MechanicDuty duty;
    duty.leave = shape;
    return duty;
}

MechanicShape Circle(HazardPoint at, float radius)
{
    MechanicShape shape;
    shape.origin = at;
    shape.radius = radius;
    return shape;
}

MechanicMemberFacts Responder()
{
    MechanicMemberFacts facts;
    facts.enabled = facts.cast = facts.bot = facts.ownerGroup = facts.covered = true;
    return facts;
}
}

LEEROY_TEST(Mechanic_ParsesAWholeTable)
{
    json rules = json::array({Rule(), Rule("naxx.anubrekhan.impale", "move_out_cone", "cone")});
    rules[1]["range"] = 45;
    rules[1]["angle"] = 24;
    rules[1]["spells"] = json::array({28783u});
    rules[1]["roles"] = json::array({"healer", "melee", "ranged"});
    std::string error;
    auto table = ParseRaidMechanics(Table(rules), error);
    CHECK(table.has_value());
    CHECK_EQ(error, std::string());
    CHECK_EQ(table->map, 631u);
    CHECK_EQ(int(table->difficulty), 3);
    CHECK_EQ(table->rules.size(), std::size_t(2));
    CHECK(table->rules[0].kind == MechanicKind::AvoidGround);
    CHECK(table->rules[0].area == MechanicArea::Dest);
    CHECK_EQ(int(table->rules[0].roles), int(kMechanicTank | kMechanicHealer | kMechanicMelee | kMechanicRanged));
    CHECK_EQ(table->rules[0].radius, 8.0f);
    CHECK(table->rules[1].kind == MechanicKind::MoveOutCone);
    CHECK_EQ(int(table->rules[1].roles), int(kMechanicHealer | kMechanicMelee | kMechanicRanged));
    CHECK_EQ(table->rules[1].angle, 24.0f);
    CHECK_EQ(MechanicRulesFor(*table, 72110).size(), std::size_t(1));
    CHECK(MechanicRulesFor(*table, 1).empty());
    CHECK(MechanicRulesFor(*table, 0).empty());
}

LEEROY_TEST(Mechanic_AnEmptyTableClearsAnInstance)
{
    std::string error;
    auto table = ParseRaidMechanics(Table(json::array()), error);
    CHECK(table.has_value());
    CHECK(table->rules.empty());
}

LEEROY_TEST(Mechanic_RefusesABadEnvelope)
{
    json bad = Table(json::array({Rule()}));
    bad["map"] = 0;
    CHECK_EQ(Refused(bad), std::string("bad field: map"));
    bad = Table(json::array({Rule()}));
    bad["map"] = -5;
    CHECK_EQ(Refused(bad), std::string("bad field: map"));
    bad = Table(json::array({Rule()}));
    bad["difficulty"] = 4;
    CHECK_EQ(Refused(bad), std::string("bad field: difficulty"));
    bad = Table(json::array({Rule()}));
    bad.erase("mechanics");
    CHECK_EQ(Refused(bad), std::string("bad field: mechanics"));
    json many = json::array();
    for (std::size_t i = 0; i <= kMechanicMaxRules; ++i)
        many.push_back(Rule("x.y.m" + std::to_string(i)));
    CHECK_EQ(Refused(Table(many)), std::string("bad field: mechanics"));
    CHECK_EQ(Refused(json::array()), std::string("bad field: map"));
}

LEEROY_TEST(Mechanic_RefusesABadRule)
{
    auto with = [](char const* key, json value)
    {
        json rule = Rule();
        rule[key] = std::move(value);
        return Refused(Table(json::array({rule})));
    };
    CHECK_EQ(with("id", "Upper.Case"), std::string("bad field: id"));
    CHECK_EQ(with("id", ""), std::string("bad field: id"));
    CHECK_EQ(with("id", std::string(kMechanicMaxIdBytes + 1, 'a')), std::string("bad field: id"));
    CHECK_EQ(Refused(Table(json::array({Rule(), Rule()}))), std::string("bad field: id")); // twice
    CHECK_EQ(with("kind", "interrupt"), std::string("bad field: kind")); // not a movement kind
    CHECK_EQ(with("kind", "soak"), std::string("bad field: kind"));
    CHECK_EQ(with("area", "cone"), std::string("bad field: area")); // a ground area is no cone
    CHECK_EQ(with("area", "somewhere"), std::string("bad field: area"));
    CHECK_EQ(with("roles", json::array()), std::string("bad field: roles"));
    CHECK_EQ(with("roles", json::array({"tank", "tank"})), std::string("bad field: roles"));
    CHECK_EQ(with("roles", json::array({"pet"})), std::string("bad field: roles"));
    CHECK_EQ(with("spells", json::array()), std::string("bad field: spells"));
    CHECK_EQ(with("spells", json::array({0})), std::string("bad field: spells"));
    CHECK_EQ(with("spells", json::array({"72110"})), std::string("bad field: spells"));
    json spells = json::array();
    for (std::size_t i = 0; i <= kMechanicMaxSpells; ++i)
        spells.push_back(70000 + i);
    CHECK_EQ(with("spells", spells), std::string("bad field: spells"));
    CHECK_EQ(with("radius", -1), std::string("bad field: radius"));
    CHECK_EQ(with("radius", 101), std::string("bad field: radius"));
    CHECK_EQ(with("radius", "8"), std::string("bad field: radius"));
    CHECK_EQ(with("radius", 0), std::string("bad field: radius")); // a ground area needs one
    CHECK_EQ(with("duration", 121), std::string("bad field: duration"));
    CHECK_EQ(with("landing", "yes"), std::string("bad field: landing"));
    CHECK_EQ(with("angle", 361), std::string("bad field: angle"));
}

LEEROY_TEST(Mechanic_EachKindNeedsItsDistance)
{
    json line = Rule("icc.marrowgar.coldflame", "avoid_ground", "line");
    line["radius"] = 3;
    CHECK_EQ(Refused(Table(json::array({line}))), std::string("bad field: range"));
    line["range"] = 60;
    CHECK_EQ(Refused(Table(json::array({line}))), std::string("accepted"));
    json cone = Rule("naxx.grobbulus.slime_spray", "move_out_cone", "cone");
    cone["radius"] = 45;
    CHECK_EQ(Refused(Table(json::array({cone}))), std::string("bad field: angle"));
    cone["angle"] = 24;
    CHECK_EQ(Refused(Table(json::array({cone}))), std::string("accepted"));
    json spread = Rule("naxx.horsemen.holy_wrath", "spread", "group");
    spread["radius"] = 0;
    spread["range"] = 10;
    CHECK_EQ(Refused(Table(json::array({spread}))), std::string("accepted"));
    json stack = Rule("naxx.horsemen.meteor", "stack", "dest");
    stack["radius"] = 0;
    CHECK_EQ(Refused(Table(json::array({stack}))), std::string("bad field: radius"));
    json run = Rule("naxx.kelthuzad.detonate_mana", "run_out_debuff", "target");
    CHECK_EQ(Refused(Table(json::array({run}))), std::string("accepted"));
    run["area"] = "dest";
    CHECK_EQ(Refused(Table(json::array({run}))), std::string("bad field: area"));
    json los = Rule("x.y.hide", "los", "caster");
    los["radius"] = 0;
    CHECK_EQ(Refused(Table(json::array({los}))), std::string("accepted"));
}

LEEROY_TEST(Mechanic_OnlyHisGroupsAndOfflineRaidsFreeCastBotsRespond)
{
    CHECK_EQ(MechanicSkipReason(Responder()), std::string_view());
    MechanicMemberFacts offline = Responder();
    offline.ownerGroup = false;
    offline.offlineRaid = true;
    CHECK_EQ(MechanicSkipReason(offline), std::string_view());

    auto reason = [](auto change)
    {
        MechanicMemberFacts facts = Responder();
        change(facts);
        return std::string(MechanicSkipReason(facts));
    };
    CHECK_EQ(reason([](auto& f) { f.enabled = false; }), std::string("off")); // the switch off moves nobody
    CHECK_EQ(reason([](auto& f) { f.cast = false; }), std::string("not_cast")); // a random bot, or him
    CHECK_EQ(reason([](auto& f) { f.bot = false; }), std::string("not_bot"));
    CHECK_EQ(reason([](auto& f) { f.ownerGroup = false; }), std::string("not_in_group"));
    CHECK_EQ(reason([](auto& f) { f.fenced = true; }), std::string("fenced"));
    CHECK_EQ(reason([](auto& f) { f.held = true; }), std::string("held")); // his or the raid lead's hold
    CHECK_EQ(reason([](auto& f) { f.alive = false; }), std::string("dead"));
    CHECK_EQ(reason([](auto& f) { f.free = false; }), std::string("busy"));
    CHECK_EQ(reason([](auto& f) { f.covered = false; }), std::string("no_table"));
    // The switch wins over everything else.
    CHECK_EQ(reason([](auto& f) { f.enabled = false; f.fenced = true; f.held = true; }), std::string("off"));
}

LEEROY_TEST(Mechanic_RolesReadAsPlayerbotsReadsTheSpec)
{
    CHECK_EQ(int(MechanicRole(true, false, false)), int(kMechanicTank));
    CHECK_EQ(int(MechanicRole(false, true, true)), int(kMechanicHealer));
    CHECK_EQ(int(MechanicRole(false, false, true)), int(kMechanicRanged));
    CHECK_EQ(int(MechanicRole(false, false, false)), int(kMechanicMelee));
}

LEEROY_TEST(Mechanic_ARuleBindsItsRolesAndARunOutOnlyItsMember)
{
    MechanicRule const ground = Make(MechanicKind::AvoidGround, MechanicArea::Dest, 8, 0, 0,
        kMechanicHealer | kMechanicRanged);
    CHECK(MechanicBinds(ground, kMechanicRanged, 101, 0));
    CHECK(MechanicBinds(ground, kMechanicHealer, 101, 0));
    CHECK(!MechanicBinds(ground, kMechanicTank, 101, 0)); // a role stock covers is never moved
    CHECK(!MechanicBinds(ground, kMechanicMelee, 101, 0));
    MechanicRule const run = Make(MechanicKind::RunOutDebuff, MechanicArea::Target, 10, 0, 0,
        kMechanicHealer | kMechanicRanged);
    CHECK(MechanicBinds(run, kMechanicRanged, 101, 101));
    CHECK(!MechanicBinds(run, kMechanicRanged, 102, 101));
    CHECK(!MechanicBinds(run, kMechanicRanged, 101, 0));
    CHECK(!MechanicBinds(run, kMechanicTank, 101, 101)); // a tank with it keeps the boss where it is
}

LEEROY_TEST(Mechanic_LifetimeIsTheCastPlusItsLinger)
{
    MechanicRule rule = Make(MechanicKind::AvoidGround, MechanicArea::Dest, 8);
    CHECK_EQ(MechanicLifetimeMs(rule, 1000, 0), 1000u + kMechanicDefaultLingerMs);
    CHECK_EQ(MechanicLifetimeMs(rule, 0, 10000), 10000u);                  // Death and Decay lasts 10 s
    CHECK_EQ(MechanicLifetimeMs(rule, 1500, 90000), 91500u);                // a void zone lasts its 90 s
    CHECK_EQ(MechanicLifetimeMs(rule, 0, 600000), kMechanicMaxLingerMs);   // capped
    CHECK_EQ(MechanicLifetimeMs(rule, 0, 200), kMechanicMinLifetimeMs);
    CHECK_EQ(MechanicLifetimeMs(rule, 200000, 0), kMechanicMaxLifetimeMs);
    rule.duration = 12;
    CHECK_EQ(MechanicLifetimeMs(rule, 0, 2000), 12000u); // the playbook's own number wins
}

LEEROY_TEST(Mechanic_ShapesHoldTheirPoints)
{
    MechanicShape circle = Circle({0, 0, 0}, 8);
    CHECK(InMechanicShape({8.5f, 0, 0}, circle, kReach));
    CHECK(!InMechanicShape({9.5f, 0, 0}, circle, kReach));
    CHECK(!InMechanicShape({1, 0, 20}, circle, kReach)); // another floor

    MechanicShape band;
    band.type = MechanicShapeType::Band;
    band.origin = {0, 0, 0};
    band.radius = 3;
    band.length = 60;
    band.facing = 0; // east
    CHECK(InMechanicShape({30, 2, 0}, band, kReach));
    CHECK(!InMechanicShape({30, 5, 0}, band, kReach));
    CHECK(!InMechanicShape({-5, 0, 0}, band, kReach)); // behind the caster
    CHECK(!InMechanicShape({62, 0, 0}, band, kReach)); // past its end

    MechanicShape cone;
    cone.type = MechanicShapeType::Cone;
    cone.origin = {0, 0, 0};
    cone.length = 45;
    cone.facing = kPi / 2; // north
    cone.halfAngle = 12.0f * kPi / 180.0f;
    CHECK(InMechanicShape({0, 30, 0}, cone, kReach));
    CHECK(InMechanicShape({4, 30, 0}, cone, kReach));   // about 7.6 degrees off
    CHECK(!InMechanicShape({12, 30, 0}, cone, kReach)); // about 22 degrees off
    CHECK(!InMechanicShape({0, -10, 0}, cone, kReach)); // behind
    CHECK(!InMechanicShape({0, 47, 0}, cone, kReach));  // out of reach
    CHECK(InMechanicShape({0.5f, -0.5f, 0}, cone, kReach)); // on the caster
}

LEEROY_TEST(Mechanic_AGroundAreaLiesAtTheDestinationElseTheTarget)
{
    MechanicRule const rule = Make(MechanicKind::AvoidGround, MechanicArea::Dest, 8);
    auto area = MechanicLeaveArea(rule, CastAt({0, 0, 0}, HazardPoint{20, 0, 0}, HazardPoint{30, 5, 0}), {0, 0, 0});
    CHECK(area.has_value());
    CHECK_EQ(area->origin.x, 30.0f);
    area = MechanicLeaveArea(rule, CastAt({0, 0, 0}, HazardPoint{20, 0, 0}), {0, 0, 0});
    CHECK_EQ(area->origin.x, 20.0f); // a cast-time spell: its destination is chosen later, at its target
    CHECK(!MechanicLeaveArea(rule, CastAt({0, 0, 0}), {0, 0, 0}).has_value()); // nowhere known: no response
}

LEEROY_TEST(Mechanic_ACasterAreaFollowsTheCaster)
{
    MechanicRule const rule = Make(MechanicKind::AvoidGround, MechanicArea::Caster, 8, 0, 0, kMechanicMelee);
    auto area = MechanicLeaveArea(rule, CastAt({0, 0, 0}), {12, 3, 0}); // Bladestorm, on the move
    CHECK(area.has_value());
    CHECK_EQ(area->origin.x, 12.0f);
    CHECK_EQ(area->origin.y, 3.0f);
}

LEEROY_TEST(Mechanic_LinesAndConesPointAtTheTargetElseTheDestinationElseTheFacing)
{
    MechanicRule const line = Make(MechanicKind::AvoidGround, MechanicArea::Line, 3, 60);
    auto toTarget = MechanicLeaveArea(line, CastAt({0, 0, 0}, HazardPoint{0, 10, 0}, std::nullopt, 0), {0, 0, 0});
    CHECK(std::fabs(toTarget->facing - kPi / 2) < 0.001f);
    CHECK_EQ(toTarget->length, 60.0f);
    auto toDest = MechanicLeaveArea(line, CastAt({0, 0, 0}, std::nullopt, HazardPoint{-10, 0, 0}, 0), {0, 0, 0});
    CHECK(std::fabs(std::fabs(toDest->facing) - kPi) < 0.001f);
    // Coldflame: aimed at the caster's feet, the caster already turned toward its victim.
    auto own = MechanicLeaveArea(line, CastAt({0, 0, 0}, std::nullopt, HazardPoint{0, 0, 0}, 1.0f), {0, 0, 0});
    CHECK(std::fabs(own->facing - 1.0f) < 0.001f);

    MechanicRule const cone = Make(MechanicKind::MoveOutCone, MechanicArea::Cone, 45, 0, 24);
    auto area = MechanicLeaveArea(cone, CastAt({0, 0, 0}, HazardPoint{10, 0, 0}), {0, 0, 0});
    CHECK(area->type == MechanicShapeType::Cone);
    CHECK_EQ(area->length, 45.0f);
    CHECK(std::fabs(area->halfAngle - 12.0f * kPi / 180.0f) < 0.001f);
    MechanicRule const ranged = Make(MechanicKind::MoveOutCone, MechanicArea::Cone, 0, 90, 24);
    CHECK_EQ(MechanicLeaveArea(ranged, CastAt({0, 0, 0}), {0, 0, 0})->length, 90.0f);
}

LEEROY_TEST(Mechanic_DutiesPerKind)
{
    MechanicCast const cast = CastAt({0, 0, 0}, HazardPoint{5, 0, 0});
    auto stack =
        MechanicDutyFor(Make(MechanicKind::Stack, MechanicArea::Dest, 8), cast, {0, 0, 0}, HazardPoint{6, 1, 0});
    CHECK(stack->gather.has_value());
    CHECK_EQ(stack->gather->x, 6.0f); // the tank moved: gather where it is now
    CHECK_EQ(stack->gatherRadius, 8.0f);
    auto spread =
        MechanicDutyFor(Make(MechanicKind::Spread, MechanicArea::Group, 0, 10), cast, {0, 0, 0}, std::nullopt);
    CHECK_EQ(spread->spacing, 10.0f);
    CHECK(!spread->leave.has_value());
    auto run =
        MechanicDutyFor(Make(MechanicKind::RunOutDebuff, MechanicArea::Target, 10), cast, {0, 0, 0}, std::nullopt);
    CHECK_EQ(run->spacing, 10.0f);
    auto hide = MechanicDutyFor(Make(MechanicKind::Los, MechanicArea::Caster, 0), cast, {0, 0, 0}, std::nullopt);
    CHECK(hide->hide);
    CHECK(!MechanicDutyFor(Make(MechanicKind::Stack, MechanicArea::Dest, 8), CastAt({0, 0, 0}), {0, 0, 0},
        std::nullopt).has_value());
}

LEEROY_TEST(Mechanic_AvoidGroundStepsOutOfTheCircleAndClearOfKnownHazards)
{
    std::vector<MechanicDuty> const duties{Leave(Circle({0, 0, 0}, 8))};
    HazardPoint const at{2, 0, 0};
    CHECK(!MechanicKept(at, duties, {}, {}, kReach, nullptr));
    auto spots = MechanicSpots(at, duties, {}, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    CHECK(Dist(spots.front(), {0, 0, 0}) >= 8 + kReach + kMechanicMarginYards);
    CHECK(Dist(spots.front(), at) < 10.0f); // straight out, the short way
    CHECK(MechanicKept(spots.front(), duties, {}, {}, kReach, nullptr));

    // A Death and Decay on the near side: the spot lies past it too.
    std::vector<HazardZone> const hazards{{10.5f, 0, 0, 3, 7}};
    for (auto const& spot : MechanicSpots(at, duties, {}, hazards, kReach, 6, nullptr))
    {
        CHECK(HazardClear(spot, hazards, kReach));
        CHECK(Dist(spot, {0, 0, 0}) >= 8 + kReach);
    }

    // Out already: no spot, no move.
    CHECK(MechanicSpots({20, 0, 0}, duties, {}, {}, kReach, 6, nullptr).empty());
    CHECK(MechanicKept({20, 0, 0}, duties, {}, {}, kReach, nullptr));
}

LEEROY_TEST(Mechanic_ConeSpotsLieOutsideTheCone)
{
    MechanicShape cone;
    cone.type = MechanicShapeType::Cone;
    cone.origin = {0, 0, 0};
    cone.length = 45;
    cone.facing = 0;
    cone.halfAngle = 12.0f * kPi / 180.0f;
    std::vector<MechanicDuty> const duties{Leave(cone)};
    HazardPoint const at{20, 0, 0};
    auto spots = MechanicSpots(at, duties, {}, {}, kReach, 3, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        CHECK(!InMechanicShape(spot, cone, kReach + kMechanicMarginYards));
    CHECK(Dist(spots.front(), at) < 8.0f); // a side step, not a run to the far wall
}

LEEROY_TEST(Mechanic_AHalfCircleConeIsLeftBehindTheCaster)
{
    MechanicShape cone;
    cone.type = MechanicShapeType::Cone;
    cone.origin = {0, 0, 0};
    cone.length = 20;
    cone.facing = 0;
    cone.halfAngle = kPi / 2; // 180 degrees: everything in front of him
    auto spots = MechanicSpots({5, 0, 0}, {Leave(cone)}, {}, {}, kReach, 3, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        CHECK(!InMechanicShape(spot, cone, kReach + kMechanicMarginYards));
}

LEEROY_TEST(Mechanic_SpreadKeepsTheDistanceFromEveryOther)
{
    MechanicDuty spread;
    spread.spacing = 10;
    spread.bodies = 2; // a chain jumps between two bodies' edges
    std::vector<HazardPoint> const others{{0, 0, 0}, {3, 3, 0}, {-30, 0, 0}};
    HazardPoint const at{2, -1, 0};
    CHECK(!MechanicKept(at, {spread}, others, {}, kReach, nullptr));
    auto spots = MechanicSpots(at, {spread}, others, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        for (auto const& other : others)
            CHECK(Dist(spot, other) >= 10 + 2 * kReach + kMechanicMarginYards);
    CHECK(MechanicKept(spots.front(), {spread}, others, {}, kReach, nullptr));
    // Others on another floor do not count.
    CHECK(MechanicKept(at, {spread}, {{0, 0, 30}}, {}, kReach, nullptr));
}

LEEROY_TEST(Mechanic_StackGathersWithinTheRadius)
{
    MechanicDuty stack;
    stack.gather = HazardPoint{0, 0, 0};
    stack.gatherRadius = 8;
    HazardPoint const at{20, 0, 0};
    CHECK(!MechanicKept(at, {stack}, {}, {}, kReach, nullptr));
    auto spots = MechanicSpots(at, {stack}, {}, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        CHECK(Dist(spot, {0, 0, 0}) <= 8 - kMechanicGatherSlackYards + 0.01f);
    CHECK(Dist(spots.front(), at) <= 15.0f); // the near edge of the circle
    CHECK(MechanicKept({3, 0, 0}, {stack}, {}, {}, kReach, nullptr));
}

LEEROY_TEST(Mechanic_TheDebuffedRunsAwayFromTheRaid)
{
    MechanicDuty run;
    run.spacing = 10;
    run.bodies = 1; // the burst reaches the other member's edge
    std::vector<HazardPoint> const raid{{1, 0, 0}, {-1, 1, 0}, {0, -2, 0}, {2, 2, 0}};
    HazardPoint const at{0, 0, 0};
    auto spots = MechanicSpots(at, {run}, raid, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    for (auto const& other : raid)
        CHECK(Dist(spots.front(), other) >= 10 + kReach + kMechanicMarginYards);
    CHECK(Dist(spots.front(), at) <= kMechanicMaxShiftYards);
}

LEEROY_TEST(Mechanic_LosHidesBehindSomething)
{
    MechanicDuty hide;
    hide.hide = true;
    auto behindPillar = [](HazardPoint p) { return p.x > 6.0f; }; // a wall at x = 6
    CHECK(!MechanicKept({0, 0, 0}, {hide}, {}, {}, kReach, behindPillar));
    auto spots = MechanicSpots({0, 0, 0}, {hide}, {}, {}, kReach, 3, behindPillar);
    CHECK(!spots.empty());
    CHECK(spots.front().x > 6.0f);
    CHECK(MechanicKept({8, 0, 0}, {hide}, {}, {}, kReach, behindPillar));
    CHECK(!MechanicRefusesMove({8, 0, 0}, {0, 0, 0}, {hide}, {}, kReach)); // no sight test per move
}

LEEROY_TEST(Mechanic_NoSpotFartherThanTheMaximumShift)
{
    std::vector<MechanicDuty> const duties{Leave(Circle({0, 0, 0}, 60))};
    CHECK(MechanicSpots({0, 0, 0}, duties, {}, {}, kReach, 6, nullptr).empty());
}

LEEROY_TEST(Mechanic_OnlyMovesIntoAnAreaAreRefused)
{
    std::vector<MechanicDuty> const duties{Leave(Circle({0, 0, 0}, 8))};
    CHECK(MechanicRefusesMove({20, 0, 0}, {3, 0, 0}, duties, {}, kReach));
    CHECK(!MechanicRefusesMove({20, 0, 0}, {15, 0, 0}, duties, {}, kReach));
    // Spacing and a gather never refuse a move: the mechanic move serves them.
    MechanicDuty spread;
    spread.spacing = 10;
    spread.bodies = 2;
    CHECK(!MechanicRefusesMove({20, 0, 0}, {4, 0, 0}, {spread}, {}, kReach));
    MechanicDuty stack;
    stack.gather = HazardPoint{0, 0, 0};
    stack.gatherRadius = 8;
    CHECK(!MechanicRefusesMove({5, 0, 0}, {30, 0, 0}, {stack}, {}, kReach));
    CHECK(!MechanicRefusesMove({60, 0, 0}, {50, 0, 0}, {}, {}, kReach)); // nothing in force: nothing refused
}

LEEROY_TEST(Mechanic_ABotInAKnownHazardIsNeverRefused)
{
    // Jaraxxus: a healer running out with Legion Flame stands in a flame patch; stock avoid aoe's one way out
    // lies toward a neighbour, and even into the run-out's own area it is let through.
    std::vector<HazardZone> const patch{{0, 0, 0, 3, 1}};
    MechanicDuty run;
    run.spacing = 15;
    run.bodies = 1;
    std::vector<MechanicDuty> const both{run, Leave(Circle({6, 0, 0}, 4))};
    CHECK(!MechanicRefusesMove({0, 0, 0}, {5, 0, 0}, both, patch, kReach));
    CHECK(MechanicRefusesMove({20, 0, 0}, {5, 0, 0}, both, patch, kReach)); // outside the patch: refused as before
    // A bound bot in the patch is not kept, though it keeps its spacing: the responder moves it out of the fire.
    CHECK(!MechanicKept({0, 0, 0}, {run}, {{40, 0, 0}}, patch, kReach, nullptr));
    auto spots = MechanicSpots({0, 0, 0}, {run}, {{40, 0, 0}}, patch, kReach, 3, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        CHECK(HazardClear(spot, patch, kReach));
    // Nothing bound: a hazard alone is stock's to handle.
    CHECK(MechanicKept({0, 0, 0}, {}, {}, patch, kReach, nullptr));
}

LEEROY_TEST(Mechanic_HorsemenSwapIsNeverRefusedBySpacing)
{
    // Every 67.5 s the attractors swap to each other's spot; Holy Wrath's spread binds them meanwhile. Their walk to
    // an occupied spot goes through.
    MechanicDuty spread;
    spread.spacing = 10;
    spread.bodies = 2;
    CHECK(!MechanicRefusesMove({0, 0, 0}, {40, 0, 0}, {spread}, {}, kReach));
}

LEEROY_TEST(Mechanic_ChainsAndBurstsCountTheBodies)
{
    float const reach = 1.5f; // a player's combat reach
    MechanicDuty chain;
    chain.spacing = 10;
    chain.bodies = 2; // Holy Wrath and Fel Lightning jump within 10 of both edges: 13 yards
    CHECK(!MechanicKept({12.5f, 0, 0}, {chain}, {{0, 0, 0}}, {}, reach, nullptr));
    CHECK(MechanicKept({13.5f, 0, 0}, {chain}, {{0, 0, 0}}, {}, reach, nullptr));
    MechanicDuty burst;
    burst.spacing = 10;
    burst.bodies = 1; // Mana Detonation reaches 10 plus the other's edge: 11.5
    CHECK(!MechanicKept({10.5f, 0, 0}, {burst}, {{0, 0, 0}}, {}, reach, nullptr));
    CHECK(MechanicKept({12.0f, 0, 0}, {burst}, {{0, 0, 0}}, {}, reach, nullptr));
    auto spots = MechanicSpots({3, 0, 0}, {chain}, {{0, 0, 0}}, {}, reach, 1, nullptr);
    CHECK(!spots.empty());
    CHECK(Dist(spots.front(), {0, 0, 0}) >= 13.0f + kMechanicMarginYards);
    // The duties carry their bodies.
    MechanicCast const cast = CastAt({0, 0, 0});
    CHECK_EQ(int(MechanicDutyFor(Make(MechanicKind::Spread, MechanicArea::Group, 10), cast, {0, 0, 0},
        std::nullopt)->bodies), 2);
    CHECK_EQ(int(MechanicDutyFor(Make(MechanicKind::RunOutDebuff, MechanicArea::Target, 10), cast, {0, 0, 0},
        std::nullopt)->bodies), 1);
}

LEEROY_TEST(Mechanic_AStackBindsOnlyTheFightAroundIt)
{
    MechanicDuty stack;
    stack.gather = HazardPoint{0, 0, 0};
    stack.gatherRadius = 8;
    CHECK(MechanicDutyReaches(stack, {15, 0, 0}));
    CHECK(!MechanicDutyReaches(stack, {60, 0, 0})); // a horseman's tank in another corner
    CHECK(!MechanicDutyReaches(stack, {5, 0, 30}));
    CHECK(MechanicDutyReaches(Leave(Circle({0, 0, 0}, 8)), {90, 0, 0})); // only a gather is scoped
}

LEEROY_TEST(Mechanic_AnEruptionRingIsNeverAPlaceToStand)
{
    // Infernal Eruption: each eruption lands 15 yards from the volcano and hits 10 round it.
    MechanicRule const rule = Make(MechanicKind::AvoidGround, MechanicArea::Ring, 10, 15);
    auto ring = MechanicLeaveArea(rule, CastAt({30, 0, 0}, std::nullopt, HazardPoint{0, 0, 0}), {30, 0, 0});
    CHECK(ring.has_value());
    CHECK(ring->type == MechanicShapeType::Ring);
    float const reach = 1.5f;
    CHECK(!InMechanicShape({2, 0, 0}, *ring, reach));  // on the volcano
    CHECK(InMechanicShape({4, 0, 0}, *ring, reach));
    CHECK(InMechanicShape({26, 0, 0}, *ring, reach));
    CHECK(!InMechanicShape({27, 0, 0}, *ring, reach));
    for (float d = 4; d <= 26; d += 2)
    {
        auto spots = MechanicSpots({d, 0, 0}, {Leave(*ring)}, {}, {}, reach, 3, nullptr);
        CHECK(!spots.empty());
        for (auto const& spot : spots)
        {
            float const away = Dist(spot, {0, 0, 0});
            CHECK(away < 15 - 10 - reach || away > 15 + 10 + reach);
        }
    }
    json eruption = Rule("toc.jaraxxus.infernal_eruption", "avoid_ground", "ring");
    eruption["radius"] = 10;
    CHECK_EQ(Refused(Table(json::array({eruption}))), std::string("bad field: range"));
    eruption["range"] = 15;
    CHECK_EQ(Refused(Table(json::array({eruption}))), std::string("accepted"));
}

LEEROY_TEST(Mechanic_ALandingRuleIsRead)
{
    json vortex = Rule("icc.council.shock_vortex");
    vortex["landing"] = true;
    std::string error;
    auto table = ParseRaidMechanics(Table(json::array({vortex})), error);
    CHECK(table.has_value());
    CHECK(table->rules[0].landing);
    CHECK(!ParseRaidMechanics(Table(json::array({Rule()})), error)->rules[0].landing);
}

LEEROY_TEST(Mechanic_AnOfflineRaidIsNotFencedByItself)
{
    MechanicOfflineFence fence;
    fence.raidLeader = 3112;
    CHECK(!MechanicOfflineFenced(fence));
    fence.dcOwners = {3112}; // the raid's own training DC job covers its whole group
    CHECK(!MechanicOfflineFenced(fence));
    fence.dcOwners = {3112, 251}; // another leader's run holds the body too
    CHECK(MechanicOfflineFenced(fence));
    fence.dcOwners = {3112};
    fence.realm = true;
    CHECK(MechanicOfflineFenced(fence));
    fence.realm = false;
    fence.dummy = true;
    CHECK(MechanicOfflineFenced(fence));
    fence.dummy = false;
    fence.cleanup = true;
    CHECK(MechanicOfflineFenced(fence));
    fence.cleanup = false;
    fence.pendingJob = true;
    CHECK(MechanicOfflineFenced(fence));
    MechanicOfflineFence unknown;
    unknown.dcOwners = {3112}; // no raid leader known: every owner counts
    CHECK(MechanicOfflineFenced(unknown));
}

LEEROY_TEST(Mechanic_AStockForcedMoveYieldsTheBot)
{
    CHECK(MechanicYieldsToStock(true, true, false));   // stock forced it since the mechanic began
    CHECK(!MechanicYieldsToStock(true, true, true));   // that forced move was ours
    CHECK(!MechanicYieldsToStock(true, false, false)); // before the mechanic
    CHECK(!MechanicYieldsToStock(false, true, false)); // an ordinary move
}

LEEROY_TEST(Mechanic_KindNamesMatchTheWire)
{
    CHECK_EQ(MechanicKindName(MechanicKind::AvoidGround), std::string_view("avoid_ground"));
    CHECK_EQ(MechanicKindName(MechanicKind::MoveOutCone), std::string_view("move_out_cone"));
    CHECK_EQ(MechanicKindName(MechanicKind::RunOutDebuff), std::string_view("run_out_debuff"));
    CHECK_EQ(MechanicKindName(MechanicKind::Los), std::string_view("los"));
}

LEEROY_TEST(Mechanic_APeriodicTickLogsOnlyWhenItMovedSomeone)
{
    CHECK(MechanicEndLogged(false, 0, false)); // a cast: always
    CHECK(MechanicEndLogged(true, 3, false));  // an inferno's tick that moved three
    CHECK(MechanicEndLogged(true, 0, true));
    CHECK(!MechanicEndLogged(true, 0, false)); // its next tick, nobody left in it
}

// Ruby Sanctum, heroic 25-man Halion, as the mind sends it (raidplay/data/rs.toml).
LEEROY_TEST(Mechanic_HalionsMeteorAndInfernoRules)
{
    json meteor = Rule("rs.halion.meteor_strike");
    meteor["roles"] = json::array({"healer", "melee", "ranged"});
    meteor["spells"] = json::array({74637});
    meteor["radius"] = 12;
    meteor["duration"] = 7;
    json fire = Rule("rs.halion.meteor_fire", "avoid_ground", "caster");
    fire["roles"] = meteor["roles"];
    fire["spells"] = json::array({75879});
    fire["radius"] = 3;
    fire["duration"] = 24;
    json aura = Rule("rs.halion.blazing_aura", "avoid_ground", "caster");
    aura["roles"] = meteor["roles"];
    aura["spells"] = json::array({75887});
    aura["radius"] = 15;
    aura["duration"] = 3;
    json message = Table(json::array({meteor, fire, aura}));
    message["map"] = 724;
    std::string error;
    auto table = ParseRaidMechanics(message, error);
    CHECK(table.has_value());
    CHECK_EQ(table->map, 724u);
    CHECK_EQ(MechanicRulesFor(*table, 74637).size(), std::size_t(1));
    CHECK_EQ(MechanicRulesFor(*table, 75887).size(), std::size_t(1));
    MechanicRule const& impact = table->rules[0];
    MechanicRule const& blazing = table->rules[2];
    CHECK(!MechanicBinds(impact, kMechanicTank, 1, 0)); // stock's tank spots
    CHECK(MechanicBinds(impact, kMechanicMelee, 1, 0));
    CHECK(!MechanicBinds(blazing, kMechanicTank, 1, 0)); // a tank holds the inferno
    CHECK_EQ(MechanicLifetimeMs(impact, 0, 30000), 7000u); // the countdown, not the mark's 30 s
    CHECK_EQ(MechanicLifetimeMs(blazing, 0, 0), 3000u);     // past the next 2 s tick

    // The meteor lands where its target stood at the cast.
    auto circle = MechanicLeaveArea(impact, CastAt({0, 0, 0}, HazardPoint{30, 0, 0}, HazardPoint{30, 0, 0}),
        {0, 0, 0});
    CHECK(circle.has_value());
    CHECK_EQ(circle->origin.x, 30.0f);
    CHECK(InMechanicShape({40, 0, 0}, *circle, kReach));
    CHECK(!InMechanicShape({44, 0, 0}, *circle, kReach));

    // The inferno walks to its tank; its fire goes with it, and a healer's ordinary move back into it is refused.
    auto burning = MechanicLeaveArea(blazing, CastAt({0, 0, 0}), {-20, 5, 0});
    CHECK(burning.has_value());
    CHECK_EQ(burning->origin.x, -20.0f);
    std::vector<MechanicDuty> const duties{Leave(*burning)};
    CHECK(MechanicRefusesMove({-40, 5, 0}, {-25, 5, 0}, duties, {}, kReach));
    CHECK(!MechanicRefusesMove({-40, 5, 0}, {-37, 5, 0}, duties, {}, kReach));
    auto spots = MechanicSpots({-22, 5, 0}, duties, {}, {}, kReach, 3, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        CHECK(Dist(spot, {-20, 5, 0}) >= 15 + kReach + kMechanicMarginYards);
}

LEEROY_TEST(Mechanic_ASpotNeverLiesInAnInfernosFireForANonTank)
{
    // A ranged leaving the meteor's circle must not step into an inferno's held zone beside it.
    std::vector<MechanicDuty> const duties{Leave(Circle({0, 0, 0}, 12))};
    HazardZone inferno{16, 0, 0, 16.5f, 9};
    inferno.held = true;
    std::vector<HazardZone> const hazards{inferno};
    auto const mine = HazardZonesFor(hazards, 1, false);
    auto spots = MechanicSpots({4, 0, 0}, duties, {}, mine, kReach, 6, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
    {
        CHECK(HazardClear(spot, mine, kReach));
        CHECK(Dist(spot, {0, 0, 0}) >= 12 + kReach);
    }
}

// Ulduar, Algalon: at Big Bang every bot but the tanks hides in the nearest black hole (32953, a worm hole 34099
// below 20 percent), which takes it into phase 16 where Big Bang cannot see it; when it lands they step out.
LEEROY_TEST(Mechanic_ANearestAreaNamesItsCreatures)
{
    json hide = Rule("uld.algalon.big_bang_hide", "stack", "nearest");
    hide["roles"] = json::array({"healer", "melee", "ranged"});
    hide["spells"] = json::array({64443, 64584});
    hide["radius"] = 6;
    hide["entries"] = json::array({32953, 34099});
    json out = Rule("uld.algalon.big_bang_out", "avoid_ground", "nearest");
    out["spells"] = json::array({64445});
    out["radius"] = 6;
    out["entries"] = json::array({32953, 34099});
    std::string error;
    auto table = ParseRaidMechanics(Table(json::array({hide, out})), error);
    CHECK(table.has_value());
    CHECK(table->rules[0].area == MechanicArea::Nearest);
    CHECK(table->rules[0].entries == std::vector<std::uint32_t>({32953, 34099}));
    CHECK(MechanicBindsAcrossPhases(table->rules[0]));
    CHECK(!MechanicBindsAcrossPhases(Make(MechanicKind::AvoidGround, MechanicArea::Caster, 8)));

    json none = hide;
    none.erase("entries");
    CHECK_EQ(Refused(Table(json::array({none}))), std::string("bad field: entries")); // a nearest area needs them
    json stray = Rule();
    stray["entries"] = json::array({32953});
    CHECK_EQ(Refused(Table(json::array({stray}))), std::string("bad field: entries")); // only a nearest area
    json many = hide;
    many["entries"] = json::array({1, 2, 3, 4, 5, 6, 7, 8, 9});
    CHECK_EQ(Refused(Table(json::array({many}))), std::string("bad field: entries"));
    json zero = hide;
    zero["entries"] = json::array({0});
    CHECK_EQ(Refused(Table(json::array({zero}))), std::string("bad field: entries"));
    json cone = Rule("x.y.z", "move_out_cone", "nearest");
    cone["entries"] = json::array({32953});
    cone["angle"] = 60;
    CHECK_EQ(Refused(Table(json::array({cone}))), std::string("bad field: area"));
}

LEEROY_TEST(Mechanic_TheNearestAnchorOnTheBotsFloor)
{
    std::vector<MechanicAnchor> const anchors{{32953, {30, 0, 0}}, {32953, {-12, 0, 0}}, {34099, {5, 0, 0}},
        {32955, {1, 0, 0}}, {32953, {2, 0, 20}}};
    auto hole = MechanicNearestAnchor(anchors, {32953}, {0, 0, 0});
    CHECK(hole.has_value());
    CHECK_EQ(hole->x, -12.0f); // the star (32955) and the hole on another floor do not count
    CHECK_EQ(MechanicNearestAnchor(anchors, {32953, 34099}, {0, 0, 0})->x, 5.0f);
    CHECK(!MechanicNearestAnchor(anchors, {32953}, {200, 0, 0}).has_value()); // past kMechanicNearestYards
    CHECK(!MechanicNearestAnchor({}, {32953}, {0, 0, 0}).has_value());
}

LEEROY_TEST(Mechanic_BigBangGathersInTheHoleThenLeavesIt)
{
    MechanicRule hide = Make(MechanicKind::Stack, MechanicArea::Nearest, 6, 0, 0,
        kMechanicHealer | kMechanicMelee | kMechanicRanged);
    hide.entries = {32953};
    MechanicCast cast = CastAt({0, 0, 0});
    CHECK(!MechanicDutyFor(hide, cast, {0, 0, 0}, std::nullopt).has_value()); // no hole within reach
    cast.nearest = HazardPoint{40, 0, 0};
    auto gather = MechanicDutyFor(hide, cast, {0, 0, 0}, std::nullopt);
    CHECK(gather.has_value());
    CHECK_EQ(gather->gather->x, 40.0f);
    CHECK(MechanicDutyReaches(*gather, {0, 0, 0})); // 40 yards off still binds, unlike a stack on a target
    CHECK(!MechanicBinds(hide, kMechanicTank, 1, 0)); // a tank stays out: Big Bang must find someone
    auto spots = MechanicSpots({0, 0, 0}, {*gather}, {}, {}, kReach, 3, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        CHECK(Dist(spot, {40, 0, 0}) <= 6.0f); // inside the hole's 6 yards
    CHECK(MechanicKept({41, 1, 0}, {*gather}, {}, {}, kReach, nullptr));

    MechanicRule out = Make(MechanicKind::AvoidGround, MechanicArea::Nearest, 6);
    out.entries = {32953};
    auto leave = MechanicDutyFor(out, cast, {0, 0, 0}, std::nullopt);
    CHECK(leave.has_value() && leave->leave.has_value());
    CHECK_EQ(leave->leave->origin.x, 40.0f);
    CHECK(!MechanicKept({41, 1, 0}, {*leave}, {}, {}, kReach, nullptr));
    for (auto const& spot : MechanicSpots({41, 1, 0}, {*leave}, {}, {}, kReach, 3, nullptr))
        CHECK(Dist(spot, {40, 0, 0}) >= 6 + kReach + kMechanicMarginYards);
}

LEEROY_TEST(Mechanic_NaxxRulesRequireTheRightGeometry)
{
    json ice = Rule("naxx.sapphiron.frost_breath", "los", "ice_block");
    ice["radius"] = 2;
    ice["range"] = 10;
    ice["spells"] = json::array({30101});
    CHECK_EQ(Refused(Table(json::array({ice}))), std::string("accepted"));
    auto invalid = ice;
    invalid["range"] = 0;
    CHECK(Refused(Table(json::array({invalid}))) != "accepted");
    invalid = ice;
    invalid["kind"] = "stack";
    CHECK(Refused(Table(json::array({invalid}))) != "accepted");
    json cloud = Rule("naxx.grobbulus.poison_pool", "avoid_ground", "caster_reach");
    CHECK_EQ(Refused(Table(json::array({cloud}))), std::string("accepted"));
    cloud["kind"] = "los";
    CHECK(Refused(Table(json::array({cloud}))) != "accepted");
}

LEEROY_TEST(Mechanic_IceCoverIsBehindAndCloseToTheFrozenPlayer)
{
    HazardPoint boss{0, 0, 0}, block{20, 0, 0};
    CHECK(MechanicIceCover(boss, block, {23, 0, 0}, 2, 10));
    CHECK(!MechanicIceCover(boss, block, {17, 0, 0}, 2, 10));
    CHECK(!MechanicIceCover(boss, block, {23, 5, 0}, 2, 10));
    CHECK(!MechanicIceCover(boss, block, {31, 0, 0}, 2, 10));
    CHECK(!MechanicIceCover(boss, block, {23, 0, 20}, 2, 10));
    CHECK(!MechanicIceCover(boss, block, block, 2, 10));
    CHECK(!MechanicIceCover(boss, block, boss, 2, 10));
    // A close boss and an off-axis block can pass the ray-width test while failing
    // the core's distance-to-the-other-endpoint condition (including vertical rays).
    CHECK(!MechanicIceCover(boss, {.2f, 1.87f, 0}, {2, 0, 0}, 2, 10));
    CHECK(!MechanicIceCover(boss, {1.87f, .2f, 0}, {0, 2, 0}, 2, 10));
    // Rotate the room: the chosen spots must satisfy the core's actual between test,
    // including its vertical-line special case, not just our own helper.
    for (int degrees = 0; degrees < 360; ++degrees)
    {
        float a = degrees * kPi / 180.0f;
        block = {20 * std::cos(a), 20 * std::sin(a), 0};
        for (auto const& at : MechanicIceSpots(boss, {block}, 10))
        {
            CHECK(MechanicIceCover(boss, block, at, 2, 10));
            float pdist = at.x * at.x + at.y * at.y + 1.0f;
            CHECK(block.x * block.x + block.y * block.y < pdist);
            CHECK(Dist(block, at) * Dist(block, at) < pdist);
            CHECK(Dist(block, at) < 10.0f);
            if (std::fabs(at.x) < .00001f)
                CHECK(std::fabs(block.x) <= 2.0f);
            else
            {
                float slope = at.y / at.x;
                CHECK(std::fabs(slope * block.x - block.y) / std::sqrt(slope * slope + 1) <= 2.0f);
            }
        }
    }
}

LEEROY_TEST(Mechanic_IceCoverRechecksAChangedOrMissingBlock)
{
    HazardPoint boss{0, 0, 0}, block{20, 0, 0};
    std::vector<HazardPoint> blocks{block};
    auto hidden = [&](HazardPoint at)
    {
        return !blocks.empty() && MechanicIceCover(boss, blocks[0], at, 2, 10);
    };
    MechanicDuty duty;
    duty.hide = true;
    HazardPoint at{17, 3, 0};
    auto spots = MechanicSpots(at, {duty}, {}, {}, kReach, 6, hidden, MechanicIceSpots(boss, blocks, 10));
    CHECK(!spots.empty());
    HazardPoint safe = spots.front();
    CHECK(MechanicKept(safe, {duty}, {}, {}, kReach, hidden));
    CHECK(MechanicRefusesMove(safe, {17, 0, 0}, {duty}, {}, kReach, hidden));
    CHECK(!MechanicRefusesMove(safe, {25, 0, 0}, {duty}, {}, kReach, hidden));
    blocks.clear();
    CHECK(!MechanicKept(safe, {duty}, {}, {}, kReach, hidden));
    CHECK(MechanicSpots(at, {duty}, {}, {}, kReach, 6, hidden).empty());
    blocks.push_back({0, 20, 0});
    CHECK(!MechanicKept(safe, {duty}, {}, {}, kReach, hidden));
}

LEEROY_TEST(Mechanic_GrowingCloudTracksItsCurrentReachAndPosition)
{
    auto cloud = Make(MechanicKind::AvoidGround, MechanicArea::CasterReach, 2);
    auto cast = CastAt({0, 0, 0});
    cast.casterReach = 2;
    auto first = MechanicDutyFor(cloud, cast, {0, 0, 0}, std::nullopt);
    CHECK(MechanicKept({10, 0, 0}, {*first}, {}, {}, kReach, nullptr));
    cast.casterReach = 17;
    auto grown = MechanicDutyFor(cloud, cast, {0, 0, 0}, std::nullopt);
    CHECK(!MechanicKept({10, 0, 0}, {*grown}, {}, {}, kReach, nullptr));
    for (auto const& spot : MechanicSpots({10, 0, 0}, {*grown}, {}, {}, kReach, 6, nullptr))
        CHECK(Dist(spot, {0, 0, 0}) >= 17 + kReach + kMechanicMarginYards);
    auto moved = MechanicDutyFor(cloud, cast, {50, 0, 0}, std::nullopt);
    CHECK(MechanicKept({10, 0, 0}, {*moved}, {}, {}, kReach, nullptr));
    // Existing fixed-radius caster rules keep their own radius.
    cloud.area = MechanicArea::Caster;
    CHECK_EQ(MechanicDutyFor(cloud, cast, {0, 0, 0}, std::nullopt)->leave->radius, 2.0f);
}

LEEROY_TEST(Mechanic_PeriodicSlimeUsesItsCastRadiusNotTheSummonOrFullSize)
{
    auto rule = Make(MechanicKind::AvoidGround, MechanicArea::CastRadius, 30);
    auto cast = CastAt({40, 0, 0}); // The pool, not the worm's current location.
    CHECK(!MechanicLeaveArea(rule, cast, {40, 0, 0}));
    for (unsigned tick = 1; tick <= 60; ++tick)
    {
        // SpellAuraEffects.cpp case 66882: base radius 30 * custom RadiusMod.
        cast.effectRadius = 30.0f * ((tick / 60.0f) * 0.9f + 0.1f) * 2.0f / 3.0f;
        auto duty = MechanicDutyFor(rule, cast, {40, 0, 0}, std::nullopt);
        CHECK(duty && duty->leave);
        CHECK(std::abs(duty->leave->radius - cast.effectRadius) < 0.001f);
        CHECK(!MechanicKept({40, 0, 0}, {*duty}, {}, {}, kReach, nullptr));
        auto spots = MechanicSpots({40, 0, 0}, {*duty}, {}, {}, kReach, 6, nullptr);
        CHECK(!spots.empty());
        for (auto const& spot : spots)
            CHECK(MechanicKept(spot, {*duty}, {}, {}, kReach, nullptr));
        // Early ticks do not evacuate the full 30-yard circle.
        CHECK(MechanicKept({65, 0, 0}, {*duty}, {}, {}, kReach, nullptr));
    }
    for (float invalid : {-1.0f, 0.0f, 101.0f, std::numeric_limits<float>::infinity(),
             std::numeric_limits<float>::quiet_NaN()})
    {
        cast.effectRadius = invalid;
        CHECK(!MechanicLeaveArea(rule, cast, {40, 0, 0}));
    }
    auto wire = Rule("toc.beasts.slime_pool", "avoid_ground", "cast_radius");
    CHECK_EQ(Refused(Table(json::array({wire}))), "accepted");
    wire["kind"] = "stack";
    CHECK(Refused(Table(json::array({wire}))) != "accepted");
}

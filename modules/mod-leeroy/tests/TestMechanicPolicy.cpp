// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMechanicPolicy.h"
#include "TestHarness.h"

#include <cmath>
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
    CHECK_EQ(with("duration", 31), std::string("bad field: duration"));
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
    CHECK_EQ(MechanicLifetimeMs(rule, 1500, 90000), 1500u + kMechanicMaxLingerMs); // a void zone, capped
    CHECK_EQ(MechanicLifetimeMs(rule, 0, 200), kMechanicMinLifetimeMs);
    CHECK_EQ(MechanicLifetimeMs(rule, 60000, 0), kMechanicMaxLifetimeMs);
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
    CHECK(!MechanicKept(at, duties, {}, kReach, nullptr));
    auto spots = MechanicSpots(at, duties, {}, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    CHECK(Dist(spots.front(), {0, 0, 0}) >= 8 + kReach + kMechanicMarginYards);
    CHECK(Dist(spots.front(), at) < 10.0f); // straight out, the short way
    CHECK(MechanicKept(spots.front(), duties, {}, kReach, nullptr));

    // A Death and Decay on the near side: the spot lies past it too.
    std::vector<HazardZone> const hazards{{10.5f, 0, 0, 3, 7}};
    for (auto const& spot : MechanicSpots(at, duties, {}, hazards, kReach, 6, nullptr))
    {
        CHECK(HazardClear(spot, hazards, kReach));
        CHECK(Dist(spot, {0, 0, 0}) >= 8 + kReach);
    }

    // Out already: no spot, no move.
    CHECK(MechanicSpots({20, 0, 0}, duties, {}, {}, kReach, 6, nullptr).empty());
    CHECK(MechanicKept({20, 0, 0}, duties, {}, kReach, nullptr));
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
    std::vector<HazardPoint> const others{{0, 0, 0}, {3, 3, 0}, {-30, 0, 0}};
    HazardPoint const at{2, -1, 0};
    CHECK(!MechanicKept(at, {spread}, others, kReach, nullptr));
    auto spots = MechanicSpots(at, {spread}, others, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        for (auto const& other : others)
            CHECK(Dist(spot, other) >= 10 + kMechanicMarginYards);
    CHECK(MechanicKept(spots.front(), {spread}, others, kReach, nullptr));
    // Others on another floor do not count.
    CHECK(MechanicKept(at, {spread}, {{0, 0, 30}}, kReach, nullptr));
}

LEEROY_TEST(Mechanic_StackGathersWithinTheRadius)
{
    MechanicDuty stack;
    stack.gather = HazardPoint{0, 0, 0};
    stack.gatherRadius = 8;
    HazardPoint const at{20, 0, 0};
    CHECK(!MechanicKept(at, {stack}, {}, kReach, nullptr));
    auto spots = MechanicSpots(at, {stack}, {}, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    for (auto const& spot : spots)
        CHECK(Dist(spot, {0, 0, 0}) <= 8 - kMechanicGatherSlackYards + 0.01f);
    CHECK(Dist(spots.front(), at) <= 15.0f); // the near edge of the circle
    CHECK(MechanicKept({3, 0, 0}, {stack}, {}, kReach, nullptr));
}

LEEROY_TEST(Mechanic_TheDebuffedRunsAwayFromTheRaid)
{
    MechanicDuty run;
    run.spacing = 10;
    std::vector<HazardPoint> const raid{{1, 0, 0}, {-1, 1, 0}, {0, -2, 0}, {2, 2, 0}};
    HazardPoint const at{0, 0, 0};
    auto spots = MechanicSpots(at, {run}, raid, {}, kReach, 6, nullptr);
    CHECK(!spots.empty());
    for (auto const& other : raid)
        CHECK(Dist(spots.front(), other) >= 10 + kMechanicMarginYards);
    CHECK(Dist(spots.front(), at) <= kMechanicMaxShiftYards);
}

LEEROY_TEST(Mechanic_LosHidesBehindSomething)
{
    MechanicDuty hide;
    hide.hide = true;
    auto behindPillar = [](HazardPoint p) { return p.x > 6.0f; }; // a wall at x = 6
    CHECK(!MechanicKept({0, 0, 0}, {hide}, {}, kReach, behindPillar));
    auto spots = MechanicSpots({0, 0, 0}, {hide}, {}, {}, kReach, 3, behindPillar);
    CHECK(!spots.empty());
    CHECK(spots.front().x > 6.0f);
    CHECK(MechanicKept({8, 0, 0}, {hide}, {}, kReach, behindPillar));
    CHECK(!MechanicRefusesMove({0, 0, 0}, {hide}, {}, kReach)); // no sight test per move
}

LEEROY_TEST(Mechanic_NoSpotFartherThanTheMaximumShift)
{
    std::vector<MechanicDuty> const duties{Leave(Circle({0, 0, 0}, 60))};
    CHECK(MechanicSpots({0, 0, 0}, duties, {}, {}, kReach, 6, nullptr).empty());
}

LEEROY_TEST(Mechanic_OrdinaryMovesBackInAreRefused)
{
    std::vector<MechanicDuty> const duties{Leave(Circle({0, 0, 0}, 8))};
    CHECK(MechanicRefusesMove({3, 0, 0}, duties, {}, kReach));
    CHECK(!MechanicRefusesMove({15, 0, 0}, duties, {}, kReach));
    MechanicDuty spread;
    spread.spacing = 10;
    CHECK(MechanicRefusesMove({4, 0, 0}, {spread}, {{0, 0, 0}}, kReach));
    CHECK(!MechanicRefusesMove({12, 0, 0}, {spread}, {{0, 0, 0}}, kReach));
    MechanicDuty stack;
    stack.gather = HazardPoint{0, 0, 0};
    stack.gatherRadius = 8;
    CHECK(MechanicRefusesMove({12, 0, 0}, {stack}, {}, kReach));
    CHECK(!MechanicRefusesMove({5, 0, 0}, {stack}, {}, kReach));
    CHECK(!MechanicRefusesMove({50, 0, 0}, {}, {}, kReach)); // nothing in force: nothing refused
}

LEEROY_TEST(Mechanic_KindNamesMatchTheWire)
{
    CHECK_EQ(MechanicKindName(MechanicKind::AvoidGround), std::string_view("avoid_ground"));
    CHECK_EQ(MechanicKindName(MechanicKind::MoveOutCone), std::string_view("move_out_cone"));
    CHECK_EQ(MechanicKindName(MechanicKind::RunOutDebuff), std::string_view("run_out_debuff"));
    CHECK_EQ(MechanicKindName(MechanicKind::Los), std::string_view("los"));
}

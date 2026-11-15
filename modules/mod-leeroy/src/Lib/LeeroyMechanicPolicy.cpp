// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMechanicPolicy.h"
#include "LeeroyProtocol.h"

#include <algorithm>
#include <cmath>
#include <iterator>
#include <set>
#include <tuple>

namespace Leeroy::Guild
{
namespace
{
constexpr float kPi = 3.14159265358979f;

struct KindName
{
    std::string_view name;
    MechanicKind kind;
};
constexpr KindName kKinds[] = {
    {"avoid_ground", MechanicKind::AvoidGround}, {"move_out_cone", MechanicKind::MoveOutCone},
    {"spread", MechanicKind::Spread}, {"stack", MechanicKind::Stack},
    {"run_out_debuff", MechanicKind::RunOutDebuff}, {"los", MechanicKind::Los},
};

struct AreaName
{
    std::string_view name;
    MechanicArea area;
};
constexpr AreaName kAreas[] = {
    {"dest", MechanicArea::Dest}, {"caster", MechanicArea::Caster}, {"line", MechanicArea::Line},
    {"cone", MechanicArea::Cone}, {"group", MechanicArea::Group}, {"target", MechanicArea::Target},
    {"ring", MechanicArea::Ring},
};

struct RoleName
{
    std::string_view name;
    std::uint8_t bit;
};
constexpr RoleName kRoles[] = {
    {"tank", kMechanicTank}, {"healer", kMechanicHealer}, {"melee", kMechanicMelee}, {"ranged", kMechanicRanged},
};

// The areas each kind may take.
bool AreaFits(MechanicKind kind, MechanicArea area)
{
    switch (kind)
    {
        case MechanicKind::AvoidGround:
            return area == MechanicArea::Dest || area == MechanicArea::Caster || area == MechanicArea::Line
                || area == MechanicArea::Ring;
        case MechanicKind::MoveOutCone:
            return area == MechanicArea::Cone;
        case MechanicKind::Spread:
            return area == MechanicArea::Group;
        case MechanicKind::Stack:
            return area == MechanicArea::Dest;
        case MechanicKind::RunOutDebuff:
            return area == MechanicArea::Target;
        case MechanicKind::Los:
            return area == MechanicArea::Caster;
    }
    return false;
}

// The distance a kind needs, given the rule's numbers.
bool NumbersFit(MechanicRule const& rule)
{
    float const either = std::max(rule.radius, rule.range);
    switch (rule.kind)
    {
        case MechanicKind::AvoidGround:
            return rule.radius > 0
                && ((rule.area != MechanicArea::Line && rule.area != MechanicArea::Ring) || rule.range > 0);
        case MechanicKind::MoveOutCone:
            return either > 0 && rule.angle > 0;
        case MechanicKind::Spread:
        case MechanicKind::RunOutDebuff:
            return either > 0;
        case MechanicKind::Stack:
            return rule.radius > 0;
        case MechanicKind::Los:
            return true;
    }
    return false;
}

bool IdText(std::string const& id)
{
    return !id.empty() && id.size() <= kMechanicMaxIdBytes && std::all_of(id.begin(), id.end(), [](char c)
        { return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '.'; });
}

// A whole number from 0 to max, written signed or unsigned.
std::optional<std::uint64_t> Whole(nlohmann::json const& value, std::uint64_t max)
{
    if (value.is_number_unsigned())
    {
        auto const number = value.get<std::uint64_t>();
        return number <= max ? std::optional<std::uint64_t>(number) : std::nullopt;
    }
    if (value.is_number_integer())
    {
        auto const number = value.get<std::int64_t>();
        return number >= 0 && std::uint64_t(number) <= max ? std::optional<std::uint64_t>(number) : std::nullopt;
    }
    return std::nullopt;
}

float Distance2d(HazardPoint a, HazardPoint b)
{
    return std::hypot(a.x - b.x, a.y - b.y);
}

bool SameFloor(HazardPoint a, HazardPoint b)
{
    return std::fabs(a.z - b.z) <= kHazardHeightYards;
}

// The angle from `from` to `to`, else nullopt when they are the same spot.
std::optional<float> Bearing(HazardPoint from, HazardPoint to)
{
    if (Distance2d(from, to) < kMechanicSelfYards)
        return std::nullopt;
    return std::atan2(to.y - from.y, to.x - from.x);
}

// Where a line or cone points: at the target, else the destination, else the caster's own facing.
float Facing(MechanicCast const& cast)
{
    if (cast.target)
        if (auto const bearing = Bearing(cast.caster, *cast.target))
            return *bearing;
    if (cast.dest)
        if (auto const bearing = Bearing(cast.caster, *cast.dest))
            return *bearing;
    return cast.facing;
}

float AngleBetween(float a, float b)
{
    float d = std::fmod(std::fabs(a - b), 2.0f * kPi);
    return d > kPi ? 2.0f * kPi - d : d;
}

bool TooClose(HazardPoint p, std::vector<HazardPoint> const& others, float spacing)
{
    for (HazardPoint const& other : others)
        if (SameFloor(p, other) && Distance2d(p, other) < spacing)
            return true;
    return false;
}

float Spacing(MechanicDuty const& duty, float reach)
{
    return duty.spacing + static_cast<float>(duty.bodies) * reach;
}

// A spot with the margins: what MechanicSpots offers.
bool SpotKeeps(HazardPoint c, std::vector<MechanicDuty> const& duties, std::vector<HazardPoint> const& others,
    float reach)
{
    for (MechanicDuty const& duty : duties)
    {
        if (duty.leave && InMechanicShape(c, *duty.leave, reach + kMechanicMarginYards))
            return false;
        if (duty.gather && Distance2d(c, *duty.gather)
            > std::max(duty.gatherRadius - kMechanicGatherSlackYards, duty.gatherRadius * 0.5f))
            return false;
        if (duty.spacing > 0 && TooClose(c, others, Spacing(duty, reach) + kMechanicMarginYards))
            return false;
    }
    return true;
}

void Ring(std::vector<HazardPoint>& out, HazardPoint centre, float radius, int directions, float z)
{
    for (int k = 0; k < directions; ++k)
    {
        float const angle = 2.0f * kPi * static_cast<float>(k) / static_cast<float>(directions);
        out.push_back({centre.x + radius * std::cos(angle), centre.y + radius * std::sin(angle), z});
    }
}

// Straight out of a shape the bot stands in, the shortest way.
std::optional<HazardPoint> DirectExit(HazardPoint at, MechanicShape const& shape, float reach)
{
    float const clear = reach + kMechanicMarginYards + 0.1f;
    switch (shape.type)
    {
        case MechanicShapeType::Circle:
        {
            float dx = at.x - shape.origin.x;
            float dy = at.y - shape.origin.y;
            float length = std::hypot(dx, dy);
            if (length < 0.01f)
            {
                dx = 1.0f;
                dy = 0.0f;
                length = 1.0f;
            }
            float const out = shape.radius + clear;
            return HazardPoint{shape.origin.x + dx / length * out, shape.origin.y + dy / length * out, at.z};
        }
        case MechanicShapeType::Ring:
        {
            // Inside the ring or outside it, whichever edge is nearer.
            float dx = at.x - shape.origin.x;
            float dy = at.y - shape.origin.y;
            float length = std::hypot(dx, dy);
            if (length < 0.01f)
                return std::nullopt; // on the centre: clear already
            float const inner = shape.length - shape.radius - clear;
            float const outer = shape.length + shape.radius + clear;
            float const to = inner > 0 && length - inner < outer - length ? inner : outer;
            return HazardPoint{shape.origin.x + dx / length * to, shape.origin.y + dy / length * to, at.z};
        }
        case MechanicShapeType::Band:
        case MechanicShapeType::Cone:
        {
            // Sideways off the centre line, on the bot's own side; a cone near a half circle wide has no short
            // sideways way out (the rings find one).
            if (shape.type == MechanicShapeType::Cone && shape.halfAngle >= 1.4f)
                return std::nullopt;
            float const dx = at.x - shape.origin.x;
            float const dy = at.y - shape.origin.y;
            float const side = -dx * std::sin(shape.facing) + dy * std::cos(shape.facing);
            float const along = dx * std::cos(shape.facing) + dy * std::sin(shape.facing);
            float const sign = side < 0 ? -1.0f : 1.0f;
            float const half = shape.type == MechanicShapeType::Band ? shape.radius
                : std::max(0.0f, along) * std::tan(shape.halfAngle);
            float const off = sign * (half + clear) - side;
            return HazardPoint{at.x - off * std::sin(shape.facing), at.y + off * std::cos(shape.facing), at.z};
        }
    }
    return std::nullopt;
}
}

std::optional<MechanicTable> ParseRaidMechanics(nlohmann::json const& message, std::string& error)
{
    error.clear();
    auto bad = [&](std::string_view key) -> std::optional<MechanicTable>
    {
        error = std::string(Err::BadField) + std::string(key);
        return std::nullopt;
    };
    if (!message.is_object())
        return bad("map");
    MechanicTable out;
    auto map = message.find("map");
    auto const mapId = map == message.end() ? std::nullopt : Whole(*map, 10000);
    if (!mapId || *mapId == 0)
        return bad("map");
    out.map = static_cast<std::uint32_t>(*mapId);
    auto difficulty = message.find("difficulty");
    auto const level = difficulty == message.end() ? std::nullopt : Whole(*difficulty, 3);
    if (!level)
        return bad("difficulty");
    out.difficulty = static_cast<std::uint8_t>(*level);
    auto mechanics = message.find("mechanics");
    if (mechanics == message.end() || !mechanics->is_array() || mechanics->size() > kMechanicMaxRules)
        return bad("mechanics");
    std::set<std::string> ids;
    for (auto const& item : *mechanics)
    {
        if (!item.is_object())
            return bad("mechanics");
        MechanicRule rule;
        auto id = item.find("id");
        if (id == item.end() || !id->is_string() || !IdText(id->get<std::string>())
            || !ids.insert(id->get<std::string>()).second)
            return bad("id");
        rule.id = id->get<std::string>();
        auto kind = item.find("kind");
        if (kind == item.end() || !kind->is_string())
            return bad("kind");
        auto const kindName = kind->get<std::string>();
        auto const k = std::find_if(std::begin(kKinds), std::end(kKinds), [&](KindName const& e)
            { return e.name == kindName; });
        if (k == std::end(kKinds))
            return bad("kind");
        rule.kind = k->kind;
        auto area = item.find("area");
        if (area == item.end() || !area->is_string())
            return bad("area");
        auto const areaName = area->get<std::string>();
        auto const a = std::find_if(std::begin(kAreas), std::end(kAreas), [&](AreaName const& e)
            { return e.name == areaName; });
        if (a == std::end(kAreas) || !AreaFits(rule.kind, a->area))
            return bad("area");
        rule.area = a->area;
        auto roles = item.find("roles");
        if (roles == item.end() || !roles->is_array() || roles->empty() || roles->size() > std::size(kRoles))
            return bad("roles");
        for (auto const& role : *roles)
        {
            if (!role.is_string())
                return bad("roles");
            auto const name = role.get<std::string>();
            auto const r = std::find_if(std::begin(kRoles), std::end(kRoles), [&](RoleName const& e)
                { return e.name == name; });
            if (r == std::end(kRoles) || (rule.roles & r->bit))
                return bad("roles");
            rule.roles |= r->bit;
        }
        auto spells = item.find("spells");
        if (spells == item.end() || !spells->is_array() || spells->empty() || spells->size() > kMechanicMaxSpells)
            return bad("spells");
        for (auto const& spell : *spells)
        {
            auto const spellId = Whole(spell, 1000000);
            if (!spellId || *spellId == 0)
                return bad("spells");
            rule.spells.push_back(static_cast<std::uint32_t>(*spellId));
        }
        for (auto const& [key, field, limit] : {std::tuple{"radius", &rule.radius, kMechanicMaxYards},
                 std::tuple{"range", &rule.range, kMechanicMaxYards}, std::tuple{"angle", &rule.angle, 360.0f},
                 std::tuple{"duration", &rule.duration, kMechanicMaxDurationSeconds}})
        {
            auto value = item.find(key);
            if (value == item.end())
                continue;
            if (!value->is_number())
                return bad(key);
            double const number = value->get<double>();
            if (!std::isfinite(number) || number < 0 || number > limit)
                return bad(key);
            *field = static_cast<float>(number);
        }
        if (auto landing = item.find("landing"); landing != item.end())
        {
            if (!landing->is_boolean())
                return bad("landing");
            rule.landing = landing->get<bool>();
        }
        if (!NumbersFit(rule))
            return bad(rule.kind == MechanicKind::MoveOutCone && rule.angle <= 0 ? "angle"
                : (rule.area == MechanicArea::Line || rule.area == MechanicArea::Ring) && rule.range <= 0 ? "range"
                : "radius");
        out.rules.push_back(std::move(rule));
    }
    return out;
}

std::vector<std::size_t> MechanicRulesFor(MechanicTable const& table, std::uint32_t spellId)
{
    std::vector<std::size_t> out;
    for (std::size_t i = 0; i < table.rules.size(); ++i)
        if (spellId && std::find(table.rules[i].spells.begin(), table.rules[i].spells.end(), spellId)
            != table.rules[i].spells.end())
            out.push_back(i);
    return out;
}

std::string_view MechanicKindName(MechanicKind kind)
{
    for (auto const& entry : kKinds)
        if (entry.kind == kind)
            return entry.name;
    return "unknown";
}

std::uint8_t MechanicRole(bool tank, bool healer, bool ranged)
{
    return tank ? kMechanicTank : healer ? kMechanicHealer : ranged ? kMechanicRanged : kMechanicMelee;
}

std::string_view MechanicSkipReason(MechanicMemberFacts const& facts)
{
    if (!facts.enabled)
        return "off";
    if (!facts.cast)
        return "not_cast";
    if (!facts.bot)
        return "not_bot";
    if (!facts.ownerGroup && !facts.offlineRaid)
        return "not_in_group";
    if (facts.fenced)
        return "fenced";
    if (facts.held)
        return "held";
    if (!facts.alive)
        return "dead";
    if (!facts.free)
        return "busy";
    if (!facts.covered)
        return "no_table";
    return {};
}

bool MechanicOfflineFenced(MechanicOfflineFence const& fence)
{
    if (fence.realm || fence.dummy || fence.cleanup || fence.pendingJob)
        return true;
    return std::any_of(fence.dcOwners.begin(), fence.dcOwners.end(), [&](std::uint32_t leader)
        { return !fence.raidLeader || leader != fence.raidLeader; });
}

bool MechanicYieldsToStock(bool lastMoveForced, bool sinceMechanicBegan, bool lastMoveOurs)
{
    return lastMoveForced && sinceMechanicBegan && !lastMoveOurs;
}

std::uint32_t MechanicLifetimeMs(MechanicRule const& rule, std::uint32_t castMs, std::uint32_t spellDurationMs)
{
    std::uint64_t linger = kMechanicDefaultLingerMs;
    if (rule.duration > 0)
        linger = static_cast<std::uint64_t>(std::lround(double(rule.duration) * 1000.0));
    else if (spellDurationMs > 0)
        linger = std::min<std::uint64_t>(spellDurationMs, kMechanicMaxLingerMs);
    std::uint64_t const total = std::uint64_t(castMs) + linger;
    return static_cast<std::uint32_t>(std::clamp<std::uint64_t>(total, kMechanicMinLifetimeMs, kMechanicMaxLifetimeMs));
}

bool MechanicBinds(MechanicRule const& rule, std::uint8_t role, std::uint32_t guid, std::uint32_t debuffed)
{
    if (!(rule.roles & role))
        return false;
    return rule.kind != MechanicKind::RunOutDebuff || (guid && guid == debuffed);
}

bool InMechanicShape(HazardPoint p, MechanicShape const& shape, float reach)
{
    if (!SameFloor(p, shape.origin))
        return false;
    float const dx = p.x - shape.origin.x;
    float const dy = p.y - shape.origin.y;
    float const distance = std::hypot(dx, dy);
    switch (shape.type)
    {
        case MechanicShapeType::Circle:
            return distance <= shape.radius + reach;
        case MechanicShapeType::Band:
        {
            float const along = dx * std::cos(shape.facing) + dy * std::sin(shape.facing);
            float const side = std::fabs(-dx * std::sin(shape.facing) + dy * std::cos(shape.facing));
            return along >= -reach && along <= shape.length + reach && side <= shape.radius + reach;
        }
        case MechanicShapeType::Cone:
        {
            if (distance <= reach)
                return true; // on the caster: the core counts anyone within its boundary
            if (distance > shape.length + reach)
                return false;
            float const bearing = std::atan2(dy, dx);
            return AngleBetween(bearing, shape.facing) <= shape.halfAngle + std::atan2(reach, distance);
        }
        case MechanicShapeType::Ring:
            return std::fabs(distance - shape.length) <= shape.radius + reach;
    }
    return false;
}

std::optional<MechanicShape> MechanicLeaveArea(MechanicRule const& rule, MechanicCast const& cast,
    HazardPoint casterNow)
{
    if (rule.kind != MechanicKind::AvoidGround && rule.kind != MechanicKind::MoveOutCone)
        return std::nullopt;
    MechanicShape shape;
    switch (rule.area)
    {
        case MechanicArea::Dest:
        case MechanicArea::Ring:
        {
            std::optional<HazardPoint> const centre = cast.dest ? cast.dest : cast.target;
            if (!centre)
                return std::nullopt;
            shape.origin = *centre;
            shape.radius = rule.radius;
            if (rule.area == MechanicArea::Ring)
            {
                shape.type = MechanicShapeType::Ring;
                shape.length = rule.range;
            }
            return shape;
        }
        case MechanicArea::Caster:
            shape.origin = casterNow;
            shape.radius = rule.radius;
            return shape;
        case MechanicArea::Line:
            shape.type = MechanicShapeType::Band;
            shape.origin = cast.caster;
            shape.radius = rule.radius;
            shape.length = rule.range;
            shape.facing = Facing(cast);
            return shape;
        case MechanicArea::Cone:
            shape.type = MechanicShapeType::Cone;
            shape.origin = cast.caster;
            shape.length = rule.range > 0 ? rule.range : rule.radius;
            shape.facing = Facing(cast);
            shape.halfAngle = rule.angle * kPi / 360.0f;
            return shape;
        case MechanicArea::Group:
        case MechanicArea::Target:
            break;
    }
    return std::nullopt;
}

std::optional<MechanicDuty> MechanicDutyFor(MechanicRule const& rule, MechanicCast const& cast, HazardPoint casterNow,
    std::optional<HazardPoint> targetNow)
{
    MechanicDuty duty;
    switch (rule.kind)
    {
        case MechanicKind::AvoidGround:
        case MechanicKind::MoveOutCone:
            duty.leave = MechanicLeaveArea(rule, cast, casterNow);
            if (!duty.leave)
                return std::nullopt;
            return duty;
        case MechanicKind::Spread:
        case MechanicKind::RunOutDebuff:
            duty.spacing = std::max(rule.radius, rule.range);
            duty.bodies = rule.kind == MechanicKind::Spread ? 2 : 1;
            return duty;
        case MechanicKind::Stack:
            duty.gather = targetNow ? targetNow : cast.target ? cast.target : cast.dest;
            if (!duty.gather)
                return std::nullopt;
            duty.gatherRadius = rule.radius;
            return duty;
        case MechanicKind::Los:
            duty.hide = true;
            return duty;
    }
    return std::nullopt;
}

bool MechanicDutyReaches(MechanicDuty const& duty, HazardPoint at)
{
    return !duty.gather || (SameFloor(at, *duty.gather) && Distance2d(at, *duty.gather) <= kMechanicGatherReachYards);
}

bool MechanicKept(HazardPoint at, std::vector<MechanicDuty> const& duties, std::vector<HazardPoint> const& others,
    std::vector<HazardZone> const& hazards, float reach, MechanicSightFn const& hidden)
{
    if (!duties.empty() && HazardAt(at, hazards, reach))
        return false; // a bound bot standing in fire is moved out with the rest
    for (MechanicDuty const& duty : duties)
    {
        if (duty.leave && InMechanicShape(at, *duty.leave, reach))
            return false;
        if (duty.gather && Distance2d(at, *duty.gather) > duty.gatherRadius)
            return false;
        if (duty.spacing > 0 && TooClose(at, others, Spacing(duty, reach)))
            return false;
        if (duty.hide && hidden && !hidden(at))
            return false;
    }
    return true;
}

std::vector<HazardPoint> MechanicSpots(HazardPoint at, std::vector<MechanicDuty> const& duties,
    std::vector<HazardPoint> const& others, std::vector<HazardZone> const& hazards, float reach, std::size_t limit,
    MechanicSightFn const& hidden)
{
    if (limit == 0 || MechanicKept(at, duties, others, hazards, reach, hidden))
        return {};

    std::vector<HazardPoint> candidates;
    bool hide = false;
    float spacing = 0;
    for (MechanicDuty const& duty : duties)
    {
        if (duty.leave && InMechanicShape(at, *duty.leave, reach))
            if (auto const exit = DirectExit(at, *duty.leave, reach))
                candidates.push_back(*exit);
        if (duty.gather)
        {
            float const inner = std::max(duty.gatherRadius - kMechanicGatherSlackYards, duty.gatherRadius * 0.5f);
            candidates.push_back({duty.gather->x, duty.gather->y, at.z});
            for (float ring = std::min(inner, kMechanicRingStepYards); ring <= inner + 0.001f;
                 ring += kMechanicRingStepYards)
                Ring(candidates, *duty.gather, ring, kMechanicRingDirections, at.z);
        }
        hide = hide || duty.hide;
        spacing = std::max(spacing, duty.spacing > 0 ? Spacing(duty, reach) : 0.0f);
    }
    for (float ring = kMechanicRingStepYards; ring <= kMechanicMaxShiftYards + 0.001f; ring += kMechanicRingStepYards)
        Ring(candidates, at, ring, kMechanicRingDirections, at.z);

    // Between equals, away from the middle of the others near enough to matter.
    HazardPoint toward = at;
    if (spacing > 0)
    {
        float sx = 0, sy = 0;
        int near = 0;
        for (HazardPoint const& other : others)
            if (SameFloor(at, other) && Distance2d(at, other) <= 2.0f * spacing)
            {
                sx += other.x;
                sy += other.y;
                ++near;
            }
        if (near)
        {
            float const dx = at.x - sx / static_cast<float>(near);
            float const dy = at.y - sy / static_cast<float>(near);
            float const length = std::hypot(dx, dy);
            if (length > 0.01f)
                toward = {at.x + dx / length * kMechanicMaxShiftYards, at.y + dy / length * kMechanicMaxShiftYards,
                    at.z};
        }
    }

    std::vector<std::tuple<float, float, std::size_t>> ranked;
    for (std::size_t i = 0; i < candidates.size(); ++i)
    {
        float const shift = Distance2d(at, candidates[i]);
        if (shift <= kMechanicMaxShiftYards + 0.001f)
            ranked.emplace_back(std::round(shift * 10.0f) / 10.0f, Distance2d(toward, candidates[i]), i);
    }
    std::stable_sort(ranked.begin(), ranked.end());

    std::vector<HazardPoint> spots;
    for (auto const& [shift, away, index] : ranked)
    {
        HazardPoint const& c = candidates[index];
        if (!SpotKeeps(c, duties, others, reach) || !HazardClear(c, hazards, reach))
            continue;
        if (hide && hidden && !hidden(c))
            continue; // the sight test last: it is the dear one
        spots.push_back(c);
        if (spots.size() == limit)
            break;
    }
    return spots;
}

bool MechanicRefusesMove(HazardPoint at, HazardPoint to, std::vector<MechanicDuty> const& duties,
    std::vector<HazardZone> const& hazards, float reach)
{
    if (HazardAt(at, hazards, reach))
        return false;
    for (MechanicDuty const& duty : duties)
        if (duty.leave && InMechanicShape(to, *duty.leave, reach))
            return true;
    return false;
}
}

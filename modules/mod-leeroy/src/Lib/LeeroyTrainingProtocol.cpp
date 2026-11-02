// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingProtocol.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyProtocol.h"

#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
#include <utility>

namespace Leeroy::Training
{
namespace
{
using json = nlohmann::json;
constexpr auto kU32Max = std::numeric_limits<std::uint32_t>::max();

template <typename Range, typename Value>
bool Contains(Range const& range, Value const& value)
{
    return std::find(range.begin(), range.end(), value) != range.end();
}

class Fields
{
public:
    Fields(json const& value, std::string& error) : _value(value), _error(error) { _error.clear(); }
    bool Failed() const { return !_error.empty(); }
    void Require(bool valid, char const* key)
    {
        if (!Failed() && !valid)
            _error = std::string(Err::BadField) + key;
    }
    json const* Find(char const* key) const
    {
        if (!_value.is_object())
            return nullptr;
        auto const it = _value.find(key);
        return it == _value.end() ? nullptr : &*it;
    }
    static bool Unsigned(json const& value, std::uint32_t low, std::uint32_t high)
    {
        return value.is_number_unsigned() && value.get<std::uint64_t>() >= low
            && value.get<std::uint64_t>() <= high;
    }
    void Number(char const* key, std::uint32_t& out, std::uint32_t low = 1,
        std::uint32_t high = kU32Max, bool optional = false)
    {
        auto v = Find(key);
        if (Failed() || (!v && optional))
            return;
        Require(v && Unsigned(*v, low, high), key);
        if (!Failed())
            out = v->get<std::uint32_t>();
    }
    void Signed(char const* key, std::int32_t& out)
    {
        auto v = Find(key);
        if (Failed() || !v)
            return;
        bool valid = v->is_number_unsigned()
            ? v->get<std::uint64_t>() <= std::uint64_t(std::numeric_limits<std::int32_t>::max())
            : v->is_number_integer() && v->get<std::int64_t>() >= std::numeric_limits<std::int32_t>::min()
                && v->get<std::int64_t>() <= std::numeric_limits<std::int32_t>::max();
        Require(valid, key);
        if (!Failed())
            out = v->get<std::int32_t>();
    }
    void Boolean(char const* key, bool& out, bool optional = true)
    {
        auto v = Find(key);
        if (Failed() || (!v && optional))
            return;
        Require(v && v->is_boolean(), key);
        if (!Failed())
            out = v->get<bool>();
    }
    void String(char const* key, std::string& out, bool optional = false)
    {
        auto v = Find(key);
        if (Failed() || (!v && optional))
            return;
        Require(v && v->is_string(), key);
        if (!Failed())
            out = v->get<std::string>();
    }
    void List(char const* key, std::vector<std::uint32_t>& out, std::size_t low, std::size_t high,
        std::uint32_t itemLow = 1, std::uint32_t itemHigh = kU32Max, bool optional = false)
    {
        auto v = Find(key);
        if (Failed() || (!v && optional))
            return;
        Require(v && v->is_array() && v->size() >= low && v->size() <= high, key);
        if (Failed())
            return;
        std::set<std::uint32_t> seen;
        for (auto const& item : *v)
        {
            Require(Unsigned(item, itemLow, itemHigh), key);
            if (Failed())
                return;
            auto number = item.get<std::uint32_t>();
            Require(seen.insert(number).second, key);
            if (Failed())
                return;
            out.push_back(number);
        }
    }
    void Strings(char const* key, std::vector<std::string>& out, std::size_t low, std::size_t high,
        std::size_t minLength, std::size_t maxLength, bool optional = false)
    {
        auto v = Find(key);
        if (Failed() || (!v && optional))
            return;
        Require(v && v->is_array() && v->size() >= low && v->size() <= high, key);
        if (Failed())
            return;
        for (auto const& item : *v)
        {
            Require(item.is_string(), key);
            if (Failed())
                return;
            auto value = item.get<std::string>();
            Require(value.size() >= minLength && value.size() <= maxLength, key);
            if (Failed())
                return;
            out.push_back(std::move(value));
        }
    }
    template <typename T>
    std::optional<T> Result(T out) const
    {
        return Failed() ? std::nullopt : std::optional<T>(std::move(out));
    }
private:
    json const& _value;
    std::string& _error;
};

bool LineName(std::string_view text)
{
    return !text.empty() && text.size() <= 40 && std::all_of(text.begin(), text.end(), [](char c)
    {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == ' ';
    });
}
}

std::optional<TelemetryCmd> ParseTelemetry(json const& j, std::string& error)
{
    Fields f(j, error);
    TelemetryCmd out;
    f.Boolean("record", out.record, false);
    f.List("watch", out.watch, 0, 32, 1, kU32Max, true);
    return f.Result(std::move(out));
}

std::optional<GetStatsCmd> ParseGetStats(json const& j, std::string& error)
{
    Fields f(j, error);
    f.Require(j.is_object(), "type");
    return f.Result(GetStatsCmd{});
}

std::optional<GetVocabCmd> ParseGetVocab(json const& j, std::string& error)
{
    Fields f(j, error);
    GetVocabCmd out;
    f.List("classes", out.classes, 0, 11, 1, 11, true);
    f.List("probes", out.probes, 0, 10, 1, kU32Max, true);
    f.Require(!out.classes.empty() || !out.probes.empty(), "classes");
    f.Strings("triggers", out.triggers, 0, 200, 1, 40, true);
    return f.Result(std::move(out));
}

std::optional<CustomStrategyCmd> ParseCustomStrategy(json const& j, std::string& error)
{
    Fields f(j, error);
    CustomStrategyCmd out;
    f.String("name", out.name);
    f.Require(Guild::CustomStrategyName("custom::" + out.name), "name");
    f.Number("class", out.cls, 1, 11);
    f.Number("probe", out.probe, 0, kU32Max, true);
    f.Strings("lines", out.lines, 1, 12, 1, 120);
    f.Require(std::all_of(out.lines.begin(), out.lines.end(), CustomLineValid), "lines");
    return f.Result(std::move(out));
}

std::optional<DcSettingCmd> ParseDcSetting(json const& j, std::string& error)
{
    Fields f(j, error);
    DcSettingCmd out;
    std::string op;
    f.Number("leader_guid", out.leaderGuid);
    f.String("op", op);
    f.Require(op == "set" || op == "scale" || op == "reset" || op == "clear", "op");
    if (op == "scale") out.op = DcOp::Scale;
    else if (op == "reset") out.op = DcOp::Reset;
    else if (op == "clear") out.op = DcOp::Clear;
    f.String("key", out.key, op == "clear");
    f.Require(op == "clear" ? out.key.empty() || DcKeyAllowed(out.key) : DcKeyAllowed(out.key), "key");
    f.Signed("centi", out.centi);
    f.Require(op != "scale" || (out.centi >= 50 && out.centi <= 150), "centi");
    return f.Result(std::move(out));
}

std::optional<DummyTrialCmd> ParseDummyTrial(json const& j, std::string& error)
{
    Fields f(j, error);
    DummyTrialCmd out;
    f.List("guids", out.guids, 1, 10);
    f.Strings("co", out.co, out.guids.size(), out.guids.size(), 0, kMaxLineBytes);
    f.Require(std::all_of(out.co.begin(), out.co.end(), [](auto const& entry)
        { return entry.empty() || Guild::BotDoParamAllowed("co", entry); }), "co");
    f.Number("place", out.place, 0, 1);
    f.Number("seconds", out.seconds, 30, 300);
    return f.Result(std::move(out));
}

std::optional<GetRivalsCmd> ParseGetRivals(json const& j, std::string& error)
{
    Fields f(j, error);
    GetRivalsCmd out;
    f.Require(j.is_object(), "type");
    f.Number("level", out.level, 70, 80, true);
    f.Number("max", out.max, 1, 400, true);
    return f.Result(std::move(out));
}

std::optional<StartBenchmarkCmd> ParseStartBenchmark(json const& j, std::string& error)
{
    Fields f(j, error);
    StartBenchmarkCmd out;
    f.Number("map_id", out.mapId, 0);
    f.Number("size", out.size, 10, 25);
    f.Require(out.size == 10 || out.size == 25, "size");
    f.Boolean("heroic", out.heroic);
    f.Number("leader_guid", out.leaderGuid);
    f.List("members", out.members, 1, out.size ? out.size - 1 : 0);
    f.Require(!Contains(out.members, out.leaderGuid), "members");
    f.List("bosses", out.bosses, 1, 20, 0);
    f.Number("max_wipes", out.maxWipes, 1, 20, true);
    f.Number("max_wipes_per_boss", out.maxWipesPerBoss, 1, 10, true);
    f.Number("timeout_min", out.timeoutMin, 10, 360, true);
    auto count = static_cast<std::uint32_t>(out.members.size()) + 1;
    out.minMembers = std::min(out.size * 4 / 5, count);
    f.Number("min_members", out.minMembers, 1, count, true);
    return f.Result(std::move(out));
}

void RegisterTrainingCommandTypes()
{
    for (auto name : kTrainingCommandTypes)
        RegisterExtensionCommand(name);
}

bool DcKeyAllowed(std::string_view key) { return Contains(kDcKeys, key); }

bool CustomLineValid(std::string_view line)
{
    if (line.size() > 120)
        return false;
    auto const arrow = line.find('>'), bang = line.find('!');
    if (arrow == std::string_view::npos || bang == std::string_view::npos || bang <= arrow
        || !LineName(line.substr(0, arrow)) || !LineName(line.substr(arrow + 1, bang - arrow - 1)))
        return false;
    auto const relevance = line.substr(bang + 1);
    return (relevance.size() == 1 || (relevance.size() == 3 && relevance[1] == '.'
            && relevance[2] >= '0' && relevance[2] <= '9'))
        && relevance[0] >= '5' && relevance[0] <= '9';
}

std::string FormatCacheEntry(std::string_view name, std::vector<std::string> const& lines)
{
    if (!Guild::CustomStrategyName("custom::" + std::string(name)) || lines.empty() || lines.size() > 12
        || !std::all_of(lines.begin(), lines.end(), CustomLineValid))
        throw std::invalid_argument("invalid training cache entry");
    std::string out;
    for (auto const& line : lines)
    {
        if (!out.empty())
            out += ",\n";
        out += "(nullptr, '" + std::string(name) + "', '" + line + "')";
    }
    return out + ";";
}

bool IsTierTwo(std::string_view action, std::string_view param)
{
    if (action == "value" || action == "restore")
        return true;
    if (action != "co" && action != "nc")
        return false;
    for (;;)
    {
        auto const comma = param.find(',');
        auto const entry = param.substr(0, comma);
        if (entry.size() > 1)
        {
            auto const name = entry.substr(1);
            if (Contains(kTierTwoStrategies, name) || name.starts_with("custom::"))
                return true;
        }
        if (comma == std::string_view::npos)
            return false;
        param.remove_prefix(comma + 1);
    }
}

bool TierTwoAllowed(bool trainingOn, std::string_view action, std::string_view param)
{
    if (!Guild::BotDoParamAllowed(action, param))
        return false;
    if (trainingOn || action == "restore")
        return true;
    if (action != "co" && action != "nc")
        return false;
    for (;;)
    {
        auto const comma = param.find(',');
        auto const entry = param.substr(0, comma);
        if (entry.size() < 2 || entry.front() != '-' || !Guild::CustomStrategyName(entry.substr(1)))
            return false;
        if (comma == std::string_view::npos)
            return true;
        param.remove_prefix(comma + 1);
    }
}

bool ShouldArm(ArmInputs const& in)
{
    return in.isRaid && in.telemetryOn && in.mindArmed && (in.trackedOnMap || in.registered)
        && (!in.registered || in.bossAllowed)
        && !Contains(kNonFightSteps, std::pair{in.map, in.bossId});
}

bool ShouldAbort(std::uint64_t startMs, std::uint64_t nowMs)
{
    return nowMs >= startMs && nowMs - startMs >= 1200000;
}

BossRecordDecision ReviewBossState(bool awaitingStart, bool terminalIntent, std::uint8_t actualState)
{
    if (actualState == 1) return BossRecordDecision::Confirm;
    if (terminalIntent && actualState == 3) return BossRecordDecision::Killed;
    if (terminalIntent && (actualState == 0 || actualState == 2)) return BossRecordDecision::Wiped;
    return awaitingStart ? BossRecordDecision::Discard : BossRecordDecision::Retain;
}

bool RecordingStillCurrent(bool enabled, std::uint64_t openingEpoch, std::uint64_t currentEpoch)
{
    return enabled && openingEpoch == currentEpoch;
}
}

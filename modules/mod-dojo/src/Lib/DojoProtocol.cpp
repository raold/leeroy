// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoProtocol.h"

#include <algorithm>
#include <cmath>
#include <initializer_list>
#include <vector>

namespace Dojo
{
namespace
{
struct Fields
{
    std::string_view type;
    std::vector<std::string_view> required;
    std::vector<std::string_view> optional;
};

std::vector<Fields> const& RequestFields()
{
    static std::vector<Fields> const fields{
        {"hello", {"protocol"}, {"client", "expect"}},
        {"reset", {"session", "episode", "decision", "scenario_seed"}, {"scenario"}},
        {"step", {"session", "episode", "decision", "actions"}, {}},
        {"status", {}, {"session"}},
        {"close", {"session"}, {"reason"}},
    };
    return fields;
}

Fields const* FindFields(std::string_view type)
{
    for (Fields const& f : RequestFields())
        if (f.type == type)
            return &f;
    return nullptr;
}

bool Contains(std::vector<std::string_view> const& names, std::string_view name)
{
    return std::find(names.begin(), names.end(), name) != names.end();
}

bool HasExactly(Json const& j, std::initializer_list<std::string_view> keys)
{
    if (!j.is_object() || j.size() != keys.size())
        return false;
    for (std::string_view key : keys)
        if (!j.contains(key))
            return false;
    return true;
}

Decoded Refuse(std::string_view code, std::optional<std::string> id, std::string message)
{
    Decoded d;
    d.code = std::string(code);
    d.id = std::move(id);
    d.message = std::move(message);
    return d;
}

// The first failed field check, or an empty string.
std::string CheckFields(std::string_view type, Json const& obj, Bounds const& bounds)
{
    auto bad = [](std::string_view name) { return "bad field: " + std::string(name); };
    if (type == "hello")
    {
        Json const& p = obj["protocol"];
        if (!HasExactly(p, {"min", "max"}) || !IsInt(p["min"], 1, kMaxProtocol) || !IsInt(p["max"], 1, kMaxProtocol)
            || p["min"].get<std::int64_t>() > p["max"].get<std::int64_t>())
            return bad("protocol");
        if (obj.contains("client") && !IsText(obj["client"]))
            return bad("client");
        if (obj.contains("expect"))
        {
            Json const& e = obj["expect"];
            if (!e.is_object())
                return bad("expect");
            for (auto const& [key, value] : e.items())
                if ((key != "observation_schema" && key != "action_schema" && key != "scenario") || !IsText(value))
                    return bad("expect");
        }
        return {};
    }
    if (obj.contains("session") && !IsId(obj["session"]))
        return bad("session");
    if (obj.contains("episode") && !IsId(obj["episode"]))
        return bad("episode");
    if (obj.contains("decision") && !IsInt(obj["decision"], 0, kMaxDecision))
        return bad("decision");
    if (type == "reset")
    {
        if (!IsInt(obj["scenario_seed"], 0, kMaxSeed))
            return bad("scenario_seed");
        if (obj.contains("scenario") && !IsId(obj["scenario"]))
            return bad("scenario");
    }
    else if (type == "step")
    {
        Json const& actions = obj["actions"];
        if (!actions.is_array() || actions.size() > bounds.maxActors)
            return bad("actions");
        for (Json const& a : actions)
            if (!HasExactly(a, {"actor", "action"}) || !IsInt(a["actor"], 0, kMaxIndex)
                || !IsInt(a["action"], 0, kMaxIndex))
                return bad("actions");
    }
    else if (type == "close")
    {
        if (obj.contains("reason") && !IsText(obj["reason"]))
            return bad("reason");
    }
    return {};
}
}

Json Bounds::ToWire() const
{
    return Json{{"max_line_bytes", maxLineBytes}, {"max_actors", maxActors}, {"max_events", maxEvents},
        {"result_cache", resultCache}, {"recent_episodes", kRecentEpisodes}, {"in_flight", 1}};
}

Bounds Bounds::FromWire(Json const& wire)
{
    Bounds b;
    if (!wire.is_object())
        return b;
    if (wire.contains("max_line_bytes"))
        b.maxLineBytes = wire["max_line_bytes"].get<std::size_t>();
    if (wire.contains("max_actors"))
        b.maxActors = wire["max_actors"].get<std::uint32_t>();
    if (wire.contains("max_events"))
        b.maxEvents = wire["max_events"].get<std::uint32_t>();
    if (wire.contains("result_cache"))
        b.resultCache = wire["result_cache"].get<std::size_t>();
    return b;
}

bool IsIdText(std::string_view s)
{
    if (s.empty() || s.size() > 64)
        return false;
    for (char c : s)
    {
        bool const ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.'
            || c == '_' || c == ':' || c == '-';
        if (!ok)
            return false;
    }
    return true;
}

bool IsId(Json const& j)
{
    return j.is_string() && IsIdText(j.get_ref<std::string const&>());
}

bool IsInt(Json const& j, std::int64_t low, std::int64_t high)
{
    if (j.is_number_unsigned())
    {
        std::uint64_t const u = j.get<std::uint64_t>();
        return high >= 0 && u <= static_cast<std::uint64_t>(high) && (low <= 0 || u >= static_cast<std::uint64_t>(low));
    }
    if (j.is_number_integer())
    {
        std::int64_t const i = j.get<std::int64_t>();
        return i >= low && i <= high;
    }
    return false;
}

bool IsNumber(Json const& j)
{
    if (j.is_number_float())
        return std::isfinite(j.get<double>());
    return j.is_number();
}

bool IsText(Json const& j)
{
    return j.is_string() && j.get_ref<std::string const&>().size() <= kMaxTextBytes;
}

bool HasNonFinite(Json const& j)
{
    std::vector<Json const*> stack{&j};
    while (!stack.empty())
    {
        Json const* node = stack.back();
        stack.pop_back();
        if (node->is_number_float() && !std::isfinite(node->get<double>()))
            return true;
        if (node->is_structured())
            for (Json const& child : *node)
                stack.push_back(&child);
    }
    return false;
}

std::string Encode(Json const& j)
{
    return j.dump(-1, ' ', false, Json::error_handler_t::strict);
}

Json MakeError(std::string_view code, std::optional<std::string> const& id, std::string_view message,
    std::optional<std::string_view> reason)
{
    Json e{{"v", kProtocolVersion}, {"type", "error"}, {"id", id ? Json(*id) : Json(nullptr)}, {"ok", false},
        {"code", std::string(code)}, {"message", std::string(message)}};
    if (reason)
        e["reason"] = std::string(*reason);
    return e;
}

Decoded DecodeRequest(std::string_view line, Bounds const& bounds)
{
    if (line.size() > bounds.maxLineBytes)
        return Refuse(Err::LineTooLong, std::nullopt, "line over " + std::to_string(bounds.maxLineBytes) + " bytes");

    bool tooDeep = false;
    Json::parser_callback_t const depthGuard = [&tooDeep](int depth, Json::parse_event_t event, Json&)
    {
        if ((event == Json::parse_event_t::object_start || event == Json::parse_event_t::array_start)
            && depth + 1 > kMaxDepth)
        {
            tooDeep = true;
            return false;
        }
        return true;
    };
    Json obj = Json::parse(line.begin(), line.end(), depthGuard, false);
    if (tooDeep)
        return Refuse(Err::Malformed, std::nullopt, "nested deeper than " + std::to_string(kMaxDepth));
    if (obj.is_discarded())
        return Refuse(Err::Malformed, std::nullopt, "not JSON");
    if (!obj.is_object())
        return Refuse(Err::Malformed, std::nullopt, "not a JSON object");

    std::optional<std::string> id;
    if (auto it = obj.find("id"); it != obj.end() && IsId(*it))
        id = it->get<std::string>();
    auto const v = obj.find("v");
    if (v == obj.end())
        return Refuse(Err::Malformed, id, "missing v");
    if (!v->is_number_integer())
        return Refuse(Err::Malformed, id, "v is not an integer");
    bool const one = v->is_number_unsigned() ? v->get<std::uint64_t>() == 1 : v->get<std::int64_t>() == 1;
    if (!one)
        return Refuse(Err::UnsupportedVersion, id, "protocol v" + v->dump() + " is not spoken here");
    if (!id)
        return Refuse(Err::Malformed, std::nullopt, "missing or invalid id");
    auto const type = obj.find("type");
    Fields const* fields = type != obj.end() && type->is_string() ? FindFields(type->get_ref<std::string const&>())
                                                                  : nullptr;
    if (!fields)
        return Refuse(Err::Malformed, id, "unknown type");

    for (auto const& [key, value] : obj.items())   // keys iterate sorted, as Python's sorted() does
    {
        (void)value;
        if (key != "v" && key != "type" && key != "id" && !Contains(fields->required, key)
            && !Contains(fields->optional, key))
            return Refuse(Err::Malformed, id, "unknown field: " + key);
    }
    for (std::string_view name : fields->required)
        if (!obj.contains(name))
            return Refuse(Err::Malformed, id, "missing " + std::string(name));
    if (std::string problem = CheckFields(fields->type, obj, bounds); !problem.empty())
        return Refuse(Err::Malformed, id, std::move(problem));

    Decoded d;
    d.id = id;
    d.request = Request{std::string(fields->type), *id, obj, obj.dump()};
    return d;
}
}

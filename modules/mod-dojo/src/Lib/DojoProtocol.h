// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_PROTOCOL_H
#define MOD_DOJO_DOJO_PROTOCOL_H

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

// The dojo wire contract, version 1: loopback NDJSON between one controller and one training worker. Requests are
// hello, reset, step, status and close; every line is one JSON object of at most Bounds::maxLineBytes bytes carrying
// "v": 1, a "type" and a request "id". Unknown fields are refused, and so are a byte order mark, NaN and an integer
// that no double can hold. Error messages are at most kMaxTextBytes. dojo/leeroy_dojo/protocol.py implements the same
// rules and tests/fixtures/dojo_protocol.jsonl holds the cases both must agree on. No AzerothCore includes.
namespace Dojo
{
using Json = nlohmann::json;

inline constexpr int kProtocolVersion = 1;
inline constexpr std::size_t kMaxLineBytes = 256 * 1024;
inline constexpr std::uint32_t kMaxActors = 2;
inline constexpr std::uint32_t kMaxEvents = 64;
inline constexpr std::size_t kMaxCopies = 16;                 // answers owed to the request in flight (inbox capacity)
inline constexpr std::size_t kResultCache = 8;
inline constexpr std::size_t kRecentEpisodes = 64;
inline constexpr std::int64_t kMaxDecision = 1000000;
inline constexpr std::int64_t kMaxSeed = 9007199254740991;   // 2^53 - 1, exact in every JSON reader
inline constexpr std::int64_t kMaxIndex = 2147483647;
inline constexpr std::int64_t kMaxProtocol = 1000;
inline constexpr std::size_t kMaxTextBytes = 256;
inline constexpr std::size_t kMaxObservationValues = 4096;
inline constexpr int kMaxDepth = 64;                          // nested arrays and objects in a request

namespace Err
{
inline constexpr std::string_view Malformed = "malformed";
inline constexpr std::string_view LineTooLong = "line_too_long";                  // the connection closes after it
inline constexpr std::string_view UnsupportedVersion = "unsupported_version";
inline constexpr std::string_view NotReady = "not_ready";
inline constexpr std::string_view BadSession = "bad_session";
inline constexpr std::string_view Busy = "busy";                                  // or maxCopies of this one
inline constexpr std::string_view StaleEpisode = "stale_episode";
inline constexpr std::string_view WrongStep = "wrong_step";
inline constexpr std::string_view ConflictingDuplicate = "conflicting_duplicate";
inline constexpr std::string_view InvalidAction = "invalid_action";
inline constexpr std::string_view Aborted = "aborted";                            // "reason" says why
inline constexpr std::string_view ResultOverflow = "result_overflow";             // the episode is aborted
}

namespace AbortReason
{
inline constexpr std::string_view Disconnect = "disconnect";
inline constexpr std::string_view Closed = "closed";
inline constexpr std::string_view Fault = "fault";
inline constexpr std::string_view ResetFailed = "reset_failed";
inline constexpr std::string_view InvalidResult = "invalid_result";
inline constexpr std::string_view ResultOverflow = "result_overflow";
}

struct Bounds
{
    std::size_t maxLineBytes = kMaxLineBytes;
    std::uint32_t maxActors = kMaxActors;
    std::uint32_t maxEvents = kMaxEvents;
    std::size_t resultCache = kResultCache;
    std::size_t maxCopies = kMaxCopies;

    Json ToWire() const;
    static Bounds FromWire(Json const& wire);   // missing keys keep their defaults
    bool operator==(Bounds const&) const = default;
};

struct Request
{
    std::string type;
    std::string id;
    Json body;               // the whole validated object
    std::string fingerprint; // canonical form: compact, keys sorted
};

// request is set when the line is valid; otherwise code names the refusal and id is the line's id if it had a
// valid one.
struct Decoded
{
    std::optional<Request> request;
    std::string code;
    std::optional<std::string> id;
    std::string message;
};

Decoded DecodeRequest(std::string_view line, Bounds const& bounds = {});

// A framed line (without its newline) that carries no request: only spaces, tabs and carriage returns. Transports
// skip it without an answer; anything else, a lone vertical tab or form feed included, is decoded.
bool IsBlankLine(std::string_view line);

// text as valid UTF-8 (an invalid byte becomes '?') of at most limit bytes, cut on a character boundary and marked
// with "..." when cut. MakeError passes every message through it.
std::string Clip(std::string_view text, std::size_t limit = kMaxTextBytes);

// One compact line without '\n'. Throws Json::type_error on invalid UTF-8; never emits NaN (see HasNonFinite).
std::string Encode(Json const& j);
bool HasNonFinite(Json const& j);

Json MakeError(std::string_view code, std::optional<std::string> const& id, std::string_view message,
    std::optional<std::string_view> reason = std::nullopt);

bool IsId(Json const& j);
bool IsIdText(std::string_view s);
bool IsInt(Json const& j, std::int64_t low, std::int64_t high);
bool IsNumber(Json const& j);   // finite, not a bool
bool IsText(Json const& j);     // a string of at most kMaxTextBytes UTF-8 bytes
}

#endif

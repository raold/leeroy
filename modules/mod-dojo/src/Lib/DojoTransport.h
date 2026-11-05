// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_TRANSPORT_H
#define MOD_DOJO_DOJO_TRANSPORT_H

#include "DojoProtocol.h"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace Dojo
{
// What the io thread hands the worker thread: pure data, never a game object.
struct TransportEvent
{
    enum class Kind : std::uint8_t { Connected, Line, Disconnected };

    Kind kind = Kind::Line;
    std::uint64_t connection = 0;   // the connection's serial
    Decoded decoded;                // Line only, decoded on the io thread
};

struct TransportStats
{
    std::uint64_t linesIn = 0;
    std::uint64_t linesOut = 0;
    std::uint64_t refused = 0;
    std::uint64_t droppedSends = 0;
    std::uint64_t readPauses = 0;
    std::size_t maxInboxLines = 0;
    std::size_t maxOutbox = 0;
};

// The dedicated loopback NDJSON endpoint of one training worker. One controller at a time: another connection gets
// a busy error line and is closed. All socket work runs on the transport's own io thread, which only frames and
// decodes lines; it never waits on the worker. Nothing is dropped to relieve pressure: while the worker has not
// taken inboxCapacity lines, or outboxLimit responses wait unwritten, reading stops and TCP pushes back on the
// controller. A line over the bound is answered with line_too_long and the connection is closed. Responses carry the
// serial of the connection they answer and are dropped once it has gone, because its session has ended.
// dojo/leeroy_dojo/transport.py follows the same rules for the fake worker.
//
// Start, Stop, Poll, Send and Close belong to the worker thread; Running, BoundPort, Connection and Stats to any.
class Transport
{
public:
    enum class LogLevel { Info, Warn, Error };
    using LogFn = std::function<void(LogLevel, std::string const&)>;

    struct Options
    {
        Bounds bounds;
        std::size_t inboxCapacity = 16;
        std::size_t outboxLimit = 64;
        std::size_t controlBacklog = 8;      // unpolled connect/disconnect events before new connections are refused
        std::chrono::milliseconds linger{1000};
    };

    explicit Transport(LogFn log, Options options);
    explicit Transport(LogFn log);
    ~Transport();   // Stop()

    Transport(Transport const&) = delete;
    Transport& operator=(Transport const&) = delete;

    // false unless host is loopback ("localhost" means 127.0.0.1) and the bind succeeds; port 0 is ephemeral
    bool Start(std::string const& host, std::uint16_t port);
    void Stop();
    bool Running() const;
    std::uint16_t BoundPort() const;
    std::uint64_t Connection() const;   // the active connection's serial, 0 for none

    // Appends up to max events in arrival order; never blocks.
    std::size_t Poll(std::vector<TransportEvent>& out, std::size_t max);
    // Queues one response line (without '\n'); false, and nothing queued, unless connection is the active one.
    bool Send(std::uint64_t connection, std::string line);
    // Closes the connection once the lines already queued for it are written.
    void Close(std::uint64_t connection);
    TransportStats Stats() const;

private:
    struct Impl;
    std::unique_ptr<Impl> _impl;
};
}

#endif

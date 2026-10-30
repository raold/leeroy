// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_BRIDGE_H
#define MOD_LEEROY_LEEROY_BRIDGE_H

#include "LeeroyProtocol.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace Leeroy
{
// The loopback NDJSON endpoint for one mind. All socket work runs on the bridge's own io thread; no call
// waits on the network. Any thread: PublishEvent, Running, BoundPort, Connected, ConnectionSerial.
// World thread: Start, Stop, Greet, Reply, PopCommands. Delivery is at most once.
class Bridge
{
public:
    enum class LogLevel { Info, Warn, Error };
    using LogFn = std::function<void(LogLevel, std::string const&)>;

    explicit Bridge(LogFn log, std::size_t eventCapacity = kEventQueueCapacity,
        std::size_t inboxCapacity = kInboxCapacity);
    ~Bridge();   // Stop()

    Bridge(Bridge const&) = delete;
    Bridge& operator=(Bridge const&) = delete;

    // false if the host is not loopback ("localhost" means 127.0.0.1), the bind fails or it already runs;
    // port 0 binds an ephemeral port (see BoundPort)
    bool Start(std::string const& host, std::uint16_t port);
    void Stop();                                // idempotent; closes every connection and joins the io thread
    bool Running() const;
    std::uint16_t BoundPort() const;            // 0 when not running
    bool Connected() const;
    std::uint64_t ConnectionSerial() const;     // increments on each accepted mind; 0 = none yet

    // Held until the current mind is greeted, kept across reconnects, dropped by the outbox policy when full.
    void PublishEvent(std::string line, bool droppable);
    // Queues hello then cast for mind `serial` and releases its events. A no-op unless that mind is still the
    // connected one and has not been greeted yet.
    void Greet(std::uint64_t serial, std::string hello, std::string cast);
    // The answer to a command from the last PopCommands; dropped unless that command's mind is still connected.
    void Reply(std::string line);
    // Appends up to max commands of the greeted mind to out (none before Greet); returns how many.
    std::size_t PopCommands(std::vector<Command>& out, std::size_t max);

private:
    struct Impl;
    std::unique_ptr<Impl> _impl;
};
}

#endif

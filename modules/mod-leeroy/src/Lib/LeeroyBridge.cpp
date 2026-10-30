// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyBridge.h"
#include "LeeroyOutbox.h"
#include "LeeroyText.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <deque>
#include <exception>
#include <mutex>
#include <optional>
#include <string_view>
#include <system_error>
#include <thread>
#include <utility>
#include <boost/asio/io_context.hpp>
#include <boost/asio/ip/address.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/read_until.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/streambuf.hpp>
#include <boost/asio/write.hpp>

namespace Leeroy
{
namespace
{
using boost::asio::ip::tcp;
using ErrorCode = boost::system::error_code;

constexpr std::chrono::milliseconds kWriteInterval{20};
constexpr std::size_t kWriteBatchLines = 256;
// Reading from the mind pauses while this many replies wait unwritten (a mind that sends but never reads).
constexpr std::size_t kControlBacklogLimit = 1024;
constexpr std::chrono::seconds kFinalWriteTimeout{2};
constexpr std::chrono::seconds kLingerTimeout{1};
constexpr std::chrono::seconds kAcceptRetryDelay{1};
constexpr std::chrono::seconds kDropLogInterval{60};

std::string Describe(tcp::endpoint const& endpoint)
{
    return endpoint.address().to_string() + ":" + std::to_string(endpoint.port());
}
}

struct Bridge::Impl
{
    // Open: reading and writing. Finishing: one last line (an error) is written, then Lingering: the send side is
    // shut down and input is drained until EOF or a timeout, so the peer reads that line instead of a reset.
    enum class Phase : std::uint8_t { Open, Finishing, Lingering, Closed };

    // Touched only on the io thread (or by Start/Stop while no io thread runs).
    struct Connection
    {
        explicit Connection(tcp::socket peer) : socket(std::move(peer)), timer(socket.get_executor())
        {
        }

        tcp::socket socket;
        boost::asio::steady_timer timer;
        boost::asio::streambuf readBuffer{kMaxLineBytes + 1};
        std::array<char, 4096> drain{};
        std::string writeBuffer;
        std::string finalLine;
        Phase phase = Phase::Open;
        bool writing = false;
        bool readPaused = false;
        bool firstLineSeen = false;       // the first non-empty line must be a JSON object
    };

    // Created by Start, destroyed by Stop after the io thread is joined. The io_context comes first so that it is
    // destroyed last: its destructor closes every socket and drops the uninvoked handlers that own connections.
    struct Net
    {
        boost::asio::io_context io;
        tcp::acceptor acceptor{io};
        boost::asio::steady_timer tick{io};
        boost::asio::steady_timer acceptRetry{io};
        std::shared_ptr<Connection> mind;
        std::vector<std::string> batch;
    };

    Impl(LogFn log, std::size_t eventCapacity, std::size_t inboxCapacity)
        : _log(std::move(log)), _outbox(eventCapacity), _inboxCapacity(std::max<std::size_t>(inboxCapacity, 1))
    {
    }

    void Log(LogLevel level, std::string const& message) const noexcept
    {
        if (!_log)
            return;
        try
        {
            _log(level, message);
        }
        catch (...)
        {
        }
    }

    // --- state shared with the world thread; callers hold _stateMutex ---

    void PushControlLocked(std::string line)
    {
        _outbox.PushControl(std::move(line));
        ++_controlPending;
    }

    void ResetConnectionLocked()
    {
        _outbox.ClearControl();
        _controlPending = 0;
        _inbox.clear();
        _greeted = false;
    }

    std::size_t ControlBacklog()
    {
        std::lock_guard<std::mutex> lock(_stateMutex);
        return _controlPending;
    }

    // Any thread. Logs at most once per kDropLogInterval; the io tick calls it with 0 to report a tail.
    void NoteDrops(std::uint64_t dropped)
    {
        std::uint64_t report = 0;
        {
            std::lock_guard<std::mutex> lock(_dropMutex);
            _unreportedDrops += dropped;
            auto const now = std::chrono::steady_clock::now();
            if (_unreportedDrops == 0 || (_lastDropLog && now - *_lastDropLog < kDropLogInterval))
                return;
            _lastDropLog = now;
            report = std::exchange(_unreportedDrops, 0);
        }
        Log(LogLevel::Warn, "event queue full: dropped " + std::to_string(report) + " events (oldest zone/chat first)");
    }

    // --- io thread ---

    void RunIo(boost::asio::io_context& io)
    {
        for (;;)
        {
            try
            {
                io.run();
                return;
            }
            catch (std::exception const& e)
            {
                Log(LogLevel::Error, std::string("bridge io thread: ") + e.what());
            }
            catch (...)
            {
                Log(LogLevel::Error, "bridge io thread: unknown exception");
            }
        }
    }

    void StartAccept()
    {
        _net->acceptor.async_accept([this](ErrorCode const& ec, tcp::socket peer) { OnAccept(ec, std::move(peer)); });
    }

    void OnAccept(ErrorCode const& ec, tcp::socket peer)
    {
        if (ec == boost::asio::error::operation_aborted)
            return;
        if (ec)
        {
            Log(LogLevel::Warn, "bridge accept failed: " + ec.message());
            _net->acceptRetry.expires_after(kAcceptRetryDelay);
            _net->acceptRetry.async_wait([this](ErrorCode const& waitError)
            {
                if (!waitError)
                    StartAccept();
            });
            return;
        }
        StartAccept();

        auto conn = std::make_shared<Connection>(std::move(peer));
        ErrorCode ignored;
        std::string const from = Describe(conn->socket.remote_endpoint(ignored));
        if (_net->mind)
        {
            Log(LogLevel::Warn, "refused a second mind from " + from);
            Finish(conn, EncodeError(NowUnixMs(), Err::AnotherMind));
            return;
        }

        conn->socket.set_option(tcp::no_delay(true), ignored);
        _net->mind = conn;
        {
            std::lock_guard<std::mutex> lock(_stateMutex);
            ResetConnectionLocked();
            _serial.store(_serial.load() + 1);
            _connected.store(true);
        }
        Log(LogLevel::Info, "mind connected from " + from);
        StartRead(conn);
    }

    void StartRead(std::shared_ptr<Connection> const& conn)
    {
        boost::asio::async_read_until(conn->socket, conn->readBuffer, '\n',
            [this, conn](ErrorCode const& ec, std::size_t length) { OnRead(conn, ec, length); });
    }

    void OnRead(std::shared_ptr<Connection> const& conn, ErrorCode const& ec, std::size_t length)
    {
        if (conn->phase != Phase::Open)
            return;
        if (ec == boost::asio::error::not_found)
        {
            Log(LogLevel::Warn, "mind sent a line over " + std::to_string(kMaxLineBytes) + " bytes, disconnecting");
            Finish(conn, EncodeError(NowUnixMs(), Err::LineTooLong));
            return;
        }
        if (ec)
        {
            HardClose(conn);
            return;
        }

        auto const data = conn->readBuffer.data();
        std::string_view line(static_cast<char const*>(data.data()), length > 0 ? length - 1 : 0);
        if (!line.empty() && line.back() == '\r')
            line.remove_suffix(1);
        bool refused = false;
        if (!line.empty())
            refused = !HandleLine(*conn, line);
        conn->readBuffer.consume(length);
        if (refused)
        {
            Log(LogLevel::Warn, "first line from the mind is not JSON, disconnecting");
            Finish(conn, EncodeError(NowUnixMs(), Err::BadJson));
            return;
        }

        if (ControlBacklog() >= kControlBacklogLimit)
            conn->readPaused = true;
        else
            StartRead(conn);
    }

    // False when the connection must close: its first line is not a JSON object, so it is not a bridge client (a
    // browser posting to the port, say), and nothing it sent may reach the command queue.
    bool HandleLine(Connection& conn, std::string_view line)
    {
        DecodeResult result = DecodeCommand(line);
        bool const first = !std::exchange(conn.firstLineSeen, true);
        if (first && !result.command && !result.ackable && result.error == Err::BadJson)
            return false;
        std::string reply;
        if (result.command)
        {
            {
                std::lock_guard<std::mutex> lock(_stateMutex);
                if (_inbox.size() < _inboxCapacity)
                {
                    _inbox.push_back(std::move(*result.command));
                    return true;
                }
            }
            reply = EncodeAck(NowUnixMs(), result.id, false, Err::Busy);
        }
        else if (result.ackable)
            reply = EncodeAck(NowUnixMs(), result.id, false, result.error);
        else
            reply = EncodeError(NowUnixMs(), result.error, result.id);

        std::lock_guard<std::mutex> lock(_stateMutex);
        PushControlLocked(std::move(reply));
        return true;
    }

    void ArmTick()
    {
        _net->tick.expires_after(kWriteInterval);
        _net->tick.async_wait([this](ErrorCode const& ec)
        {
            if (!ec)
                OnTick();
        });
    }

    void OnTick()
    {
        ArmTick();
        NoteDrops(0);
        std::shared_ptr<Connection> const mind = _net->mind;
        if (mind && mind->phase == Phase::Open && !mind->writing)
            WriteBatch(mind);
    }

    void WriteBatch(std::shared_ptr<Connection> const& conn)
    {
        std::vector<std::string>& lines = _net->batch;
        lines.clear();
        {
            // Control lines first; events only once this mind is greeted. Every push holds _stateMutex, so the
            // control count is exact and, when it is below the batch size, the second pop takes only events.
            std::lock_guard<std::mutex> lock(_stateMutex);
            std::size_t const control = _outbox.PopBatch(lines, kWriteBatchLines, false);
            _controlPending -= std::min(control, _controlPending);
            if (_greeted && control < kWriteBatchLines)
                _outbox.PopBatch(lines, kWriteBatchLines - control, true);
        }
        if (lines.empty())
            return;

        std::size_t total = 0;
        for (std::string const& line : lines)
            total += line.size() + 1;
        conn->writeBuffer.clear();
        conn->writeBuffer.reserve(total);
        for (std::string const& line : lines)
        {
            conn->writeBuffer += line;
            conn->writeBuffer += '\n';
        }
        lines.clear();

        conn->writing = true;
        boost::asio::async_write(conn->socket, boost::asio::buffer(conn->writeBuffer),
            [this, conn](ErrorCode const& ec, std::size_t) { OnBatchWritten(conn, ec); });
    }

    void OnBatchWritten(std::shared_ptr<Connection> const& conn, ErrorCode const& ec)
    {
        conn->writing = false;
        if (conn->phase == Phase::Finishing)
        {
            if (ec)
                HardClose(conn);
            else
                WriteFinal(conn);
            return;
        }
        if (conn->phase != Phase::Open)
            return;
        if (ec)
        {
            HardClose(conn);
            return;
        }
        if (conn->readPaused && ControlBacklog() < kControlBacklogLimit)
        {
            conn->readPaused = false;
            StartRead(conn);
        }
    }

    // Stops reading, writes line after any write in flight, then lingers. Used for an overlong line from the mind
    // and to refuse a second mind (which never becomes the mind).
    void Finish(std::shared_ptr<Connection> const& conn, std::string line)
    {
        conn->phase = Phase::Finishing;
        conn->finalLine = std::move(line);
        ArmConnectionTimer(conn, kFinalWriteTimeout);
        if (!conn->writing)
            WriteFinal(conn);
    }

    void WriteFinal(std::shared_ptr<Connection> const& conn)
    {
        conn->writeBuffer = std::move(conn->finalLine);
        conn->writeBuffer += '\n';
        conn->writing = true;
        boost::asio::async_write(conn->socket, boost::asio::buffer(conn->writeBuffer),
            [this, conn](ErrorCode const& ec, std::size_t)
            {
                conn->writing = false;
                if (conn->phase != Phase::Finishing)
                    return;
                if (ec)
                    HardClose(conn);
                else
                    Linger(conn);
            });
    }

    void Linger(std::shared_ptr<Connection> const& conn)
    {
        Detach(conn);
        conn->phase = Phase::Lingering;
        ErrorCode ignored;
        conn->socket.shutdown(tcp::socket::shutdown_send, ignored);
        ArmConnectionTimer(conn, kLingerTimeout);
        Drain(conn);
    }

    void Drain(std::shared_ptr<Connection> const& conn)
    {
        conn->socket.async_read_some(boost::asio::buffer(conn->drain),
            [this, conn](ErrorCode const& ec, std::size_t)
            {
                if (conn->phase != Phase::Lingering)
                    return;
                if (ec)
                    HardClose(conn);
                else
                    Drain(conn);
            });
    }

    // Closes the connection unless it has moved on to another phase by the time the timer fires.
    void ArmConnectionTimer(std::shared_ptr<Connection> const& conn, std::chrono::steady_clock::duration after)
    {
        Phase const armed = conn->phase;
        conn->timer.expires_after(after);
        conn->timer.async_wait([this, conn, armed](ErrorCode const& ec)
        {
            if (!ec && conn->phase == armed)
                HardClose(conn);
        });
    }

    void HardClose(std::shared_ptr<Connection> const& conn)
    {
        if (conn->phase == Phase::Closed)
            return;
        Detach(conn);
        conn->phase = Phase::Closed;
        ErrorCode ignored;
        conn->socket.close(ignored);
        conn->timer.cancel();
    }

    // conn must not be a reference to _net->mind itself.
    void Detach(std::shared_ptr<Connection> const& conn)
    {
        if (_net->mind != conn)
            return;
        _net->mind.reset();
        {
            std::lock_guard<std::mutex> lock(_stateMutex);
            _connected.store(false);
            ResetConnectionLocked();
        }
        Log(LogLevel::Info, "mind disconnected");
    }

    LogFn const _log;
    Outbox _outbox;
    std::size_t const _inboxCapacity;

    // Connection state shared by the io thread and the world thread. Lock order: _stateMutex, then the outbox's.
    std::mutex _stateMutex;
    std::deque<Command> _inbox;
    bool _greeted = false;
    std::uint64_t _replySerial = 0;               // the mind whose commands the last PopCommands returned
    std::size_t _controlPending = 0;              // control lines queued in the outbox
    std::atomic<bool> _connected{false};          // written under _stateMutex
    std::atomic<std::uint64_t> _serial{0};        // written under _stateMutex

    std::mutex _dropMutex;
    std::uint64_t _unreportedDrops = 0;
    std::optional<std::chrono::steady_clock::time_point> _lastDropLog;

    std::mutex _lifecycleMutex;                   // Start and Stop
    std::unique_ptr<Net> _net;
    std::thread _thread;
    std::atomic<bool> _running{false};
    std::atomic<std::uint16_t> _port{0};
};

Bridge::Bridge(LogFn log, std::size_t eventCapacity, std::size_t inboxCapacity)
    : _impl(std::make_unique<Impl>(std::move(log), eventCapacity, inboxCapacity))
{
}

Bridge::~Bridge()
{
    Stop();
}

bool Bridge::Start(std::string const& host, std::uint16_t port)
{
    Impl& d = *_impl;
    std::lock_guard<std::mutex> lifecycle(d._lifecycleMutex);
    if (d._net)
    {
        d.Log(LogLevel::Warn, "bridge already running on port " + std::to_string(d._port.load()));
        return false;
    }

    std::string const address = host == "localhost" ? std::string("127.0.0.1") : host;
    if (!IsLoopbackHost(address))
    {
        d.Log(LogLevel::Error, "bridge host " + host + " is not a loopback address, not listening");
        return false;
    }

    ErrorCode ec;
    boost::asio::ip::address const ip = boost::asio::ip::make_address(address, ec);
    auto net = std::make_unique<Impl::Net>();
    tcp::endpoint local;
    if (!ec)
    {
        tcp::endpoint const endpoint(ip, port);
        net->acceptor.open(endpoint.protocol(), ec);
#ifndef _WIN32
        // On Windows SO_REUSEADDR would let a second process bind the same port; the core skips it there too.
        if (!ec)
            net->acceptor.set_option(tcp::acceptor::reuse_address(true), ec);
#endif
        if (!ec)
            net->acceptor.bind(endpoint, ec);
        if (!ec)
            net->acceptor.listen(boost::asio::socket_base::max_listen_connections, ec);
        if (!ec)
            local = net->acceptor.local_endpoint(ec);
    }
    if (ec)
    {
        d.Log(LogLevel::Error, "bridge cannot listen on " + host + ":" + std::to_string(port) + ": " + ec.message());
        return false;
    }

    d._net = std::move(net);
    d._port.store(local.port());
    d._running.store(true);
    d.StartAccept();
    d.ArmTick();
    try
    {
        boost::asio::io_context& io = d._net->io;
        d._thread = std::thread([&d, &io] { d.RunIo(io); });
    }
    catch (std::system_error const& e)
    {
        d.Log(LogLevel::Error, std::string("bridge cannot start its thread: ") + e.what());
        d._net.reset();
        d._port.store(0);
        d._running.store(false);
        return false;
    }
    d.Log(LogLevel::Info, "bridge listening on " + Describe(local));
    return true;
}

void Bridge::Stop()
{
    Impl& d = *_impl;
    std::lock_guard<std::mutex> lifecycle(d._lifecycleMutex);
    if (!d._net)
        return;

    d._net->io.stop();
    if (d._thread.joinable())
        d._thread.join();
    d._net.reset();
    d._port.store(0);
    d._running.store(false);

    bool wasConnected = false;
    {
        std::lock_guard<std::mutex> lock(d._stateMutex);
        wasConnected = d._connected.exchange(false);
        d.ResetConnectionLocked();
    }
    if (wasConnected)
        d.Log(LogLevel::Info, "mind disconnected");
    d.Log(LogLevel::Info, "bridge stopped");
}

bool Bridge::Running() const
{
    return _impl->_running.load();
}

std::uint16_t Bridge::BoundPort() const
{
    return _impl->_port.load();
}

bool Bridge::Connected() const
{
    return _impl->_connected.load();
}

std::uint64_t Bridge::ConnectionSerial() const
{
    return _impl->_serial.load();
}

void Bridge::PublishEvent(std::string line, bool droppable)
{
    std::size_t const dropped = _impl->_outbox.PushEvent(std::move(line), droppable);
    if (dropped > 0)
        _impl->NoteDrops(dropped);
}

void Bridge::Greet(std::uint64_t serial, std::string hello, std::string cast)
{
    Impl& d = *_impl;
    std::lock_guard<std::mutex> lock(d._stateMutex);
    if (!d._connected.load() || d._greeted || serial != d._serial.load())
        return;
    d.PushControlLocked(std::move(hello));
    d.PushControlLocked(std::move(cast));
    d._greeted = true;
}

void Bridge::Reply(std::string line)
{
    Impl& d = *_impl;
    std::lock_guard<std::mutex> lock(d._stateMutex);
    if (!d._connected.load() || !d._greeted || d._replySerial != d._serial.load())
        return;
    d.PushControlLocked(std::move(line));
}

std::size_t Bridge::PopCommands(std::vector<Command>& out, std::size_t max)
{
    Impl& d = *_impl;
    std::lock_guard<std::mutex> lock(d._stateMutex);
    if (!d._connected.load() || !d._greeted)
        return 0;
    std::size_t popped = 0;
    while (popped < max && !d._inbox.empty())
    {
        out.push_back(std::move(d._inbox.front()));
        d._inbox.pop_front();
        ++popped;
    }
    if (popped > 0)
        d._replySerial = d._serial.load();
    return popped;
}
}

// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoTransport.h"

#include <algorithm>
#include <array>
#include <atomic>
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
#include <boost/asio/post.hpp>
#include <boost/asio/read_until.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/streambuf.hpp>
#include <boost/asio/write.hpp>

namespace Dojo
{
namespace
{
using boost::asio::ip::tcp;
using ErrorCode = boost::system::error_code;

constexpr std::size_t kWriteBatchLines = 64;
constexpr std::chrono::seconds kAcceptRetryDelay{1};

std::string ErrorLine(std::string_view code, std::string_view message)
{
    return Encode(MakeError(code, std::nullopt, message));
}
}

struct Transport::Impl
{
    // Open: reading and writing. Closing: a final line is queued and no more is read. Lingering: the send side is
    // shut down and input is drained until EOF or a timeout, so the peer reads the last line instead of a reset.
    enum class Phase : std::uint8_t { Open, Closing, Lingering, Closed };

    // Touched only on the io thread.
    struct Connection
    {
        Connection(tcp::socket peer, std::size_t maxLine, std::uint64_t id)
            : socket(std::move(peer)), timer(socket.get_executor()), readBuffer(maxLine + 1), serial(id)
        {
        }

        tcp::socket socket;
        boost::asio::steady_timer timer;
        boost::asio::streambuf readBuffer;
        std::array<char, 4096> drain{};
        std::string writeBuffer;
        std::uint64_t serial;                     // 0 for a refused connection, which never becomes active
        std::size_t writingLines = 0;
        Phase phase = Phase::Open;
        bool writing = false;
        bool reading = false;
        bool reported = false;
        std::optional<TransportEvent> parked;     // a decoded line waiting for room in the inbox
    };

    // Created by Start, destroyed by Stop after the io thread is joined; the io_context comes first so it is
    // destroyed last.
    struct Net
    {
        boost::asio::io_context io;
        tcp::acceptor acceptor{io};
        boost::asio::steady_timer acceptRetry{io};
        std::shared_ptr<Connection> active;
    };

    Impl(LogFn log, Options options) : _log(std::move(log)), _options(options)
    {
        _options.inboxCapacity = std::max<std::size_t>(_options.inboxCapacity, 1);
        _options.outboxLimit = std::max<std::size_t>(_options.outboxLimit, 1);
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
                Log(LogLevel::Error, std::string("dojo transport io thread: ") + e.what());
            }
            catch (...)
            {
                Log(LogLevel::Error, "dojo transport io thread: unknown exception");
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
            Log(LogLevel::Warn, "dojo accept failed: " + ec.message());
            _net->acceptRetry.expires_after(kAcceptRetryDelay);
            _net->acceptRetry.async_wait([this](ErrorCode const& waitError)
            {
                if (!waitError)
                    StartAccept();
            });
            return;
        }
        StartAccept();

        ErrorCode ignored;
        peer.set_option(tcp::no_delay(true), ignored);
        std::uint64_t serial = 0;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            if (!_net->active && _controlsQueued < _options.controlBacklog)
            {
                serial = ++_lastSerial;
                _activeSerial = serial;
                _closeRequested = false;
                _outbox.clear();
                PushControlLocked(TransportEvent{TransportEvent::Kind::Connected, serial, {}});
            }
            else
                ++_stats.refused;
        }
        if (serial == 0)
        {
            Log(LogLevel::Warn, "refused a second controller");
            auto conn = std::make_shared<Connection>(std::move(peer), 0, 0);
            conn->writeBuffer = ErrorLine(Err::Busy, "another controller is connected") + "\n";
            conn->writing = true;
            ArmTimer(conn, _options.linger);
            boost::asio::async_write(conn->socket, boost::asio::buffer(conn->writeBuffer),
                [this, conn](ErrorCode const& writeError, std::size_t)
                {
                    conn->writing = false;
                    if (writeError)
                        HardClose(conn);
                    else
                        Linger(conn);
                });
            return;
        }
        auto conn = std::make_shared<Connection>(std::move(peer), _options.bounds.maxLineBytes, serial);
        _net->active = conn;
        Log(LogLevel::Info, "controller connected (connection " + std::to_string(serial) + ")");
        StartRead(conn);
    }

    void StartRead(std::shared_ptr<Connection> const& conn)
    {
        conn->reading = true;
        boost::asio::async_read_until(conn->socket, conn->readBuffer, '\n',
            [this, conn](ErrorCode const& ec, std::size_t length) { OnRead(conn, ec, length); });
    }

    void OnRead(std::shared_ptr<Connection> const& conn, ErrorCode const& ec, std::size_t length)
    {
        conn->reading = false;
        if (conn->phase == Phase::Lingering)
        {
            if (ec && ec != boost::asio::error::not_found)
                HardClose(conn);
            else
            {
                conn->readBuffer.consume(conn->readBuffer.size());
                Drain(conn);
            }
            return;
        }
        if (conn->phase != Phase::Open)
            return;
        if (ec == boost::asio::error::not_found)
        {
            Log(LogLevel::Warn, "controller sent a line over " + std::to_string(_options.bounds.maxLineBytes)
                + " bytes, closing");
            TooLong(conn);
            return;
        }
        if (ec)
        {
            HardClose(conn);
            return;
        }

        auto const data = conn->readBuffer.data();
        std::string_view view(static_cast<char const*>(data.data()), length > 0 ? length - 1 : 0);
        if (!view.empty() && view.back() == '\r')
            view.remove_suffix(1);
        std::string const line(view);
        conn->readBuffer.consume(length);
        bool closing = false;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            closing = _closeRequested;   // the session has ended; what follows is drained, not delivered
        }
        if (!closing && !IsBlankLine(line)
            && !Enqueue(conn, TransportEvent{TransportEvent::Kind::Line, conn->serial,
                DecodeRequest(line, _options.bounds)}))
            return;   // parked; Resume reads on once the worker makes room
        ContinueReading(conn);
    }

    // False when the inbox is full: the event is parked and reading stops.
    bool Enqueue(std::shared_ptr<Connection> const& conn, TransportEvent event)
    {
        std::lock_guard<std::mutex> lock(_mutex);
        if (_linesQueued >= _options.inboxCapacity)
        {
            conn->parked = std::move(event);
            ++_stats.readPauses;
            return false;
        }
        _events.push_back(std::move(event));
        ++_linesQueued;
        ++_stats.linesIn;
        _stats.maxInboxLines = std::max(_stats.maxInboxLines, _linesQueued);
        return true;
    }

    void ContinueReading(std::shared_ptr<Connection> const& conn)
    {
        if (conn->phase != Phase::Open || conn->reading || conn->parked)
            return;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            if (_outbox.size() >= _options.outboxLimit)
            {
                ++_stats.readPauses;
                return;   // OnWritten reads on once the controller has taken some
            }
        }
        StartRead(conn);
    }

    // Posted by Poll after it took lines.
    void Resume()
    {
        std::shared_ptr<Connection> const conn = _net->active;
        if (!conn || conn->phase != Phase::Open)
            return;
        if (conn->parked)
        {
            TransportEvent event = std::move(*conn->parked);
            conn->parked.reset();
            if (!Enqueue(conn, std::move(event)))
                return;
        }
        ContinueReading(conn);
    }

    void TooLong(std::shared_ptr<Connection> const& conn)
    {
        {
            std::lock_guard<std::mutex> lock(_mutex);
            if (_activeSerial == conn->serial)
            {
                _outbox.push_back(ErrorLine(Err::LineTooLong,
                    "line over " + std::to_string(_options.bounds.maxLineBytes) + " bytes"));
                _closeRequested = true;
            }
        }
        conn->phase = Phase::Closing;
        if (!conn->writing)
            WriteBatch(conn);
    }

    // Posted by Send and Close.
    void Kick()
    {
        std::shared_ptr<Connection> const conn = _net->active;
        if (conn && (conn->phase == Phase::Open || conn->phase == Phase::Closing) && !conn->writing)
            WriteBatch(conn);
    }

    void WriteBatch(std::shared_ptr<Connection> const& conn)
    {
        std::vector<std::string> lines;
        bool closing = false;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            if (_activeSerial == conn->serial)
            {
                while (!_outbox.empty() && lines.size() < kWriteBatchLines)
                {
                    lines.push_back(std::move(_outbox.front()));
                    _outbox.pop_front();
                }
                closing = _closeRequested;
            }
        }
        if (lines.empty())
        {
            if (closing || conn->phase == Phase::Closing)
                Linger(conn);
            return;
        }
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
        conn->writingLines = lines.size();
        conn->writing = true;
        boost::asio::async_write(conn->socket, boost::asio::buffer(conn->writeBuffer),
            [this, conn](ErrorCode const& ec, std::size_t) { OnWritten(conn, ec); });
    }

    void OnWritten(std::shared_ptr<Connection> const& conn, ErrorCode const& ec)
    {
        conn->writing = false;
        if (conn->phase == Phase::Lingering || conn->phase == Phase::Closed)
            return;
        if (ec)
        {
            HardClose(conn);
            return;
        }
        {
            std::lock_guard<std::mutex> lock(_mutex);
            _stats.linesOut += conn->writingLines;
        }
        WriteBatch(conn);
        ContinueReading(conn);
    }

    void Linger(std::shared_ptr<Connection> const& conn)
    {
        if (conn->phase == Phase::Lingering || conn->phase == Phase::Closed)
            return;
        conn->phase = Phase::Lingering;
        ErrorCode ignored;
        conn->socket.shutdown(tcp::socket::shutdown_send, ignored);
        ArmTimer(conn, _options.linger);
        if (!conn->reading)
            Drain(conn);
    }

    void Drain(std::shared_ptr<Connection> const& conn)
    {
        conn->reading = true;
        conn->socket.async_read_some(boost::asio::buffer(conn->drain),
            [this, conn](ErrorCode const& ec, std::size_t)
            {
                conn->reading = false;
                if (conn->phase != Phase::Lingering)
                    return;
                if (ec)
                    HardClose(conn);
                else
                    Drain(conn);
            });
    }

    void ArmTimer(std::shared_ptr<Connection> const& conn, std::chrono::steady_clock::duration after)
    {
        conn->timer.expires_after(after);
        conn->timer.async_wait([this, conn](ErrorCode const& ec)
        {
            if (!ec && conn->phase != Phase::Closed)
                HardClose(conn);
        });
    }

    void HardClose(std::shared_ptr<Connection> const& conn)
    {
        if (conn->phase == Phase::Closed)
            return;
        conn->phase = Phase::Closed;
        ErrorCode ignored;
        conn->socket.close(ignored);
        conn->timer.cancel();
        if (_net->active == conn)
            _net->active.reset();
        if (conn->serial == 0 || conn->reported)
            return;
        conn->reported = true;
        {
            std::lock_guard<std::mutex> lock(_mutex);
            if (_activeSerial == conn->serial)
            {
                _activeSerial = 0;
                _closeRequested = false;
                _outbox.clear();
            }
            PushControlLocked(TransportEvent{TransportEvent::Kind::Disconnected, conn->serial, {}});
        }
        Log(LogLevel::Info, "controller disconnected (connection " + std::to_string(conn->serial) + ")");
    }

    void PushControlLocked(TransportEvent event)
    {
        _events.push_back(std::move(event));
        ++_controlsQueued;
    }

    LogFn const _log;
    Options _options;

    // Shared by the io thread and the worker thread.
    mutable std::mutex _mutex;
    std::deque<TransportEvent> _events;
    std::size_t _linesQueued = 0;
    std::size_t _controlsQueued = 0;
    std::uint64_t _lastSerial = 0;
    std::uint64_t _activeSerial = 0;
    bool _closeRequested = false;
    std::deque<std::string> _outbox;          // lines for _activeSerial, not yet taken by the io thread
    TransportStats _stats;

    std::unique_ptr<Net> _net;
    std::thread _thread;
    std::atomic<bool> _running{false};
    std::atomic<std::uint16_t> _port{0};
};

Transport::Transport(LogFn log, Options options) : _impl(std::make_unique<Impl>(std::move(log), options))
{
}

Transport::Transport(LogFn log) : Transport(std::move(log), Options{})
{
}

Transport::~Transport()
{
    Stop();
}

bool Transport::Start(std::string const& host, std::uint16_t port)
{
    Impl& d = *_impl;
    if (d._net)
    {
        d.Log(LogLevel::Warn, "dojo transport already running on port " + std::to_string(d._port.load()));
        return false;
    }
    ErrorCode ec;
    boost::asio::ip::address const ip = boost::asio::ip::make_address(host == "localhost" ? "127.0.0.1" : host, ec);
    if (ec || !ip.is_loopback())
    {
        d.Log(LogLevel::Error, "dojo transport host " + host + " is not a loopback address, not listening");
        return false;
    }

    auto net = std::make_unique<Impl::Net>();
    tcp::endpoint const endpoint(ip, port);
    tcp::endpoint local;
    net->acceptor.open(endpoint.protocol(), ec);
#ifndef _WIN32
    if (!ec)
        net->acceptor.set_option(tcp::acceptor::reuse_address(true), ec);
#endif
    if (!ec)
        net->acceptor.bind(endpoint, ec);
    if (!ec)
        net->acceptor.listen(boost::asio::socket_base::max_listen_connections, ec);
    if (!ec)
        local = net->acceptor.local_endpoint(ec);
    if (ec)
    {
        d.Log(LogLevel::Error, "dojo transport cannot listen on " + host + ":" + std::to_string(port) + ": "
            + ec.message());
        return false;
    }

    d._net = std::move(net);
    d._port.store(local.port());
    d._running.store(true);
    d.StartAccept();
    try
    {
        boost::asio::io_context& io = d._net->io;
        d._thread = std::thread([&d, &io] { d.RunIo(io); });
    }
    catch (std::system_error const& e)
    {
        d.Log(LogLevel::Error, std::string("dojo transport cannot start its thread: ") + e.what());
        d._net.reset();
        d._port.store(0);
        d._running.store(false);
        return false;
    }
    d.Log(LogLevel::Info, "dojo transport listening on " + local.address().to_string() + ":"
        + std::to_string(local.port()));
    return true;
}

void Transport::Stop()
{
    Impl& d = *_impl;
    if (!d._net)
        return;
    d._net->io.stop();
    if (d._thread.joinable())
        d._thread.join();
    d._net.reset();
    d._port.store(0);
    d._running.store(false);
    std::lock_guard<std::mutex> lock(d._mutex);
    d._events.clear();
    d._linesQueued = 0;
    d._controlsQueued = 0;
    d._activeSerial = 0;
    d._closeRequested = false;
    d._outbox.clear();
}

bool Transport::Running() const
{
    return _impl->_running.load();
}

std::uint16_t Transport::BoundPort() const
{
    return _impl->_port.load();
}

std::uint64_t Transport::Connection() const
{
    std::lock_guard<std::mutex> lock(_impl->_mutex);
    return _impl->_activeSerial;
}

std::size_t Transport::Poll(std::vector<TransportEvent>& out, std::size_t max)
{
    Impl& d = *_impl;
    std::size_t taken = 0;
    bool tookLines = false;
    {
        std::lock_guard<std::mutex> lock(d._mutex);
        while (taken < max && !d._events.empty())
        {
            TransportEvent& event = d._events.front();
            if (event.kind == TransportEvent::Kind::Line)
            {
                --d._linesQueued;
                tookLines = true;
            }
            else
                --d._controlsQueued;
            out.push_back(std::move(event));
            d._events.pop_front();
            ++taken;
        }
    }
    if (tookLines && d._net)
        boost::asio::post(d._net->io, [&d] { d.Resume(); });
    return taken;
}

bool Transport::Send(std::uint64_t connection, std::string line)
{
    Impl& d = *_impl;
    {
        std::lock_guard<std::mutex> lock(d._mutex);
        if (!d._net || connection == 0 || connection != d._activeSerial || d._closeRequested)
        {
            ++d._stats.droppedSends;
            return false;
        }
        d._outbox.push_back(std::move(line));
        d._stats.maxOutbox = std::max(d._stats.maxOutbox, d._outbox.size());
    }
    boost::asio::post(d._net->io, [&d] { d.Kick(); });
    return true;
}

void Transport::Close(std::uint64_t connection)
{
    Impl& d = *_impl;
    {
        std::lock_guard<std::mutex> lock(d._mutex);
        if (!d._net || connection == 0 || connection != d._activeSerial || d._closeRequested)
            return;
        d._closeRequested = true;
    }
    boost::asio::post(d._net->io, [&d] { d.Kick(); });
}

TransportStats Transport::Stats() const
{
    std::lock_guard<std::mutex> lock(_impl->_mutex);
    return _impl->_stats;
}
}

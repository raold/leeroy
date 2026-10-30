// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_TEST_CLIENT_H
#define MOD_LEEROY_TEST_CLIENT_H

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <boost/asio/io_context.hpp>
#include <boost/asio/ip/address_v4.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/read_until.hpp>
#include <boost/asio/streambuf.hpp>
#include <boost/asio/write.hpp>

namespace LeeroyTest
{
// A blocking test peer for the bridge: every call starts one asio operation and drives the client's own
// io_context with run_one_for until it completes or the timeout passes (then it is cancelled).
class TestClient
{
public:
    TestClient() = default;
    ~TestClient()
    {
        Close();
    }

    TestClient(TestClient const&) = delete;
    TestClient& operator=(TestClient const&) = delete;

    bool Connect(std::uint16_t port)
    {
        auto op = std::make_shared<Op>();
        boost::asio::ip::tcp::endpoint const endpoint(boost::asio::ip::address_v4::loopback(), port);
        _socket.async_connect(endpoint, [op](boost::system::error_code const& ec)
        {
            op->error = ec;
            op->done = true;
        });
        if (!Await(*op, std::chrono::seconds(2)))
            return false;
        return !op->error;
    }

    void Send(std::string const& line)
    {
        auto op = std::make_shared<Op>();
        op->data = line + "\n";
        boost::asio::async_write(_socket, boost::asio::buffer(op->data),
            [op](boost::system::error_code const& ec, std::size_t)
            {
                op->error = ec;
                op->done = true;
            });
        // A write that cannot finish in time keeps going during later calls; op stays alive in its handler.
        RunUntil([op] { return op->done; }, std::chrono::seconds(5));
    }

    // nullopt on timeout, EOF or error.
    std::optional<std::string> ReadLine(std::chrono::milliseconds timeout)
    {
        auto op = std::make_shared<Op>();
        boost::asio::async_read_until(_socket, _buffer, '\n',
            [op](boost::system::error_code const& ec, std::size_t bytes)
            {
                op->error = ec;
                op->bytes = bytes;
                op->done = true;
            });
        if (!Await(*op, timeout) || op->error || op->bytes == 0)
            return std::nullopt;
        std::string line(static_cast<char const*>(_buffer.data().data()), op->bytes - 1);
        _buffer.consume(op->bytes);
        return line;
    }

    // True once the server has closed the connection (EOF or reset); unread data is discarded.
    bool WaitClosed(std::chrono::milliseconds timeout)
    {
        auto const deadline = std::chrono::steady_clock::now() + timeout;
        for (;;)
        {
            auto op = std::make_shared<Op>();
            _socket.async_read_some(boost::asio::buffer(_scratch),
                [op](boost::system::error_code const& ec, std::size_t)
                {
                    op->error = ec;
                    op->done = true;
                });
            auto const now = std::chrono::steady_clock::now();
            auto const left = deadline > now
                ? std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now)
                : std::chrono::milliseconds(0);
            if (!Await(*op, left))
                return false;
            if (op->error)
                return true;
        }
    }

    void Close()
    {
        boost::system::error_code ignored;
        _socket.close(ignored);
    }

private:
    struct Op
    {
        bool done = false;
        boost::system::error_code error;
        std::size_t bytes = 0;
        std::string data;
    };

    bool RunUntil(std::function<bool()> const& done, std::chrono::steady_clock::duration timeout)
    {
        auto const deadline = std::chrono::steady_clock::now() + timeout;
        while (!done())
        {
            auto const now = std::chrono::steady_clock::now();
            if (now >= deadline)
                return false;
            _io.restart();
            if (_io.run_one_for(deadline - now) == 0 && _io.stopped())
                return done();
        }
        return true;
    }

    // Waits for op; on timeout cancels it and waits for its handler, so no operation outlives the call.
    bool Await(Op const& op, std::chrono::steady_clock::duration timeout)
    {
        if (RunUntil([&op] { return op.done; }, timeout))
            return true;
        boost::system::error_code ignored;
        _socket.cancel(ignored);
        RunUntil([&op] { return op.done; }, std::chrono::seconds(1));
        return false;
    }

    boost::asio::io_context _io;
    boost::asio::ip::tcp::socket _socket{_io};
    boost::asio::streambuf _buffer;
    std::array<char, 4096> _scratch{};
};

inline bool WaitFor(std::function<bool()> pred, std::chrono::milliseconds timeout)
{
    auto const deadline = std::chrono::steady_clock::now() + timeout;
    while (!pred())
    {
        if (std::chrono::steady_clock::now() >= deadline)
            return false;
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    return true;
}

inline std::string NextLine(TestClient& c)
{
    return c.ReadLine(std::chrono::seconds(1)).value_or("");
}
}

#endif

// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_SHA256_H
#define MOD_DOJO_DOJO_SHA256_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

// SHA-256 (FIPS 180-4) without OpenSSL, so the pure library and its tests can hash a scenario manifest exactly as
// leeroy_dojo.scenario does in Python.
namespace Dojo
{
class Sha256
{
public:
    Sha256();
    void Update(void const* data, std::size_t size);
    void Update(std::string_view text) { Update(text.data(), text.size()); }
    std::array<std::uint8_t, 32> Final();

private:
    void Block(std::uint8_t const* block);

    std::array<std::uint32_t, 8> _h;
    std::array<std::uint8_t, 64> _buffer{};
    std::size_t _used = 0;
    std::uint64_t _bits = 0;
};

// Lower-case hex of SHA-256(text).
std::string Sha256Hex(std::string_view text);
}

#endif

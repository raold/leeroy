// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoSha256.h"

#include <algorithm>
#include <cstring>

namespace Dojo
{
namespace
{
constexpr std::array<std::uint32_t, 64> kRound = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

constexpr std::uint32_t Rotr(std::uint32_t x, int n)
{
    return (x >> n) | (x << (32 - n));
}
}

Sha256::Sha256()
    : _h{0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19}
{
}

void Sha256::Block(std::uint8_t const* block)
{
    std::array<std::uint32_t, 64> w{};
    for (int i = 0; i < 16; ++i)
        w[i] = (std::uint32_t(block[4 * i]) << 24) | (std::uint32_t(block[4 * i + 1]) << 16)
            | (std::uint32_t(block[4 * i + 2]) << 8) | std::uint32_t(block[4 * i + 3]);
    for (int i = 16; i < 64; ++i)
    {
        std::uint32_t const s0 = Rotr(w[i - 15], 7) ^ Rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        std::uint32_t const s1 = Rotr(w[i - 2], 17) ^ Rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    std::uint32_t a = _h[0], b = _h[1], c = _h[2], d = _h[3], e = _h[4], f = _h[5], g = _h[6], h = _h[7];
    for (int i = 0; i < 64; ++i)
    {
        std::uint32_t const s1 = Rotr(e, 6) ^ Rotr(e, 11) ^ Rotr(e, 25);
        std::uint32_t const ch = (e & f) ^ (~e & g);
        std::uint32_t const t1 = h + s1 + ch + kRound[i] + w[i];
        std::uint32_t const s0 = Rotr(a, 2) ^ Rotr(a, 13) ^ Rotr(a, 22);
        std::uint32_t const maj = (a & b) ^ (a & c) ^ (b & c);
        std::uint32_t const t2 = s0 + maj;
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    _h[0] += a;
    _h[1] += b;
    _h[2] += c;
    _h[3] += d;
    _h[4] += e;
    _h[5] += f;
    _h[6] += g;
    _h[7] += h;
}

void Sha256::Update(void const* data, std::size_t size)
{
    auto const* bytes = static_cast<std::uint8_t const*>(data);
    _bits += std::uint64_t(size) * 8;
    while (size > 0)
    {
        std::size_t const take = std::min(size, _buffer.size() - _used);
        std::memcpy(_buffer.data() + _used, bytes, take);
        _used += take;
        bytes += take;
        size -= take;
        if (_used == _buffer.size())
        {
            Block(_buffer.data());
            _used = 0;
        }
    }
}

std::array<std::uint8_t, 32> Sha256::Final()
{
    std::uint64_t const bits = _bits;
    std::uint8_t const one = 0x80;
    Update(&one, 1);
    std::uint8_t const zero = 0;
    while (_used != 56)
        Update(&zero, 1);
    std::uint8_t length[8];
    for (int i = 0; i < 8; ++i)
        length[i] = std::uint8_t(bits >> (56 - 8 * i));
    Update(length, 8);
    std::array<std::uint8_t, 32> out{};
    for (int i = 0; i < 8; ++i)
    {
        out[4 * i] = std::uint8_t(_h[i] >> 24);
        out[4 * i + 1] = std::uint8_t(_h[i] >> 16);
        out[4 * i + 2] = std::uint8_t(_h[i] >> 8);
        out[4 * i + 3] = std::uint8_t(_h[i]);
    }
    return out;
}

std::string Sha256Hex(std::string_view text)
{
    Sha256 sha;
    sha.Update(text);
    static char const digits[] = "0123456789abcdef";
    std::string hex;
    hex.reserve(64);
    for (std::uint8_t const b : sha.Final())
    {
        hex.push_back(digits[b >> 4]);
        hex.push_back(digits[b & 0x0f]);
    }
    return hex;
}
}

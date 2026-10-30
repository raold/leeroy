// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyText.h"

namespace Leeroy
{
namespace
{
bool IsCont(unsigned char c)
{
    return (c & 0xC0) == 0x80;
}

// Length of the well-formed UTF-8 sequence starting at text[i], or 0 if there is none.
std::size_t SequenceLength(std::string_view text, std::size_t i)
{
    auto const at = [&](std::size_t k) { return static_cast<unsigned char>(text[k]); };
    unsigned char const b0 = at(i);
    std::size_t const left = text.size() - i;

    if (b0 < 0x80)
        return 1;
    if (b0 >= 0xC2 && b0 <= 0xDF)
        return left >= 2 && IsCont(at(i + 1)) ? 2 : 0;
    if (b0 >= 0xE0 && b0 <= 0xEF)
    {
        if (left < 3 || !IsCont(at(i + 1)) || !IsCont(at(i + 2)))
            return 0;
        unsigned char const b1 = at(i + 1);
        if (b0 == 0xE0 && b1 < 0xA0)
            return 0;
        if (b0 == 0xED && b1 > 0x9F)
            return 0;
        return 3;
    }
    if (b0 >= 0xF0 && b0 <= 0xF4)
    {
        if (left < 4 || !IsCont(at(i + 1)) || !IsCont(at(i + 2)) || !IsCont(at(i + 3)))
            return 0;
        unsigned char const b1 = at(i + 1);
        if (b0 == 0xF0 && b1 < 0x90)
            return 0;
        if (b0 == 0xF4 && b1 > 0x8F)
            return 0;
        return 4;
    }
    return 0;
}
}

std::string SanitizeChatText(std::string_view text, std::size_t maxBytes)
{
    std::string out;
    out.reserve(text.size());
    bool pendingSpace = false;

    for (std::size_t i = 0; i < text.size();)
    {
        std::size_t const len = SequenceLength(text, i);
        if (len == 0)
        {
            ++i;
            continue;
        }

        if (len == 1)
        {
            unsigned char const c = static_cast<unsigned char>(text[i]);
            ++i;
            if (c == ' ' || c == '\r' || c == '\n' || c == '\t')
                pendingSpace = !out.empty();
            else if (c == '|' || c < 0x20 || c == 0x7F)
                continue;
            else
            {
                if (pendingSpace)
                    out += ' ';
                pendingSpace = false;
                out += static_cast<char>(c);
            }
            continue;
        }

        if (pendingSpace)
            out += ' ';
        pendingSpace = false;
        out.append(text.substr(i, len));
        i += len;
    }

    if (out.size() > maxBytes)
    {
        std::size_t cut = maxBytes;
        while (cut > 0 && IsCont(static_cast<unsigned char>(out[cut])))
            --cut;
        out.resize(cut);
        while (!out.empty() && out.back() == ' ')
            out.pop_back();
    }

    return out;
}

bool StartsWithCommandPrefix(std::string_view text)
{
    for (char c : text)
    {
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n')
            continue;
        return c == '.' || c == '!';
    }
    return false;
}

bool IsLoopbackHost(std::string_view host)
{
    if (host == "localhost" || host == "::1")
        return true;

    int parts = 0;
    int first = -1;
    std::size_t i = 0;
    while (i <= host.size())
    {
        std::size_t digits = 0;
        int value = 0;
        while (i < host.size() && host[i] >= '0' && host[i] <= '9')
        {
            value = value * 10 + (host[i] - '0');
            ++digits;
            ++i;
            if (digits > 3)
                return false;
        }
        if (digits == 0 || value > 255)
            return false;
        if (parts == 0)
            first = value;
        ++parts;
        if (i == host.size())
            break;
        if (host[i] != '.')
            return false;
        ++i;
    }
    return parts == 4 && first == 127;
}
}

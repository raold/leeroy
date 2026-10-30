// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_TEST_HARNESS_H
#define MOD_LEEROY_TEST_HARNESS_H

#include <functional>
#include <sstream>
#include <string>
#include <vector>

namespace LeeroyTest
{
struct TestCase
{
    std::string name;
    std::function<void()> fn;
};

inline std::vector<TestCase>& Registry()
{
    static std::vector<TestCase> tests;
    return tests;
}

inline std::vector<std::string>& Failures()
{
    static std::vector<std::string> failures;
    return failures;
}

struct Registrar
{
    Registrar(char const* name, void (*fn)())
    {
        Registry().push_back({name, fn});
    }
};

template <typename T>
std::string Show(T const& value)
{
    std::ostringstream out;
    out << value;
    return out.str();
}

inline void Fail(char const* file, int line, std::string const& message)
{
    std::ostringstream out;
    out << file << ":" << line << ": " << message;
    Failures().push_back(out.str());
}
}

#define LEEROY_TEST(name) \
    static void name(); \
    static LeeroyTest::Registrar registrar_##name(#name, &name); \
    static void name()

#define CHECK(expr) \
    do \
    { \
        if (!(expr)) \
            LeeroyTest::Fail(__FILE__, __LINE__, "CHECK failed: " #expr); \
    } while (false)

#define CHECK_EQ(a, b) \
    do \
    { \
        auto const leeroyA = (a); \
        auto const leeroyB = (b); \
        if (!(leeroyA == leeroyB)) \
            LeeroyTest::Fail(__FILE__, __LINE__, "CHECK_EQ failed: " #a " == " #b " (" \
                + LeeroyTest::Show(leeroyA) + " vs " + LeeroyTest::Show(leeroyB) + ")"); \
    } while (false)

#endif

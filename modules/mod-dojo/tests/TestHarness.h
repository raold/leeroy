// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_TEST_HARNESS_H
#define MOD_DOJO_TEST_HARNESS_H

#include <functional>
#include <sstream>
#include <string>
#include <vector>

namespace DojoTest
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

#define DOJO_TEST(name) \
    static void name(); \
    static DojoTest::Registrar registrar_##name(#name, &name); \
    static void name()

#define CHECK(expr) \
    do \
    { \
        if (!(expr)) \
            DojoTest::Fail(__FILE__, __LINE__, "CHECK failed: " #expr); \
    } while (false)

#define CHECK_MSG(expr, message) \
    do \
    { \
        if (!(expr)) \
            DojoTest::Fail(__FILE__, __LINE__, std::string("CHECK failed: " #expr "\n    ") + (message)); \
    } while (false)

#define CHECK_EQ(a, b) \
    do \
    { \
        auto const dojoA = (a); \
        auto const dojoB = (b); \
        if (!(dojoA == dojoB)) \
            DojoTest::Fail(__FILE__, __LINE__, "CHECK_EQ failed: " #a " == " #b " (" \
                + DojoTest::Show(dojoA) + " vs " + DojoTest::Show(dojoB) + ")"); \
    } while (false)

#endif

// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "TestHarness.h"

#include <exception>
#include <iostream>

int main(int argc, char** argv)
{
    std::string const filter = argc > 1 ? argv[1] : "";
    int passed = 0;
    int failed = 0;

    for (auto const& test : LeeroyTest::Registry())
    {
        if (!filter.empty() && test.name.find(filter) == std::string::npos)
            continue;

        LeeroyTest::Failures().clear();
        try
        {
            test.fn();
        }
        catch (std::exception const& e)
        {
            LeeroyTest::Failures().push_back(std::string("exception: ") + e.what());
        }
        catch (...)
        {
            LeeroyTest::Failures().push_back("unknown exception");
        }

        if (LeeroyTest::Failures().empty())
        {
            ++passed;
            std::cout << "PASS " << test.name << "\n";
        }
        else
        {
            ++failed;
            std::cout << "FAIL " << test.name << "\n";
            for (auto const& message : LeeroyTest::Failures())
                std::cout << "  " << message << "\n";
        }
    }

    std::cout << passed << " passed, " << failed << " failed\n";
    return failed != 0 ? 1 : 0;
}

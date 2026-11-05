// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "TestHarness.h"

#include <exception>
#include <iostream>

int main(int argc, char** argv)
{
    std::string const filter = argc > 1 ? argv[1] : "";
    int passed = 0;
    int failed = 0;

    for (auto const& test : DojoTest::Registry())
    {
        if (!filter.empty() && test.name.find(filter) == std::string::npos)
            continue;

        DojoTest::Failures().clear();
        try
        {
            test.fn();
        }
        catch (std::exception const& e)
        {
            DojoTest::Failures().push_back(std::string("exception: ") + e.what());
        }
        catch (...)
        {
            DojoTest::Failures().push_back("unknown exception");
        }

        if (DojoTest::Failures().empty())
        {
            ++passed;
            std::cout << "PASS " << test.name << "\n";
        }
        else
        {
            ++failed;
            std::cout << "FAIL " << test.name << "\n";
            for (auto const& message : DojoTest::Failures())
                std::cout << "  " << message << "\n";
        }
    }

    std::cout << passed << " passed, " << failed << " failed\n";
    return failed != 0 ? 1 : 0;
}
